import React, {useEffect, useId, useMemo, useRef, useState} from 'react';
import {ArrowDown, ArrowUp, CheckCircle2, Download, History, Play, RefreshCw, Route, Save, Square, Truck} from 'lucide-react';
import {api} from './api';
import {DayData} from './demand-panel';
import {ErrorMessage, Spinner} from './ui';
import {Pagination,PageSize,pageIndex} from './pagination';
import {LocationMap} from './location-map';
import {buildPlanMap,roadSegments,routeColors} from './plan-map';
import './plan-panel.css';
import {fleetSummary,optimizationLabel,optimizationStop,roadReferenceSummary,beijingReferenceTime,deepPlanningStages,planningStageIndex,planningProgressDetails,progressFee,deepValidationLabel,elapsedTimeText} from './plan-metrics';
import type {PlanningProgress,FinalRoadValidation} from './plan-metrics';
import {confirmDiscard} from './unsaved';

type Task={id:string;code:string;name:string;pieces:number;warehouse_id:string;common_unit_price:string|null;longitude?:string;latitude?:string;crs?:string};
type Vehicle={id:string;plate?:string;max_pieces:number;warehouse_id:string;day_warehouse_id?:string;available:boolean;day_available?:boolean};
type Stop={task_id:string;arrival_at:string;unload_start_at:string;departure_at:string;wait_seconds:number;pieces:number};
type Segment={received_at?:number;time_status?:string;source:string;verified:boolean;crs:string;polyline:number[][];meters:number;seconds:number;evidence_hash:string;request?:{coordinate_policy?:string}};
type RouteRow={vehicle_id:string;plate?:string;task_ids:string[];pieces:number;meters:number;fee:string|null;regional_surcharge?:string;road_verified:boolean;stops:Stop[];segments:Segment[];errors:{message:string}[]};
type Result={routes:RouteRow[];common:{task_id:string;pieces:number;fee:string|null;reason:string}[];unassigned:{task_id:string;pieces:number;reason:string}[];total_fee:string;fees_complete:boolean;comparison?:{delta:string|null;previous_total:string;current_total?:string;comparable:boolean;input_changed?:boolean;previous_dedicated?:string;current_dedicated?:string;previous_common?:string;current_common?:string};confirmable:boolean;road_source:string;check:{valid:boolean;task_count:number;total_pieces:number;used_vehicles:number;dedicated_fee:string;common_fee:string;issues:{code:string;message:string;task_id?:string}[]};algorithm:{automatic_dedicated_ids?:string[];mode:string;stop_reason:string;distinct_route_count?:number;elapsed_seconds?:number;total_wait_elapsed_seconds?:number;all_common_baseline?:string|null;optimality_proven?:boolean;final_validation?:FinalRoadValidation;road_stats?:{calls:number;cache_hits:number}}};
type Input={tasks:Record<string,unknown>[];stores?:Record<string,unknown>[];vehicles:Vehicle[];warehouses:Record<string,unknown>[];delivery_date:string};
type PlanMeta={id:string;version:number;status:string;total_fee:string;input_changed:boolean;created_at:string;created_by:string;road_source:string;valid:boolean;snapshot_id:string;snapshot_version:number;content_hash:string;note:string;input_fingerprint:string;confirmable:boolean;blocking_issue_count?:number};
type Plan=PlanMeta&{input:Input;result:Result};
type Example={id:string;input:Input;result:Result;notice:string};
type Job={id:string;status:string;kind:string;budget_seconds?:number;wall_budget_seconds?:number;cancel_requested:boolean;plan_id:string|null;error_message:string|null;created_at?:string;server_now?:string;progress?:PlanningProgress|null};
type WallWaitPolicy={extra_compute_multiplier:number;extra_min_seconds:number;extra_max_seconds:number;includes_queue:boolean;includes_road_wait:boolean};
type Capabilities={live_calls_enabled:boolean;truck_key_configured:boolean;max_calls:number|null;qps?:number;road_source:string;route_mode?:string;wall_wait_policy?:WallWaitPolicy};
type Choice={mode:string;order:number};
type Draft={routes:{vehicle_id:string;task_ids:string[]}[];common_ids:string[];unassigned_ids:string[];auto_dedicated_ids:string[]};
const colors=routeColors;
const text=(e:unknown)=>e instanceof Error?e.message:'操作失败，请重试。';
const shortTime=(s:string)=>s?.replace('T',' ').replace('+08:00','')??'—';
function defaultWallSeconds(compute:number,policy?:WallWaitPolicy):number|null{if(!policy||![policy.extra_compute_multiplier,policy.extra_min_seconds,policy.extra_max_seconds].every(Number.isFinite))return null;return compute+Math.min(policy.extra_max_seconds,Math.max(policy.extra_min_seconds,compute*policy.extra_compute_multiplier));}
function waitLimitText(compute:number,wall:number|null){return `优化计算上限 ${compute} 秒；${wall===null?'总等待上限以服务端任务为准':`总等待上限 ${wall} 秒（含排队、道路及复核）`}`;}
function taskList(input:Input):Task[]{const stores=new Map((input.stores??[]).map(s=>[s.id,s]));return input.tasks.map(t=>{const s=stores.get(t.store_id)??{};return {...s,...t,id:String(t.store_id??t.id),code:String(s.code??t.code??t.id),name:String(s.name??t.name??t.id)} as Task;});}
function initialChoices(result:Result):Record<string,Choice>{const out:Record<string,Choice>={};for(const r of result.routes)r.task_ids.forEach((id,i)=>out[id]={mode:r.vehicle_id,order:i+1});for(const r of result.common)out[r.task_id]={mode:'common',order:1};for(const r of result.unassigned)out[r.task_id]={mode:'unassigned',order:1};for(const id of result.algorithm.automatic_dedicated_ids??[])if(out[id])out[id]={...out[id],mode:'dedicated_auto'};return out;}
function makeDraft(tasks:Task[],choices:Record<string,Choice>):Draft{const routes:Record<string,{id:string;order:number}[]>={};const out:Draft={routes:[],common_ids:[],unassigned_ids:[],auto_dedicated_ids:[]};for(const t of tasks){const c=choices[t.id]??{mode:'unassigned',order:1};if(c.mode==='common')out.common_ids.push(t.id);else if(c.mode==='unassigned')out.unassigned_ids.push(t.id);else if(c.mode==='dedicated_auto')out.auto_dedicated_ids.push(t.id);else (routes[c.mode]??=[]).push({id:t.id,order:c.order});}out.routes=Object.entries(routes).map(([vehicle_id,rows])=>({vehicle_id,task_ids:rows.sort((a,b)=>a.order-b.order||a.id.localeCompare(b.id)).map(r=>r.id)}));return out;}

function jobElapsedMs(createdAt?: string, serverNow?: string, stageElapsed?: number) {
  const created = Date.parse(createdAt ?? '');
  const server = Date.parse(serverNow ?? '');
  if (Number.isFinite(created) && Number.isFinite(server)) return Math.max(0, server - created);
  const fromStage = Number.isFinite(stageElapsed) ? (stageElapsed ?? 0) * 1000 : 0;
  const fromCreation = Number.isFinite(created) ? Date.now() - created : 0;
  return Math.max(0, fromCreation, fromStage);
}

function JobElapsed({job}: {job: Job}) {
  const {created_at: createdAt, server_now: serverNow} = job;
  const stageElapsed = job.progress?.elapsed_seconds;
  const [clock, setClock] = useState(() => {
    const now = performance.now();
    return {now, started: now - jobElapsedMs(createdAt, serverNow, stageElapsed)};
  });

  useEffect(() => {
    const now = performance.now();
    const started = now - jobElapsedMs(createdAt, serverNow, stageElapsed);
    // Responses can repeat the same server time or arrive late. Never rewind.
    setClock(previous => ({now, started: Math.min(previous.started, started)}));
  }, [createdAt, serverNow, stageElapsed]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const now = performance.now();
      setClock(previous => ({...previous, now}));
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  return <small role="timer" aria-live="off">已用 {elapsedTimeText((clock.now - clock.started) / 1000)}</small>;
}

function JobProgress({job,budget,policy}:{job:Job;budget:number;policy?:WallWaitPolicy}){
  const progress=job.progress??{};const phase=job.status==='QUEUED'?-1:planningStageIndex(progress.stage);const fee=progressFee(progress.best_fee);
  const manual=job.kind==='MANUAL';const heading=job.cancel_requested?'正在停止，检查可保留的已验证结果':job.status==='QUEUED'?'任务等待执行':manual?'正在核算人工方案的里程、时序和费用':`深度规划 · ${deepPlanningStages[phase]??deepPlanningStages[0]}`;
  const compute=job.budget_seconds??budget;const wall=job.wall_budget_seconds??defaultWallSeconds(compute,policy);
  const details = planningProgressDetails(progress);
  return <div className="plan-job-content"><b>{heading}</b>{!manual&&<ol className="plan-deep-stages" aria-label="深度规划阶段">{deepPlanningStages.map((label,i)=><li key={label} className={i===phase?'current':i<phase?'done':''} aria-current={i===phase?'step':undefined}>{label}</li>)}</ol>}{!manual&&progress.message&&<small className="plan-job-message">{progress.message}</small>}{!manual&&details&&<small>{details}</small>}{!manual&&<JobElapsed key={job.id} job={job}/>}{!manual&&fee!==null&&<strong className="plan-job-best">当前参考最低费用 ¥{fee}<small>道路复核后确认最终报价</small></strong>}<small>可继续查看业务资料；{waitLimitText(compute,wall)}</small>{job.cancel_requested&&<small>通过完整校验的结果才会保留为草稿；未完成核验的候选不会保存。</small>}</div>;
}

export function PlanPanel({day,groupId,demand,notify}:{day:string;groupId?:string;demand:DayData|null;notify:(s:string)=>void}){
  const [versions,setVersions]=useState<PlanMeta[]>([]);const [versionTotal,setVersionTotal]=useState(0);const [offset,setOffset]=useState(0);const [historySize,setHistorySize]=useState<PageSize>(10);const historyGeneration=useRef(0);
  const [plan,setPlan]=useState<Plan|null>(null);const [example,setExample]=useState<Example|null>(null);const [job,setJob]=useState<Job|null>(null);
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [budget,setBudget]=useState(300);
  const [cap,setCap]=useState<Capabilities|null>(null);const budgetId=useId();
  const liveRoads = cap?.road_source === 'LIVE' && cap.live_calls_enabled && cap.truck_key_configured;
  const [optimizeRoutes,setOptimizeRoutes]=useState(false);
  const [choices,setChoices]=useState<Record<string,Choice>>({});const [editing,setEditing]=useState(false);const [dirty,setDirty]=useState(false);const [note,setNote]=useState('');
  const [mapVehicle,setMapVehicle]=useState('all');const [routeMapId,setRouteMapId]=useState<string|null>(null);
  useEffect(()=>{setMapVehicle('all');setRouteMapId(null);},[plan?.id,example?.id]);
  const [resultQuery,setResultQuery]=useState('');const [resultPage,setResultPage]=useState(0);const [resultSize,setResultSize]=useState<PageSize>(10);const [issuePage,setIssuePage]=useState(0);const [issueSize,setIssueSize]=useState<PageSize>(10);
  const [sub,setSub]=useState<'routes'|'common'|'errors'|'map'>('routes');const [query,setQuery]=useState('');const [page,setPage]=useState(0);const [pageSize,setPageSize]=useState<PageSize>(10);const [head,setHead]=useState(0);
  const selectionGeneration=useRef(0);const dirtyRef=useRef(dirty);dirtyRef.current=dirty;
  const alive=useRef(true);useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  const active=job&&['QUEUED','RUNNING'].includes(job.status);const current=example??plan;const result=current?.result;
  const tasks=useMemo(()=>current?taskList(current.input):[],[current]);const taskById=new Map(tasks.map(t=>[t.id,t]));
  // Background selection retains the token captured before history loading;
  // later user actions invalidate both the list and plan response.
  async function loadVersions(start=offset,open=false,size=historySize){const initialSelection=selectionGeneration.current;const request=++historyGeneration.current;const r=await api<{items:PlanMeta[];total:number;head_version:number}>(`/planning/days/${day}/plans?offset=${start}&limit=${size}${groupId?'&group_id='+encodeURIComponent(groupId):''}`);if(!alive.current||request!==historyGeneration.current)return;setVersions(r.items);setVersionTotal(r.total);setHead(r.head_version);if(open&&r.items[0]&&initialSelection===selectionGeneration.current&&!dirtyRef.current)await openPlan(r.items[0].id,true,initialSelection);}
  async function openPlan(id:string,force=false,initialSelection?:number){if(initialSelection!==undefined&&(initialSelection!==selectionGeneration.current||dirtyRef.current))return;if(!force&&dirtyRef.current&&!await confirmDiscard('当前人工选择尚未重算保存，切换版本会丢弃这些未提交选择。确定切换？'))return;const generation=initialSelection??++selectionGeneration.current;let r:Plan;try{r=await api<Plan>('/planning/plans/'+id);}catch(e){if(initialSelection!==undefined&&(!alive.current||generation!==selectionGeneration.current||dirtyRef.current))return;throw e;}if(!alive.current||generation!==selectionGeneration.current||initialSelection!==undefined&&dirtyRef.current)return;setPlan(r);setExample(null);setChoices(initialChoices(r.result));setEditing(false);setDirty(false);setPage(0);setQuery('');setResultPage(0);}

  useEffect(()=>{
    api<typeof cap>('/planning/capabilities').then(setCap).catch(e=>setError(text(e)));
    loadVersions(0,true).catch(e=>setError(text(e)));
    api<{items:Job[]}>('/planning/days/'+day+'/jobs'+(groupId?'?group_id='+encodeURIComponent(groupId):'')).then(r=>{
      if(alive.current) setJob(current=>current??r.items.find(j=>['QUEUED','RUNNING'].includes(j.status))??null);
    }).catch(e=>setError(text(e)));
  },[]);
  useEffect(()=>{
    if(!active||!job)return;
    const id=job.id;
    let stopped=false;
    let timer:number|undefined;
    let timeout:number|undefined;
    let pending:AbortController|undefined;
    async function poll() {
      const request = new AbortController();
      pending = request;
      timeout = window.setTimeout(()=>request.abort(), 10000);
      try {
        const r=await api<Job>('/planning/jobs/'+id,'GET',undefined,request.signal);
        window.clearTimeout(timeout);
        if(stopped||!alive.current)return;
        const running=['QUEUED','RUNNING'].includes(r.status);
        setJob(current=>running && current?.id===id && current.cancel_requested
          ? {...r,cancel_requested:true} : r);
        if(!running){
          if(r.error_message)setError(r.error_message);
          if(r.plan_id){
            setBusy(true);
            try{
              await openPlan(r.plan_id,true);await loadVersions(0);
              if(alive.current){setOffset(0);notify(r.status==='CANCELLED'?'已取消，并保留停止前的结果草稿':r.kind==='MANUAL'?'核算完成，已保存新方案版本':'新方案版本已保存');}
            }catch(e){if(alive.current)setError(text(e));}
            finally{if(alive.current)setBusy(false);}
          }
          return;
        }
      }catch(e){if(!stopped&&!request.signal.aborted)setError(text(e));}
      finally {window.clearTimeout(timeout);pending=undefined;}
      if(!stopped)timer=window.setTimeout(poll,700);
    }
    timer=window.setTimeout(poll,250);
    return()=>{stopped=true;window.clearTimeout(timer);window.clearTimeout(timeout);pending?.abort();};
  },[active,job?.id]);
  useEffect(()=>{const handler=(e:BeforeUnloadEvent)=>{if(dirtyRef.current){e.preventDefault();e.returnValue='';}};const leave=(e:Event)=>{if(dirtyRef.current)(e as CustomEvent<Promise<boolean>[]>).detail.push(confirmDiscard('离开会丢失尚未重算保存的人工选择。'));};window.addEventListener('beforeunload',handler);window.addEventListener('suka-before-leave',leave);return()=>{window.removeEventListener('beforeunload',handler);window.removeEventListener('suka-before-leave',leave);};},[]);

  async function start(kind:'OPTIMIZE'|'MANUAL'){
    const snapshot=kind==='MANUAL'?plan?.snapshot_id:demand?.latest_snapshot?.id;if(!snapshot)return;
    setBusy(true);setError('');try{const r=await api<Job>('/planning/jobs','POST',{snapshot_id:snapshot,...(groupId?{group_id:groupId}:{}),expected_head:head,kind,optimize_routes:kind==='MANUAL'&&optimizeRoutes,budget_seconds:budget,call_limit:null,seed:0,note,...(kind==='MANUAL'?{draft:makeDraft(tasks,choices),parent_id:plan?.id}:{})});setJob(r);}catch(e){setError(text(e));}finally{setBusy(false);}
  }
  async function showExample(id:string){if(dirtyRef.current&&!await confirmDiscard('当前人工选择尚未重算保存，切换样例会丢弃这些未提交选择。确定切换？'))return;setBusy(true);setError('');try{const generation=++selectionGeneration.current;const r=await api<Example>('/planning/examples/'+id);if(!alive.current||generation!==selectionGeneration.current)return;setExample(r);setChoices(initialChoices(r.result));setEditing(false);setDirty(false);setPage(0);setResultPage(0);setSub('routes');}catch(e){setError(text(e));}finally{setBusy(false);}}
  async function recalcExample(){if(!example)return;setBusy(true);setError('');try{const r=await api<Example>('/planning/examples/'+example.id+'/recalculate'+(optimizeRoutes?'?optimize_routes=true':''),'POST',makeDraft(tasks,choices));setExample(r);setDirty(false);notify(r.result.check.valid?'样例重算通过；固定道路不可用于执行':'样例冲突已保留，请继续调整');}catch(e){setError(text(e));}finally{setBusy(false);}}
  async function versionAction(action:'confirm'|'restore'){if(!plan)return;setBusy(true);setError('');try{const r=await api<Plan>(`/planning/plans/${plan.id}/${action}`,'POST',{expected_head:head,note});setPlan(r);setExample(null);setEditing(false);setDirty(false);setChoices(initialChoices(r.result));await loadVersions(0);setOffset(0);notify(action==='confirm'?'已创建人工确认版本':'历史方案已恢复为新草稿');}catch(e){setError(text(e));}finally{setBusy(false);}}
  function touchEditor(){selectionGeneration.current++;dirtyRef.current=true;setDirty(true);}
  function change(id:string,mode:string){setChoices(old=>({...old,[id]:{mode,order:Math.max(0,...Object.values(old).filter(c=>c.mode===mode).map(c=>c.order))+1}}));touchEditor();}
  function move(id:string,direction:number){const c=choices[id];const group=tasks.filter(t=>choices[t.id]?.mode===c.mode).sort((a,b)=>choices[a.id].order-choices[b.id].order||a.id.localeCompare(b.id));const index=group.findIndex(t=>t.id===id);const next=group[index+direction];if(!next)return;[group[index],group[index+direction]]=[group[index+direction],group[index]];setChoices(old=>{const out={...old};group.forEach((t,i)=>out[t.id]={...out[t.id],order:i+1});return out;});touchEditor();}
  const currentDraft=useMemo(()=>makeDraft(tasks,choices),[tasks,choices]);const taskOrders=useMemo(()=>{const orders=new Map<string,number>();for(const r of currentDraft.routes)r.task_ids.forEach((id,i)=>orders.set(id,i+1));return orders;},[currentDraft]);
  const filtered=tasks.filter(t=>[t.code,t.name].some(v=>v.includes(query)));const editorPage=pageIndex(page,filtered.length,pageSize);const visible=filtered.slice(editorPage*pageSize,editorPage*pageSize+pageSize);
  const vehicles=current?.input.vehicles??[];const proof=result?.check;const fleet=fleetSummary(result?.routes??[],vehicles);const references=roadReferenceSummary(result?.routes??[]);const blockingIssues=proof?.issues.filter(issue=>issue.code!=='DRIVING_REFERENCE')??[];
  const demandSnapshot=demand?.latest_snapshot;
  const sameSnapshot=plan&&demandSnapshot?.id===plan.snapshot_id?demandSnapshot:null;
  const matchingInputProof=demand&&plan?.input_changed===false&&sameSnapshot?.input_changed===false&&
    typeof sameSnapshot.equivalent_input_fingerprint==='string'&&
    sameSnapshot.equivalent_input_fingerprint===demand.input_fingerprint;
  const planStale=Boolean(plan&&(plan.input_changed||sameSnapshot?.input_changed||
    (demand&&plan.input_fingerprint!==demand.input_fingerprint&&!matchingInputProof)));
  const filteredCommon=(result?.common??[]).filter(r=>{const t=taskById.get(r.task_id);return !resultQuery||Boolean(t&&(t.code+t.name).includes(resultQuery));});const commonPages=Math.ceil(filteredCommon.length/resultSize);const commonPage=Math.min(resultPage,Math.max(0,commonPages-1));const unassignedPage=pageIndex(resultPage,result?.unassigned.length??0,resultSize);const conflictPage=pageIndex(issuePage,proof?.issues.length??0,issueSize);
  const mapped=buildPlanMap(current?.input.warehouses??[],taskById,result?.routes??[],(result?.common??[]).map(row=>row.task_id),mapVehicle==='all'?undefined:mapVehicle);
  const inlineMap=routeMapId?buildPlanMap(current?.input.warehouses??[],taskById,result?.routes??[],[],routeMapId):null;
  return <div className="plan-workspace">
    <section className="panel plan-launch"><div><div className="section-kicker">PLAN & REVIEW</div><h2>生成配送方案</h2><p className="muted">自动多轮搜索、汇总线路并联合选优，经道路复核后保存找到的最低费用可行方案。</p></div><div className="plan-launch-actions"><label htmlFor={budgetId}>优化计算时限（秒）<input id={budgetId} aria-describedby={budgetId+'-wait'} aria-label="优化时限" type="number" min={1} max={24000} value={budget} onChange={e=>setBudget(Math.max(1,Math.min(24000,Number(e.target.value)||1)))}/></label><div className="plan-call-policy"><span>高德算路调用次数</span><b aria-label="高德算路调用上限">{cap?.live_calls_enabled&&cap.truck_key_configured?(cap.max_calls==null?'无限制':cap.max_calls+'次'):'外部算路关闭'}</b><small>每秒最多 {cap?.qps??3} 次，可取消任务</small></div><button className="button primary" disabled={busy||!!active||!demand?.latest_snapshot||demand.latest_snapshot.input_changed} onClick={()=>void start('OPTIMIZE')}><Play size={15}/> 开始规划</button></div><div className="plan-reference-policy"><b>自动深度规划</b><span>多轮多起点搜索 → 汇总线路 → 联合选优 → 道路复核与修复 → 保存方案</span><small>{liveRoads?'搜索、核算和复核均使用本轮高德回执。道路等待计入总等待上限。':cap?.road_source==='TEST_GRID'?'当前为隔离测试道路，只保存测试结果，不提供真实道路核验。':'当前入口未开放高德算路，无法核算新专车道路。'}</small></div><p className="plan-launch-note" id={budgetId+'-wait'}>{waitLimitText(budget,defaultWallSeconds(budget,cap?.wall_wait_policy))}。{demand?.latest_snapshot?`输入快照 V${demand.latest_snapshot.version} · ${day}`:'请先导入需求并冻结输入快照。'} · 规划采用全部仓库的冻结输入{cap?.road_source==='TEST_GRID'?' · 正在使用非地理模拟路网，仅供隔离测试；结果不能执行':cap?.route_mode==='DRIVING'?` · 驾车参考规划：未核验货车限行；${cap.live_calls_enabled?'按每秒限流排队生成道路轨迹，总调用次数不限。':'本轮外部调用关闭，可查看已保存轨迹。'}`:!cap?.live_calls_enabled?' · 本轮真实调用关闭，无法核验的专车按共配配置回退或列入未分配。':''}</p></section>
    <ErrorMessage text={error}/>
    {active&&<div className="plan-job" role="status" aria-atomic="true"><Spinner/><JobProgress job={job!} budget={budget} policy={cap?.wall_wait_policy}/><button className="button secondary" disabled={job?.cancel_requested} onClick={()=>{api<Job>('/planning/jobs/'+job!.id+'/cancel','POST').then(r=>setJob(v=>v?{...v,cancel_requested:r.cancel_requested}:r)).catch(e=>setError(text(e)));}}><Square size={13}/>{job?.cancel_requested?'正在停止':'取消任务'}</button></div>}
    <div className="plan-history-layout"><section className="panel plan-main"><div className="panel-toolbar"><div className="panel-title"><Route size={17}/><h2>{example?'固定道路演练':plan?`方案 V${plan.version}`:'方案结果'}</h2>{plan&&!example&&<span className="count">{plan.status==='CONFIRMED'?'已确认':'草稿'}</span>}</div><div className="toolbar-actions">{current&&<button className="button secondary" disabled={busy||!!active} onClick={()=>{selectionGeneration.current++;setEditing(!editing);setPage(0);}}><Truck size={14}/>{editing?'收起人工调整':'人工调整'}</button>}{plan&&!example&&<a className="button secondary" href={'/api/planning/plans/'+plan.id+'/export.xlsx'}><Download size={14}/> 导出 Excel</a>}</div></div>
    {!current&&<div className="table-empty plan-empty"><Route size={32}/><h3>方案将保存在这里</h3><p>导入需求后开始规划，或使用右侧固定样例检查排线与人工调整。</p></div>}
    {current&&result&&proof&&<>
      {!example&&result.road_source==='DRIVING'&&<div className="plan-banner">驾车参考方案：未核验货车限高、限重、限行及计划发车时刻，路线、预计到店和费用需人工核对，不能确认执行。</div>}{!example&&result.road_source==='DRIVING'&&result.routes.some(r=>r.segments.some(s=>s.request?.coordinate_policy==='EXPLICIT_REFERENCE_CONVERSION_TO_GCJ02'))&&<p className="plan-launch-note">部分位置采用坐标换算，原资料保留；请核对实际位置。</p>}{!example&&result.road_source==='FIXTURE'&&<div className="plan-banner fixture">隔离测试：使用非地理模拟道路，里程、费用、时刻仅用于验收，不是实际承运或导航方案。</div>}{example&&<div className="plan-banner fixture">固定道路测试 · {example.id} · 不写入业务数据 · 不可确认执行{plan&&<button className="text-button" onClick={async()=>{if(dirtyRef.current&&!await confirmDiscard('尚未重算的选择会丢失，确定返回业务方案？'))return;setExample(null);setEditing(false);setDirty(false);setChoices(initialChoices(plan.result));}}>返回业务方案</button>}</div>}
      {!example&&planStale&&<div className="plan-banner">输入已变化。本版本保留原始费用、时序与道路证据；请重新冻结输入后规划。</div>}
      <div className="plan-totals"><div><span>{result.road_source==='DRIVING'?'驾车参考费用':result.fees_complete!==false?'方案费用':'已核算金额（有未定费用）'}</span><strong>¥ {result.total_fee}</strong></div><div><span>专车 / 共配费用</span><b>¥ {proof.dedicated_fee} / ¥ {proof.common_fee}</b></div><div><span>任务 / 件数 / 用车</span><b>{proof.task_count} 票 · {proof.total_pieces} 件 · {proof.used_vehicles} 辆</b></div><div><span>专车总里程 / 件数装载率</span><b>{(fleet.meters/1000).toFixed(2)} km · {fleet.loadedPercent===null?'—':fleet.loadedPercent.toFixed(1)+'%'}</b><small>总支出优先，同费用下缩短里程</small></div><div><span>独立复核</span><b className={!blockingIssues.length?'':'plan-danger'}>{!blockingIssues.length?'分配校验通过':`${blockingIssues.length} 项待处理`}</b><small>{result.road_source==='DRIVING'?'驾车参考，货车限行待核验':result.road_source==='FIXTURE'?'固定测试道路':result.road_source==='COMMON_ONLY'?'全共配方案':result.confirmable?'货车道路已核验':'专车道路待核验'}</small></div></div>
      {result.comparison&&<div className="plan-comparison">{result.comparison.comparable?<><b>核算前 ¥{result.comparison.previous_total} → 最新报价 ¥{result.comparison.current_total??result.total_fee}</b><span>{Number(result.comparison.delta)>0?'增加':Number(result.comparison.delta)<0?'减少':'差额'} ¥{Math.abs(Number(result.comparison.delta)).toFixed(2)}</span>{result.comparison.previous_dedicated!==undefined&&<small>专车 ¥{result.comparison.previous_dedicated} → ¥{result.comparison.current_dedicated}；共配 ¥{result.comparison.previous_common} → ¥{result.comparison.current_common}</small>}</>:'存在未核算费用，处理后再比较调整差额。'}{result.comparison.input_changed&&<small>本次费用对比包含仓库归属或其他规划输入变化，请结合快照查看。</small>}</div>}<div className="plan-algorithm">{optimizationLabel(result.algorithm.mode,result.algorithm.optimality_proven)} · {optimizationStop(result.algorithm.stop_reason)}{!result.algorithm.optimality_proven&&' · 未证明全局最低'} {result.algorithm.mode!=='DAILY_DEEP_PORTFOLIO'&&result.algorithm.distinct_route_count!==undefined?`· 已比较 ${result.algorithm.distinct_route_count} 条候选线路`:''} {result.algorithm.total_wait_elapsed_seconds!==undefined?`· 本次任务用时 ${elapsedTimeText(result.algorithm.total_wait_elapsed_seconds)}`:result.algorithm.elapsed_seconds!==undefined?`· 规划阶段用时 ${elapsedTimeText(result.algorithm.elapsed_seconds)}`:''}{result.algorithm.all_common_baseline?` · 全共配基线 ¥${result.algorithm.all_common_baseline}`:''}{result.algorithm.road_stats?` · 外部调用 ${result.algorithm.road_stats.calls} 次 / 缓存命中 ${result.algorithm.road_stats.cache_hits} 次`:''}</div>
      {result.algorithm.mode==='DAILY_DEEP_PORTFOLIO'&&<p className="plan-deep-validation">{deepValidationLabel(result.algorithm.final_validation)}</p>}{references.count>0&&<p className="plan-reference-summary">{references.historical?'历史实路参考':'实路参考'} · 采集时间（北京时间）：{beijingReferenceTime(references.oldest)}{references.newest!==references.oldest?' 至 '+beijingReferenceTime(references.newest):''}{references.missingTime?' · 部分路段未记录时间':''}。{liveRoads?'开始核算会向高德获取本轮道路。':''}</p>}{editing&&<div className="plan-editor"><div className="plan-editor-heading"><div><h3>人工调整配送方式、车辆和顺序</h3><p>选择是否优化线路后点击“开始核算”，重新计算里程、到店时间和最新报价，保存新版本并与核算前报价对比。可直接选择车辆，或选择“专车（自动分车排线）”，由系统按货量、线路与费用分配车辆；共配及指定车辆保持人工选择。</p></div><button className="button primary" disabled={busy||!!active} onClick={()=>void (example?recalcExample():start('MANUAL'))}><Play size={14}/> {busy||active?'正在核算…':'开始核算'}</button></div><div className="plan-manual-policy"><label htmlFor="manual-optimize">是否优化线路</label><select id="manual-optimize" disabled={busy||!!active} value={optimizeRoutes?'yes':'no'} onChange={e=>{setOptimizeRoutes(e.target.value==='yes');touchEditor();}}><option value="no">不优化，严格按人工排序核算</option><option value="yes">优化，搜索约束内最低费用分车与线路</option></select><small>{optimizeRoutes?'指定车辆保持不变；自动专车依据容量与路线分配。小规模枚举，大规模限时搜索，并标明是否已证明最优。':'指定车辆按人工相对顺序核算；选择自动专车的任务仍会自动分车排线。'}</small></div>{dirty&&<p className="plan-dirty">人工选择已修改，费用与地图尚未更新。完成调整后点击“开始核算”。</p>}<input aria-label="搜索调整门店" placeholder="搜索门店编号或名称" value={query} onChange={e=>{setQuery(e.target.value);setPage(0);}}/><div className="table-scroll"><table className="accounts-table"><thead><tr><th>门店 / 件数</th><th>配送方式与车辆</th><th>当前顺序</th><th>调整顺序</th></tr></thead><tbody>{visible.map(t=>{const c=choices[t.id]??{mode:'unassigned',order:1};return <tr key={t.id}><td><b>{t.code} · {t.name}</b><small className="cell-secondary">{t.pieces} 件 · 共配 {t.common_unit_price===null?'未配置':`¥${t.common_unit_price}/件`}</small></td><td><select aria-label={'配送安排 '+t.code} value={c.mode} disabled={busy||!!active} onChange={e=>change(t.id,e.target.value)}><option value="dedicated_auto">专车（自动分车排线）</option><option value="common">共配</option><option value="unassigned">未分配</option>{vehicles.map(v=><option key={v.id} value={v.id}>{v.plate??v.id} · {v.max_pieces}件 · {(v.day_available??v.available)?'可用':'不可用'}</option>)}</select></td><td>{c.mode==='dedicated_auto'?'核算后自动分配':['common','unassigned'].includes(c.mode)?'—':taskOrders.get(t.id)}</td><td><div className="toolbar-actions"><button className="icon-button" aria-label={'上移 '+t.code} disabled={busy||!!active||['common','unassigned','dedicated_auto'].includes(c.mode)} onClick={()=>move(t.id,-1)}><ArrowUp size={15}/></button><button className="icon-button" aria-label={'下移 '+t.code} disabled={busy||!!active||['common','unassigned','dedicated_auto'].includes(c.mode)} onClick={()=>move(t.id,1)}><ArrowDown size={15}/></button></div></td></tr>;})}</tbody></table></div><Pagination label="人工调整" total={filtered.length} page={editorPage} pageSize={pageSize} disabled={busy||!!active} onPageChange={setPage} onPageSizeChange={size=>{setPageSize(size);setPage(0);}}/><label htmlFor="plan-note">版本备注</label><input id="plan-note" maxLength={240} placeholder="填写本次调整原因（选填）" value={note} onChange={e=>setNote(e.target.value)}/></div>}
      <nav className="plan-result-tabs" aria-label="方案详情">{([['routes','专车线路'],['common',`共配 ${result.common.length} 票`],['errors',`异常 ${result.unassigned.length} 票`],['map','地图与行驶轨迹']] as const).map(([id,label])=><button key={id} className={sub===id?'selected':''} onClick={()=>{setSub(id);setResultPage(0);}}>{label}</button>)}</nav>
      {sub==='routes'&&<div className="plan-routes">{result.routes.map((r,i)=><article className="route-card" key={r.vehicle_id+'-'+i}><header><span className="route-color" style={{background:colors[i%colors.length]}}/><h3>{r.plate??r.vehicle_id}</h3><span>{r.pieces}/{vehicles.find(v=>v.id===r.vehicle_id)?.max_pieces??'—'} 件 · {(r.meters/1000).toFixed(3)} km</span><b>¥ {r.fee??'待核验'}</b></header><p>{r.task_ids.map(id=>taskById.get(id)?.name??id).join(' → ')}{Number(r.regional_surcharge)>0&&<small className="cell-secondary">报价已含区域附加费 ¥{r.regional_surcharge}，按当前报价规则收取。</small>}</p><div className="route-track-toolbar"><span>道路轨迹 {roadSegments(r).length}/{r.task_ids.length} 段</span><button className="button secondary" aria-label={'查看行驶轨迹 '+(r.plate??r.vehicle_id)} disabled={!!example} onClick={()=>setRouteMapId(routeMapId===r.vehicle_id?null:r.vehicle_id)}>{routeMapId===r.vehicle_id?'收起轨迹':'查看行驶轨迹'}</button></div>{routeMapId===r.vehicle_id&&inlineMap&&<div className="route-inline-map"><p>仓库出发，按配送顺序展示；箭头表示行驶方向。</p><LocationMap points={inlineMap.points} lines={inlineMap.lines} label={(r.plate??r.vehicle_id)+'行驶轨迹'}/>{inlineMap.lines.length<r.task_ids.length&&<p className="map-service-error">{inlineMap.lines.length?`已返回 ${inlineMap.lines.length}/${r.task_ids.length} 段道路，缺失路段未绘制。`:'该方案尚无真实道路轨迹，请关联仓库和门店坐标后，使用高德算路重新规划或重算。'}</p>}</div>}{r.errors.map((e,n)=><ErrorMessage key={n} text={e.message}/>)}<div className="table-scroll"><table className="accounts-table"><thead><tr><th>顺序 / 门店</th><th>件数</th><th>预计到店</th><th>开始卸货 / 等待</th><th>预计离店</th></tr></thead><tbody>{r.stops.map((s,n)=><tr key={s.task_id+'-'+n}><td>{n+1} · {taskById.get(s.task_id)?.name??s.task_id}</td><td>{s.pieces}</td><td>{shortTime(s.arrival_at)}</td><td>{shortTime(s.unload_start_at)}<small className="cell-secondary">等待 {Number((s.wait_seconds/60).toFixed(1))} 分钟</small></td><td>{shortTime(s.departure_at)}</td></tr>)}</tbody></table></div></article>)}{!result.routes.length&&<p className="table-empty">此版本没有专车线路。请在共配与异常中核对全部任务。</p>}</div>}
      {sub==='common'&&<><p className="field-hint">共配仅核算件数和单价，不安排或校验到店时间、营业时间、卸货时间及最晚到店限制。</p><div className="plan-list-filter"><input aria-label="搜索共配门店" placeholder="搜索共配门店编号或名称" value={resultQuery} onChange={e=>{setResultQuery(e.target.value);setResultPage(0);}}/><span>{filteredCommon.length} 票</span></div><div className="table-scroll"><table className="accounts-table"><thead><tr><th>门店</th><th>件数</th><th>费用</th><th>到店时间</th><th>原因</th></tr></thead><tbody>{filteredCommon.slice(commonPage*resultSize,commonPage*resultSize+resultSize).map(r=><tr key={r.task_id}><td>{taskById.get(r.task_id)?.name??r.task_id}</td><td>{r.pieces}</td><td>¥ {r.fee??'未配置'}</td><td>无需校验</td><td>{/到店|经营|卸货|时间/.test(r.reason)&&!/共配.*不校验/.test(r.reason)?`历史专车候选说明（共配不校验到店）：${r.reason}`:r.reason}</td></tr>)}</tbody></table>{!filteredCommon.length&&<p className="table-empty">没有匹配的共配任务。</p>}</div><Pagination label="共配任务" total={filteredCommon.length} page={commonPage} pageSize={resultSize} onPageChange={setResultPage} onPageSizeChange={size=>{setResultSize(size);setResultPage(0);}}/></>}
      {sub==='errors'&&<div className="plan-errors">{proof.issues.slice(conflictPage*issueSize,conflictPage*issueSize+issueSize).map((e,i)=><ErrorMessage key={i} text={`${e.task_id?taskById.get(e.task_id)?.name??e.task_id:''} ${e.message}`}/>)}{proof.issues.length>0&&<Pagination label="核算冲突" total={proof.issues.length} page={conflictPage} pageSize={issueSize} onPageChange={setIssuePage} onPageSizeChange={size=>{setIssueSize(size);setIssuePage(0);}}/>}{result.unassigned.slice(unassignedPage*resultSize,unassignedPage*resultSize+resultSize).map(r=><article key={r.task_id}><b>{taskById.get(r.task_id)?.name??r.task_id} · {r.pieces} 件</b><p>{r.reason}</p></article>)}{result.unassigned.length>0&&<Pagination label="未分配任务" total={result.unassigned.length} page={unassignedPage} pageSize={resultSize} onPageChange={setResultPage} onPageSizeChange={size=>{setResultSize(size);setResultPage(0);}}/>}{proof.valid&&<p><CheckCircle2 size={17}/> 全部任务已分配，分配校验通过。</p>}</div>}
      {sub==='map'&&<div className="plan-map"><p>按方案配送顺序展示高德道路轨迹，颜色与专车线路一致，箭头表示方向；共配显示门店位置。</p><label htmlFor="map-vehicle">查看车辆轨迹</label><select id="map-vehicle" value={mapVehicle} onChange={e=>setMapVehicle(e.target.value)}><option value="all">全部车辆</option>{result.routes.map(r=><option key={r.vehicle_id} value={r.vehicle_id}>{r.plate??r.vehicle_id} · {roadSegments(r).length}/{r.task_ids.length}段</option>)}</select>{example?<div className="table-empty">此固定样例没有真实坐标与道路折线，仅用于费用和时序回归。</div>:<LocationMap points={mapped.points} lines={mapped.lines} label="方案配送地图"/>}{!mapped.lines.length&&!example&&<p className="muted">当前方案没有返回道路轨迹；请使用高德算路重新规划或重算，缺失路段不会以直线代替。</p>}</div>}
      {!example&&plan&&<div className="plan-confirm"><span>{plan.status==='CONFIRMED'?'此版本已人工确认':!result.confirmable?result.road_source==='DRIVING'?'驾车参考方案需人工核对货车限行，当前不能确认执行':'请先处理异常或完成专车道路核验':planStale?'输入已变化，需要重新规划':'全部任务已复核，可人工确认'}</span><a className="text-button" href={'/api/planning/plans/'+plan.id+'/export.json'}>下载证据 JSON</a><button className="button primary" disabled={busy||!!active||dirty||planStale||!result.confirmable||plan.version!==head||plan.status==='CONFIRMED'} onClick={()=>void versionAction('confirm')}><CheckCircle2 size={15}/> 确认此方案</button></div>}
    </>}
    </section><aside className="plan-history"><section className="panel"><div className="panel-toolbar"><div className="panel-title"><History size={16}/><h2>历史版本</h2></div><button className="icon-button" aria-label="刷新方案历史" onClick={()=>{loadVersions().catch(e=>setError(text(e)));}}><RefreshCw size={14}/></button></div>{versions.map(v=><button className={'plan-version '+(!example&&plan?.id===v.id?'selected':'')} key={v.id} disabled={busy||!!active} onClick={()=>void openPlan(v.id).catch(e=>setError(text(e)))}><span><b>V{v.version} · {v.status==='CONFIRMED'?'已确认':'草稿'}</b><strong>¥{v.total_fee}</strong></span><small>{shortTime(v.created_at)}</small><small>{v.created_by} · {v.valid?'校验通过':v.blocking_issue_count===0?'分配校验通过，驾车参考待核验':'有冲突'}{v.input_changed?' · 输入已变更':''}</small></button>)}{!versions.length&&<p className="plan-side-empty">尚未保存方案</p>}<Pagination label="方案历史" total={versionTotal} page={Math.floor(offset/historySize)} pageSize={historySize} disabled={busy||!!active} onPageChange={n=>{const start=n*historySize;setOffset(start);void loadVersions(start).catch(e=>setError(text(e)));}} onPageSizeChange={size=>{setHistorySize(size);setOffset(0);void loadVersions(0,false,size).catch(e=>setError(text(e)));}}/>{plan&&!example&&plan.version!==head&&<button className="button secondary plan-restore" disabled={busy||!!active} onClick={()=>void versionAction('restore')}><Save size={14}/> 从此版本恢复新草稿</button>}</section><section className="panel plan-examples"><h3>固定道路演练</h3><p>检查联合排线、改车、改方式及顺序，不产生真实接口费用。</p><button className="button secondary" disabled={busy||!!active} onClick={()=>void showExample('MIXED_56')}>混合配送 · 预期 ¥56</button><button className="button secondary" disabled={busy||!!active} onClick={()=>void showExample('SHARED_40')}>车辆共享 · 预期 ¥40</button></section></aside></div>
  </div>;
}
