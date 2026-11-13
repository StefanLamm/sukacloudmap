import React from 'react';
import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import {PlanPanel} from './plan-panel';
const call=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:call}));
vi.mock('./location-map',()=>({LocationMap:()=>null}));
const day='2026-11-12';
const tasks=Array.from({length:12},(_,i)=>({id:'S'+(i+1),code:'T'+String(i+1).padStart(2,'0'),name:'门店'+(i+1),warehouse_id:'W',pieces:4,common_unit_price:'2'}));
const saved={id:'P',version:1,status:'DRAFT',snapshot_id:'SNAP',snapshot_version:1,input_fingerprint:'fp',input_changed:false,created_at:day+'T08:00:00+08:00',created_by:'QA',
 input:{delivery_date:day,tasks,vehicles:[{id:'V',plate:'测试车辆',warehouse_id:'W',day_warehouse_id:'W',max_pieces:400,available:true,day_available:true}],warehouses:[{id:'W',name:'仓库'}]},
 result:{routes:[],common:tasks.map(t=>({task_id:t.id,pieces:4,fee:'8.00',reason:'共配'})),unassigned:[],total_fee:'96.00',fees_complete:true,confirmable:false,road_source:'FIXTURE',
 check:{valid:true,issues:[],task_count:12,total_pieces:48,used_vehicles:0,dedicated_fee:'0.00',common_fee:'96.00'},algorithm:{mode:'MANUAL',stop_reason:'SEARCH_COMPLETED'}}};
beforeEach(()=>{call.mockReset();call.mockImplementation(async(path:string,method='GET')=>{
 if(path==='/planning/capabilities')return {road_source:'TEST_GRID',live_calls_enabled:false,max_calls:0};
 if(path.includes('/plans?'))return {items:[saved],total:1,head_version:1};
 if(path.endsWith('/jobs'))return method==='POST'?{id:'J',status:'QUEUED',kind:'MANUAL'}:{items:[]};
 if(path==='/planning/plans/P')return saved;
 if(path==='/planning/jobs/J')return {id:'J',status:'QUEUED',kind:'MANUAL'};
 throw Error('Unhandled '+path);
});});
async function edit(){render(<PlanPanel day={day} demand={null} notify={()=>{}}/>);await screen.findByRole('heading',{name:'方案 V1'});fireEvent.click(screen.getByRole('button',{name:'人工调整'}));}
it('自动专车与明确车辆可并存，翻页保留选择且默认10条',async()=>{
 await edit();expect(screen.queryByLabelText('配送安排 T11')).toBeNull();
 fireEvent.change(screen.getByLabelText('配送安排 T01'),{target:{value:'dedicated_auto'}});
 fireEvent.change(screen.getByLabelText('配送安排 T02'),{target:{value:'V'}});
 expect(screen.getByRole('button',{name:'上移 T01'}).hasAttribute('disabled')).toBe(true);
 fireEvent.click(screen.getByRole('navigation',{name:'人工调整分页'}).querySelector('button[aria-label="第 2 页"]')!);
 fireEvent.change(screen.getByLabelText('配送安排 T11'),{target:{value:'dedicated_auto'}});
 fireEvent.click(screen.getByRole('button',{name:'开始核算'}));
 await waitFor(()=>expect(call.mock.calls.some(c=>c[0]==='/planning/jobs'&&c[1]==='POST')).toBe(true));
 const sent=call.mock.calls.find(c=>c[0]==='/planning/jobs'&&c[1]==='POST')![2];
 expect(sent.draft.auto_dedicated_ids).toEqual(['S1','S11']);
 expect(sent.draft.routes).toEqual([{vehicle_id:'V',task_ids:['S2']}]);
 expect(sent.draft.common_ids).not.toContain('S1');expect(sent.optimize_routes).toBe(false);
});
it('优化时限支持24000秒，核算请求带上时限和自动分车选择',async()=>{
 await edit();const limit=screen.getByLabelText('优化时限') as HTMLInputElement;
 expect(limit.max).toBe('24000');fireEvent.change(limit,{target:{value:'24000'}});expect(limit.value).toBe('24000');
 fireEvent.change(screen.getByLabelText('配送安排 T01'),{target:{value:'dedicated_auto'}});
 fireEvent.change(screen.getByLabelText('是否优化线路'),{target:{value:'yes'}});
 fireEvent.click(screen.getByRole('button',{name:'开始核算'}));
 await waitFor(()=>expect(call.mock.calls.some(c=>c[0]==='/planning/jobs'&&c[1]==='POST')).toBe(true));
 const sent=call.mock.calls.find(c=>c[0]==='/planning/jobs'&&c[1]==='POST')![2];
 expect(sent.budget_seconds).toBe(24000);expect(sent.optimize_routes).toBe(true);expect(sent.draft.auto_dedicated_ids).toEqual(['S1']);
});

it('重新打开核算结果仍保留自动专车意图，失败待分车的票也可继续重试',async()=>{
 const old=call.getMockImplementation()!;
 call.mockImplementation(async(path:string,method='GET')=>path==='/planning/plans/P'?{...saved,result:{...saved.result,algorithm:{...saved.result.algorithm,mode:'MANUAL_AUTO_DEDICATED',automatic_dedicated_ids:['S1']}}}:old(path,method));
 await edit();expect((screen.getByLabelText('配送安排 T01') as HTMLSelectElement).value).toBe('dedicated_auto');
});
