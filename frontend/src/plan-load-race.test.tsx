import React from 'react';
import {act,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import {PlanPanel} from './plan-panel';
const call=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:call}));
vi.mock('./location-map',()=>({LocationMap:()=>null}));
const day='2026-11-12';
const result={routes:[],common:[{task_id:'E',pieces:4,fee:'8.00',reason:'共配'}],unassigned:[],total_fee:'8.00',fees_complete:true,confirmable:false,road_source:'FIXTURE',check:{valid:true,issues:[],task_count:1,total_pieces:4,used_vehicles:0,dedicated_fee:'0.00',common_fee:'8.00'},algorithm:{mode:'MANUAL',stop_reason:'SEARCH_COMPLETED'}};
const input={delivery_date:day,tasks:[{id:'E',code:'EX',name:'样例门店',warehouse_id:'W',pieces:4,common_unit_price:'2'}],vehicles:[{id:'V',plate:'测试车辆',warehouse_id:'W',max_pieces:400,available:true}],warehouses:[{id:'W',name:'仓库'}]};
const example={id:'MIXED_56',input,result,notice:'固定样例'};
const saved={id:'P',version:5,status:'DRAFT',snapshot_id:'SNAP',snapshot_version:1,input_fingerprint:'fp',input_changed:false,created_at:day+'T08:00:00+08:00',created_by:'QA',input,result};
const history={items:[saved],total:1,head_version:5};
function deferred<T>(){let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};}
beforeEach(()=>{call.mockReset();});
async function chooseExampleAndEdit(){
 fireEvent.click(screen.getByRole('button',{name:'混合配送 · 预期 ¥56'}));
 await screen.findByRole('heading',{name:'固定道路演练',level:2});
 fireEvent.click(screen.getByRole('button',{name:'人工调整'}));
 fireEvent.change(screen.getByLabelText('配送安排 EX'),{target:{value:'dedicated_auto'}});
 expect((screen.getByLabelText('配送安排 EX') as HTMLSelectElement).value).toBe('dedicated_auto');
}
for(const phase of ['history','plan'] as const)it(`迟到的初始${phase==='history'?'版本列表':'方案内容'}不能覆盖样例中的未保存自动专车选择`,async()=>{
 const slowHistory=deferred<typeof history>();const slowPlan=deferred<typeof saved>();
 call.mockImplementation(async(path:string,method='GET')=>{
  if(path==='/planning/capabilities')return {road_source:'TEST_GRID',live_calls_enabled:false,max_calls:0};
  if(path.includes('/plans?'))return phase==='history'?slowHistory.promise:history;
  if(path==='/planning/plans/P')return phase==='plan'?slowPlan.promise:saved;
  if(path.endsWith('/jobs'))return {items:[]};
  if(path==='/planning/examples/MIXED_56'||path.startsWith('/planning/examples/MIXED_56/recalculate'))return example;
  throw Error('Unhandled '+method+' '+path);
 });
 render(<PlanPanel day={day} demand={null} notify={()=>{}}/>);
 if(phase==='plan')await waitFor(()=>expect(call.mock.calls.some(c=>c[0]==='/planning/plans/P')).toBe(true));
 await chooseExampleAndEdit();
 await act(async()=>{if(phase==='history'){slowHistory.resolve(history);await slowHistory.promise;}else{slowPlan.resolve(saved);await slowPlan.promise;}});
 expect(screen.getByRole('heading',{name:'固定道路演练',level:2})).toBeTruthy();
 expect(screen.queryByRole('heading',{name:'方案 V5'})).toBeNull();
 expect((screen.getByLabelText('配送安排 EX') as HTMLSelectElement).value).toBe('dedicated_auto');
 expect(screen.getByText(/人工选择已修改，费用与地图尚未更新/)).toBeTruthy();
 if(phase==='history')expect(call.mock.calls.some(c=>c[0]==='/planning/plans/P')).toBe(false);
 fireEvent.click(screen.getByRole('button',{name:'开始核算'}));
 await waitFor(()=>expect(call.mock.calls.some(c=>String(c[0]).startsWith('/planning/examples/MIXED_56/recalculate')&&c[1]==='POST')).toBe(true));
 const submitted=call.mock.calls.find(c=>String(c[0]).startsWith('/planning/examples/MIXED_56/recalculate')&&c[1]==='POST')!;
 expect(submitted[2].auto_dedicated_ids).toEqual(['E']);
});
