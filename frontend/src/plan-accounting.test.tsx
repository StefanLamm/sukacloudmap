import React from 'react';
import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import {PlanPanel} from './plan-panel';
import type {DayData} from './demand-panel';
const mock=vi.hoisted(()=>({api:vi.fn()}));
vi.mock('./api',()=>({api:mock.api}));
vi.mock('./location-map',()=>({LocationMap:()=> <div>地图</div>}));
const day='2026-11-12';
const demand={input_fingerprint:'F',latest_snapshot:{id:'S',version:1,input_changed:false}} as DayData;
const result={routes:[{vehicle_id:'V',plate:'测试车',task_ids:['A','B'],pieces:5,meters:2000,fee:'100.00',road_verified:false,stops:[],segments:[],errors:[]}],common:[],unassigned:[],total_fee:'100.00',fees_complete:true,confirmable:false,road_source:'FIXTURE',check:{valid:true,task_count:2,total_pieces:5,used_vehicles:1,dedicated_fee:'100.00',common_fee:'0.00',issues:[]},algorithm:{mode:'MANUAL',stop_reason:'MANUAL_RECALCULATED'},comparison:{previous_total:'120.00',current_total:'100.00',delta:'-20.00',comparable:true,previous_dedicated:'120.00',current_dedicated:'100.00',previous_common:'0.00',current_common:'0.00'}};
const plan={id:'P',snapshot_id:'S',snapshot_version:1,version:1,status:'DRAFT',input_changed:false,input_fingerprint:'F',created_at:day+'T08:00:00+08:00',created_by:'测试',total_fee:'100.00',valid:true,road_source:'FIXTURE',input:{delivery_date:day,warehouses:[],vehicles:[{id:'V',plate:'测试车',max_pieces:400,warehouse_id:'W',available:true}],tasks:[{id:'A',code:'A',name:'甲店',pieces:2,warehouse_id:'W',common_unit_price:'1'},{id:'B',code:'B',name:'乙店',pieces:3,warehouse_id:'W',common_unit_price:'1'}]},result};
beforeEach(()=>{mock.api.mockReset();mock.api.mockImplementation(async(path:string,method='GET')=>{
 if(path.includes('/plans?'))return {items:[plan],total:1,head_version:1};
 if(path.endsWith('/jobs')||path.includes('/jobs?'))return {items:[]};
 if(path==='/planning/plans/P')return plan;
 if(path==='/planning/capabilities')return {road_source:'TEST_GRID',live_calls_enabled:false,truck_key_configured:false,max_calls:0};
 if(path==='/planning/jobs'&&method==='POST')return {id:'J',kind:'MANUAL',status:'COMPLETED',plan_id:null};
 throw Error('Unexpected '+path);
});});
async function editor(groupId?:string){render(<PlanPanel day={day} groupId={groupId} demand={demand} notify={()=>{}}/>);await screen.findByRole('heading',{name:'方案 V1'});fireEvent.click(screen.getByRole('button',{name:'人工调整'}));}
it('不优化时核算请求严格保留人工顺序，并显示前后报价',async()=>{
 await editor();expect((screen.getByLabelText('是否优化线路') as HTMLSelectElement).value).toBe('no');
 fireEvent.click(screen.getByRole('button',{name:'上移 B'}));fireEvent.click(screen.getByRole('button',{name:'开始核算'}));
 await waitFor(()=>expect(mock.api.mock.calls.some(c=>c[0]==='/planning/jobs'&&c[1]==='POST')).toBe(true));
 const payload=mock.api.mock.calls.find(c=>c[0]==='/planning/jobs'&&c[1]==='POST')?.[2];
 expect(payload.optimize_routes).toBe(false);expect(payload.draft.routes[0]).toEqual({vehicle_id:'V',task_ids:['B','A']});
 expect(screen.getByText('核算前 ¥120.00 → 最新报价 ¥100.00')).toBeTruthy();expect(screen.getByText('减少 ¥20.00')).toBeTruthy();
 expect(screen.queryByText('本次费用对比包含仓库归属或其他规划输入变化，请结合快照查看。')).toBeNull();
});
it.each([true,false])('费用对比input_changed=%s时仅在true显示规划输入变化提示',async(inputChanged)=>{
 const original=mock.api.getMockImplementation()!;
 const shown={...plan,result:{...result,comparison:{...result.comparison,input_changed:inputChanged}}};
 mock.api.mockImplementation(async(path:string,method='GET')=>path==='/planning/plans/P'?shown:original(path,method));
 render(<PlanPanel day={day} demand={demand} notify={()=>{}}/>);
 expect(await screen.findByText('核算前 ¥120.00 → 最新报价 ¥100.00')).toBeTruthy();
 expect(Boolean(screen.queryByText('本次费用对比包含仓库归属或其他规划输入变化，请结合快照查看。'))).toBe(inputChanged);
 expect(screen.getByText('减少 ¥20.00')).toBeTruthy();
});
it('选择优化后仍提交人工分车和顺序作为起点，按分组加载及核算',async()=>{
 await editor('G');fireEvent.change(screen.getByLabelText('是否优化线路'),{target:{value:'yes'}});
 fireEvent.click(screen.getByRole('button',{name:'开始核算'}));
 await waitFor(()=>expect(mock.api.mock.calls.some(c=>c[0]==='/planning/jobs'&&c[1]==='POST')).toBe(true));
 const payload=mock.api.mock.calls.find(c=>c[0]==='/planning/jobs'&&c[1]==='POST')?.[2];
 expect(payload.optimize_routes).toBe(true);expect(payload.group_id).toBe('G');expect(payload.draft.routes).toEqual([{vehicle_id:'V',task_ids:['A','B']}]);
 expect(mock.api.mock.calls.some(c=>c[0].includes('/plans?')&&c[0].includes('group_id=G'))).toBe(true);
 expect(mock.api.mock.calls.some(c=>c[0].includes('/jobs?group_id=G'))).toBe(true);
});

it.each([{seconds:2254,minutes:'37.6'},{seconds:5618,minutes:'93.6'},{seconds:0,minutes:'0'},{seconds:6,minutes:'0.1'}])('等待时长$seconds秒最多显示1位小数，不改变原秒数与报价',async({seconds,minutes})=>{
 const original=mock.api.getMockImplementation()!;
 const stop={task_id:'A',arrival_at:day+'T08:00:00+08:00',unload_start_at:day+'T10:00:00+08:00',departure_at:day+'T10:15:00+08:00',wait_seconds:seconds,pieces:2};
 const shown={...plan,result:{...result,routes:[{...result.routes[0],stops:[stop]}]}};
 mock.api.mockImplementation(async(path:string,method='GET')=>path==='/planning/plans/P'?shown:original(path,method));
 render(<PlanPanel day={day} demand={demand} notify={()=>{}}/>);
 expect(await screen.findByText('等待 '+minutes+' 分钟')).toBeTruthy();
 expect(stop.wait_seconds).toBe(seconds);
 expect(screen.getByText('核算前 ¥120.00 → 最新报价 ¥100.00')).toBeTruthy();
});

it.each([{kind:'MANUAL',refresh:true},{kind:'MANUAL',refresh:false}])('真实驾车模式$kind按选择提交refresh=$refresh，保持人工payload',async({kind,refresh})=>{
 const original=mock.api.getMockImplementation()!;
 mock.api.mockImplementation(async(path:string,method='GET')=>path==='/planning/capabilities'?{road_source:'LIVE',route_mode:'DRIVING',live_calls_enabled:true,truck_key_configured:true,max_calls:null,qps:3}:original(path,method));
 render(<PlanPanel day={day} demand={demand} notify={()=>{}}/>);
 await screen.findByRole('heading',{name:'方案 V1'});
 await screen.findByText('自动调用高德复核选中道路并重新报价；按每秒限流排队，等待时间另计，支持取消。');
 if(kind==='MANUAL'){
  fireEvent.click(screen.getByRole('button',{name:'人工调整'}));
  const select=screen.getByLabelText('道路核算方式') as HTMLSelectElement;
  expect(select.value).toBe('refresh');
  fireEvent.change(select,{target:{value:refresh?'refresh':'cached'}});
  fireEvent.click(screen.getByRole('button',{name:'开始核算'}));
 }
 await waitFor(()=>expect(mock.api.mock.calls.some(c=>c[0]==='/planning/jobs'&&c[1]==='POST')).toBe(true));
 const payload=mock.api.mock.calls.find(c=>c[0]==='/planning/jobs'&&c[1]==='POST')![2];
 expect(payload.kind).toBe(kind);expect(payload.refresh_roads).toBe(refresh);expect(payload.call_limit).toBeNull();
 if(kind==='MANUAL')expect(payload.draft.routes).toEqual([{vehicle_id:'V',task_ids:['A','B']}]);
 else expect(payload).not.toHaveProperty('draft');
});

const unsupported=[{route_mode:'TRUCK',road_source:'LIVE',live:true},{route_mode:'TRUCK',road_source:'LIVE',live:false},
 {route_mode:'DRIVING',road_source:'TEST_GRID',live:false},{route_mode:'DRIVING',road_source:'TEST_GRID',live:true}];
it.each(unsupported.flatMap(cap=>['OPTIMIZE','MANUAL'].map(kind=>({...cap,kind}))))('$road_source/$route_mode live=$live时$kind维持通常核算，不提供或提交强制刷新',async({route_mode,road_source,live,kind})=>{
 const original=mock.api.getMockImplementation()!;
 mock.api.mockImplementation(async(path:string,method='GET')=>path==='/planning/capabilities'?{road_source,route_mode,live_calls_enabled:live,truck_key_configured:true,max_calls:null,qps:3}:original(path,method));
 render(<PlanPanel day={day} demand={demand} notify={()=>{}}/>);
 await screen.findByRole('heading',{name:'方案 V1'});
 await waitFor(()=>expect(screen.getByLabelText('高德算路调用上限').textContent).toBe(live?'无限制':'外部算路关闭'));
 expect(screen.queryByLabelText('规划道路参考方式')).toBeNull();
 expect(screen.queryByRole('option',{name:'核算时刷新所选道路'})).toBeNull();
 if(kind==='MANUAL'){
  fireEvent.click(screen.getByRole('button',{name:'人工调整'}));
  const select=screen.getByLabelText('道路核算方式') as HTMLSelectElement;
  expect(select.disabled).toBe(true);expect(select.value).toBe('cached');
  expect(screen.queryByRole('option',{name:'刷新所选道路并重新核算'})).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'开始核算'}));
 }else fireEvent.click(screen.getByRole('button',{name:'开始规划'}));
 await waitFor(()=>expect(mock.api.mock.calls.some(c=>c[0]==='/planning/jobs'&&c[1]==='POST')).toBe(true));
 const payload=mock.api.mock.calls.find(c=>c[0]==='/planning/jobs'&&c[1]==='POST')![2];
 expect(payload.kind).toBe(kind);expect(payload).not.toHaveProperty('refresh_roads');
 if(kind==='MANUAL')expect(payload.draft.routes).toEqual([{vehicle_id:'V',task_ids:['A','B']}]);
});
