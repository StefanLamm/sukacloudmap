from fastapi import APIRouter, Depends, Query, Request, Response

from app.api import Identity, authenticated
from app.amap_service import secret, security_proxy

router = APIRouter(prefix='/api/maps')
proxy_router = APIRouter()


@router.get('/config')
def map_config(request: Request, identity: Identity = Depends(authenticated)):
    s = request.app.state.settings
    if s.map_provider == 'OPEN':
        return {'tile_url': s.map_tile_url, 'attribution': '© OpenStreetMap contributors', 'crs': 'WGS84'}
    js_ready = bool(secret(s.amap_js_key) and secret(s.amap_security_js_code))
    return {'provider': 'AMAP', 'crs': 'GCJ02', 'attribution': '© 高德地图',
            'js_ready': js_ready, 'geocoder_ready': bool(secret(s.amap_web_service_key)),
            'js_key': secret(s.amap_js_key) if js_ready else None, 'security_proxy': '/_AMapService'}


@proxy_router.get('/_AMapService/{path:path}')
def amap_security_proxy(path: str, request: Request, identity: Identity = Depends(authenticated)):
    content, content_type = security_proxy(request.app.state.settings, path, list(request.query_params.multi_items()),
                                          transport=getattr(request.app.state, 'map_proxy_transport', None))
    return Response(content=content, headers={'Content-Type': content_type})


@router.get('/search')
def search_places(request: Request, q: str = Query(min_length=2, max_length=200), identity: Identity = Depends(authenticated)):
    return request.app.state.map_search.search(q)
