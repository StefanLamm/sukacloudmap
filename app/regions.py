"""Complete province/city/county choices from AMap's official district tree.

The public administrative data is cached locally; server credentials never leave
this module. A failed refresh retains the last available tree.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Lock
import time

import httpx

from app.api import ApiError
from app.amap_limits import AMapRateLimiter, RateLimitWaitError
from app.amap_service import secret
from app.config import ROOT

CACHE = ROOT / 'data/amap/districts.json'
REGION_FIELDS = ('province', 'city', 'district', 'region_code')
_lock = Lock()
_memory = None


def normalize_tree(rows):
    result=[]
    for province in rows:
        cities=[]
        children=province.get('districts') or []
        # Municipalities may omit the city level in the provider's tree.
        if children and all(child.get('level') == 'district' for child in children):
            children=[{'name':province['name'],'adcode':province['adcode'],'districts':children}]
        for city in children:
            districts=[{'code':str(d['adcode']),'name':d['name']} for d in city.get('districts') or [] if d.get('adcode') and d.get('name')]
            cities.append({'code':str(city['adcode']),'name':city['name'],'children':districts})
        result.append({'code':str(province['adcode']),'name':province['name'],'children':cities})
    return result


def canonical_items(items):
    # Keep the UI at province/city/county even for municipalities and counties
    # administered directly by a province. Do not expose township-level codes.
    for province in items:
        cities=province['children']
        if province['code'] in {'110000','120000','310000','500000'}:
            province['children']=[{'code':province['code'],'name':province['name'],'children':[d for c in cities for d in c['children'] if len(d['code'])==6]}]
            continue
        direct=[c for c in cities if not c['code'].endswith('00')]
        regular=[c for c in cities if c['code'].endswith('00')]
        for city in regular:city['children']=[d for d in city['children'] if len(d['code'])==6]
        if direct:
            code=province['code'][:2]+'9000'
            group=next((c for c in regular if c['code']==code),None)
            if group is None:
                group={'code':code,'name':'省直辖县级行政区','children':[]};regular.append(group)
            group['children'].extend({'code':c['code'],'name':c['name']} for c in direct)
        province['children']=regular
    return items


def cached_tree():
    global _memory
    if _memory is not None:return _memory
    try:
        value=json.loads(CACHE.read_text())
        if isinstance(value.get('items'),list) and value['items']:
            value['items']=canonical_items(value['items']);_memory=value
    except (OSError, ValueError):pass
    return _memory


def region_tree(settings, *, transport=None):
    global _memory
    with _lock:
        previous=cached_tree()
        if previous and time.time()-previous.get('fetched_at',0)<30*86400:return previous
        key=secret(settings.amap_web_service_key)
        if not key:
            if previous:return {**previous,'notice':'正在使用已缓存的行政区资料。'}
            raise ApiError(503,'REGION_CONFIG_MISSING','行政区选项尚未配置，请联系管理员设置高德服务端Key。')
        try:
            AMapRateLimiter.from_settings(settings).acquire('district')
            with httpx.Client(timeout=20,transport=transport,follow_redirects=False) as client:
                response=client.get('https://restapi.amap.com/v3/config/district',params={'key':key,'keywords':'中国','subdistrict':3,'extensions':'base'})
            response.raise_for_status();payload=response.json()
            if str(payload.get('status'))!='1':raise ValueError()
            items=canonical_items(normalize_tree(payload['districts'][0]['districts']))
            if len(items)<30 or sum(len(p['children']) for p in items)<300:raise ValueError()
            value={'items':items,'fetched_at':int(time.time()),'source':'高德行政区域查询'}
            CACHE.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            temp=CACHE.with_suffix(f'.{os.getpid()}.tmp');temp.touch(mode=0o600,exist_ok=True);temp.write_text(json.dumps(value,ensure_ascii=False));temp.replace(CACHE)
            _memory=value;return value
        except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError,OSError,RateLimitWaitError):
            if previous:return {**previous,'notice':'行政区服务暂时不可用，已保留现有选项。'}
            raise ApiError(502,'REGION_UNAVAILABLE','省市区选项暂时加载失败，请稍后重试；已填写的资料会保留。') from None


def persisted_region(values):
    return {key: values.get(key) or '' for key in REGION_FIELDS}


def infer_region(values):
    """Use only an existing cache; opening management never calls AMap."""
    result=persisted_region(values)
    tree=cached_tree()
    if not tree:return result
    address=values.get('address') or ''
    if not result['province']:
        provinces=[p['name'] for p in tree['items'] if p['name'] in address]
        if len(provinces)==1:result['province']=provinces[0]
    candidates=[]
    for province in tree['items']:
        for city in province['children']:
            aliases={city['name'],city['name'].removesuffix('市')}
            if result['city'] in aliases or city['name'] in address or any(d['name']==result['city'] or (not result['city'] and d['name'] in address) for d in city['children']):
                if result['province'] and result['province']!=province['name']:continue
                candidates.append((province,city))
    if len(candidates)!=1:return result
    province,city=candidates[0];result['province']=result['province'] or province['name'];result['city']=result['city'] or city['name']
    districts=[d for d in city['children'] if d['name']==result['district'] or d['name'] in address]
    if len(districts)==1:result.update(district=districts[0]['name'],region_code=districts[0]['code'])
    elif not result['region_code']:result['region_code']=city['code']
    return result
