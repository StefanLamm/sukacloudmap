from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.database import start_write
from app.models import Account, AuditEvent, LoginSession, LoginThrottle, SharedRecord, now
from app.schemas import CompleteRecord, CreateAccount, CreateRecord, Login, ResetPassword, UpdateAccount, UpdateRecord
from app.security import check_password, csrf_for_session, digest, hash_password

router = APIRouter(prefix="/api")
BEIJING = timezone(timedelta(hours=8))


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details


def database(request: Request):
    with request.app.state.sessions() as db:
        try:
            is_import_upload = (request.url.path.startswith("/api/master-data/imports/") and request.url.path.endswith("/preview")) or request.url.path in {"/api/planning/demand-imports/upload","/api/planning/demand-imports/inspect"}
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not is_import_upload:
                start_write(db, request.app.state.engine)
            yield db
        except Exception:
            db.rollback()
            raise


@dataclass
class Identity:
    account: Account
    session: LoginSession


def find_identity(request: Request, db: Session) -> Identity | None:
    raw = request.cookies.get(request.app.state.settings.cookie_name)
    if not raw or len(raw) > 256:
        return None
    session = db.get(LoginSession, digest(raw))
    if not session or session.expires_at <= now():
        return None
    account = db.get(Account, session.account_id)
    if not account or account.deleted_at is not None or not account.active or account.auth_revision != session.auth_revision:
        return None
    return Identity(account, session)


def authenticated(request: Request, db: Session = Depends(database)) -> Identity:
    identity = find_identity(request, db)
    if not identity:
        raise ApiError(401, "AUTH_REQUIRED", "登录已失效，请重新登录。")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        token = request.headers.get("x-csrf-token", "")
        if len(token) > 256 or not secrets.compare_digest(digest(token), identity.session.csrf_hash):
            raise ApiError(403, "CSRF_INVALID", "会话校验失败，请刷新页面后重试。")
    return identity


def admin(identity: Identity = Depends(authenticated)) -> Identity:
    if not identity.account.is_admin:
        raise ApiError(403, "ADMIN_REQUIRED", "仅管理员可执行此操作。")
    return identity


def stamp(epoch):
    return datetime.fromtimestamp(epoch, BEIJING).isoformat() if epoch is not None else None


def account_json(a: Account):
    return {"id": a.id, "username": a.username, "display_name": a.display_name,
            "is_admin": a.is_admin, "active": a.active, "revision": a.revision,
            "created_at": stamp(a.created_at), "updated_at": stamp(a.updated_at), "last_login_at": stamp(a.last_login_at)}


def event(db, actor_id, action, entity_id, summary):
    db.add(AuditEvent(actor_id=actor_id, action=action, entity_id=entity_id, summary=summary))


def stale(entity, expected_revision):
    if entity.revision != expected_revision:
        raise ApiError(409, "VERSION_CONFLICT", "内容已被更新，请刷新后重新操作。", {"current_revision": entity.revision})


@router.post("/auth/login")
def login(payload: Login, request: Request, response: Response, db: Session = Depends(database)):
    if not request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json":
        raise ApiError(415, "JSON_REQUIRED", "请使用JSON提交登录信息。")
    username = payload.username.strip().lower()
    instant = now()
    # Persisted throttling is shared by workers and survives a service restart.
    peer = request.client.host if request.client else "unknown"
    key = digest(f"{peer}\0{username}")
    throttle = db.get(LoginThrottle, key)
    if throttle and instant - throttle.window_start < 60 and throttle.attempts >= request.app.state.settings.login_max_attempts:
        raise ApiError(429, "LOGIN_RATE_LIMITED", "尝试次数过多，请一分钟后重试。")
    account = db.scalar(select(Account).where(Account.username == username))
    verified, new_hash = check_password(payload.password.get_secret_value(), account.password_hash if account else None)
    if not account or not verified or not account.active or account.deleted_at is not None:
        if not throttle:
            throttle = LoginThrottle(key_hash=key, window_start=instant, attempts=0)
            db.add(throttle)
        if instant - throttle.window_start >= 60:
            throttle.window_start, throttle.attempts = instant, 0
        throttle.attempts += 1
        event(db, None, "LOGIN_FAILED", None, "登录失败")
        db.commit()
        raise ApiError(401, "INVALID_CREDENTIALS", "账号或密码错误，或账号已停用。")
    if throttle:
        db.delete(throttle)
    db.execute(delete(LoginThrottle).where(LoginThrottle.window_start < instant - 86400))
    db.execute(delete(LoginSession).where(LoginSession.expires_at <= instant))
    old_token = request.cookies.get(request.app.state.settings.cookie_name)
    if old_token:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == digest(old_token)))
    token = secrets.token_urlsafe(32)
    csrf = csrf_for_session(token)
    db.add(LoginSession(token_hash=digest(token), csrf_hash=digest(csrf), account_id=account.id,
                        auth_revision=account.auth_revision, expires_at=instant + request.app.state.settings.session_hours * 3600))
    if new_hash:
        account.password_hash = new_hash
    account.last_login_at = instant
    event(db, account.id, "LOGIN", account.id, "登录工作区")
    db.commit()
    response.set_cookie(request.app.state.settings.cookie_name, token, httponly=True,
                        secure=request.app.state.settings.secure_cookie, samesite="lax", path="/",
                        max_age=request.app.state.settings.session_hours * 3600)
    return {"account": account_json(account), "csrf_token": csrf}


@router.get("/auth/me")
def me(request: Request, identity: Identity = Depends(authenticated)):
    # Only a same-origin reader can obtain this value. The HttpOnly session
    # secret stays unavailable to browser JavaScript and is only hashed at rest.
    token = request.cookies[request.app.state.settings.cookie_name]
    return {"account": account_json(identity.account), "csrf_token": csrf_for_session(token)}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    db.delete(identity.session)
    event(db, identity.account.id, "LOGOUT", identity.account.id, "退出登录")
    db.commit()
    response.delete_cookie(request.app.state.settings.cookie_name, path="/", secure=request.app.state.settings.secure_cookie,
                           httponly=True, samesite="lax")


@router.get("/accounts")
def accounts(identity: Identity = Depends(admin), db: Session = Depends(database)):
    return {"items": [account_json(a) for a in db.scalars(select(Account).where(Account.deleted_at.is_(None)).order_by(Account.created_at, Account.username))]}


@router.post("/accounts", status_code=201)
def create_account(payload: CreateAccount, identity: Identity = Depends(admin), db: Session = Depends(database)):
    if db.scalar(select(Account.id).where(Account.username == payload.username)):
        raise ApiError(409, "USERNAME_EXISTS", "该账号已被使用（包含已删除的历史账号），请使用其他账号。")
    account = Account(username=payload.username, display_name=payload.display_name,
                      password_hash=hash_password(payload.password.get_secret_value()), is_admin=payload.is_admin)
    db.add(account)
    db.flush()
    event(db, identity.account.id, "ACCOUNT_CREATED", account.id, f"创建账号 {account.username}")
    db.commit()
    return account_json(account)


@router.patch("/accounts/{account_id}")
def update_account(account_id: str, payload: UpdateAccount, identity: Identity = Depends(admin), db: Session = Depends(database)):
    account = db.get(Account, account_id)
    if not account or account.deleted_at is not None:
        raise ApiError(404, "ACCOUNT_NOT_FOUND", "账号不存在。")
    stale(account, payload.expected_revision)
    next_active = payload.active if payload.active is not None else account.active
    next_admin = payload.is_admin if payload.is_admin is not None else account.is_admin
    if account.active and account.is_admin and not (next_active and next_admin):
        count = db.scalar(select(func.count()).select_from(Account).where(Account.active.is_(True), Account.is_admin.is_(True), Account.deleted_at.is_(None)))
        if count <= 1:
            raise ApiError(409, "LAST_ADMIN", "至少保留一位启用的管理员。")
    auth_changed = account.active != next_active or account.is_admin != next_admin
    account.active, account.is_admin = next_active, next_admin
    if payload.display_name is not None:
        account.display_name = payload.display_name
    if auth_changed:
        account.auth_revision += 1
        db.execute(delete(LoginSession).where(LoginSession.account_id == account.id))
    account.revision += 1
    account.updated_at = now()
    event(db, identity.account.id, "ACCOUNT_UPDATED", account.id, f"更新账号 {account.username}，状态：{'启用' if account.active else '停用'}")
    db.commit()
    return account_json(account)


@router.post("/accounts/{account_id}/reset-password")
def reset_password(account_id: str, payload: ResetPassword, identity: Identity = Depends(admin), db: Session = Depends(database)):
    account = db.get(Account, account_id)
    if not account or account.deleted_at is not None:
        raise ApiError(404, "ACCOUNT_NOT_FOUND", "账号不存在。")
    stale(account, payload.expected_revision)
    new_password = payload.password.get_secret_value()
    if check_password(new_password, account.password_hash)[0]:
        raise ApiError(422, "PASSWORD_UNCHANGED", "新密码不能与当前密码相同。")
    account.password_hash = hash_password(new_password)
    account.auth_revision += 1
    account.revision += 1
    account.updated_at = now()
    db.execute(delete(LoginSession).where(LoginSession.account_id == account.id))
    event(db, identity.account.id, "PASSWORD_RESET", account.id, f"重置账号 {account.username} 的密码并注销原会话")
    db.commit()
    return account_json(account)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: str, expected_revision: int = Query(ge=1),
                   identity: Identity = Depends(admin), db: Session = Depends(database)):
    account = db.get(Account, account_id)
    if not account or account.deleted_at is not None:
        raise ApiError(404, "ACCOUNT_NOT_FOUND", "账号不存在。")
    stale(account, expected_revision)
    if account.active and account.is_admin:
        count = db.scalar(select(func.count()).select_from(Account).where(
            Account.active.is_(True), Account.is_admin.is_(True), Account.deleted_at.is_(None)))
        if count <= 1:
            raise ApiError(409, "LAST_ADMIN", "至少保留一位启用的管理员。")
    if account.id == identity.account.id:
        raise ApiError(409, "DELETE_SELF", "不能删除当前登录账号，请使用其他管理员账号操作。")
    account.deleted_at = account.updated_at = now()
    account.active = False
    account.auth_revision += 1
    account.revision += 1
    db.execute(delete(LoginSession).where(LoginSession.account_id == account.id))
    event(db, identity.account.id, "ACCOUNT_DELETED", account.id, f"删除账号 {account.username}，已注销全部会话，保留历史资料")
    db.commit()


def today_in_beijing():
    return datetime.fromtimestamp(now(), BEIJING).date().isoformat()


def record_json(record: SharedRecord, db: Session, names=None):
    creator = names[record.created_by] if names is not None else db.get(Account,record.created_by).display_name
    editor = names[record.updated_by] if names is not None else db.get(Account,record.updated_by).display_name
    status = 'COMPLETED' if record.completed_at is not None else 'OVERDUE' if record.task_date < today_in_beijing() else 'PENDING'
    completer = (names.get(record.completed_by) if names is not None else db.get(Account, record.completed_by).display_name) if record.completed_by else None
    return {"id": record.id, "title": record.title, "content": record.content, "revision": record.revision,
            "task_date": record.task_date, "status": status, "completed_at": stamp(record.completed_at), "completed_by": completer,
            "created_by": creator, "updated_by": editor,
            "created_at": stamp(record.created_at), "updated_at": stamp(record.updated_at)}


@router.get("/workspace/records")
def records(q:str=Query('',max_length=200),offset:int=Query(0,ge=0,le=2147483647),limit:int=Query(10,ge=1,le=200),
            task_date:str|None=Query(None,pattern=r'^\d{4}-\d{2}-\d{2}$'),status:str=Query('all',pattern='^(all|pending|completed|overdue)$'),
            identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    # All users query the same records. No account ownership filter.
    base=select(SharedRecord)
    if task_date:
        from datetime import date
        try: date.fromisoformat(task_date)
        except ValueError: raise ApiError(422, 'TASK_DATE_INVALID', '任务日期格式不正确。') from None
        base=base.where(SharedRecord.task_date==task_date)
    today=today_in_beijing()
    if status=='completed':base=base.where(SharedRecord.completed_at.is_not(None))
    elif status=='overdue':base=base.where(SharedRecord.completed_at.is_(None),SharedRecord.task_date<today)
    elif status=='pending':base=base.where(SharedRecord.completed_at.is_(None),SharedRecord.task_date>=today)
    if q.strip():base=base.where(SharedRecord.title.icontains(q.strip(),autoescape=True)|SharedRecord.content.icontains(q.strip(),autoescape=True))
    rows=list(db.scalars(base.order_by(SharedRecord.task_date,SharedRecord.updated_at.desc(),SharedRecord.id).offset(offset).limit(limit)))
    ids={id for r in rows for id in [r.created_by,r.updated_by,r.completed_by] if id}
    names=dict(db.execute(select(Account.id,Account.display_name).where(Account.id.in_(ids))).all()) if ids else {}
    return {'items':[record_json(r,db,names) for r in rows],'total':db.scalar(select(func.count()).select_from(base.subquery())),
            'total_all':db.scalar(select(func.count()).select_from(SharedRecord)),'latest_updated_at':stamp(db.scalar(select(func.max(SharedRecord.updated_at)))),
            'counts':{'pending':db.scalar(select(func.count()).select_from(SharedRecord).where(SharedRecord.completed_at.is_(None),SharedRecord.task_date>=today)),
                      'completed':db.scalar(select(func.count()).select_from(SharedRecord).where(SharedRecord.completed_at.is_not(None))),
                      'overdue':db.scalar(select(func.count()).select_from(SharedRecord).where(SharedRecord.completed_at.is_(None),SharedRecord.task_date<today))}}


@router.post("/workspace/records", status_code=201)
def create_record(payload: CreateRecord, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    record = SharedRecord(title=payload.title, content=payload.content, task_date=payload.task_date, created_by=identity.account.id, updated_by=identity.account.id)
    db.add(record)
    db.flush()
    event(db, identity.account.id, "RECORD_CREATED", record.id, "新增共享记录")
    db.commit()
    return record_json(record, db)


@router.patch("/workspace/records/{record_id}")
def update_record(record_id: str, payload: UpdateRecord, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    record = db.get(SharedRecord, record_id)
    if not record:
        raise ApiError(404, "RECORD_NOT_FOUND", "共享记录不存在。")
    stale(record, payload.expected_revision)
    record.title, record.content = payload.title, payload.content
    if payload.task_date is not None:
        record.task_date = payload.task_date
    record.updated_by, record.updated_at, record.revision = identity.account.id, now(), record.revision + 1
    event(db, identity.account.id, "RECORD_UPDATED", record.id, "更新共享记录")
    db.commit()
    return record_json(record, db)


@router.post("/workspace/records/{record_id}/completion")
def complete_record(record_id: str, payload: CompleteRecord, identity: Identity = Depends(admin), db: Session = Depends(database)):
    record = db.get(SharedRecord, record_id)
    if not record:
        raise ApiError(404, "RECORD_NOT_FOUND", "共享记录不存在。")
    stale(record, payload.expected_revision)
    if payload.completed == (record.completed_at is not None):
        return record_json(record, db)
    record.completed_at = now() if payload.completed else None
    record.completed_by = identity.account.id if payload.completed else None
    record.updated_by, record.updated_at, record.revision = identity.account.id, now(), record.revision + 1
    event(db, identity.account.id, "RECORD_COMPLETED" if payload.completed else "RECORD_REOPENED", record.id,
          "团队任务已完成" if payload.completed else "团队任务恢复待办")
    db.commit()
    return record_json(record, db)


@router.get("/audit-events")
def audit_events(limit: int = Query(default=10, ge=1, le=200), offset:int=Query(default=0, ge=0, le=2147483647), identity: Identity = Depends(admin), db: Session = Depends(database)):
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).offset(offset).limit(limit))
    items = []
    for e in rows:
        actor = db.get(Account, e.actor_id) if e.actor_id else None
        items.append({"id": e.id, "actor": actor.display_name if actor else "未登录用户", "action": e.action,
                      "entity_id": e.entity_id, "summary": e.summary, "occurred_at": stamp(e.occurred_at)})
    return {"items": items, "total":db.scalar(select(func.count()).select_from(AuditEvent))}

