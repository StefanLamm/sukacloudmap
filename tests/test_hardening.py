from copy import deepcopy
from datetime import datetime
from io import BytesIO
import time

from openpyxl import Workbook
import pytest

from app.demand_imports import delivery_date_value
from app.road_service import FixtureRoad,evidence,load_fixture,road_request
from app.routing_engine import optimize
from conftest import login
from test_stage3 import post,quote,store_payload,warehouse
from test_stage4 import basis,day_state


def multi_source(name,quantity=3,date='2026-11-11'):
    wb=Workbook();ws=wb.active;ws.title='登记表';ws.append(['月度物流登记表']);ws.append(['配送日期\n（下拉选择）','下货门店\n（下拉选择）','配送货物（件）\n（下拉选择）','收入公式'])
    ws.append([date,name,quantity,'=C3*6']);ws.append(['2026-11-12','其他日期未知店',99,'=C4*6']);ws.append([None,None,'=1/0','=1/0']);wb.create_sheet('日期')
    stream=BytesIO();wb.save(stream);return stream.getvalue()


def test_multi_sheet_header_date_filter_and_irrelevant_formula(basis):
    c,w,q,s,s2,v=basis;content=multi_source(s['name'])
    info=c.post('/api/planning/demand-imports/inspect?filename=source.xlsx',content=content);assert info.status_code==200,info.text
    assert info.json()['suggested']=={'sheet_name':'登记表','header_row':2}
    assert c.post('/api/planning/demand-imports/upload?delivery_date=2026-11-11&filename=source.xlsx',content=content).status_code==422
    uploaded=c.post('/api/planning/demand-imports/upload?delivery_date=2026-11-11&filename=source.xlsx&sheet_name=登记表&header_row=2',content=content)
    assert uploaded.status_code==200,uploaded.text;u=uploaded.json();assert u['suggested_mapping']['delivery_date']==0
    assert c.post('/api/planning/demand-imports/'+u['id']+'/preview',json={'store_name':1,'pieces':2}).status_code==422
    p=c.post('/api/planning/demand-imports/'+u['id']+'/preview',json={'store_name':1,'pieces':2,'delivery_date':0});assert p.status_code==200,p.text
    r=p.json()['result'];assert r['can_commit'] and r['after']['total_pieces']==3
    assert r['other_date_row_count']==1 and r['ignored_nonbusiness_row_count']==1
    receipt=c.post('/api/planning/demand-imports/'+u['id']+'/commit',json={'delivery_date':'2026-11-11','preview_revision':1});assert receipt.status_code==200
    snap=c.get('/api/planning/snapshots/'+receipt.json()['snapshot_id']).json()['input']
    assert snap['demand_batch']['source']['sheet_name']=='登记表' and snap['tasks'][0]['source_rows'][0]['row']==3


@pytest.mark.parametrize('quantity,day',[('=1+2','2026-11-11'),(3,None),(3,'11/12/26')])
def test_source_critical_formula_missing_or_ambiguous_date_blocks_all(basis,quantity,day):
    c,w,q,s,s2,v=basis
    r=c.post('/api/planning/demand-imports/upload?delivery_date=2026-11-11&filename=source.xlsx&sheet_name=登记表&header_row=2',content=multi_source(s['name'],quantity,day));assert r.status_code==200,r.text
    p=c.post('/api/planning/demand-imports/'+r.json()['id']+'/preview',json={'store_name':1,'pieces':2,'delivery_date':0});assert p.status_code==200 and not p.json()['result']['can_commit']
    assert day_state(c)['revision']==0


def test_no_matching_date_cannot_clear_existing_daily_demand(basis):
    c,w,q,s,s2,v=basis;r=c.post('/api/planning/demand-imports/upload?delivery_date=2026-11-10&filename=source.xlsx&sheet_name=登记表&header_row=2',content=multi_source(s['name']));assert r.status_code==200
    p=c.post('/api/planning/demand-imports/'+r.json()['id']+'/preview',json={'store_name':1,'pieces':2,'delivery_date':0})
    assert not p.json()['result']['can_commit'] and p.json()['result']['selected_date_row_count']==0


@pytest.mark.parametrize('value,expected',[('2026/10/1','2026-10-01'),('2026-10-01T23:00:00-02:00','2026-10-02'),(46310,'2026-10-15')])
def test_explicit_date_parsing(value,expected):
    assert delivery_date_value(value).isoformat()==expected


def small_input():
    f=load_fixture('MIXED_56');base=f['tasks'][0]
    f['tasks']=[{**base,'id':str(i),'store_id':str(i),'pieces':1,'common_unit_price':None,'latest_arrival_at':None} for i in range(8)]
    f['vehicles'][0]['max_pieces']=8
    class UnitRoad:
        count=0
        def segment(self,v,a,b,at):
            self.count+=1
            return evidence(road_request(v,a,b,at,'FIXTURE'),100,10,'FIXTURE')
        def stats(self):return {'calls':0,'cache_hits':0}
    return f,UnitRoad()


def test_small_search_retains_complete_feasible_solution_when_budget_expires():
    f,road=small_input();r=optimize(f,road,budget_seconds=.08)
    assert r['check']['valid'] and not r['unassigned']
    assert r['algorithm']['stop_reason'] in ['TIME_BUDGET','CANDIDATE_BUDGET','SEARCH_COMPLETED']


def test_test_grid_requires_isolation_and_disables_real_calls(settings):
    from app.config import Settings
    from pydantic import ValidationError
    with pytest.raises(ValidationError):Settings(_env_file=None,planning_road_source='TEST_GRID')
    with pytest.raises(ValidationError):Settings(_env_file=None,environment='test',planning_road_source='TEST_GRID',truck_live_calls_enabled=True)
    assert settings.model_copy(update={'cookie_namespace':'uat'}).cookie_name=='suka_uat_session'
    assert settings.cookie_name=='suka_session'


def test_uat_fixture_versions_can_never_be_confirmed_and_keep_manual_choices(basis,app):
    from test_stage4 import commit,previewed
    from test_plan_api import new_job,waited,plan
    c,w,q,s,s2,v=basis
    app.state.settings.planning_road_source='TEST_GRID'
    snapshot=commit(c,previewed(c,[[s['name'],6,s['code']],[s2['name'],4,s2['code']]])).json()['snapshot_id']
    j=waited(c,new_job(c,snapshot));p=plan(c,j['plan_id'])
    assert p['result']['check']['valid'] and p['result']['execution_scope']=='TEST_ONLY' and not p['result']['confirmable']
    assert c.post('/api/planning/plans/'+p['id']+'/confirm',json={'expected_head':1}).status_code==422
    draft={'routes':[],'common_ids':[s['id'],s2['id']],'unassigned_ids':[]}
    j=waited(c,new_job(c,snapshot,1,kind='MANUAL',draft=draft,parent_id=p['id']))
    p=plan(c,j['plan_id']);assert p['result']['road_source']=='FIXTURE' and not p['result']['confirmable']
    assert p['result']['comparison']['comparable']


def test_expired_worker_does_not_publish_late_result_over_terminal_state(basis,app,monkeypatch):
    from app.models import PlanningJob,now
    from test_stage4 import commit,previewed
    from test_plan_api import new_job,waited
    import app.plan_service as service
    original=service.optimize_deep
    def lose_lease(body,road,budget,seed,cancelled,*,initial_result=None,progress=None):
        from app.database import start_write
        from sqlalchemy import select
        with app.state.sessions() as db:
            start_write(db,app.state.engine)
            job=db.scalar(select(PlanningJob).where(PlanningJob.status=='RUNNING'));job.status='FAILED';job.error_code='WORKER_INTERRUPTED';job.finished_at=now();db.commit()
        return original(body,road,budget,seed,cancelled,initial_result=initial_result,progress=progress)
    monkeypatch.setattr(service,'optimize_deep',lose_lease)
    c,w,q,s,s2,v=basis;snapshot=commit(c,previewed(c,[[s['name'],3,s['code']]])).json()['snapshot_id']
    result=waited(c,new_job(c,snapshot));time.sleep(.1)
    result=c.get('/api/planning/jobs/'+result['id']).json()
    assert result['status']=='FAILED' and result['plan_id'] is None
    assert result['error_code']=='WORKER_INTERRUPTED'


def test_runtime_endpoint_precedes_generic_master_route_and_contains_no_secrets(client,app):
    r=client.get('/api/runtime');assert r.status_code==200,r.text
    assert r.json()['environment']=='test' and not r.json()['test_fixture']
    assert 'database' not in r.text and 'key' not in r.text


def test_new_sqlite_databases_are_private_and_use_wal(settings):
    from pathlib import Path
    from app.database import make_engine
    from sqlalchemy import text
    e=make_engine(settings)
    with e.connect() as db:assert db.scalar(text('PRAGMA journal_mode'))=='wal'
    assert Path(e.url.database).stat().st_mode & 0o777==0o600
    e.dispose()


def test_shared_records_page_search_and_literal_wildcards(client):
    login(client)
    for i in range(17):
        r=client.post('/api/workspace/records',json={'title':'100%_准确' if i==0 else f'记录{i}','content':'正文'});assert r.status_code==201
    first=client.get('/api/workspace/records?limit=12').json();second=client.get('/api/workspace/records?limit=12&offset=12').json()
    assert len(first['items'])==12 and len(second['items'])==5 and first['total_all']==17
    assert not set(r['id'] for r in first['items']).intersection(r['id'] for r in second['items'])
    result=client.get('/api/workspace/records',params={'q':'%_'}).json()
    assert result['total']==1 and result['items'][0]['title']=='100%_准确'


def test_road_preflight_does_not_enumerate_impossible_candidates(settings):
    from app.road_service import TruckRoad
    f=load_fixture('MIXED_56');road=TruckRoad(settings)
    r=optimize(f,road)
    assert r['algorithm']['candidate_count']==0 and r['algorithm']['road_stats']['calls']==0
    assert r['total_fee']=='130.00' and r['check']['valid'] and not r['algorithm']['optimality_proven']


@pytest.mark.parametrize('path',['/api/workspace/records','/api/planning/days/2026-11-11/plans','/api/planning/days/2026-11-11/snapshots'])
def test_extreme_pagination_is_validation_error_not_database_overflow(client,path):
    login(client)
    r=client.get(path,params={'offset':str(2**100)})
    assert r.status_code==422,r.text


def test_ambiguous_date_headers_still_require_explicit_date_mapping(basis):
    from test_stage4 import uploaded
    c,w,q,s,s2,v=basis
    batch=uploaded(c,[[s['name'],3,'2026-11-11','2026-11-11']],headers=['门店名称','件数','配送日期','日期'])
    assert batch['suggested_mapping']['delivery_date'] is None
    no_date=c.post('/api/planning/demand-imports/'+batch['id']+'/preview',json={'store_name':0,'pieces':1})
    assert no_date.status_code==422 and no_date.json()['code']=='DATE_MAPPING_REQUIRED'
    selected=c.post('/api/planning/demand-imports/'+batch['id']+'/preview',json={'store_name':0,'pieces':1,'delivery_date':2})
    assert selected.status_code==200 and selected.json()['result']['can_commit']
