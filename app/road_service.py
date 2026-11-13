"""Separate truck evidence, ordinary driving references and fixed test roads.

AMap advanced truck documents full vehicle dimensions but no arbitrary departure
instant. Consequently its responses are drafts (TIME_UNVERIFIED), even when the
restriction flag is good. Never silently promote current-time data to an ETD proof.
"""
from copy import deepcopy
from collections import OrderedDict
import json
import math
import time

import httpx

from app.config import ROOT
from app.amap_limits import AMapRateLimiter, QPS_CODES, RateLimitWaitError, log_throttle
from app.coordinates import provider_point
from app.planning_inputs import content_hash
from app.routing_engine import RoadError

PROFILE = ['plate', 'size', 'cartype', 'plate_color', 'length_m', 'width_m', 'height_m', 'rated_load_t', 'full_gross_t', 'axles']
ENDPOINT = 'https://tsapi.amap.com/v1/logistics/transport/route/direction/truck'
DRIVING_ENDPOINT = 'https://restapi.amap.com/v5/direction/driving'


def load_fixture(fixture_id):
    path = ROOT / '产品交付/第一阶段需求与开发交接/固定道路样例.json'
    item = next((f for f in json.loads(path.read_text())['fixtures'] if f['id'] == fixture_id), None)
    if item is None: raise RoadError('FIXTURE_NOT_FOUND', '固定道路样例不存在')
    item['delivery_date'] = item['date']; item['warehouses'] = [item['warehouse']]
    return item


def road_request(vehicle, origin, destination, departure, source, strategy=11):
    return {'schema_version': 1, 'source': source, 'endpoint': ENDPOINT if source == 'LIVE' else DRIVING_ENDPOINT if source == 'DRIVING' else 'repository-fixture',
            'vehicle_id': vehicle['id'], 'profile': {k: vehicle.get(k) for k in PROFILE},
            'from_id': origin['id'], 'to_id': destination['id'],
            'origin': {k: origin.get(k) for k in ['longitude','latitude','crs']},
            'destination': {k: destination.get(k) for k in ['longitude','latitude','crs']},
            'departure_at': departure, 'strategy': strategy, 'coordinate_policy': 'GCJ02_REQUIRED_NO_IMPLICIT_CONVERSION'}


def evidence(request, meters, seconds, source, verified=False, polyline=None, restriction='UNKNOWN', **extra):
    if not isinstance(meters, int) or not isinstance(seconds, int) or meters < 0 or seconds < 0:
        raise RoadError('ROAD_RESPONSE', '道路返回了无效距离或耗时')
    item = {'request': request, 'meters': meters, 'seconds': seconds, 'source': source, 'verified': verified,
            'polyline': polyline or [], 'crs': 'GCJ02', 'restriction': restriction, **extra}
    item['evidence_hash'] = content_hash(item)
    return item


class FixtureRoad:
    def __init__(self, fixture, cache=None):
        self.fixture = fixture; self.cache = cache if cache is not None else {}; self.hits = self.misses = 0
    def segment(self, vehicle, origin, destination, departure):
        request = road_request(vehicle, origin, destination, departure, 'FIXTURE')
        request['fixture_hash'] = content_hash(self.fixture['roads'])
        key = content_hash(request)
        if key in self.cache: self.hits += 1; return deepcopy(self.cache[key])
        roads = self.fixture['roads']; a = roads['labels'].index(origin['id']); b = roads['labels'].index(destination['id'])
        item = evidence(request, roads['directed_distance_m'][a][b], roads['directed_duration_seconds'][a][b], 'FIXTURE',
                        restriction='VERIFIED_FIXTURE_ONLY', fixture_id=self.fixture['id'])
        self.cache[key] = item; self.misses += 1
        return deepcopy(item)
    def stats(self): return {'source': 'FIXTURE', 'calls': 0, 'fixture_reads': self.misses, 'cache_hits': self.hits, 'call_limit': 0}


class TruckRoad:
    def __init__(self, settings, call_limit=0, cache_get=None, cache_put=None, transport=None, cancelled=lambda: False, limiter=None):
        self.settings = settings; self.call_limit = call_limit; self.calls = self.hits = self.failures = 0
        self.cache_get = cache_get or (lambda key: None); self.cache_put = cache_put or (lambda key, item: None)
        self.local = {}; self.transport = transport; self.cancelled = cancelled
        self.limiter = limiter or AMapRateLimiter.from_settings(settings)

    def pace(self, service, *, retry=None):
        try:
            if retry is not None:
                self.limiter.defer(service, retry)
            else:
                self.limiter.acquire(service, cancelled=self.cancelled)
                if self.cancelled():
                    raise RateLimitWaitError('CANCELLED')
        except RateLimitWaitError as exc:
            if exc.code == 'CANCELLED':
                raise RoadError('CANCELLED', '规划已取消') from None
            raise RoadError('ROAD_QPS_WAIT', '高德算路请求正在排队，请稍后重试，当前方案已保留') from None
    def stats(self):
        return {'source': 'LIVE', 'calls': self.calls, 'cache_hits': self.hits, 'failures': self.failures,
                'call_limit': self.call_limit, 'time_semantics': 'NOT_VERIFIED', 'endpoint': ENDPOINT}
    def preflight(self,vehicle,origin):
        if not self.settings.amap_web_service_key or not self.settings.amap_web_service_key.get_secret_value():
            raise RoadError('TRUCK_KEY_MISSING','未配置真实货车服务，共配保留，专车待道路验证')
        if any(vehicle.get(k) is None for k in PROFILE):
            raise RoadError('TRUCK_PROFILE_MISSING','车辆货车通行参数不全')
        if origin.get('crs') != 'GCJ02' or origin.get('longitude') is None or origin.get('latitude') is None:
            raise RoadError('COORDINATES_NOT_READY','仓库算路坐标尚未核验')
    def segment(self, vehicle, origin, destination, departure):
        missing = [k for k in PROFILE if vehicle.get(k) is None]
        if missing: raise RoadError('TRUCK_PROFILE_MISSING', '车辆货车通行参数不全：' + '、'.join(missing))
        for point in [origin, destination]:
            if point.get('crs') != 'GCJ02' or point.get('longitude') is None or point.get('latitude') is None:
                raise RoadError('COORDINATES_NOT_READY', '货车服务要求已核实的GCJ02坐标，请先完成坐标转换核验')
        if not self.settings.amap_web_service_key: raise RoadError('TRUCK_KEY_MISSING', '未配置真实货车服务，共配保留，专车待道路验证')
        request = road_request(vehicle, origin, destination, departure, 'LIVE')
        key = content_hash(request)
        cached = self.local.get(key) or self.cache_get(key)
        if cached is not None:
            self.hits += 1; return deepcopy(cached)
        if self.call_limit is not None and self.call_limit <= self.calls: raise RoadError('ROAD_CALL_BUDGET', '本次货车调用额度已用完，保留已找到的完整方案')
        if self.cancelled(): raise RoadError('CANCELLED', '规划已取消')
        if float(vehicle['length_m']) > 25 or any(float(vehicle[k]) > 25.5 for k in ['height_m','width_m']) or any(float(vehicle[k]) > 100 for k in ['rated_load_t','full_gross_t']) or vehicle['axles'] > 255:
            raise RoadError('TRUCK_PROVIDER_RANGE', '车辆参数超出当前货车服务支持范围')
        params = {'key': self.settings.amap_web_service_key.get_secret_value(),
                  'origin': f"{float(origin['longitude']):.6f},{float(origin['latitude']):.6f}",
                  'destination': f"{float(destination['longitude']):.6f},{float(destination['latitude']):.6f}",
                  'strategy': 11, 'size': vehicle['size'], 'length': vehicle['length_m'], 'height': vehicle['height_m'],
                  'width': vehicle['width_m'], 'load': vehicle['full_gross_t'], 'weight': vehicle['rated_load_t'],
                  'axis': vehicle['axles'], 'cartype': vehicle['cartype'], 'plate': vehicle['plate'],
                  'restriction': 1, 'consider_load': 0, 'has_pass': 'false', 'showpolyline': 1, 'nosteps': 0}
        # At most two attempts, both charged to the same explicit job budget.
        for attempt in range(2):
            if self.cancelled(): raise RoadError('CANCELLED', '规划已取消')
            if self.call_limit is not None and self.calls >= self.call_limit: raise RoadError('ROAD_CALL_BUDGET', '本次货车调用额度已用完')
            self.pace('truck')
            self.calls += 1
            try:
                with httpx.Client(timeout=6, transport=self.transport, follow_redirects=False) as client:
                    response = client.get(ENDPOINT, params=params)
                if response.status_code == 429:
                    log_throttle('truck', 'HTTP_429', attempt)
                    self.pace('truck', retry=attempt)
                    if attempt == 0: continue
                    raise RoadError('ROAD_QPS_LIMIT', '高德货车服务请求暂时较多，请稍后重试')
                if response.status_code in [502,503,504] and attempt == 0:
                    self.pace('truck', retry=attempt); continue
                if response.status_code != 200: raise RoadError('ROAD_HTTP_ERROR', '货车道路服务暂时不可用')
                if len(response.content) > 8 * 1024 * 1024: raise RoadError('ROAD_RESPONSE', '道路响应超出大小限制')
                payload = response.json()
                if not isinstance(payload, dict): raise ValueError()
                code = str(payload.get('errcode', payload.get('infocode', '')))
                if code in QPS_CODES:
                    log_throttle('truck', code, attempt)
                    self.pace('truck', retry=attempt)
                    if attempt == 0: continue
                    raise RoadError('ROAD_QPS_LIMIT', '高德货车服务请求暂时较多，请稍后重试')
                if code not in {'0', '10000'}:
                    raise RoadError('ROAD_API_ERROR', '货车服务权限、额度或参数未通过校验')
                path = payload['data']['route']['paths'][0]
                restriction = str(path.get('restriction', '0'))
                if restriction not in ['-1','1']: raise RoadError('RESTRICTION_UNKNOWN', '货车道路限行未知或未规避，不能作为可行专车')
                meters, seconds = float(path['distance']), float(path['duration'])
                if not math.isfinite(meters) or not math.isfinite(seconds): raise ValueError()
                polyline = []
                for step in path.get('steps', []):
                    for point in str(step.get('polyline','')).split(';'):
                        if not point: continue
                        lng, lat = map(float, point.split(','))
                        if not math.isfinite(lng + lat) or abs(lng)>180 or abs(lat)>90: raise ValueError()
                        polyline.append([lng,lat])
                item = evidence(request, math.ceil(meters), math.ceil(seconds), 'LIVE', False, polyline, restriction,
                                time_status='TIME_UNVERIFIED', received_at=int(time.time()),
                                notice='该接口未证明指定发车时刻及车牌颜色约束，结果仅供草稿复核')
                self.local[key] = item; self.cache_put(key, item); return deepcopy(item)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 0:
                    self.pace('truck', retry=attempt); continue
                self.failures += 1; raise RoadError('ROAD_TIMEOUT', '货车服务超时或网络异常')
            except (ValueError, KeyError, IndexError, TypeError):
                self.failures += 1; raise RoadError('ROAD_RESPONSE', '货车道路返回内容不完整或格式错误')
            except RoadError:
                self.failures += 1; raise
        raise RoadError('ROAD_UNAVAILABLE', '货车道路不可用')


class DrivingRoad(TruckRoad):
    """Ordinary car routing, explicitly a reference draft without truck proof.

    The full frozen vehicle profile stays in local evidence for binding. Only
    route coordinates are sent upstream; car parameters cannot represent
    truck dimensions, load, future departure time or truck restrictions.
    """
    def __init__(self,*args,**kwargs):
        self.allow_historical=kwargs.pop('allow_historical',False)
        self.force_refresh=kwargs.pop('force_refresh',False)
        super().__init__(*args,**kwargs)
        self.graph=None;self._metric_keys={};self.metric_hints={};self.io_seconds=0.;self.historical_hits=0
        self.local=OrderedDict();self._freshened_keys=set();self._fresh_bound_keys=set()
        path=getattr(self.settings,'amap_reference_graph_path',None)
        if self.settings.environment!='test' and path is not None and path.is_file():
            from app.road_graph import ReferenceGraph
            self.graph=ReferenceGraph(path)

    def close(self):
        if self.graph is not None:self.graph.close()

    def pace(self,service,*,retry=None):
        started=time.monotonic()
        try:return super().pace(service,retry=retry)
        finally:self.io_seconds+=time.monotonic()-started

    def known_segment(self,origin,destination):
        """Only nonbinding measured hints; accepted routes still use segment()."""
        try:
            coords=tuple(origin.get(k) for k in ('longitude','latitude','crs'))+tuple(destination.get(k) for k in ('longitude','latitude','crs'))
            key=self._metric_keys.get(coords)
            if key is None:
                key=content_hash(self.reference_request(origin,destination));self._metric_keys[coords]=key
            local=self.metric_hints.get(key)
            stored=self.graph.get_metrics(key) if self.graph is not None else None
            return stored if stored is not None and (local is None or stored['received_at']>local['received_at']) else local
        except (KeyError,TypeError,ValueError):return None

    @staticmethod
    def trim_geometry(cache):
        points=sum(len(v.get('polyline',[])) for v in cache.values())
        while len(cache)>1 and (len(cache)>96 or points>150000):
            discarded=cache.pop(next(iter(cache)));points-=len(discarded.get('polyline',[]))

    def remember_metrics(self,key,item):
        self.metric_hints[key]={k:item[k] for k in ('meters','seconds','received_at')}
        self.metric_hints[key]['heuristic_only']=True
        while len(self.metric_hints)>100000:self.metric_hints.pop(next(iter(self.metric_hints)))

    @staticmethod
    def reference_request(origin,destination):
        return {'schema_version':1,'source':'DRIVING_PROVIDER_REFERENCE','endpoint':DRIVING_ENDPOINT,
                'origin':provider_point(origin),'destination':provider_point(destination),'strategy':32,
                'time_semantics':'CURRENT_TRAFFIC_REFERENCE','vehicle_policy':'NO_VEHICLE_OR_FUTURE_TIME_SENT'}

    @staticmethod
    def valid_reference(item, *, allow_expired=False,allow_historical_status=False):
        if not isinstance(item,dict) or not isinstance(item.get('request'),dict) or item.get('source')!='DRIVING' or item.get('verified') is not False:return False
        if any(not isinstance(item['request'].get(k),dict) for k in ('origin','destination')):return False
        statuses={'CURRENT_TRAFFIC_REFERENCE','HISTORICAL_TRAFFIC_REFERENCE'} if allow_historical_status else {'CURRENT_TRAFFIC_REFERENCE'}
        if item.get('time_status') not in statuses or item.get('crs')!='GCJ02':return False
        if not isinstance(item.get('received_at'),int) or isinstance(item['received_at'],bool):return False
        age=int(time.time())-item['received_at']
        if age<0 or (age>=3600 and not allow_expired):return False
        try:
            if content_hash({k:v for k,v in item.items() if k!='evidence_hash'})!=item.get('evidence_hash'):return False
        except (ValueError,TypeError,OverflowError):return False
        if any(isinstance(item.get(k),bool) or not isinstance(item.get(k),int) for k in ('meters','seconds')) or min(item['meters'],item['seconds'])<0:return False
        points=item.get('polyline')
        return isinstance(points,list) and len(points)>=2 and all(isinstance(p,(list,tuple)) and len(p)==2 and all(isinstance(v,(float,int)) and math.isfinite(v) for v in p) and abs(p[0])<=180 and abs(p[1])<=90 for p in points)

    def reference_item(self, request, item):
        return evidence(request,item['meters'],item['seconds'],'DRIVING',False,item['polyline'],'TRUCK_NOT_VERIFIED',
                        time_status='CURRENT_TRAFFIC_REFERENCE',received_at=item['received_at'],
                        notice='普通驾车当前路况参考；路段复用不代表货车或计划发车时刻已核验')

    def cached_item(self,key):
        try:return self.cache_get(key)
        except (ValueError,TypeError,KeyError,OSError):return None

    def usable_provider(self,item,request):
        if not self.valid_reference(item,allow_expired=self.allow_historical):return False
        age=int(time.time())-item['received_at']
        if age>=7*86400:return False
        raw=item['request']
        try:
            if raw.get('source')=='DRIVING_PROVIDER_REFERENCE':return raw==request
            return (raw.get('source')=='DRIVING' and raw.get('endpoint')==DRIVING_ENDPOINT and raw.get('strategy')==32
                    and self.reference_request(raw['origin'],raw['destination'])==request)
        except (KeyError,ValueError,TypeError):return False

    def seed_reference_cache(self, items):
        self.references=getattr(self,'references',{})
        self.route_seed_samples=getattr(self,'route_seed_samples',{})
        for item in items:
            try:
                if not self.valid_reference(item,allow_expired=True):continue
                raw=item['request']
                if raw.get('source')=='DRIVING_PROVIDER_REFERENCE':request=raw
                elif raw.get('source')=='DRIVING' and raw.get('endpoint')==DRIVING_ENDPOINT and raw.get('strategy')==32:
                    request=self.reference_request(raw['origin'],raw['destination'])
                    # Historical measurements only suggest candidate ordering.
                    # Expired samples never enter the usable provider cache.
                    if all(raw.get(k) for k in ('vehicle_id','from_id','to_id','departure_at')):
                        self.route_seed_samples[item['evidence_hash']]={
                            'request':deepcopy(raw),'meters':item['meters'],'seconds':item['seconds']}
                else:continue
                key=content_hash(request)
                prior_hint=self.metric_hints.get(key)
                if prior_hint is None or prior_hint['received_at']<item['received_at']:self.remember_metrics(key,item)
                if not self.valid_reference(item):continue
                key=content_hash(request);prior=self.references.get(key)
                if prior is None or prior['received_at']<item['received_at']:self.references[key]=self.reference_item(request,item)
                self.trim_geometry(self.references)
            except (KeyError,TypeError,ValueError):continue
        return len(self.references)

    def stats(self):
        return {**super().stats(), 'source': 'DRIVING', 'endpoint': DRIVING_ENDPOINT,
                'time_semantics': 'CURRENT_TRAFFIC_REFERENCE',
                'provider_cache_hits':getattr(self,'provider_hits',0),'provider_seeded_edges':len(getattr(self,'references',{})),
                'historical_route_hints':len(getattr(self,'route_seed_samples',{})),
                'historical_segment_hits':self.historical_hits,'network_wait_seconds':round(self.io_seconds,4),
                'reference_graph_edges':len(self.graph.metrics) if self.graph is not None else 0}

    def preflight(self, vehicle, origin):
        if not self.settings.amap_web_service_key:
            raise RoadError('DRIVING_KEY_MISSING', '未配置高德Web服务Key，无法生成驾车参考路线')
        self.validate_point(origin)

    @staticmethod
    def validate_point(point):
        try:
            provider_point(point)
        except (KeyError, TypeError, ValueError):
            raise RoadError('COORDINATES_NOT_READY', '驾车参考算路要求已关联且声明坐标系的位置，请先核对仓库和门店位置')

    def segment(self, vehicle, origin, destination, departure):
        self.preflight(vehicle, origin); self.validate_point(destination)
        request = road_request(vehicle, origin, destination, departure, 'DRIVING', strategy=32)
        provider_origin,provider_destination=provider_point(origin),provider_point(destination)
        if origin['crs']!='GCJ02' or destination['crs']!='GCJ02':
            request.update(coordinate_policy='EXPLICIT_REFERENCE_CONVERSION_TO_GCJ02',
                           provider_origin=provider_origin,provider_destination=provider_destination,
                           coordinate_conversion='coordtransform-2.1.2-reference')
        key = content_hash(request)
        provider_request=self.reference_request(origin,destination);provider_key=content_hash(provider_request)
        refresh=self.force_refresh and provider_key not in self._freshened_keys
        cached=self.local.get(key) if not refresh and (not self.force_refresh or key in self._fresh_bound_keys) else None
        if cached is None and not self.force_refresh:cached=self.cached_item(key)
        cached_valid=(self.valid_reference(cached,allow_expired=self.allow_historical,allow_historical_status=self.allow_historical)
                      and cached['request']==request and int(time.time())-cached['received_at']<7*86400)
        if cached_valid and (int(time.time())-cached['received_at']<3600 or cached['time_status']=='HISTORICAL_TRAFFIC_REFERENCE'):
            self.hits += 1
            if cached['time_status']=='HISTORICAL_TRAFFIC_REFERENCE':self.historical_hits+=1
            self.seed_reference_cache([cached]);return deepcopy(cached)
        if self.cancelled():raise RoadError('CANCELLED','规划已取消')
        self.references=getattr(self,'references',{})
        provider=None if refresh else self.references.get(provider_key)
        if not self.usable_provider(provider,provider_request) and not refresh:
            provider=self.cached_item(provider_key)
            if self.force_refresh:
                threshold=self.metric_hints.get(provider_key,{}).get('received_at',int(time.time()))
                if not isinstance(provider,dict) or provider.get('received_at',-1)<threshold:provider=None
        if not self.usable_provider(provider,provider_request) and cached_valid and not self.force_refresh:provider=cached
        if not self.usable_provider(provider,provider_request) and self.graph is not None and not self.force_refresh:
            provider=self.graph.get_recent(provider_key) or (self.graph.get_recent(provider_key,ttl_seconds=7*86400) if self.allow_historical else None)
        usable=self.usable_provider(provider,provider_request)
        historical=usable and int(time.time())-provider['received_at']>=3600
        if usable and (not historical or int(time.time())-provider['received_at']<7*86400):
            self.hits+=1;self.provider_hits=getattr(self,'provider_hits',0)+1
            item=self.reference_item(request,provider)
            if historical:
                self.historical_hits+=1
                item.update(time_status='HISTORICAL_TRAFFIC_REFERENCE',notice='历史实路参考；保留原采集时间，未核验当前路况、货车限行或计划发车时刻')
            item.update(provider_cache_key=provider_key,provider_evidence_hash=provider['evidence_hash'],provider_received_at=provider['received_at'])
            item['evidence_hash']=content_hash({k:v for k,v in item.items() if k!='evidence_hash'})
            self.local[key]=item;self.references[provider_key]=provider
            if self.force_refresh:self._fresh_bound_keys.add(key)
            self.remember_metrics(provider_key,provider);self.trim_geometry(self.local);self.trim_geometry(self.references)
            return deepcopy(item)
        params = {'key': self.settings.amap_web_service_key.get_secret_value(),
                  'origin': f"{provider_origin['longitude']:.6f},{provider_origin['latitude']:.6f}",
                  'destination': f"{provider_destination['longitude']:.6f},{provider_destination['latitude']:.6f}",
                  'strategy': 32, 'show_fields': 'cost,polyline', 'output': 'json'}
        for attempt in range(2):
            if self.cancelled(): raise RoadError('CANCELLED', '规划已取消')
            if self.call_limit is not None and self.calls >= self.call_limit: raise RoadError('ROAD_CALL_BUDGET', '本次高德算路调用额度已用完，保留完整参考方案')
            self.pace('driving')
            self.calls += 1
            try:
                io_started=time.monotonic()
                try:
                    with httpx.Client(timeout=6, transport=self.transport, follow_redirects=False) as client:
                        response = client.get(DRIVING_ENDPOINT, params=params)
                finally:self.io_seconds+=time.monotonic()-io_started
                if response.status_code == 429:
                    log_throttle('driving', 'HTTP_429', attempt)
                    self.pace('driving', retry=attempt)
                    if attempt == 0: continue
                    raise RoadError('ROAD_QPS_LIMIT', '高德驾车服务请求暂时较多，请稍后重试')
                if response.status_code in [502,503,504] and attempt == 0:
                    self.pace('driving', retry=attempt); continue
                if response.status_code != 200: raise RoadError('ROAD_HTTP_ERROR', '高德驾车道路服务暂时不可用')
                if len(response.content) > 8 * 1024 * 1024: raise RoadError('ROAD_RESPONSE', '道路响应超出大小限制')
                payload = response.json()
                if not isinstance(payload, dict): raise ValueError()
                code = str(payload.get('infocode', ''))
                if str(payload.get('status')) != '1' and code in QPS_CODES:
                    log_throttle('driving', code, attempt)
                    self.pace('driving', retry=attempt)
                    if attempt == 0: continue
                    raise RoadError('ROAD_QPS_LIMIT', '高德驾车服务请求暂时较多，请稍后重试')
                if str(payload.get('status')) != '1' or str(payload.get('infocode')) != '10000':
                    raise RoadError('ROAD_API_ERROR', '驾车服务权限、额度或参数未通过校验')
                path = payload['route']['paths'][0]
                meters, seconds = float(path['distance']), float(path['cost']['duration'])
                if not math.isfinite(meters) or not math.isfinite(seconds): raise ValueError()
                polyline = []
                for step in path['steps']:
                    for point in str(step.get('polyline','')).split(';'):
                        if not point: continue
                        lng, lat = map(float, point.split(','))
                        if not math.isfinite(lng+lat) or abs(lng)>180 or abs(lat)>90: raise ValueError()
                        if not polyline or polyline[-1] != [lng,lat]: polyline.append([lng,lat])
                if len(polyline) < 2: raise ValueError()
                item = evidence(request, math.ceil(meters), math.ceil(seconds), 'DRIVING', False, polyline,
                                'TRUCK_NOT_VERIFIED', time_status='CURRENT_TRAFFIC_REFERENCE', received_at=int(time.time()),
                                notice='普通驾车参考；未核验货车限高、限重、限行及计划发车时刻，需人工核对')
                self.local[key] = item; self.cache_put(key, item)
                self._freshened_keys.add(provider_key)
                self._fresh_bound_keys.add(key)
                self.trim_geometry(self.local)
                provider=self.reference_item(provider_request,item);self.references[provider_key]=provider;self.cache_put(provider_key,provider)
                self.remember_metrics(provider_key,provider);self.trim_geometry(self.references)
                if self.graph is not None:self.graph.put_reference(provider_key,provider)
                return deepcopy(item)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 0:
                    self.pace('driving', retry=attempt); continue
                self.failures += 1; raise RoadError('ROAD_TIMEOUT', '高德驾车服务超时或网络异常')
            except (ValueError, KeyError, IndexError, TypeError):
                self.failures += 1; raise RoadError('ROAD_RESPONSE', '驾车道路返回内容不完整或格式错误')
            except RoadError:
                self.failures += 1; raise
        raise RoadError('ROAD_UNAVAILABLE', '驾车道路不可用')


class SyntheticRoad:
    """Explicit isolated UAT network, unrelated to geography. Never live-verified."""
    def __init__(self,data):
        self.points={};self.cache={};self.hits=self.reads=0
        warehouses=sorted(data['warehouses'])
        for n,wid in enumerate(warehouses):self.points[wid]=(1000*n,0)
        for wid in warehouses:
            ids=sorted(i for i,t in data['tasks'].items() if t['warehouse_id']==wid)
            x,_=self.points[wid]
            for n,i in enumerate(ids):self.points[i]=(x+1+(n%15)*3,1+(n//15)*3)
        self.input_hash=content_hash(data)
    def segment(self,v,a,b,at):
        request=road_request(v,a,b,at,'FIXTURE');request['fixture_id']='UAT_GRID_V1';request['input_hash']=self.input_hash
        key=content_hash(request)
        if key in self.cache:self.hits+=1;return deepcopy(self.cache[key])
        ax,ay=self.points[a['id']];bx,by=self.points[b['id']]
        meters=100+100*(abs(ax-bx)+abs(ay-by))+(0 if b['id']>a['id'] else 25)
        item=evidence(request,meters,meters*3//25,'FIXTURE',restriction='VERIFIED_FIXTURE_ONLY',notice='非地理模拟路网，仅用于隔离验收，不能执行')
        self.reads+=1;self.cache[key]=item;return deepcopy(item)
    def stats(self):return {'source':'FIXTURE','fixture_id':'UAT_GRID_V1','calls':0,'cache_hits':self.hits,'fixture_reads':self.reads,'call_limit':0}
