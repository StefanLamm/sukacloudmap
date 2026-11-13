from copy import deepcopy
from decimal import Decimal

import pytest

from app.master_schemas import QuoteInput
from app.master_service import signature
from app.routing_engine import RoadError, line_fee, quote_signature, regional_extra
from test_stage3 import quote
from conftest import login

TARGET_A={'province_code':'340000','province_name':'安徽省','city_code':'340100','city_name':'合肥市','district_code':'340104','district_name':'蜀山区'}
TARGET_B={**TARGET_A,'district_code':'340111','district_name':'包河区'}
CITY={k:v for k,v in TARGET_A.items() if not k.startswith('district_')}
A={'region_code':'340104','province':'安徽省','city':'合肥市','district':'蜀山区'}
B={'region_code':'340111','province':'安徽省','city':'合肥市','district':'包河区'}


def rule(mode='PER_REGION', targets=None):
    return {'amount':'80','billing_mode':mode,'targets':deepcopy(targets or [TARGET_A,TARGET_B])}


def q(mode='PER_REGION', targets=None, price_mode='KM_REGION'):
    return {'mode':price_mode,'per_km':'5.8','base_price':'100','included_stops':1,'extra_stop_price':'10','region_surcharge':rule(mode,targets)}


@pytest.mark.parametrize('price_mode,expected',[('KM_REGION','218.00'),('BASE_STOPS_REGION','280.00')])
def test_multiple_districts_charge_once_each_regardless_store_count(price_mode,expected):
    quote=q(price_mode=price_mode)
    assert line_fee(quote,10000,3,[A,A,B])==expected
    assert regional_extra(quote,[A,A,B])==Decimal('160')
    assert line_fee(q('ONCE',price_mode=price_mode),10000,3,[A,A,B])==('138.00' if price_mode=='KM_REGION' else '200.00')


def test_whole_city_and_explicit_counties_do_not_double_charge():
    quote=q(targets=[CITY,TARGET_A,TARGET_B])
    assert regional_extra(quote,[A,A,B])==Decimal('160')
    assert regional_extra(q(targets=[CITY]),[A,B])==Decimal('160')
    assert regional_extra(quote,[{'region_code':'320803'}])==0


def test_named_and_coded_same_county_dedupe_with_short_city_name():
    legacy={k:v for k,v in A.items() if k!='region_code'};legacy['city']='合肥'
    assert regional_extra(q(targets=[CITY]),[A,legacy,B])==Decimal('160')
    assert regional_extra(q(targets=[TARGET_A]),[legacy,legacy])==Decimal('80')


def test_equal_district_names_in_different_cities_do_not_merge():
    other_target={'province_code':'320000','province_name':'江苏省','city_code':'320100','city_name':'南京市','district_code':'320111','district_name':'浦口区'}
    # Synthetic equal-name districts isolate the key construction, not geography.
    first={**A,'district':'同名区'}
    other={'region_code':'320111','province':'江苏省','city':'南京市','district':'同名区'}
    cities=[CITY,{k:v for k,v in other_target.items() if not k.startswith('district_')}]
    assert regional_extra(q(targets=cities),[first,{k:v for k,v in first.items() if k!='region_code'},other,{k:v for k,v in other.items() if k!='region_code'}])==Decimal('160')


def test_city_match_requires_actual_county_for_per_county_charging():
    with pytest.raises(RoadError,match='REGION_MISSING'):
        regional_extra(q(targets=[CITY]),[{'province':'安徽省','city':'合肥市','region_code':'340100'}])
    with pytest.raises(RoadError,match='REGION_MISSING'):
        regional_extra(q(targets=[CITY]),[A,{'province':'安徽省','city':'合肥市'}])
    assert regional_extra(q('ONCE',targets=[CITY]),[{'province':'安徽省','city':'合肥市','region_code':'340100'}])==Decimal('80')


def test_old_once_rules_and_signatures_keep_compatibility():
    legacy=q('ONCE');del legacy['region_surcharge']['billing_mode']
    explicit=q('ONCE')
    assert regional_extra(legacy,[A,B])==Decimal('80')
    assert quote_signature(legacy)==quote_signature(explicit)
    assert signature(legacy)==signature(explicit)
    assert quote_signature(q())!=quote_signature(explicit)
    assert signature(q())!=signature(explicit)
    assert quote_signature(q())==quote_signature(q(targets=[TARGET_B,TARGET_A]))


def test_modes_validate_and_quote_history_retains_old_rule(client):
    login(client)
    before=quote(client,code='REG-V2',mode='KM_REGION',per_km='5.8',region_surcharge=rule('ONCE'))
    assert before['latest_version']['region_surcharge'].get('billing_mode','ONCE')=='ONCE'
    updated=client.patch('/api/quotes/'+before['id'],json={'expected_revision':before['revision'],'region_surcharge':rule()})
    assert updated.status_code==200
    assert updated.json()['latest_version']['region_surcharge']['billing_mode']=='PER_REGION'
    versions=client.get('/api/quotes/'+before['id']+'/versions').json()['items']
    assert versions[0]['region_surcharge']['billing_mode']=='PER_REGION'
    assert versions[1]['region_surcharge'].get('billing_mode','ONCE')=='ONCE'
    assert versions[0]['signature']!=versions[1]['signature']
    bad=client.post('/api/quotes',json={'code':'BAD-MODE','name':'错误模式','mode':'KM_REGION','per_km':'5.8','region_surcharge':rule('UNKNOWN')})
    assert bad.status_code==422
