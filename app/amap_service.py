"""AMap server-side address lookup and a narrowly scoped JS security proxy."""
from collections import OrderedDict
import math
import re
import unicodedata
from threading import Lock
from time import monotonic

import httpx

from app.api import ApiError
from app.amap_limits import AMapRateLimiter, QPS_CODES, RateLimitWaitError, log_throttle

GEOCODE_URL = 'https://restapi.amap.com/v3/geocode/geo'
POI_URL = 'https://restapi.amap.com/v3/place/text'
PRECISE_LEVELS = {'兴趣点', '门牌号', '单元号', '公交地铁站点', '门址'}
LEVEL_RANK = {'单元号': 5, '门址': 4, '门牌号': 3, '兴趣点': 2, '公交地铁站点': 2}
EXACT_AREA_LEVELS = {'住宅区'}


def location_text(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value))


def exact_administrative_poi(query, row, city):
    """Match an exact POI name after administrative prefixes, never fuzzy names.

    E.g. 安徽省安庆市桐城市水源小区 -> 水源小区 in 桐城市. Road,
    house, unit and floor qualifiers prevent dropping meaningful address detail.
    """
    name = location_text(text_value(row.get('name')))
    query = location_text(query)
    district = location_text(text_value(row.get('adname')))
    result_city = text_value(row.get('cityname'))
    if not city or city != result_city or len(name) < 4 or not query.endswith(name):
        return False
    prefix = query[:-len(name)]
    if not prefix or not district or district not in prefix:
        return False
    if re.search(r'\d|路|巷|弄|号|栋|楼|单元|室', prefix):
        return False
    return bool(re.fullmatch(r'(?:[\u4e00-\u9fff]{1,20}(?:省|自治区|市|区|县|镇|乡|街道))+', prefix))


def geocode_quality(query, row):
    address = text_value(row.get('formatted_address'))
    query = unicodedata.normalize('NFKC', query).replace(' ', '')
    road = re.search(r'([^省市区县镇乡\s]+?(?:大道|路|街|巷|弄|道))(\d+(?:-\d+)?)', query)
    if road:
        if road[1] not in address or not re.search(r'(?<!\d)' + re.escape(road[2]) + r'(?!\d)', address):
            return False
    district = re.search(r'([^省市区县镇乡\s]{1,12}(?:区|县))', query)
    if district and district[1] not in address:
        return False
    level = text_value(row.get('level'))
    if level in EXACT_AREA_LEVELS:
        normalized = location_text(query)
        return len(normalized) >= 6 and bool(re.search(r'(小区|社区|公寓|花园|住宅|校区)$', normalized)) and location_text(address).endswith(normalized)
    return level in PRECISE_LEVELS


def secret(value):
    return value.get_secret_value().strip() if hasattr(value, 'get_secret_value') else str(value or '').strip()


def text_value(value):
    return value.strip() if isinstance(value, str) else ''


def point(location):
    try:
        parts = location.split(',')
        if len(parts) != 2:
            return None
        lng, lat = map(float, parts)
        if not all(math.isfinite(v) for v in [lng, lat]) or not (-180 <= lng <= 180 and -90 <= lat <= 90):
            return None
        return {'longitude': format(lng, '.8f').rstrip('0').rstrip('.'), 'latitude': format(lat, '.8f').rstrip('0').rstrip('.')}
    except (ValueError, TypeError, AttributeError):
        return None


class AMapSearchService:
    def __init__(self, settings, *, transport=None, clock=monotonic, limiter=None):
        self.key = secret(settings.amap_web_service_key)
        self.transport, self.clock = transport, clock
        self.cache = OrderedDict(); self.lock = Lock()
        self.limiter = limiter or AMapRateLimiter.from_settings(settings)
        self.poi_cooldown_until = 0; self.poi_notice = ''

    def fetch(self, url, params):
        service = 'geocode' if url == GEOCODE_URL else 'poi_search'
        try:
            for attempt in range(3):
                self.limiter.acquire(service)
                with httpx.Client(timeout=10, follow_redirects=False, transport=self.transport) as client:
                    response = client.get(url, params={**params, 'key': self.key, 'output': 'JSON'})
                    if response.status_code == 429:
                        log_throttle(service, 'HTTP_429', attempt)
                        self.limiter.defer(service, attempt)
                        if attempt < 2:
                            continue
                        raise ApiError(429, 'AMAP_QPS_LIMIT', '高德请求暂时较多，当前地址已暂缓，请稍后重试。')
                    response.raise_for_status()
                    if len(response.content) > 2 * 1024 * 1024:
                        raise ValueError()
                    payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError()
                code = str(payload.get('infocode', ''))
                if str(payload.get('status')) != '1' and code in QPS_CODES:
                    log_throttle(service, code, attempt)
                    self.limiter.defer(service, attempt)
                    if attempt < 2:
                        continue
                    raise ApiError(429, 'AMAP_QPS_LIMIT', '高德请求暂时较多，当前地址已暂缓，请稍后重试。')
                break
        except RateLimitWaitError:
            raise ApiError(429, 'AMAP_QPS_WAIT', '地图请求正在排队，请稍后重试。') from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise ApiError(502, 'AMAP_UNAVAILABLE', '高德地址服务暂时不可用，请稍后重试或手动选点。')
        if str(payload.get('status')) != '1':
            code = str(payload.get('infocode', ''))
            if code in {'10045','40003','20011'}:
                # These concern overseas scope/quota. A domestic query may be
                # misinterpreted by the provider; other domestic POIs still work.
                raise ApiError(422, 'AMAP_QUERY_SCOPE', '该地点的检索范围或海外服务额度受限，请核对城市和地址。')
            # 3xxxx is an engine/query response error, not an invalid Key.
            # In particular 30001 for one address must not stop the whole queue.
            if code.startswith('3') or code in {'20000', '20001', '20002', '20003', '20012'}:
                raise ApiError(422, 'AMAP_QUERY_RESPONSE', '高德未能解析该地址，请补充地址或在地图上选择位置。')
            if code in {'10003', '10004', '10010', '10044', '40000'}:
                raise ApiError(502, 'AMAP_QUOTA', '高德服务额度或调用频率受限，请检查配额后重试。')
            raise ApiError(502, 'AMAP_AUTH', '高德地址服务未通过权限校验，请管理员检查Web服务Key、服务权限和IP配置。')
        return payload

    def search(self, query):
        query = ' '.join(query.split())
        if not 2 <= len(query) <= 200:
            raise ApiError(422, 'MAP_QUERY_INVALID', '请输入2至200字的城市、地址或地点名称。')
        if not self.key:
            raise ApiError(503, 'AMAP_KEY_MISSING', '高德地址服务尚未配置，请联系管理员。')
        with self.lock:
            current = self.clock()
            if query in self.cache and self.cache[query][0] > current:
                self.cache.move_to_end(query)
                return {'items': self.cache[query][1], 'provider': '高德地图', **({'notice':self.cache[query][2]} if self.cache[query][2] else {})}
            # A city hint is optional; the full original address is always sent.
            tail = re.split(r'省|自治区', query)[-1]
            city_match = re.search(r'([\u4e00-\u9fff]{1,12}?市)', tail)
            city = city_match[1] if city_match else ''
            scope = {'city': city} if city else {}
            try:
                payload = self.fetch(GEOCODE_URL, {'address': query, **scope})
            except ApiError as exc:
                if exc.code not in {'AMAP_QUERY_RESPONSE','AMAP_QUERY_SCOPE'}:
                    raise
                # Complex mall/campus addresses may fail geocoding while POI
                # search remains available. Still require a unique exact match.
                payload = {'geocodes': []}
            rows = payload.get('geocodes', [])
            if not isinstance(rows, list):
                raise ApiError(502, 'AMAP_RESPONSE', '高德地址结果格式不完整，请稍后重试。')
            items = []
            for row in rows[:5]:
                if not isinstance(row, dict):
                    continue
                coordinates = point(row.get('location'))
                if coordinates is None:
                    continue
                address = text_value(row.get('formatted_address'))
                level = text_value(row.get('level'))
                result_city = text_value(row.get('city')) or text_value(row.get('province'))
                scope_ok = not city or city == result_city
                items.append({**coordinates, 'name': address[:120], 'address': address[:300], 'crs': 'GCJ02',
                              'location_source': '高德地图地址解析', 'provider': 'AMAP', 'match_level': level,
                              'automatic_eligible': bool(geocode_quality(query, row) and scope_ok), 'automatic_rank': LEVEL_RANK.get(level, 1 if level in EXACT_AREA_LEVELS else 0),
                              'address_parts': {k: text_value(row.get(field)) for k, field in [
                                  ('state', 'province'), ('city', 'city'), ('district', 'district'), ('street', 'street'), ('housenumber', 'number')]}})
            notice = ''
            if not any(i['automatic_eligible'] for i in items):
                pois = []
                if current < self.poi_cooldown_until:
                    notice = self.poi_notice
                else:
                    try:
                        pois = self.fetch(POI_URL, {'keywords': query, 'offset': 5, 'page': 1, 'extensions': 'base', **scope,
                                                  **({'citylimit': 'true'} if city else {})}).get('pois', [])
                    except ApiError as exc:
                        # POI quota/permission is independent of geocoding.
                        # Preserve geocodes, continue other addresses, and avoid
                        # repeating a known-unavailable secondary endpoint.
                        if exc.status == 429:
                            # Retry the current queue item later; a temporary
                            # POI throttle is not evidence of a missing address.
                            raise
                        notice = '高德地点搜索额度受限，地址解析候选已保留，请核对或在地图上选点。' if exc.code=='AMAP_QUOTA' else '高德未能检索该地点，地址解析候选已保留，请核对城市和地址或在地图上选点。'
                        if exc.status in {502,503}:
                            self.poi_cooldown_until = current + (900 if exc.code in {'AMAP_QUOTA','AMAP_AUTH'} else 30)
                            self.poi_notice = notice
                if not isinstance(pois, list):
                    raise ApiError(502, 'AMAP_RESPONSE', '高德地点结果格式不完整，请稍后重试。')
                for row in pois[:5]:
                    if not isinstance(row, dict):
                        continue
                    coordinates = point(row.get('location'))
                    if coordinates is None:
                        continue
                    address = ''.join(text_value(row.get(k)) for k in ['pname', 'cityname', 'adname', 'address'])
                    items.append({**coordinates, 'name': text_value(row.get('name'))[:120], 'address': address[:300],
                                  'crs': 'GCJ02', 'location_source': '高德地图地点搜索', 'provider': 'AMAP', 'match_level': 'POI',
                                  'automatic_eligible': exact_administrative_poi(query, row, city), 'automatic_rank': 2,
                                  'automatic_scope_ok': not city or city == text_value(row.get('cityname')),
                                  'address_parts': {'city': text_value(row.get('cityname')), 'district': text_value(row.get('adname'))}})
            unique = {}
            for item in items:
                key = (item['longitude'], item['latitude'])
                if key not in unique or (not unique[key].get('automatic_eligible') and item['match_level'] == 'POI'):
                    unique[key] = item
            items = list(unique.values())[:10]
            self.cache[query] = (current + (30 if notice else 86400), items, notice)
            self.cache.move_to_end(query)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
            return {'items': items, 'provider': '高德地图', **({'notice':notice} if notice else {})}


def security_proxy(settings, path, query, *, transport=None, limiter=None):
    # Only SDK map authentication/styles are proxied. Search and truck routing
    # must use their authenticated, rate-limited server endpoints.
    upstream = {'v4/map/styles': 'https://webapi.amap.com/v4/map/styles',
                'v4/maps': 'https://restapi.amap.com/v4/maps'}.get(path)
    if upstream is None:
        raise ApiError(404, 'AMAP_PROXY_PATH', '不支持的地图服务路径。')
    if not secret(settings.amap_js_key) or not secret(settings.amap_security_js_code):
        raise ApiError(503, 'AMAP_JS_CONFIG_MISSING', '高德地图尚未配置完整，请联系管理员。')
    if len(query) > 40 or sum(len(k) + len(v) for k, v in query) > 8192:
        raise ApiError(413, 'AMAP_PROXY_QUERY', '地图请求过大。')
    params = [(k, v) for k, v in query if k not in {'key', 'jscode'}]
    for k, v in params:
        if k in {'callback', 'cb'} and not re.fullmatch(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*|\[\d+\])*', v, flags=re.ASCII):
            raise ApiError(422, 'AMAP_CALLBACK', '地图回调格式不正确。')
    params += [('key', secret(settings.amap_js_key)), ('jscode', secret(settings.amap_security_js_code))]
    limiter = limiter or AMapRateLimiter.from_settings(settings)
    try:
        for attempt in range(2):
            limiter.acquire('js_map')
            with httpx.Client(timeout=10, transport=transport, follow_redirects=False) as client:
                response = client.get(upstream, params=params)
            if response.status_code == 429:
                log_throttle('js_map', 'HTTP_429', attempt)
                limiter.defer('js_map', attempt)
                if attempt == 0:
                    continue
                raise ApiError(429, 'AMAP_QPS_LIMIT', '地图正在排队加载，请稍后重试。')
            response.raise_for_status()
            if len(response.content) > 8 * 1024 * 1024:
                raise ValueError()
            for value in [settings.amap_security_js_code, settings.amap_web_service_key]:
                if secret(value) and secret(value).encode() in response.content:
                    raise ValueError()
            if response.headers.get('content-type', '').split(';')[0].lower() not in {
                'application/javascript', 'text/javascript', 'application/x-javascript',
                'application/json', 'application/octet-stream', 'text/plain', ''}:
                raise ValueError()
            break
    except RateLimitWaitError:
        raise ApiError(429, 'AMAP_QPS_WAIT', '地图正在排队加载，请稍后重试。') from None
    except (httpx.HTTPError, ValueError):
        raise ApiError(502, 'AMAP_PROXY_UNAVAILABLE', '高德地图服务未能加载，请检查Key权限、安全密钥及网络后重试。')
    return response.content, response.headers.get('content-type', 'application/javascript')
