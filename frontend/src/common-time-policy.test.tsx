import React from 'react';
import {fireEvent,render,screen} from '@testing-library/react';
import {expect,it,vi} from 'vitest';
import {PlanPanel} from './plan-panel';
const call=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:call}));
vi.mock('./location-map',()=>({LocationMap:()=>null}));

async function showCommon(reasons:string[]){
 const fixture={id:'MIXED_56',input:{delivery_date:'2026-11-12',tasks:reasons.map((_,index)=>({id:'S'+index,code:'S'+index,name:'已闭店门店'+index,pieces:2,warehouse_id:'W',common_unit_price:'1',latest_arrival_at:'1900-01-01T00:00:00+08:00'})),vehicles:[],warehouses:[]},
  result:{routes:[],common:reasons.map((reason,index)=>({task_id:'S'+index,pieces:2,fee:'2.00',reason})),unassigned:[],total_fee:(reasons.length*2).toFixed(2),fees_complete:true,confirmable:false,road_source:'FIXTURE',check:{valid:true,task_count:reasons.length,total_pieces:reasons.length*2,used_vehicles:0,dedicated_fee:'0.00',common_fee:(reasons.length*2).toFixed(2),issues:[]},algorithm:{mode:'MANUAL',stop_reason:'MANUAL_RECALCULATED'}},notice:'固定测试'};
 call.mockImplementation(async(path:string)=>{
  if(path==='/planning/capabilities')return {road_source:'TEST_GRID',live_calls_enabled:false,truck_key_configured:false,max_calls:0};
  if(path.includes('/plans?'))return {items:[],total:0,head_version:0};
  if(path.endsWith('/jobs'))return {items:[]};
  if(path==='/planning/examples/MIXED_56')return fixture;
  throw Error('unexpected '+path);
 });
 render(<PlanPanel day="2026-11-12" demand={null} notify={()=>{}}/>);
 fireEvent.click(screen.getByRole('button',{name:'混合配送 · 预期 ¥56'}));
 fireEvent.click(await screen.findByRole('button',{name:`共配 ${reasons.length} 票`}));
 return fixture;
}

it('共配明示不校验到店时间，不把历史收货期限显示为共配时序',async()=>{
 await showCommon(['选择共配']);
 expect(screen.getByText('无需校验')).toBeTruthy();expect(screen.getByText(/不安排或校验到店时间、营业时间、卸货时间及最晚到店限制/)).toBeTruthy();
 expect(screen.queryByText(/1900-01-01/)).toBeNull();
});

it('历史到店失败原因标为专车候选说明，新结果不重复添加说明且不修改历史数据',async()=>{
 const legacy='实际到店晚于最晚到店时间；开始卸货时已经超过经营时间';
 const current='选择共配（不校验到店时间）；专车候选说明：实际到店晚于最晚到店时间';
 const fixture=await showCommon([legacy,current]);
 expect(screen.getByText('历史专车候选说明（共配不校验到店）：'+legacy)).toBeTruthy();
 expect(screen.getByText(current)).toBeTruthy();
 expect(screen.queryByText('历史专车候选说明（共配不校验到店）：'+current)).toBeNull();
 expect(fixture.result.common.map(row=>row.reason)).toEqual([legacy,current]);
});
