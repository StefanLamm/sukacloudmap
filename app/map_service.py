"""Cached address lookup shared by interactive searches and the serial import queue."""
from collections import OrderedDict
import math
from threading import Lock
from time import monotonic

import httpx

from app.api import ApiError


class OpenMapSearchService:
    def __init__(self, settings, *, transport=None, clock=monotonic):
        self.url = settings.map_geocoder_url
        self.transport, self.clock = transport, clock
        self.cache = OrderedDict()
        self.lock = Lock()
        self.last_request = float('-inf')

    def search(self, query):
        query = ' '.join(query.split())
        if not 2 <= len(query) <= 200:
            raise ApiError(422, 'MAP_QUERY_INVALID', '请输入2至200字的城市、地址或地点名称。')
        with self.lock:
            current = self.clock()
            cached = self.cache.get(query)
            if cached and cached[0] > current:
                self.cache.move_to_end(query)
                return {'items': cached[1], 'provider': 'Photon / OpenStreetMap'}
            if current - self.last_request < 1:
                raise ApiError(429, 'MAP_SEARCH_RATE_LIMIT', '搜索较频繁，请稍后再试。')
            self.last_request = current
            try:
                with httpx.Client(timeout=10, follow_redirects=False, transport=self.transport,
                                  headers={'User-Agent': 'SukaCloudmap/0.10 (address location association)'}) as client:
                    response = client.get(self.url, params={'q': query, 'limit': 5})
                    response.raise_for_status()
                    features = response.json()['features']
                    if not isinstance(features, list):
                        raise ValueError('Invalid map response')
                    items = []
                    for feature in features[:5]:
                        coordinates = feature.get('geometry', {}).get('coordinates', [])
                        if len(coordinates) != 2:
                            continue
                        longitude, latitude = map(float, coordinates)
                        if not all(math.isfinite(v) for v in [longitude, latitude]) or not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                            continue
                        properties = feature.get('properties', {})
                        parts = list(dict.fromkeys(str(properties[k]) for k in ['country', 'state', 'city', 'district', 'street', 'housenumber', 'name'] if properties.get(k)))
                        address = ' · '.join(parts)[:300]
                        items.append({'name': str(properties.get('name') or address)[:120], 'address': address,
                                      'longitude': f'{longitude:.8f}'.rstrip('0').rstrip('.'),
                                      'latitude': f'{latitude:.8f}'.rstrip('0').rstrip('.'), 'crs': 'WGS84', 'location_source': 'Photon / OpenStreetMap文字搜索',
                                      'address_parts': {k: str(properties.get(k) or '')[:120] for k in ['state', 'city', 'district', 'street', 'housenumber']}})
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                raise ApiError(502, 'MAP_SEARCH_UNAVAILABLE', '地点搜索暂时不可用，请稍后重试，或在地图上选点。')
            self.cache[query] = (current + 86400, items)
            self.cache.move_to_end(query)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
            return {'items': items, 'provider': 'Photon / OpenStreetMap'}


class MapSearchService:
    def __init__(self, settings, *, transport=None, clock=monotonic):
        from app.amap_service import AMapSearchService
        service = AMapSearchService if settings.map_provider == 'AMAP' else OpenMapSearchService
        self.service = service(settings, transport=transport, clock=clock)

    def search(self, query):
        return self.service.search(query)
