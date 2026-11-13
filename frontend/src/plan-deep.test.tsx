import React from 'react';
import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import {PlanPanel} from './plan-panel';
import type {DayData} from './demand-panel';

const mock=vi.hoisted(()=>({api:vi.fn()}));
vi.mock('./api',()=>({api:mock.api}));
vi.mock('./location-map',()=>({LocationMap:()=>null}));
const day='2026-11-13';
const demand={input_fingerprint:'F',latest_snapshot:{id:'S',version:1,input_changed:false}} as DayData;
const cap={road_source:'LIVE',route_mode:'DRIVING',live_calls_enabled:true,truck_key_configured:true,max_calls:null,qps:3,automatic_workflow:'DEEP_PORTFOLIO',default_budget_seconds:300,automatic_road_verification:true};
const result={routes:[],common:[{task_id:'A',pieces:10,fee:'70.00',reason:'共配'}],unassigned:[],total_fee:'70.00',fees_complete:true,confirmable:false,road_source:'COMMON_ONLY',check:{valid:true,task_count:1,total_pieces:10,used_vehicles:0,dedicated_fee:'0.00',common_fee:'70.00',issues:[]},algorithm:{mode:'DAILY_DEEP_PORTFOLIO',stop_reason:'DEEP_COMPLETED',optimality_proven:false,final_validation:{mode:'COMMON_ONLY_VALIDATED',fresh:false,selected_segments:0}}};
const plan={id:'P',snapshot_id:'S',snapshot_version:1,version:1,status:'DRAFT',input_changed:false,input_fingerprint:'F',created_at:day+'T08:00:00+08:00',created_by:'测试',total_fee:'70.00',valid:true,road_source:'COMMON_ONLY',input:{delivery_date:day,warehouses:[],vehicles:[],tasks:[{id:'A',code:'A',name:'甲店',pieces:10,warehouse_id:'W',common_unit_price:'7'}]},result};
beforeEach(()=>{
 mock.api.mockReset();mock.api.mockImplementation(async(path:string)=>{
  if(path==='/planning/capabilities')return cap;
  if(path.includes('/plans?'))return {items:[],total:0,head_version:0};
  if(path.startsWith('/planning/days/')&&path.includes('/jobs'))return {items:[]};
  throw Error('Unexpected '+path);
 });
});

it('新分组默认300秒自动深度规划，不再提供跳过自动道路复核的选项',async()=>{
 const initial=mock.api.getMockImplementation()!;
 mock.api.mockImplementation(async(path:string,method='GET',body?:unknown)=>path==='/planning/jobs'&&method==='POST'?{id:'J',kind:'OPTIMIZE',status:'COMPLETED',cancel_requested:false,plan_id:null}:initial(path,method,body));
 render(<PlanPanel day={day} groupId="G" demand={demand} notify={()=>{}}/>);
 await screen.findByText(/自动调用高德复核选中道路并重新报价/);
 expect((screen.getByLabelText('优化时限') as HTMLInputElement).value).toBe('300');
 expect(screen.queryByLabelText('规划道路参考方式')).toBeNull();
 fireEvent.click(screen.getByRole('button',{name:'开始规划'}));
 await waitFor(()=>expect(mock.api.mock.calls.some(c=>c[0]==='/planning/jobs'&&c[1]==='POST')).toBe(true));
 const payload=mock.api.mock.calls.find(c=>c[0]==='/planning/jobs'&&c[1]==='POST')![2];
 expect(payload).toMatchObject({snapshot_id:'S',group_id:'G',kind:'OPTIMIZE',budget_seconds:300,call_limit:null});
 expect(payload).not.toHaveProperty('refresh_roads');expect(payload).not.toHaveProperty('draft');
});

it('显示服务端实际阶段、轮次、起点和参考费用，并允许取消',async()=>{
 const initial=mock.api.getMockImplementation()!;let cancelled=false;
 const running=()=>({id:'J',kind:'OPTIMIZE',status:'RUNNING',budget_seconds:900,cancel_requested:cancelled,plan_id:null,error_message:null,
  progress:{stage:'MULTI_START',message:'正在比较不同专车与共配组合',round_index:2,round_total:6,seed:17,best_fee:'62.50',elapsed_seconds:125.9,candidate_count:217}});
 mock.api.mockImplementation(async(path:string,method='GET',body?:unknown)=>{
  if(path.startsWith('/planning/days/')&&path.includes('/jobs'))return {items:[running()]};
  if(path==='/planning/jobs/J/cancel'){cancelled=true;return {cancel_requested:true};}
  if(path==='/planning/jobs/J')return running();
  return initial(path,method,body);
 });
 render(<PlanPanel day={day} groupId="G" demand={demand} notify={()=>{}}/>);
 expect(await screen.findByText('深度规划 · 多轮搜索')).toBeTruthy();
 expect(screen.getByText('正在比较不同专车与共配组合')).toBeTruthy();
 expect(screen.getByText('第 2 / 6 轮 · 搜索起点 17 · 已用 2分5秒 · 已比较 217 条候选线路')).toBeTruthy();
 expect(screen.getByText('当前参考最低费用 ¥62.50')).toBeTruthy();
 expect(screen.getByText('道路复核后确认最终报价')).toBeTruthy();
 expect(screen.getByText('多轮搜索').getAttribute('aria-current')).toBe('step');
 fireEvent.click(screen.getByRole('button',{name:'取消任务'}));
 expect(await screen.findByText('正在停止，检查可保留的已验证结果')).toBeTruthy();
 expect((screen.getByRole('button',{name:'正在停止'}) as HTMLButtonElement).disabled).toBe(true);
 expect(screen.getByText('通过完整校验的结果才会保留为草稿；未完成核验的候选不会保存。')).toBeTruthy();
 expect(mock.api.mock.calls.some(c=>c[0]==='/planning/jobs/J/cancel'&&c[1]==='POST')).toBe(true);
});

it.each([
 ['FRESH_DRIVING_VALIDATED',true,'已按本轮高德道路复核并重新报价'],
 ['OFFLINE_REFERENCE_VALIDATED',false,'已用已有道路参考复核，未实时更新道路'],
 ['FIXTURE_VALIDATED',false,'固定道路已复核，仅供隔离测试'],
 ['COMMON_ONLY_VALIDATED',false,'全共配方案已复核，无专车道路调用'],
])('深度结果按真实%s状态描述最终道路证明',async(mode,fresh,label)=>{
 const initial=mock.api.getMockImplementation()!;
 const shown={...plan,result:{...result,algorithm:{...result.algorithm,final_validation:{mode,fresh,selected_segments:0}}}};
 mock.api.mockImplementation(async(path:string,method='GET',body?:unknown)=>{
  if(path.includes('/plans?'))return {items:[shown],total:1,head_version:1};
  if(path==='/planning/plans/P')return shown;
  return initial(path,method,body);
 });
 render(<PlanPanel day={day} demand={demand} notify={()=>{}}/>);
 expect(await screen.findByText(label)).toBeTruthy();
 expect(screen.getByText(/日常深度规划 · 多轮搜索与联合选优/)).toBeTruthy();
 expect(screen.getByText(/未证明全局最低/)).toBeTruthy();
 if(!fresh)expect(screen.queryByText('已按本轮高德道路复核并重新报价')).toBeNull();
});

it('人工核算任务保留原说明，不显示自动多轮进度',async()=>{
 const initial=mock.api.getMockImplementation()!;
 mock.api.mockImplementation(async(path:string,method='GET',body?:unknown)=>path.startsWith('/planning/days/')&&path.includes('/jobs')?{items:[{id:'J',kind:'MANUAL',status:'RUNNING',budget_seconds:45,cancel_requested:false,plan_id:null,error_message:null}]}:path==='/planning/jobs/J'?{id:'J',kind:'MANUAL',status:'RUNNING',budget_seconds:45,cancel_requested:false,plan_id:null,error_message:null}:initial(path,method,body));
 render(<PlanPanel day={day} demand={demand} notify={()=>{}}/>);
 expect(await screen.findByText('正在核算人工方案的里程、时序和费用')).toBeTruthy();
 expect(screen.queryByRole('list',{name:'深度规划阶段'})).toBeNull();
 expect(screen.getByText(/优化计算上限 45 秒，道路排队另计/)).toBeTruthy();
});

it('外部算路关闭时明确只使用已有参考，不宣传实时道路复核',async()=>{
 const initial=mock.api.getMockImplementation()!;
 mock.api.mockImplementation(async(path:string,method='GET',body?:unknown)=>path==='/planning/capabilities'?{...cap,live_calls_enabled:false}:initial(path,method,body));
 render(<PlanPanel day={day} demand={demand} notify={()=>{}}/>);
 expect(await screen.findByText('当前入口未开放外部算路，将使用已有实路参考复核；不会标记为实时道路结果。')).toBeTruthy();
 expect(screen.queryByText(/自动调用高德复核选中道路并重新报价/)).toBeNull();
});
