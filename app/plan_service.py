"""Durable jobs execute outside request/write transactions; versions are append-only."""
import json
import logging
import math
import threading
import time
from time import time as epoch_now

from sqlalchemy import func, select

from app.api import ApiError, event, stamp
from app.database import start_write
from app.models import Account, InputSnapshot, PlanningJob, PlanVersion, now
from app.planning_inputs import content_hash, encode, input_context
from app.road_service import DrivingRoad, SyntheticRoad, TruckRoad
from app.auto_dedicated import allocate_dedicated
from app.routing_engine import RoadError, StopSearch, check_result, compare_manual, normalize, optimize, optimize_manual, recalculate
from app.route_deep import optimize_deep
from app.source_provenance import collect_source_provenance

log = logging.getLogger('suka.planning')


def planning_wall_budget_seconds(compute_seconds, explicit=None):
    return explicit if explicit is not None else compute_seconds+min(1800,max(120,compute_seconds*2))


def default_wall_wait_policy():
    return {'formula':'compute + min(1800, max(120, 2 * compute))',
            'extra_compute_multiplier':2,'extra_min_seconds':120,'extra_max_seconds':1800,
            'includes_queue':True,'includes_road_wait':True,
            'scope':'ACCEPTANCE_TO_PUBLICATION_INCLUDING_QUEUE_PREPARATION_ROADS_AND_VERIFICATION',
            'authority':'SERVER_DEADLINE'}


def head(db, day, group_id=None):
    return db.scalar(select(PlanVersion).where(PlanVersion.delivery_date == day,PlanVersion.group_id==group_id).order_by(PlanVersion.version.desc()).limit(1))


def expect_head(db, day, expected, group_id=None):
    latest = head(db, day, group_id)
    if (latest.version if latest else 0) != expected:
        raise ApiError(409, 'PLAN_VERSION_CONFLICT', '方案已有新版本，请刷新后再保存；当前调整不会覆盖他人版本。')
    return latest


def verified_body(row):
    body = json.loads(row.content)
    if content_hash(body) != row.content_hash:
        raise ApiError(500, 'PLAN_INTEGRITY', '保存内容完整性检查失败，请联系管理员。')
    return body


def meta(db, row, context=None):
    body = verified_body(row); snapshot = db.get(InputSnapshot, row.snapshot_id)
    context = context or input_context(db, row.delivery_date,group_id=row.group_id)
    return {'id': row.id, 'delivery_date': row.delivery_date, 'group_id':row.group_id, 'version': row.version, 'parent_id': row.parent_id,
            'snapshot_id': row.snapshot_id, 'snapshot_version': snapshot.version, 'status': row.status,
            'input_fingerprint':snapshot.input_fingerprint,
            'content_hash': row.content_hash, 'note': row.note, 'created_at': stamp(row.created_at), 'created_by': row.created_by_name,
            'input_changed': snapshot.input_fingerprint != context['fingerprint'], 'total_fee': body['result']['total_fee'],
            'valid': body['result']['check']['valid'], 'confirmable': body['result']['confirmable'],
            'blocking_issue_count':sum(issue['code']!='DRIVING_REFERENCE' for issue in body['result']['check']['issues']),
            'reference_notice_count':sum(issue['code']=='DRIVING_REFERENCE' for issue in body['result']['check']['issues']),
            'unassigned_count': len(body['result']['unassigned']), 'road_source': body['result']['road_source']}


def detail(db, row):
    return {**meta(db, row), **verified_body(row)}


def append_version(db, snapshot, result, actor_id, actor_name, parent=None, job_id=None, status='DRAFT', note='', *, control=None):
    latest_version = db.scalar(select(func.max(PlanVersion.version)).where(PlanVersion.delivery_date==snapshot.delivery_date)) or 0
    body = {'schema_version': 1, 'input': verified_body(snapshot), 'input_hash': snapshot.content_hash,
            'result': result, 'snapshot_id': snapshot.id}
    row = PlanVersion(delivery_date=snapshot.delivery_date, group_id=snapshot.group_id,version=latest_version+1,
                      parent_id=parent.id if parent else None, snapshot_id=snapshot.id, job_id=job_id, status=status,
                      content=encode(body), content_hash=content_hash(body), note=note, created_by=actor_id, created_by_name=actor_name)
    if control:
        control()
    db.add(row); db.flush(); event(db, actor_id, 'PLAN_'+status, row.id, f'{snapshot.delivery_date}方案V{row.version}：{note}'[:240])
    return row


def job_view(db, job):
    payload=json.loads(job.request)
    compute_seconds=payload.get('budget_seconds',15)
    plan = db.scalar(select(PlanVersion).where(PlanVersion.job_id == job.id))
    result = json.loads(job.result) if job.result else None
    progress = result.get('_progress') if isinstance(result, dict) else None
    complete = isinstance(result, dict) and 'total_fee' in result and 'check' in result
    if complete and result.get('algorithm', {}).get('mode') == 'DAILY_DEEP_PORTFOLIO':
        published=plan is not None and job.status in ('COMPLETED','CANCELLED')
        stage=('CANCELLED' if job.status == 'CANCELLED' else 'COMPLETE') if published else ('CANCELLED' if job.status=='CANCELLED' else 'FAILED')
        message=('已保留取消前的完整方案' if job.status == 'CANCELLED' else '深度规划已完成并保存') if published else '计算结果保留在任务中，未保存新方案'
        progress = {'stage':stage,
                    'message':message,
                    'best_fee':result['total_fee'],
                    'elapsed_seconds':result['algorithm'].get('elapsed_seconds',0)}
    elif job.status in ('FAILED','CANCELLED'):
        progress={**(progress if isinstance(progress,dict) else {}),'stage':job.status,
                  'message':job.error_message or ('已取消，未保存新方案' if job.status=='CANCELLED' else '计算未完成，未保存新方案')}
    return {'id': job.id, 'delivery_date': job.delivery_date, 'group_id':job.group_id, 'status': job.status, 'kind': job.kind,
            'cancel_requested': job.cancel_requested, 'budget_seconds':compute_seconds,
            'wall_budget_seconds':planning_wall_budget_seconds(compute_seconds,payload.get('wall_budget_seconds')),
            'server_now': stamp(epoch_now()), 'created_at': stamp(job.created_at), 'started_at': stamp(job.started_at),
            'finished_at': stamp(job.finished_at), 'error_code': job.error_code, 'error_message': job.error_message,
            'plan_id': plan.id if plan else None, 'progress':progress,
            'summary': {'total_fee': result['total_fee'], 'valid': result['check']['valid'],
            'algorithm': result.get('algorithm')} if complete else None}


def public_progress(value):
    """Persist only the small, public job-progress contract, not solver objects."""
    if not isinstance(value,dict):return {}
    result={}
    for name,maximum in [('stage',64),('message',240),('best_fee',40)]:
        if isinstance(value.get(name),str):result[name]=value[name][:maximum]
    for name in ('round_index','round_total','seed','candidate_count'):
        number=value.get(name)
        if isinstance(number,int) and not isinstance(number,bool) and number>=0:result[name]=number
    elapsed=value.get('elapsed_seconds')
    if isinstance(elapsed,(int,float)) and not isinstance(elapsed,bool) and math.isfinite(elapsed) and elapsed>=0:
        result['elapsed_seconds']=round(elapsed,3)
    result['updated_at']=now()
    return result


def verify_deep_result(body,result,road,was_cancelled=False,*,control=None):
    """A second publication gate, separate from the search and road collector."""
    if control:
        control()
    data=normalize(body);proof=check_result(data,result,control)
    if not result.get('fees_complete') or result.get('unassigned') or any(i['code']!='DRIVING_REFERENCE' for i in proof['issues']):
        raise ApiError(422,'DEEP_NO_FEASIBLE_PLAN','尚未找到覆盖全部门店且通过费用与配送限制校验的方案，请核对资料后重试。',proof['issues'])
    if result.get('check')!=proof:
        raise ApiError(422,'DEEP_RESULT_INTEGRITY','深度规划结果复核不一致，未保存新方案。')
    validation=result.get('algorithm',{}).get('final_validation',{})
    if not result['routes']:
        if validation.get('mode')!='COMMON_ONLY_VALIDATED' or validation.get('fresh') is not False or validation.get('selected_segments')!=0:
            raise ApiError(422,'DEEP_ROAD_RECEIPT','全共配方案不能标记为专车道路实时复核。')
        return
    if not isinstance(road,DrivingRoad):return
    if result.get('road_source')!='DRIVING' or result.get('confirmable') is not False:
        raise ApiError(422,'DEEP_DRIVING_SCOPE','普通驾车参考不能标记为已核验货车执行方案。')
    live=road.call_limit!=0 and road.settings.road_calls_enabled
    claims_fresh=validation.get('fresh') is True or validation.get('mode')=='FRESH_DRIVING_VALIDATED'
    if not live and claims_fresh:
        raise ApiError(422,'DEEP_ROAD_RECEIPT','当前入口未开放实时算路，结果不能标记为本轮高德实时复核。')
    if (was_cancelled or not live) and not claims_fresh:
        for route in result['routes']:
            for segment in route['segments']:
                if control:
                    control()
                if not DrivingRoad.valid_reference(segment,allow_expired=True,allow_historical_status=True) or not 0<=int(time.time())-segment['received_at']<7*86400:
                    raise ApiError(422,'DEEP_ROAD_REFERENCE_EXPIRED','参考道路已过期或证据无效，未保存新方案。')
        return
    if validation.get('mode')!='FRESH_DRIVING_VALIDATED' or validation.get('fresh') is not True:
        raise ApiError(422,'DEEP_ROAD_VERIFICATION_REQUIRED','专车道路尚未完成本轮高德复核，未保存新方案。')
    started,finished=validation.get('network_started_at'),validation.get('network_finished_at')
    if any(isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) for n in (started,finished)) or not 0<started<=finished<=time.time()+1:
        raise ApiError(422,'DEEP_ROAD_RECEIPT','道路复核时间记录无效，未保存新方案。')
    selected={}
    for route in result['routes']:
        origin=data['warehouses'][data['vehicles'][route['vehicle_id']]['warehouse_id']]
        for task_id,segment in zip(route['task_ids'],route['segments']):
            if control:
                control()
            target=data['tasks'][task_id]
            key=content_hash(DrivingRoad.reference_request(origin,target))
            metadata=segment.get('actual_source_receipt')
            if not isinstance(metadata,dict):raise ApiError(422,'DEEP_ROAD_RECEIPT','缺少本轮道路真实回执，未保存新方案。')
            receipt={**metadata,'polyline':segment['polyline']}
            request=receipt.get('request',{})
            try:canonical=content_hash(DrivingRoad.reference_request(request.get('origin',{}),request.get('destination',{})))
            except (KeyError,TypeError,ValueError,RoadError):
                raise ApiError(422,'DEEP_ROAD_RECEIPT','道路实际回执的坐标记录无效，未保存新方案。') from None
            if (canonical!=key or not DrivingRoad.valid_reference(receipt, allow_expired=getattr(road, 'fresh_only', False))
                or not started<=receipt['received_at']<=finished
                or segment.get('time_status')!='CURRENT_TRAFFIC_REFERENCE'
                or segment.get('provider_evidence_hash')!=receipt['evidence_hash']
                or segment['received_at']!=receipt['received_at']
                or segment['meters']!=receipt['meters'] or segment['seconds']!=receipt['seconds']):
                raise ApiError(422,'DEEP_ROAD_RECEIPT','选中道路与本轮实际回执不一致，未保存新方案。')
            selected[key]={k:receipt[k] for k in ('received_at','meters','seconds','evidence_hash')}
            origin=target
    counters=(validation.get('actual_calls'),validation.get('total_job_calls'),validation.get('source_count'))
    if any(type(n) is not int or n < 0 for n in counters):
        raise ApiError(422,'DEEP_ROAD_RECEIPT','道路采集数量无效，未保存新方案。')
    batch_mode = getattr(road, 'fresh_only', False)
    reused = validation.get('reused_job_directions', 0) if batch_mode else 0
    if batch_mode and (validation.get('measurement_policy') != 'CURRENT_JOB_PROVIDER_ONLY'
            or started != road.batch_started_at
            or type(reused) is not int or not 0 <= reused <= validation.get('source_count', -1)):
        raise ApiError(422,'DEEP_ROAD_RECEIPT','本轮道路采集批次记录不一致，未保存新方案。')
    verified_directions = validation.get('actual_calls',0) + reused
    if (validation.get('total_job_calls')!=road.stats().get('calls')
        or validation.get('actual_calls',0)>validation.get('total_job_calls',0)
        or validation.get('source_count',0)>verified_directions
        or validation.get('source_count',0)<len(selected)
        or validation.get('selected_provider_sources')!=selected or validation.get('selected_provider_count')!=len(selected)
        or validation.get('selected_segments')!=sum(len(row['segments']) for row in result['routes'])
        or validation.get('selected_subset_of_collected') is not True
        or not isinstance(validation.get('actual_calls'),int) or isinstance(validation.get('actual_calls'),bool)
        or verified_directions<len(selected)):
        raise ApiError(422,'DEEP_ROAD_RECEIPT','道路采集数量与最终选择不一致，未保存新方案。')
    if batch_mode:
        for key, metadata in selected.items():
            if control:
                control()
            receipt = road.batch_receipt(key)
            if receipt is None or any(receipt.get(k)!=v for k,v in metadata.items()):
                raise ApiError(422,'DEEP_ROAD_RECEIPT','选中道路不属于本轮实时采集批次，未保存新方案。')


def _verify_current_job_roads(result, road, control=None):
    """Check that priced manual and truck routes use only measurements made by this job."""
    if not getattr(road, 'fresh_only', False):
        return
    for route in result['routes']:
        for segment in route['segments']:
            if control:
                control()
            request = segment['request']
            if isinstance(road, DrivingRoad):
                key = content_hash(DrivingRoad.reference_request(request['origin'], request['destination']))
                measured = road.batch_receipt(key)
            else:
                measured = road.local.get(content_hash(request))
            if (measured is None or measured.get('received_at', 0) < road.batch_started_at
                    or any(segment.get(k) != measured.get(k) for k in ('meters','seconds','received_at','polyline'))):
                raise ApiError(422,'CURRENT_ROAD_REQUIRED','参与核算的道路不属于本轮高德回执，未保存新方案。')


class PlanningRunner:
    def __init__(self, engine, sessions, settings):
        self.engine, self.sessions, self.settings = engine, sessions, settings
        self.stop_event = threading.Event(); self.thread = None
    def start(self):
        self.thread = threading.Thread(target=self.loop, name='suka-planning', daemon=True); self.thread.start()
    def close(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=15)
    def loop(self):
        while not self.stop_event.is_set():
            try:
                job_id = self.claim()
                if job_id: self.run(job_id)
                else: self.stop_event.wait(.25)
            except Exception as exc:
                log.error('planning_worker_error exception_type=%s', type(exc).__name__)
                self.stop_event.wait(.5)
    def claim(self):
        with self.sessions() as db:
            # Poll read-only first; idle workers never reserve a SQLite writer.
            pending = db.scalar(select(PlanningJob.id).where(PlanningJob.status == 'QUEUED').order_by(PlanningJob.created_at).limit(1))
            expired = db.scalar(select(PlanningJob.id).where(PlanningJob.status == 'RUNNING', PlanningJob.lease_until < now()).limit(1))
            if pending is None and expired is None: return None
        with self.sessions() as db:
            start_write(db, self.engine)
            for job in db.scalars(select(PlanningJob).where(PlanningJob.status == 'RUNNING', PlanningJob.lease_until < now())):
                job.status = 'FAILED'; job.error_code = 'WORKER_INTERRUPTED'; job.error_message = '服务中断，原输入已保留，请重新规划。'; job.finished_at = now()
            job = db.scalar(select(PlanningJob).where(PlanningJob.status == 'QUEUED').order_by(PlanningJob.created_at).limit(1))
            if job is None: db.commit(); return None
            if job.cancel_requested:
                job.status='CANCELLED'; job.finished_at=now(); db.commit(); return None
            payload = json.loads(job.request)
            job.status='RUNNING'; job.started_at=now(); job.lease_until=now()+60
            db.commit(); return job.id
    def renew_lease(self,job_id):
        with self.sessions() as db:
            start_write(db,self.engine);job=db.get(PlanningJob,job_id)
            if job is None or job.status!='RUNNING' or job.cancel_requested or job.lease_until is None or job.lease_until<now():return False
            job.lease_until=now()+60;db.commit();return True
    def run(self, job_id):
        job_started=time.monotonic()
        job_epoch_started=epoch_now()
        wall_state=None
        result=None
        result_verified=False
        def wait_audit(instant=None):
            if wall_state is None:return {}
            instant=time.monotonic() if instant is None else instant
            elapsed=max(0.,instant-job_started)
            total=wall_state['queue_wait_seconds']+elapsed
            return {'queue_wait_seconds':round(wall_state['queue_wait_seconds'],6),
                    'requested_compute_budget_seconds':wall_state['compute_budget_seconds'],
                    'job_elapsed_seconds':round(elapsed,6),
                    'total_wait_elapsed_seconds':round(total,6),
                    'deadline_overrun_seconds':round(max(0.,total-wall_state['wall_budget_seconds']),6),
                    'job_wall_budget_seconds':wall_state['wall_budget_seconds'],
                    'job_wall_deadline':wall_state['deadline'],
                    'job_wall_deadline_epoch':wall_state['deadline_epoch'],
                    'queue_clock_policy':'CREATED_AT_SECONDS_FLOOR_PLUS_WORKER_MONOTONIC; CONSERVATIVE_PRECISION'}
        def wall_publication_gate(current, checked_result):
            instant=time.monotonic()
            checked_result.setdefault('algorithm',{}).update(wait_audit(instant))
            if instant<wall_state['deadline']:return False
            checked_result['algorithm'].update(stop_reason='WALL_BUDGET',publication='NOT_PUBLISHED_WALL_DEADLINE')
            current.result=encode(checked_result);current.status='FAILED';current.error_code='WALL_BUDGET'
            current.error_message='总等待截止（含排队），已校验结果保留在任务中，未新增业务方案。'
            current.finished_at=now()
            return True
        heartbeat_stop=threading.Event();lost_lease=threading.Event();heartbeat_thread=None
        def heartbeat():
            while not heartbeat_stop.wait(10):
                try:
                    if not self.renew_lease(job_id):lost_lease.set();return
                except Exception as exc:
                    log.error('planning_heartbeat_error exception_type=%s',type(exc).__name__)
        road=None
        try:
            with self.sessions() as db:
                job = db.get(PlanningJob, job_id)
                if job is None:return
                payload = json.loads(job.request)
                # Acceptance, queue, preparation, search and verification share
                # one wall allowance. created_at is a seconds-floor timestamp;
                # using that lower bound can shorten, never extend, its cutoff.
                compute_budget=payload['budget_seconds']
                wall_budget=planning_wall_budget_seconds(compute_budget,payload.get('wall_budget_seconds'))
                if isinstance(wall_budget,bool) or not math.isfinite(float(wall_budget)) or wall_budget<=0:
                    raise ApiError(422,'WALL_BUDGET_INVALID','任务总等待时限无效，请重新提交规划。')
                accepted_epoch=float(job.created_at)
                queue_wait=max(0.,job_epoch_started-accepted_epoch)
                wall_deadline=job_started+max(0.,wall_budget-queue_wait)
                wall_state={'queue_wait_seconds':queue_wait,'wall_budget_seconds':wall_budget,
                            'compute_budget_seconds':compute_budget,
                            'deadline':wall_deadline,'deadline_epoch':accepted_epoch+wall_budget}
                if time.monotonic()>=wall_deadline:raise StopSearch('WALL_BUDGET')
                snapshot = db.get(InputSnapshot, job.snapshot_id); body = verified_body(snapshot)
                parent_result = verified_body(db.get(PlanVersion,payload['parent_id']))['result'] if payload.get('parent_id') else None
                latest=head(db,job.delivery_date,job.group_id)
                incumbent_result=verified_body(latest)['result'] if latest and latest.snapshot_id==snapshot.id else None
            heartbeat_thread=threading.Thread(target=heartbeat,name='suka-planning-heartbeat',daemon=True);heartbeat_thread.start()
            last_poll = [0, False]
            def cancelled():
                if self.stop_event.is_set() or lost_lease.is_set(): return True
                if time.monotonic()-last_poll[0] >= .2:
                    with self.sessions() as db:
                        current=db.get(PlanningJob,job_id)
                        last_poll[:] = [time.monotonic(),current is None or current.status!='RUNNING' or current.cancel_requested or current.lease_until is None or current.lease_until<now()]
                return last_poll[1]
            def publication_deadline():
                if time.monotonic()>=wall_deadline:raise StopSearch('WALL_BUDGET')
            def wall_control():
                if cancelled():raise StopSearch('CANCELLED')
                publication_deadline()
            last_progress=[0,None]
            def progress(value):
                state=public_progress(value)
                if 'stage' not in state:return
                instant=time.monotonic()
                if state['stage']==last_progress[1] and instant-last_progress[0]<1:return
                try:
                    with self.sessions() as progress_db:
                        start_write(progress_db,self.engine)
                        current=progress_db.get(PlanningJob,job_id)
                        if current is None or current.status!='RUNNING' or current.cancel_requested:return
                        current.result=encode({'_progress':state});progress_db.commit()
                    last_progress[:]=[instant,state['stage']]
                except Exception as exc:
                    # A transient progress-write conflict must not discard a
                    # valid optimization incumbent. The lease/cancel gates
                    # remain authoritative before any final publication.
                    log.warning('planning_progress_error exception_type=%s',type(exc).__name__)
            adapter = DrivingRoad if self.settings.planning_route_mode == 'DRIVING' else TruckRoad
            wall_control()
            road = SyntheticRoad(normalize(body)) if self.settings.planning_road_source == 'TEST_GRID' else adapter(
                self.settings,cancelled=cancelled,fresh_only=True,
                production_planning=True,wall_deadline=wall_deadline,network_control=wall_control)
            preparation_seconds=time.monotonic()-job_started
            wall_control()
            if job.kind == 'OPTIMIZE':
                deep_options = dict(initial_result=incumbent_result, progress=progress,
                                    wall_budget_seconds=max(0.,wall_deadline-time.monotonic()))
                test_experiments = getattr(self.settings, 'planning_deep_experiments', None)
                if test_experiments is not None:
                    if self.settings.environment != 'test':
                        raise ApiError(409,'TEST_EXPERIMENTS_FORBIDDEN','规划实验开关仅允许隔离 test 环境。')
                    deep_options['experiments'] = dict(test_experiments)
                    deep_options['deadline_limited_candidates'] = True
                result = optimize_deep(body, road, payload['budget_seconds'], payload['seed'], cancelled,
                                       **deep_options)
                if test_experiments is not None:
                    result.setdefault('algorithm', {})['test_experiments_override'] = dict(test_experiments)
                if incumbent_result:compare_manual(result,incumbent_result)
            else:
                started = time.monotonic()
                started_io=float(getattr(road,'io_seconds',0))
                def control():
                    wall_control()
                    elapsed=time.monotonic()-started
                    if elapsed-max(0,float(getattr(road,'io_seconds',0))-started_io)>payload['budget_seconds']:raise StopSearch('TIME_BUDGET')
                try: result = allocate_dedicated(body,payload['draft'],road,control,optimize_routes=payload.get('optimize_routes',False))
                except StopSearch as exc:
                    raise ApiError(409,exc.reason,'重算已停止，人工选择已保留在浏览器，尚未保存新版本。')
                result['algorithm']['road_stats'] = road.stats()
                if parent_result: compare_manual(result,parent_result)
            if self.settings.planning_road_source == 'TEST_GRID':
                result['confirmable']=False;result['road_source']='FIXTURE';result['execution_scope']='TEST_ONLY'
            _verify_current_job_roads(result, road, publication_deadline)
            if job.kind == 'OPTIMIZE':
                if (isinstance(road, DrivingRoad) and road.fresh_only and road.calls
                        and not road.stats()['job_measured_directions']):
                    raise ApiError(502,'CURRENT_AMAP_UNAVAILABLE','本轮高德未返回可用道路，未生成替代方案，请稍后重试。')
                verify_deep_result(body,result,road,cancelled(),control=publication_deadline)
                result_verified=True
                progress({'stage':'SAVING','message':'校验完成，正在保存最低费用可行方案','best_fee':result['total_fee']})
            result.setdefault('algorithm',{}).update(
                source_provenance=collect_source_provenance(),
                requested_compute_budget_seconds=compute_budget,
                job_wall_budget_seconds=wall_budget,
                job_wall_deadline=wall_deadline,
                worker_preparation_seconds=round(preparation_seconds,6),
                total_call_policy='PRODUCTION_UNLIMITED' if self.settings.planning_road_source!='TEST_GRID' else 'ISOLATED_FIXTURE')
            with self.sessions() as db:
                start_write(db,self.engine); current = db.get(PlanningJob,job_id)
                if current is None or current.status != 'RUNNING':return
                if wall_publication_gate(current,result):db.commit();return
                if current.lease_until < now():
                    current.status='FAILED';current.error_code='WORKER_INTERRUPTED';current.error_message='任务租约已过期，原输入保留，请重新规划。';current.finished_at=now();db.commit();return
                current.result=encode(result); current.finished_at=now()
                snapshot=db.get(InputSnapshot,current.snapshot_id)
                latest=head(db,current.delivery_date,current.group_id)
                actor=db.get(Account,current.created_by)
                if current.kind=='OPTIMIZE' and (actor is None or not actor.active or actor.deleted_at is not None):
                    current.status='FAILED';current.error_code='ACCOUNT_DISABLED';current.error_message='发起账号已停用，计算结果保留在任务中，未新增方案。'
                elif current.kind=='OPTIMIZE' and snapshot.input_fingerprint!=input_context(db,current.delivery_date,group_id=current.group_id)['fingerprint']:
                    current.status='CANCELLED' if current.cancel_requested else 'FAILED';current.error_code='INPUT_CHANGED';current.error_message='计算期间需求或基础资料已变化，结果保留在任务中；请冻结最新输入后重新规划。'
                elif (latest.version if latest else 0) != payload['expected_head']:
                    current.status='FAILED';current.error_code='PLAN_VERSION_CONFLICT';current.error_message='重算期间已有新方案，结果保留，请刷新后重新操作。'
                else:
                    parent=db.get(PlanVersion,payload['parent_id']) if payload.get('parent_id') else latest
                    stopped=result.get('algorithm',{}).get('stop_reason') == 'CANCELLED' or current.cancel_requested
                    if wall_publication_gate(current,result):db.commit();return
                    current.result=encode(result)
                    append_version(db,snapshot,result,current.created_by,current.created_by_name,parent,current.id,
                                   note=payload.get('note') or ('取消前保留的完整结果' if stopped else '自动规划' if current.kind=='OPTIMIZE' else '人工调整重算'),
                                   control=publication_deadline)
                    publication_deadline()
                    current.status='CANCELLED' if stopped else 'COMPLETED'
                    current.finished_at=now()
                    db.flush()
                    publication_deadline()
                    db.commit()
                    return
                current.finished_at=now()
                db.commit()
        except Exception as exc:
            if isinstance(exc,StopSearch):
                exc=ApiError(409,exc.reason,'总等待截止（含排队），原输入保留，未新增业务方案。'
                             if exc.reason=='WALL_BUDGET' else '任务已停止，原输入保留，尚未保存新方案。')
            elif isinstance(exc,RoadError) and exc.code in {'CANCELLED','WALL_BUDGET','LIVE_CALLS_DISABLED'}:
                exc=ApiError(409,exc.code,exc.message)
            with self.sessions() as db:
                start_write(db,self.engine); job=db.get(PlanningJob,job_id)
                if job is None or job.status != 'RUNNING':return
                if wall_state is not None:
                    saved=result if result_verified and isinstance(exc,ApiError) and exc.code=='WALL_BUDGET' else None
                    if saved is None:
                        try:saved=json.loads(job.result) if job.result else {}
                        except (TypeError,ValueError):saved={}
                    if not isinstance(saved,dict):saved={}
                    saved.setdefault('algorithm',{}).update(wait_audit())
                    if isinstance(exc,ApiError) and exc.code=='WALL_BUDGET':
                        saved['algorithm'].update(stop_reason='WALL_BUDGET',publication='NOT_PUBLISHED_WALL_DEADLINE')
                    job.result=encode(saved)
                job.status='CANCELLED' if isinstance(exc,ApiError) and exc.code=='CANCELLED' else 'FAILED'
                job.error_code=exc.code if isinstance(exc,ApiError) else 'PLANNING_FAILED'
                job.error_message=exc.message if isinstance(exc,ApiError) else '规划未完成，已保存输入，请检查配置后重试。'
                job.finished_at=now(); db.commit()
            if not isinstance(exc,ApiError): log.error('planning_failed job_id=%s exception_type=%s',job_id,type(exc).__name__)

        finally:
            heartbeat_stop.set()
            if heartbeat_thread:heartbeat_thread.join(timeout=1)
            if road is not None and callable(getattr(road,'close',None)):road.close()
