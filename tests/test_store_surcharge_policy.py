"""Per-store pricing and shared-delivery timing are separate business policies."""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.master_service import signature
from app.road_service import FixtureRoad, load_fixture
from app.routing_engine import RoadError, check_result, line_fee, normalize, quote_signature, recalculate, regional_extra
from conftest import login
from test_stage3 import quote as create_quote

A_TARGET={'province_code':'340000','province_name':'安徽省','city_code':'340100','city_name':'合肥市','district_code':'340104','district_name':'蜀山区'}
B_TARGET={**A_TARGET,'district_code':'340111','district_name':'包河区'}
CITY={key:value for key,value in A_TARGET.items() if not key.startswith('district_')}


def priced(mode='PER_STORE',kind='KM_REGION',targets=None):
    return {'mode':kind,'per_km':'5.8','base_price':'100','included_stops':1,'extra_stop_price':'10',
            'region_surcharge':{'amount':'80','billing_mode':mode,'targets':deepcopy(targets or [A_TARGET,B_TARGET])}}


def store(store_id,code='340104'):
    return {'id':store_id,'region_code':code,'province':'安徽省','city':'合肥市','district':'蜀山区' if code=='340104' else '包河区'}


@pytest.mark.parametrize('kind,base',[('KM_REGION',Decimal('58')),('BASE_STOPS_REGION',Decimal('120'))])
def test_per_store_differs_from_county_and_line_rules(kind,base):
    tasks=[store('A'),store('B'),store('C','340111')]
    assert line_fee(priced(kind=kind),10000,3,tasks)==f'{base+240:.2f}'
    assert line_fee(priced('PER_REGION',kind),10000,3,tasks)==f'{base+160:.2f}'
    assert line_fee(priced('ONCE',kind),10000,3,tasks)==f'{base+80:.2f}'


def test_overlapping_city_county_rules_charge_each_physical_store_once():
    rule=priced(targets=[CITY,A_TARGET,B_TARGET])
    assert regional_extra(rule,[store('A'),store('B'),store('C','340111')])==Decimal('240')
    assert regional_extra(rule,[store('A'),store('A')])==Decimal('80')
    assert regional_extra(rule,[{'id':'outside','region_code':'320803'}])==0
    # Frozen inputs can carry both demand and store IDs. Store identity wins.
    assert regional_extra(rule,[{**store('D1'),'store_id':'A'},{**store('D2'),'store_id':'A'}])==Decimal('80')


def test_per_store_whole_city_does_not_require_county_but_ambiguous_county_does():
    coarse={'id':'A','province':'安徽省','city':'合肥市','region_code':'340100'}
    assert regional_extra(priced(targets=[CITY]),[coarse])==Decimal('80')
    with pytest.raises(RoadError,match='REGION_MISSING'):
        regional_extra(priced(targets=[A_TARGET]),[coarse])
    with pytest.raises(RoadError,match='REGION_MISSING'):
        regional_extra(priced(targets=[A_TARGET]),[store('B'),coarse])


def test_per_store_signature_is_distinct_and_old_once_shape_stays_compatible():
    legacy=priced('ONCE');legacy['region_surcharge'].pop('billing_mode')
    assert quote_signature(legacy)==quote_signature(priced('ONCE'))
    assert signature(legacy)==signature(priced('ONCE'))
    assert len({quote_signature(priced(mode)) for mode in ['ONCE','PER_REGION','PER_STORE']})==3
    assert len({signature(priced(mode)) for mode in ['ONCE','PER_REGION','PER_STORE']})==3


def test_quote_update_preserves_all_previous_billing_versions(client):
    login(client)
    saved=create_quote(client,code='STORE-FEE',mode='KM_REGION',per_km='5.8',region_surcharge=priced('ONCE')['region_surcharge'])
    original=client.get('/api/quotes/'+saved['id']+'/versions').json()['items'][0]
    for mode in ['PER_REGION','PER_STORE']:
        updated=client.patch('/api/quotes/'+saved['id'],json={'expected_revision':saved['revision'],'region_surcharge':priced(mode)['region_surcharge']})
        assert updated.status_code==200,updated.text
        saved=updated.json()
    versions=client.get('/api/quotes/'+saved['id']+'/versions').json()['items']
    assert [version['region_surcharge'].get('billing_mode','ONCE') for version in versions]==['PER_STORE','PER_REGION','ONCE']
    assert versions[-1]==original


class NoRoad:
    def segment(self,*args,**kwargs):
        pytest.fail('共配不能请求道路或使用专车到店时序')


def test_all_common_ignores_missing_or_expired_business_and_arrival_times():
    body=load_fixture('MIXED_56')
    for task in body['tasks']:
        task.update(open_at=None,close_at=None,latest_arrival_at='1900-01-01T00:00:00+08:00',unload_minutes=10**9)
    result=recalculate(body,{'routes':[],'common_ids':['A','B','C','D'],'unassigned_ids':[]},NoRoad())
    assert result['check']['valid'] and result['fees_complete']
    assert result['total_fee']=='130.00'
    assert result['common_time_policy']=='NOT_REQUIRED'
    assert all(row['arrival_at'] is None and not row['arrival_time_required'] and row['time_policy']=='NOT_REQUIRED' for row in result['common'])
    altered=deepcopy(result)
    for row in altered['common']:row['arrival_at']='1900-01-01T00:00:00+08:00'
    assert check_result(normalize(body),altered)['valid']


def test_ignoring_common_arrival_does_not_relax_dedicated_constraints():
    body=load_fixture('MIXED_56')
    body['tasks'][0]['latest_arrival_at']=body['warehouses'][0]['earliest_departure_at']
    result=recalculate(body,{'routes':[{'vehicle_id':'V10','task_ids':['A','B']}],'common_ids':['C','D'],'unassigned_ids':[]},FixtureRoad(body))
    assert not result['check']['valid']
    assert any(issue['code'] in {'LATEST_ARRIVAL','TIME_CONSTRAINT'} for issue in result['check']['issues'])


def test_dedicated_timing_failure_is_not_misrepresented_as_a_common_conflict():
    from app.routing_engine import assemble
    body=load_fixture('MIXED_56');data=normalize(body)
    result=assemble(data,[],set(data['tasks']),[],{'A':'实际到店晚于最晚到店时间'})
    row=next(item for item in result['common'] if item['task_id']=='A')
    assert result['check']['valid']
    assert row['reason'].startswith('选择共配（不校验到店时间）；专车候选说明：')
    assert row['dedicated_reference_note']=='实际到店晚于最晚到店时间'
