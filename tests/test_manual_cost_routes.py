from copy import deepcopy
from decimal import Decimal

import pytest

from app.road_service import FixtureRoad, load_fixture
from app.routing_engine import StopSearch, check_result, compare_manual, line_fee, normalize, optimize_manual, quote_signature, recalculate
from test_plan_api import frozen, new_job, plan, waited
from test_stage4 import basis


def manual_fixture():
    fixture = load_fixture('MIXED_56')
    labels = fixture['roads']['labels']; rows = fixture['roads']['directed_distance_m']
    warehouse = fixture['warehouses'][0]['id']
    for a,b,meters in [(warehouse,'A',10000),(warehouse,'B',30000),('A','B',5000),('B','A',50000)]:
        rows[labels.index(a)][labels.index(b)] = meters
    return fixture, {'routes':[{'vehicle_id':'V10','task_ids':['B','A']}], 'common_ids':['C','D'], 'unassigned_ids':[]}


def test_manual_optimization_minimizes_price_without_changing_vehicle_or_modes():
    fixture,draft = manual_fixture()
    before = recalculate(fixture,draft,FixtureRoad(fixture))
    after = optimize_manual(fixture,draft,FixtureRoad(fixture))
    assert before['routes'][0]['task_ids'] == ['B','A']
    assert after['routes'][0]['task_ids'] == ['A','B']
    assert after['routes'][0]['vehicle_id'] == 'V10'
    assert after['common'] == before['common'] and after['unassigned'] == before['unassigned']
    assert Decimal(after['total_fee']) < Decimal(before['total_fee'])
    assert after['check']['valid'] and after['algorithm']['optimality_proven']
    compare_manual(after,before)
    comparison = after['comparison']
    assert comparison['delta'] == f"{Decimal(after['total_fee'])-Decimal(before['total_fee']):.2f}"
    assert comparison['current_dedicated'] == after['check']['dedicated_fee']
    assert comparison['previous_common'] == before['check']['common_fee']


def test_manual_optimization_budget_keeps_the_submitted_complete_order():
    fixture,draft = manual_fixture(); calls=[0]
    def control():
        calls[0]+=1
        if calls[0]>=3:raise StopSearch('TIME_BUDGET')
    result=optimize_manual(fixture,draft,FixtureRoad(fixture),control)
    assert result['routes'][0]['task_ids'] == ['B','A'] and result['check']['valid']
    assert result['algorithm']['stop_reason']=='TIME_BUDGET' and not result['algorithm']['optimality_proven']


def test_manual_optimization_cancellation_does_not_save_a_reordered_result():
    fixture,draft = manual_fixture(); calls=[0]
    def control():
        calls[0]+=1
        if calls[0]>=3:raise StopSearch('CANCELLED')
    with pytest.raises(StopSearch, match='CANCELLED'):
        optimize_manual(fixture,draft,FixtureRoad(fixture),control)


RULE={'amount':'150.00','targets':[{'province_code':'340000','province_name':'安徽省','city_code':'340100','city_name':'合肥市','district_code':'340104','district_name':'蜀山区'}, {'province_code':'340000','province_name':'安徽省','city_code':'340100','city_name':'合肥市'}]}


@pytest.mark.parametrize('mode,base',[('KM_REGION',{'per_km':'2'}),('BASE_STOPS_REGION',{'base_price':'30','included_stops':1,'extra_stop_price':'5'})])
def test_city_and_district_surcharge_is_one_time_per_line(mode,base):
    quote={'mode':mode,**base,'region_surcharge':deepcopy(RULE)}
    tasks=[{'province':'安徽省','city':'合肥市','district':'蜀山区','region_code':'340104'}]*3
    expected = '170.00' if mode=='KM_REGION' else '190.00'
    assert line_fee(quote,10000,3,tasks)==expected
    assert line_fee(quote,10000,0,tasks)=='0.00'
    assert line_fee(quote,10000,3,[{'region_code':'320801','province':'江苏省','city':'淮安市'}]) == ('20.00' if mode=='KM_REGION' else '40.00')
    assert quote_signature(quote)==quote_signature({**quote,'region_surcharge':{**RULE,'targets':list(reversed(RULE['targets']))}})
    assert quote_signature(quote)!=quote_signature({**quote,'region_surcharge':{**RULE,'amount':'160'}})


def test_surcharge_is_independently_rechecked_for_the_new_route():
    fixture,draft=manual_fixture()
    fixture['quotes']['Q_KM'].update(mode='KM_REGION',region_surcharge=deepcopy(RULE)) if 'Q_KM' in fixture['quotes'] else None
    first_quote=fixture['tasks'][0]['quote_id']
    fixture['quotes'][first_quote].update(mode='KM_REGION',per_km='2',region_surcharge=deepcopy(RULE))
    for task in fixture['tasks'][:2]:task.update(province='安徽省',city='合肥市',district='蜀山区',region_code='340104')
    result=optimize_manual(fixture,draft,FixtureRoad(fixture))
    assert result['routes'][0]['regional_surcharge']=='150.00' and result['check']['valid']
    mutated=deepcopy(result);mutated['routes'][0]['fee']='0.00'
    assert not check_result(normalize(fixture),mutated)['valid']


def test_manual_job_switch_reprices_and_compares_saved_versions(basis,app):
    client,warehouse,quote,store,store2,vehicle=basis
    app.state.settings.planning_road_source='TEST_GRID'
    snapshot=frozen(basis)
    draft={'routes':[{'vehicle_id':vehicle['id'],'task_ids':[store2['id'],store['id']]}],'common_ids':[],'unassigned_ids':[]}
    first=waited(client,new_job(client,snapshot,kind='MANUAL',draft=draft))
    assert first['status']=='COMPLETED',first
    before=plan(client,first['plan_id'])
    assert before['result']['routes'][0]['task_ids']==draft['routes'][0]['task_ids']
    job=waited(client,new_job(client,snapshot,1,kind='MANUAL',draft=draft,optimize_routes=True,parent_id=before['id']))
    assert job['status']=='COMPLETED',job
    after=plan(client,job['plan_id'])
    assert after['result']['algorithm']['mode']=='MANUAL_OPTIMIZED'
    assert after['result']['comparison']['previous_total']==before['result']['total_fee']
    assert after['result']['comparison']['current_total']==after['result']['total_fee']
    assert after['result']['routes'][0]['vehicle_id']==vehicle['id']
    assert plan(client,before['id'])['result']==before['result']


def test_municipality_city_target_covers_its_districts_and_names_accept_short_city():
    quote={'mode':'KM_REGION','per_km':'2','region_surcharge':{'amount':'80','targets':[{'province_code':'110000','province_name':'北京市','city_code':'110000','city_name':'北京市'}]}}
    assert line_fee(quote,10000,1,[{'region_code':'110105'}])=='100.00'
    assert line_fee(quote,10000,1,[{'region_code':'120101'}])=='20.00'
    quote['region_surcharge']={'amount':'80','targets':[{'province_code':'460000','province_name':'海南省','city_code':'469000','city_name':'省直辖县级行政区划'}]}
    assert line_fee(quote,10000,1,[{'region_code':'469025'}])=='100.00'
    assert line_fee(quote,10000,1,[{'region_code':'460105'}])=='20.00'
    quote['region_surcharge']=deepcopy(RULE)
    assert line_fee(quote,10000,1,[{'province':'安徽省','city':'合肥','district':'蜀山区'}])=='170.00'


def test_group_job_heads_and_parent_snapshots_cannot_cross_groups(basis,app):
    client,w,q,s,s2,v=basis
    app.state.settings.planning_road_source='TEST_GRID'
    snapshots=[];groups=[];plans=[]
    for name,store in [('早班',s),('晚班',s2)]:
        group=client.post('/api/planning/days/2026-11-11/groups',json={'name':name})
        assert group.status_code==201,group.text
        group_id=group.json()['id'];groups.append(group_id)
        created=client.put('/api/planning/groups/'+group_id+'/demands',json={'expected_revision':0,'items':[{'store_id':store['id'],'pieces':2}]})
        assert created.status_code==200,created.text
        snapshot=created.json()['snapshot_id'];snapshots.append(snapshot)
        job=waited(client,new_job(client,snapshot,group_id=group_id))
        assert job['status']=='COMPLETED',job
        plans.append(plan(client,job['plan_id']))
    assert [p['version'] for p in plans]==[1,2]
    for group,p in zip(groups,plans):
        rows=client.get('/api/planning/days/2026-11-11/plans',params={'group_id':group}).json()
        assert rows['head_version']==p['version'] and [r['id'] for r in rows['items']]==[p['id']]
    mismatch=client.post('/api/planning/jobs',json={'snapshot_id':snapshots[0],'group_id':groups[1],'expected_head':2})
    assert mismatch.status_code==422 and mismatch.json()['code']=='GROUP_MISMATCH'
    parent=client.post('/api/planning/jobs',json={'snapshot_id':snapshots[1],'group_id':groups[1],'expected_head':2,'kind':'MANUAL','parent_id':plans[0]['id'],'draft':{'routes':[],'common_ids':[s2['id']],'unassigned_ids':[]}})
    assert parent.status_code==422 and parent.json()['code']=='SNAPSHOT_MISMATCH'


@pytest.mark.parametrize('task',[
    {'province':'安徽省','city':'合肥市','district':'','region_code':'340100'},
    {'province':'安徽省','city':'','district':'','region_code':'340000'},
    {'province':'安徽省','city':'合肥市','district':'','region_code':''},
])
def test_unknown_region_is_not_silently_treated_as_no_surcharge(task):
    from app.routing_engine import RoadError
    quote={'mode':'KM_REGION','per_km':'2','region_surcharge':{'amount':'150','targets':[RULE['targets'][0]]}}
    with pytest.raises(RoadError,match='REGION_MISSING'):
        line_fee(quote,10000,1,[task])
    fixture,draft=manual_fixture()
    first_quote=fixture['tasks'][0]['quote_id']
    fixture['quotes'][first_quote]=quote
    for row in fixture['tasks'][:2]:row.update(task)
    result=recalculate(fixture,draft,FixtureRoad(fixture))
    assert result['routes'][0]['fee'] is None and not result['fees_complete'] and not result['confirmable']
    assert any(e['code']=='REGION_MISSING' for e in result['check']['issues'])


def test_known_other_city_excludes_target_but_any_precise_hit_covers_uncertainty():
    quote={'mode':'KM_REGION','per_km':'2','region_surcharge':{'amount':'150','targets':[RULE['targets'][0]]}}
    assert line_fee(quote,10000,1,[{'province':'安徽省','city':'芜湖市','region_code':'340200'}])=='20.00'
    unknown={'province':'安徽省','city':'合肥市','region_code':'340100'}
    precise={'region_code':'340104'}
    assert line_fee(quote,10000,2,[unknown,precise])=='170.00'
    assert line_fee(quote,10000,2,[precise,unknown])=='170.00'
    quote['region_surcharge']=deepcopy(RULE)
    assert line_fee(quote,10000,1,[unknown])=='170.00'
