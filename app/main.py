from contextlib import asynccontextmanager
import logging
from pathlib import Path
import uuid
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import ApiError, database, find_identity, router
from app.config import ROOT, Settings
from app.database import SCHEMA_HEAD, make_engine, session_factory
from app.master_api import router as master_router
from app.map_api import router as map_router, proxy_router as map_proxy_router
from app.map_service import MapSearchService
from app.planning_api import router as planning_router
from app.plan_api import router as plan_router
from app.plan_service import PlanningRunner
from app.location_tasks import LocationRunner

log = logging.getLogger("suka")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(app):
        with engine.connect() as connection:
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            if version != SCHEMA_HEAD:
                raise RuntimeError("数据库版本不匹配，请先执行 python -m app.cli migrate")
        app.state.planning_runner.start()
        app.state.location_runner.start()
        try:
            yield
        finally:
            app.state.location_runner.close()
            app.state.planning_runner.close()
        engine.dispose()

    app = FastAPI(title="苏咔云图", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.engine, app.state.sessions = settings, engine, session_factory(engine)
    app.state.map_search = MapSearchService(settings)
    app.state.location_runner = LocationRunner(engine, app.state.sessions, app.state.map_search, enabled=settings.map_auto_geocode_enabled)
    app.state.planning_runner = PlanningRunner(engine, app.state.sessions, settings)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    def error_response(request, status, code, message, details=None):
        return JSONResponse(status_code=status, content={"code": code, "message": message,
                            "details": details, "request_id": getattr(request.state, "request_id", "unknown")})

    @app.middleware("http")
    async def request_policy(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        try:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                allowed = {settings.public_origin.rstrip("/")}
                if settings.environment != "production":
                    allowed.update({"http://127.0.0.1:5173", "http://localhost:5173"})
                if origin and origin not in allowed:
                    response = error_response(request, 403, "ORIGIN_REJECTED", "请求来源校验失败。")
                else:
                    response = await call_next(request)
            else:
                response = await call_next(request)
        except Exception as exc:
            # Catch before ASGI's outer error middleware re-raises to the server;
            # traceback exception text may include credentials from dependencies.
            log.error("request_failed request_id=%s exception_type=%s", request.state.request_id, type(exc).__name__)
            response = error_response(request, 500, "INTERNAL_ERROR", "服务暂时不可用，请稍后重试。")
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        tile_url = urlsplit(settings.map_tile_url)
        tile_origin = f"{tile_url.scheme}://{tile_url.netloc}"
        if settings.map_provider == 'AMAP':
            hosts = 'https://*.amap.com https://*.autonavi.com https://*.amap.cn'
            # AMap 2.0's WebGL renderer compiles its bundled modules with eval.
            # This exception applies only to the explicitly selected AMap SDK;
            # CSRF, origin checks, HttpOnly cookies and fixed proxy paths remain.
            response.headers['Content-Security-Policy'] = f"default-src 'self'; script-src 'self' 'unsafe-eval' {hosts}; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: {hosts}; connect-src 'self' {hosts}; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        else:
            response.headers["Content-Security-Policy"] = f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: {tile_origin}; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if not request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-store"
        if settings.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(ApiError)
    async def business_error(request, exc):
        return error_response(request, exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Pydantic's raw input and context can contain a submitted password.
        fields = [{"field": ".".join(map(str, e["loc"][1:])), "type": e["type"]} for e in exc.errors()]
        return error_response(request, 422, "VALIDATION_ERROR", "输入格式不正确，请检查字段。", fields)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        return error_response(request, 409, "DATA_CONFLICT", "数据已存在或被其他操作更新，请刷新后重试。")

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(request, exc.status_code, "NOT_FOUND" if exc.status_code == 404 else "REQUEST_REJECTED", "请求的资源不存在。" if exc.status_code == 404 else "请求未被接受。")

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        # Never log exception text/bind parameters or request payloads here.
        log.error("request_failed request_id=%s exception_type=%s", getattr(request.state, "request_id", "unknown"), type(exc).__name__)
        return error_response(request, 500, "INTERNAL_ERROR", "服务暂时不可用，请稍后重试。")

    app.include_router(router)
    app.include_router(map_router)
    app.include_router(map_proxy_router)
    app.include_router(planning_router)
    app.include_router(plan_router)

    @app.get('/api/runtime')
    def runtime_info():
        return {'environment':settings.environment,'test_fixture':settings.planning_road_source == 'TEST_GRID',
                'default_delivery_date':settings.uat_delivery_date if settings.planning_road_source == 'TEST_GRID' else None,
                'label':'隔离测试 · 模拟道路，不能执行' if settings.planning_road_source == 'TEST_GRID' else ''}

    # The generic master /api/{kind} route must follow fixed system endpoints.
    app.include_router(master_router)

    @app.get("/healthz")
    def health():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    dist = ROOT / "frontend" / "dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str, request: Request, db=Depends(database)):
        if path not in {"", "login", "accounts", "warehouses", "stores", "vehicles", "quotes", "activity", "planning"}:
            raise ApiError(404, "NOT_FOUND", "页面不存在。")
        identity = find_identity(request, db)
        if path != "login" and not identity:
            return RedirectResponse("/login", status_code=303)
        if path in {"accounts", "activity"} and not identity.account.is_admin:
            return RedirectResponse("/", status_code=303)
        if path == "login" and identity:
            return RedirectResponse("/", status_code=303)
        if not (dist / "index.html").is_file():
            raise ApiError(503, "FRONTEND_NOT_BUILT", "请先构建前端，或使用前端开发服务。")
        return FileResponse(dist / "index.html")

    return app

