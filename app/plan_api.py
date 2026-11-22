from datetime import date
import json
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import ApiError, Identity, authenticated, database, event
from app.models import InputSnapshot, PlanningJob, PlanVersion
from app.plan_export import export_xlsx
from app.plan_service import (append_version, default_wall_wait_policy, detail, expect_head,
                              job_view, meta, planning_wall_budget_seconds, verified_body)
from app.planning_inputs import encode, input_context, snapshot_input_is_current
from app.road_service import FixtureRoad, load_fixture
from app.auto_dedicated import allocate_dedicated
from app.routing_engine import check_result, compare_manual, normalize, optimize, optimize_manual, recalculate

router=APIRouter(prefix='/api/planning')


class Payload(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class RouteChoice(Payload):
    vehicle_id: str=Field(min_length=1,max_length=36)
    task_ids: list[str]=Field(max_length=10000)


class Draft(Payload):
    routes: list[RouteChoice]=Field(max_length=50)
    common_ids: list[str]=Field(max_length=10000)
    unassigned_ids: list[str]=Field(max_length=10000)
    auto_dedicated_ids: list[str]=Field(default_factory=list,max_length=10000)


class NewJob(Payload):
    group_id: str | None=Field(default=None,max_length=36)
    snapshot_id: str=Field(max_length=36)
    expected_head: int=Field(ge=0)
    kind: Literal['OPTIMIZE','MANUAL']='OPTIMIZE'
    draft: Draft | None=None
    parent_id: str | None=Field(default=None,max_length=36)
    budget_seconds: int=Field(default=300,ge=1,le=24000)
    wall_budget_seconds: float | None=Field(default=None,ge=1,le=28800,allow_inf_nan=False)
    call_limit: int | None=Field(default=None,ge=0)
    seed: int=Field(default=0,ge=0,le=2147483647)
    note: str=Field(default='',max_length=240)
    optimize_routes: bool=False
    refresh_roads: bool=False

    @field_validator('call_limit', mode='before')
    @classmethod
    def production_has_no_total_call_ceiling(cls, value):
        # Accept old clients without retaining their obsolete total ceilings.
        # Strict invalid types/negative values still reach normal validation.
        return None if value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0) else value


class VersionAction(Payload):
    expected_head: int=Field(ge=0)
    note: str=Field(default='',max_length=240)


def get_plan(db,plan_id):
    row=db.get(PlanVersion,plan_id)
    if not row: raise ApiError(404,'PLAN_NOT_FOUND','方案版本不存在。')
    return row


@router.get('/capabilities')
def capabilities(request:Request,identity:Identity=Depends(authenticated)):
    s=request.app.state.settings
    return {'truck_key_configured':bool(s.amap_web_service_key),'live_calls_enabled':s.road_calls_enabled,
            'road_source':s.planning_road_source,'route_mode':s.planning_route_mode,
            'max_calls':None,'total_call_policy':'PRODUCTION_UNLIMITED',
            'max_budget_seconds':24000,'default_budget_seconds':300,
            'default_wall_budget_seconds':planning_wall_budget_seconds(300),
            'wall_wait_policy':default_wall_wait_policy(),
            'automatic_workflow':'DEEP_PORTFOLIO','automatic_road_verification':True,
            'qps':s.amap_web_qps,'live_time_verified':False,
            'notice':'普通驾车参考：未核验货车限行及计划发车时刻，路线、时序和费用需人工核对，不能确认执行。' if s.planning_route_mode=='DRIVING' else '固定样例已开放；实际货车道路需要独立验证指定发车时间、车牌颜色与限行能力。未核验的专车阻止确认。'}


@router.post('/jobs',status_code=202)
def create_job(payload:NewJob,request:Request,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    snapshot=db.get(InputSnapshot,payload.snapshot_id)
    if snapshot is None: raise ApiError(404,'SNAPSHOT_NOT_FOUND','规划输入快照不存在。')
    if payload.group_id != snapshot.group_id:
        raise ApiError(422,'GROUP_MISMATCH','请选择快照所属的需求分组。')
    body=verified_body(snapshot); expect_head(db,snapshot.delivery_date,payload.expected_head,snapshot.group_id)
    if payload.kind=='OPTIMIZE' and not snapshot_input_is_current(
            db,snapshot,input_context(db,snapshot.delivery_date,group_id=snapshot.group_id),body=body):
        raise ApiError(409,'INPUT_CHANGED','输入已变化，请先重新冻结当日快照。')
    if payload.kind=='MANUAL' and payload.draft is None: raise ApiError(422,'DRAFT_REQUIRED','请提交人工调整内容。')
    if payload.parent_id:
        parent=get_plan(db,payload.parent_id)
        if parent.snapshot_id!=snapshot.id: raise ApiError(422,'SNAPSHOT_MISMATCH','调整必须使用原方案冻结输入；更新输入请重新规划。')
    active=db.scalar(select(func.count()).select_from(PlanningJob).where(PlanningJob.status.in_(['QUEUED','RUNNING'])))
    if active>=4: raise ApiError(409,'PLANNING_BUSY','已有4个待完成任务，请稍后重试。')
    if db.scalar(select(PlanningJob.id).where(PlanningJob.delivery_date==snapshot.delivery_date,PlanningJob.group_id==snapshot.group_id,PlanningJob.status.in_(['QUEUED','RUNNING']))):
        raise ApiError(409,'DAY_PLANNING_BUSY','此需求分组已有规划任务，请等待或取消后再开始。')
    s=request.app.state.settings
    if payload.refresh_roads and (s.planning_route_mode!='DRIVING' or s.planning_road_source!='LIVE'):
        raise ApiError(422,'ROAD_REFRESH_UNSUPPORTED','当前道路模式不支持强制刷新，请取消刷新选项后按当前道路配置核算。')
    if payload.refresh_roads and not s.road_calls_enabled:
        raise ApiError(422,'LIVE_CALLS_DISABLED','当前入口未开放实时道路刷新，可取消刷新选项后使用已有实路参考。')
    if s.road_calls_enabled and not s.amap_web_service_key:
        raise ApiError(422,'LIVE_KEY_MISSING','高德Web服务Key尚未配置。')
    job=PlanningJob(delivery_date=snapshot.delivery_date,group_id=snapshot.group_id,snapshot_id=snapshot.id,kind=payload.kind,
                    request=encode(payload.model_dump()),created_by=identity.account.id,created_by_name=identity.account.display_name)
    db.add(job);db.flush();event(db,identity.account.id,'PLANNING_JOB_CREATED',job.id,f'提交{snapshot.delivery_date}规划任务')
    db.commit();return job_view(db,job)


@router.get('/days/{delivery_date}/jobs')
def jobs(delivery_date:date,group_id:str|None=Query(None,max_length=36),identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    rows=db.scalars(select(PlanningJob).where(PlanningJob.delivery_date==delivery_date.isoformat(),PlanningJob.group_id==group_id).order_by(PlanningJob.created_at.desc(),PlanningJob.id.desc()).limit(20))
    return {'items':[job_view(db,row) for row in rows]}


@router.get('/jobs/{job_id}')
def read_job(job_id:str,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    job=db.get(PlanningJob,job_id)
    if not job: raise ApiError(404,'JOB_NOT_FOUND','规划任务不存在。')
    return job_view(db,job)


@router.post('/jobs/{job_id}/cancel')
def cancel_job(job_id:str,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    job=db.get(PlanningJob,job_id)
    if not job: raise ApiError(404,'JOB_NOT_FOUND','规划任务不存在。')
    if job.status in ['QUEUED','RUNNING']:
        job.cancel_requested=True
        event(db,identity.account.id,'PLANNING_CANCEL_REQUESTED',job.id,'请求停止规划任务')
    db.commit();return job_view(db,job)


@router.get('/days/{delivery_date}/plans')
def versions(delivery_date:date,offset:int=Query(0,ge=0,le=2147483647),limit:int=Query(10,ge=1,le=100),group_id:str|None=Query(None,max_length=36),identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    day=delivery_date.isoformat();context=input_context(db,day,group_id=group_id)
    rows=db.scalars(select(PlanVersion).where(PlanVersion.delivery_date==day,PlanVersion.group_id==group_id).order_by(PlanVersion.version.desc()).offset(offset).limit(limit))
    total=db.scalar(select(func.count()).select_from(PlanVersion).where(PlanVersion.delivery_date==day,PlanVersion.group_id==group_id))
    head_version=db.scalar(select(func.max(PlanVersion.version)).where(PlanVersion.delivery_date==day,PlanVersion.group_id==group_id)) or 0
    return {'items':[meta(db,r,context) for r in rows],'total':total,'head_version':head_version}


@router.get('/plans/{plan_id}')
def read_plan(plan_id:str,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    return detail(db,get_plan(db,plan_id))


@router.get('/plans/{plan_id}/export.xlsx')
def download_xlsx(plan_id:str,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    plan=detail(db,get_plan(db,plan_id))
    return Response(export_xlsx(plan),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition':f'attachment; filename="suka-{plan["delivery_date"]}-v{plan["version"]}.xlsx"'})


@router.get('/plans/{plan_id}/export.json')
def download_json(plan_id:str,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    plan=detail(db,get_plan(db,plan_id))
    return Response(encode(plan),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="suka-plan-{plan_id}.json"'})


@router.post('/plans/{plan_id}/confirm',status_code=201)
def confirm(plan_id:str,payload:VersionAction,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    row=get_plan(db,plan_id);latest=expect_head(db,row.delivery_date,payload.expected_head,row.group_id)
    if latest.id!=row.id: raise ApiError(409,'NOT_LATEST_PLAN','请先恢复为新草稿后再确认历史版本。')
    body=verified_body(row);snapshot=db.get(InputSnapshot,row.snapshot_id)
    if not snapshot_input_is_current(db,snapshot,input_context(db,row.delivery_date,group_id=row.group_id)):
        raise ApiError(409,'INPUT_CHANGED','输入已变化，旧版本保留只读，请基于新快照重新规划。')
    proof=check_result(normalize(body['input']),body['result'])
    live=all(s['source']=='LIVE' and s['verified'] for r in body['result']['routes'] for s in r['segments'])
    if not proof['valid'] or not body['result']['confirmable'] or not live:
        raise ApiError(422,'PLAN_NOT_CONFIRMABLE','方案存在冲突、未分配或专车道路未核验，不能确认。',proof['issues'])
    if row.status=='CONFIRMED':return detail(db,row)
    new=append_version(db,snapshot,body['result'],identity.account.id,identity.account.display_name,row,status='CONFIRMED',note=payload.note or '人工确认')
    db.commit();return detail(db,new)


@router.post('/plans/{plan_id}/restore',status_code=201)
def restore(plan_id:str,payload:VersionAction,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    row=get_plan(db,plan_id);expect_head(db,row.delivery_date,payload.expected_head,row.group_id)
    body=verified_body(row);snapshot=db.get(InputSnapshot,row.snapshot_id)
    new=append_version(db,snapshot,body['result'],identity.account.id,identity.account.display_name,row,note=payload.note or f'从V{row.version}恢复；保留原输入与道路证据')
    db.commit();return detail(db,new)


@router.get('/examples/{fixture_id}')
def example(fixture_id:Literal['MIXED_56','SHARED_40'],identity:Identity=Depends(authenticated)):
    fixture=load_fixture(fixture_id);result=optimize(fixture,FixtureRoad(fixture))
    return {'id':fixture_id,'input':fixture,'result':result,'notice':'固定道路测试；不写入业务资料，不可确认执行。'}


@router.post('/examples/{fixture_id}/recalculate')
def example_manual(fixture_id:Literal['MIXED_56','SHARED_40'],payload:Draft,optimize_routes:bool=False,identity:Identity=Depends(authenticated)):
    fixture=load_fixture(fixture_id);result=allocate_dedicated(fixture,payload.model_dump(),FixtureRoad(fixture),optimize_routes=optimize_routes)
    compare_manual(result,optimize(fixture,FixtureRoad(fixture)))
    return {'id':fixture_id,'input':fixture,'result':result,'notice':'固定道路测试；不写入业务资料，不可确认执行。'}
