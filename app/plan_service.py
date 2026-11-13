"""Durable jobs execute outside request/write transactions; versions are append-only."""
import json
import logging
import math
import threading
import time

from sqlalchemy import func, select

from app.api import ApiError, event, stamp
from app.database import start_write
from app.models import Account, InputSnapshot, PlanningJob, PlanVersion, RoadCache, now
from app.planning_inputs import content_hash, encode, input_context
from app.road_service import DrivingRoad, SyntheticRoad, TruckRoad
from app.auto_dedicated import allocate_dedicated
from app.routing_engine import RoadError, check_result, compare_manual, normalize, optimize, optimize_manual, recalculate
from app.route_deep import optimize_deep

log = logging.getLogger('suka.planning')


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


def append_version(db, snapshot, result, actor_id, actor_name, parent=None, job_id=None, status='DRAFT', note=''):
    latest_version = db.scalar(select(func.max(PlanVersion.version)).where(PlanVersion.delivery_date==snapshot.delivery_date)) or 0
    body = {'schema_version': 1, 'input': verified_body(snapshot), 'input_hash': snapshot.content_hash,
            'result': result, 'snapshot_id': snapshot.id}
    row = PlanVersion(delivery_date=snapshot.delivery_date, group_id=snapshot.group_id,version=latest_version+1,
                      parent_id=parent.id if parent else None, snapshot_id=snapshot.id, job_id=job_id, status=status,
                      content=encode(body), content_hash=content_hash(body), note=note, created_by=actor_id, created_by_name=actor_name)
    db.add(row); db.flush(); event(db, actor_id, 'PLAN_'+status, row.id, f'{snapshot.delivery_date}方案V{row.version}：{note}'[:240])
    return row


def job_view(db, job):
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
            'cancel_requested': job.cancel_requested, 'budget_seconds':json.loads(job.request).get('budget_seconds',15), 'created_at': stamp(job.created_at), 'started_at': stamp(job.started_at),
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


def verify_deep_result(body,result,road,was_cancelled=False):
    """A second publication gate, separate from the search and road collector."""
    data=normalize(body);proof=check_result(data,result)
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
            target=data['tasks'][task_id]
            key=content_hash(DrivingRoad.reference_request(origin,target))
            metadata=segment.get('actual_source_receipt')
            if not isinstance(metadata,dict):raise ApiError(422,'DEEP_ROAD_RECEIPT','缺少本轮道路真实回执，未保存新方案。')
            receipt={**metadata,'polyline':segment['polyline']}
            request=receipt.get('request',{})
            try:canonical=content_hash(DrivingRoad.reference_request(request.get('origin',{}),request.get('destination',{})))
            except (KeyError,TypeError,ValueError,RoadError):
                raise ApiError(422,'DEEP_ROAD_RECEIPT','道路实际回执的坐标记录无效，未保存新方案。') from None
            if (canonical!=key or not DrivingRoad.valid_reference(receipt)
                or not started<=receipt['received_at']<=finished
                or segment.get('time_status')!='CURRENT_TRAFFIC_REFERENCE'
                or segment.get('provider_evidence_hash')!=receipt['evidence_hash']
                or segment['received_at']!=receipt['received_at']
                or segment['meters']!=receipt['meters'] or segment['seconds']!=receipt['seconds']):
                raise ApiError(422,'DEEP_ROAD_RECEIPT','选中道路与本轮实际回执不一致，未保存新方案。')
            selected[key]={k:receipt[k] for k in ('received_at','meters','seconds','evidence_hash')}
            origin=target
    counters=(validation.get('actual_calls'),validation.get('total_job_calls'),validation.get('source_count'))
    if (any(isinstance(n,bool) or not isinstance(n,int) or n<0 for n in counters)
        or validation.get('total_job_calls')!=road.stats().get('calls')
        or validation.get('actual_calls',0)>validation.get('total_job_calls',0)
        or validation.get('source_count',0)>validation.get('actual_calls',0)
        or validation.get('source_count',0)<len(selected)
        or validation.get('selected_provider_sources')!=selected or validation.get('selected_provider_count')!=len(selected)
        or validation.get('selected_segments')!=sum(len(row['segments']) for row in result['routes'])
        or validation.get('selected_subset_of_collected') is not True
        or not isinstance(validation.get('actual_calls'),int) or isinstance(validation.get('actual_calls'),bool)
        or validation['actual_calls']<len(selected)):
        raise ApiError(422,'DEEP_ROAD_RECEIPT','道路采集数量与最终选择不一致，未保存新方案。')


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
            def cache_get(key):
                with self.sessions() as db:
                    cache = db.get(RoadCache, key)
                    if not cache or cache.expires_at <= now(): return None
                    item = json.loads(cache.content)
                    if content_hash(item['request']) != key or content_hash({k:v for k,v in item.items() if k != 'evidence_hash'}) != item['evidence_hash']: return None
                    return item
            def cache_put(key,item):
                with self.sessions() as db:
                    start_write(db,self.engine); row = db.get(RoadCache,key)
                    if row: row.content=encode(item); row.expires_at=now()+3600
                    else: db.add(RoadCache(key=key,content=encode(item),expires_at=now()+3600))
                    db.commit()
            caps = [value for value in [payload.get('call_limit'),self.settings.road_job_max_calls] if value is not None]
            limit = (min(caps) if caps else None) if self.settings.road_calls_enabled else 0
            adapter = DrivingRoad if self.settings.planning_route_mode == 'DRIVING' else TruckRoad
            # Daily deep planning first searches the measured reference graph.
            # Its final phase always validates selected roads independently;
            # forcing every exploratory candidate upstream would waste QPS.
            manual_refresh=job.kind=='MANUAL' and payload.get('refresh_roads',False)
            adapter_options={'allow_historical':not manual_refresh,
                             'force_refresh':bool(manual_refresh and self.settings.road_calls_enabled)} if adapter is DrivingRoad else {}
            road = SyntheticRoad(normalize(body)) if self.settings.planning_road_source == 'TEST_GRID' else adapter(self.settings, limit, cache_get, cache_put, cancelled=cancelled,**adapter_options)
            if isinstance(road,DrivingRoad):
                # Only the provider's ordinary-driving current-reference paths
                # are reusable across local vehicle/ETD bindings; truck proof is not.
                with self.sessions() as cache_db:
                    total_bytes=0
                    rows=cache_db.scalars(select(RoadCache).order_by(RoadCache.expires_at.desc()).limit(10000).execution_options(yield_per=50))
                    for cached_row in rows:
                        if cancelled():break
                        total_bytes+=len(cached_row.content)
                        if total_bytes>128*1024*1024:break
                        try:
                            item=json.loads(cached_row.content)
                            if content_hash(item['request'])==cached_row.key:road.seed_reference_cache([item])
                        except (KeyError,ValueError,TypeError):continue
            if job.kind == 'OPTIMIZE':
                result = optimize_deep(body, road, payload['budget_seconds'], payload['seed'], cancelled,
                                       initial_result=incumbent_result,progress=progress)
                if incumbent_result:compare_manual(result,incumbent_result)
            else:
                from app.routing_engine import StopSearch
                started = time.monotonic()
                started_io=float(getattr(road,'io_seconds',0))
                wall_limit=payload['budget_seconds']+min(1800,max(120,payload['budget_seconds']*2))
                def control():
                    if cancelled(): raise StopSearch('CANCELLED')
                    elapsed=time.monotonic()-started
                    if elapsed>wall_limit:raise StopSearch('WALL_BUDGET')
                    if elapsed-max(0,float(getattr(road,'io_seconds',0))-started_io)>payload['budget_seconds']:raise StopSearch('TIME_BUDGET')
                try: result = allocate_dedicated(body,payload['draft'],road,control,optimize_routes=payload.get('optimize_routes',False))
                except StopSearch as exc:
                    raise ApiError(409,exc.reason,'重算已停止，人工选择已保留在浏览器，尚未保存新版本。')
                result['algorithm']['road_stats'] = road.stats()
                if parent_result: compare_manual(result,parent_result)
            if self.settings.planning_road_source == 'TEST_GRID':
                result['confirmable']=False;result['road_source']='FIXTURE';result['execution_scope']='TEST_ONLY'
            if job.kind == 'OPTIMIZE':
                verify_deep_result(body,result,road,cancelled())
                progress({'stage':'SAVING','message':'校验完成，正在保存最低费用可行方案','best_fee':result['total_fee']})
            with self.sessions() as db:
                start_write(db,self.engine); current = db.get(PlanningJob,job_id)
                if current is None or current.status != 'RUNNING':return
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
                    append_version(db,snapshot,result,current.created_by,current.created_by_name,parent,current.id,
                                   note=payload.get('note') or ('取消前保留的完整结果' if stopped else '自动规划' if current.kind=='OPTIMIZE' else '人工调整重算'))
                    current.status='CANCELLED' if stopped else 'COMPLETED'
                db.commit()
        except Exception as exc:
            with self.sessions() as db:
                start_write(db,self.engine); job=db.get(PlanningJob,job_id)
                if job is None or job.status != 'RUNNING':return
                job.status='CANCELLED' if isinstance(exc,ApiError) and exc.code=='CANCELLED' else 'FAILED'
                job.error_code=exc.code if isinstance(exc,ApiError) else 'PLANNING_FAILED'
                job.error_message=exc.message if isinstance(exc,ApiError) else '规划未完成，已保存输入，请检查配置后重试。'
                job.finished_at=now(); db.commit()
            if not isinstance(exc,ApiError): log.error('planning_failed job_id=%s exception_type=%s',job_id,type(exc).__name__)

        finally:
            heartbeat_stop.set()
            if heartbeat_thread:heartbeat_thread.join(timeout=1)
            if road is not None and callable(getattr(road,'close',None)):road.close()
