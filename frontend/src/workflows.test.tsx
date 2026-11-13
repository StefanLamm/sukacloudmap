import React from 'react';
import {act,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import {PlanPanel} from './plan-panel';
import {DemandPanel,DayData} from './demand-panel';
import {canLeaveEditor,DiscardConfirmation} from './unsaved';
const call=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:call,ApiError:class extends Error{status=409;},setCsrf:vi.fn()}));
vi.mock('./location-map',()=>({LocationMap:({label,lines}:{label?:string;lines?:unknown[]})=> <div aria-label={label} data-lines={lines?.length??0}>地图位置</div>}));
const day='2026-11-11';
const totals={task_count:2,total_pieces:10,active_warehouse_count:1,warehouses:[{warehouse_id:'W',warehouse_code:'W',warehouse_name:'仓库',task_count:2,total_pieces:10}]};
const demand:DayData={delivery_date:day,revision:1,input_fingerprint:'fp',latest_import_id:'B',items:[],totals,
 latest_snapshot:{id:'SNAP',version:1,delivery_date:day,demand_revision:1,content_hash:'hash',created_at:day+'T08:00:00+08:00',created_by:'QA',totals,input_changed:false,changed_reasons:[]},snapshot_count:1};
function plan(version=1){const task_ids=version===1?['S1','S2']:['S2'];return {id:'P'+version,version,snapshot_id:'SNAP',snapshot_version:1,input_fingerprint:'fp',input_changed:false,status:'DRAFT',created_at:day+'T08:00:00+08:00',created_by:'QA',total_fee:'10.00',valid:version===1,
 input:{delivery_date:day,warehouses:[{id:'W',name:'仓库'}],vehicles:[{id:'V',plate:'测试车辆',max_pieces:10,warehouse_id:'W',day_warehouse_id:'W',available:true,day_available:true}],
 stores:[{id:'S1',name:'一店',code:'T01',warehouse_id:'W',common_unit_price:null},{id:'S2',name:'二店',code:'T02',warehouse_id:'W',common_unit_price:null}],tasks:[{store_id:'S1',pieces:6},{store_id:'S2',pieces:4}]},
 result:{routes:[{vehicle_id:'V',plate:'测试车辆',warehouse_id:'W',task_ids,pieces:version===1?10:4,meters:1000,fee:'10.00',road_verified:false,segments:[],stops:[],errors:[]}],common:[],unassigned:version===1?[]:[{task_id:'S1',pieces:6,reason:'人工选择'}],total_fee:'10.00',fees_complete:version===1,confirmable:false,road_source:'FIXTURE',
 check:{valid:version===1,issues:version===1?[]:[{code:'UNASSIGNED',message:'仍有未分配任务'}],task_count:2,total_pieces:10,used_vehicles:1,dedicated_fee:'10.00',common_fee:'0.00'},algorithm:{mode:'BOUNDED_HEURISTIC',stop_reason:'SEARCH_COMPLETED'}}};}
let saved=false;
beforeEach(()=>{saved=false;call.mockReset();call.mockImplementation(async(path:string,method='GET')=>{
 if(path==='/planning/capabilities')return {road_source:'TEST_GRID',live_calls_enabled:false,truck_key_configured:false,max_calls:0};
 if(path.includes('/days/')&&path.includes('/plans'))return {items:[plan(saved?2:1)],total:saved?2:1,head_version:saved?2:1};
 if(path.includes('/days/')&&path.endsWith('/jobs'))return {items:[]};
 if(path==='/planning/plans/P1')return plan();
 if(path==='/planning/plans/P2')return plan(2);
 if(path==='/planning/jobs'&&method==='POST')return {id:'J',status:'QUEUED',kind:'MANUAL',plan_id:null};
 if(path==='/planning/jobs/J'){saved=true;return {id:'J',status:'COMPLETED',kind:'MANUAL',plan_id:'P2'};}
 throw new Error('Unhandled test API '+path);
 });});
async function editor(){render(<><PlanPanel day={day} demand={demand} notify={()=>{}}/><DiscardConfirmation/></>);await screen.findByRole('heading',{name:'方案 V1'});fireEvent.click(screen.getByRole('button',{name:'人工调整'}));fireEvent.change(screen.getByLabelText('配送安排 T01'),{target:{value:'unassigned'}});}

describe('人工调整工作流',()=>{
 it('应用内确认可取消，保留方式选择；确认后允许离开',async()=>{await editor();const native=vi.spyOn(window,'confirm').mockImplementation(()=>{throw Error('不得使用原生确认');});let decision!:Promise<boolean>;act(()=>{decision=canLeaveEditor();});await screen.findByRole('heading',{name:'人工调整尚未保存'});fireEvent.click(screen.getByRole('button',{name:'继续编辑'}));expect(await decision).toBe(false);expect((screen.getByLabelText('配送安排 T01') as HTMLSelectElement).value).toBe('unassigned');act(()=>{decision=canLeaveEditor();});fireEvent.click(await screen.findByRole('button',{name:'放弃修改并继续'}));expect(await decision).toBe(true);expect(native).not.toHaveBeenCalled();native.mockRestore();});
 it('自己的后台重算回执直接载入新版本，不弹出丢弃修改确认',async()=>{await editor();fireEvent.click(screen.getByRole('button',{name:'开始核算'}));await screen.findByRole('heading',{name:'方案 V2'},{timeout:3000});expect(screen.queryByRole('heading',{name:'人工调整尚未保存'})).toBe(null);fireEvent.click(screen.getByRole('button',{name:'人工调整'}));expect((screen.getByLabelText('配送安排 T01') as HTMLSelectElement).value).toBe('unassigned');const posted=call.mock.calls.find(c=>c[0]==='/planning/jobs'&&c[1]==='POST');expect(posted?.[2].draft.unassigned_ids).toEqual(['S1']);expect(screen.getByRole('button',{name:'确认此方案'}).hasAttribute('disabled')).toBe(true);});
 it('版本冲突时不丢弃表单和人工选择',async()=>{await editor();const original=call.getMockImplementation()!;call.mockImplementation(async(path:string,method='GET',body?:unknown)=>{if(path==='/planning/jobs'&&method==='POST')throw Error('方案已有新版本');return original(path,method,body);});fireEvent.click(screen.getByRole('button',{name:'开始核算'}));await screen.findByText('方案已有新版本');expect((screen.getByLabelText('配送安排 T01') as HTMLSelectElement).value).toBe('unassigned');expect(screen.getByRole('heading',{name:'方案 V1'})).toBeTruthy();});
 it('当前输入指纹变化立即禁止确认旧输入',async()=>{const p=plan();p.result.confirmable=true;const original=call.getMockImplementation()!;call.mockImplementation(async(path:string,method='GET',body?:unknown)=>path==='/planning/plans/P1'?p:original(path,method,body));render(<><PlanPanel day={day} demand={{...demand,input_fingerprint:'new-fingerprint'}} notify={()=>{}}/><DiscardConfirmation/></>);await screen.findByRole('heading',{name:'方案 V1'});expect(screen.getByRole('button',{name:'确认此方案'}).hasAttribute('disabled')).toBe(true);expect(screen.getByText(/输入已变化。本版本保留/)).toBeTruthy();});
});

it('月表来源选择、日期匹配和整日替换需明确确认',async()=>{
 const uploaded={id:'IMPORT',delivery_date:day,preview_revision:0,columns:[{index:0,label:'配送日期'},{index:1,label:'下货门店'},{index:2,label:'配送货物（件）'}],source_row_count:3,source:{sheet_name:'登记表',header_row:2,ignored_sheets:['日期']},sample_rows:[{row:3,values:[day,'一店',10]}],suggested_mapping:{store_name:1,pieces:2,store_code:null,delivery_date:0},result:null};
 call.mockImplementation(async(path:string,method='GET',body?:unknown)=>{
 if(path.includes('/inspect?'))return {sheets:[{name:'登记表',sample_rows:[{row:1,values:['物流运输登记表']},{row:2,values:['配送日期','下货门店','配送货物（件）']}]},{name:'日期',sample_rows:[]}],suggested:{sheet_name:'登记表',header_row:2}};
 if(path.includes('/upload?'))return uploaded;
 if(path.endsWith('/preview'))return {...uploaded,preview_revision:1,result:{can_commit:true,source_row_count:3,blank_rows:0,error_count:0,errors:[],zero_row_count:0,zero_rows:[],merged_row_count:0,selected_date_row_count:1,other_date_row_count:1,ignored_nonbusiness_row_count:1,before:{...totals,task_count:0,total_pieces:0},after:totals,changes:{added:2,removed:0,updated:0},configuration_warning_count:2,items:[]}};
 if(path.endsWith('/commit'))return {revision:2,totals,snapshot_version:2};
 throw Error('Unhandled '+path);
 });
 const completed=vi.fn();render(<DemandPanel day={day} data={demand} loading={false} onReload={completed} notify={()=>{}}/>);
 fireEvent.click(screen.getByRole('button',{name:'导入当日需求'}));fireEvent.change(screen.getByLabelText('选择每日需求文件'),{target:{files:[new File(['test'],'history.xlsx',{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})]}});
 fireEvent.submit(screen.getByLabelText('选择每日需求文件').closest('form')!);await screen.findByLabelText('需求工作表');expect((screen.getByLabelText('列名所在行') as HTMLInputElement).value).toBe('2');
 fireEvent.submit(screen.getByLabelText('选择每日需求文件').closest('form')!);await screen.findByLabelText('配送日期列（月度文件必选）');expect((screen.getByLabelText('配送日期列（月度文件必选）') as HTMLSelectElement).value).toBe('0');
 fireEvent.click(screen.getByRole('button',{name:'校验并预览'}));await screen.findByText('校验通过，尚未替换当日需求');expect(screen.getByRole('button',{name:'确认替换当日需求'}).hasAttribute('disabled')).toBe(true);
 fireEvent.click(screen.getByRole('checkbox'));fireEvent.click(screen.getByRole('button',{name:'确认替换当日需求'}));await waitFor(()=>expect(completed).toHaveBeenCalled());
 const mapping=call.mock.calls.find(c=>String(c[0]).endsWith('/preview'))?.[2];expect(mapping.delivery_date).toBe(0);
 const commit=call.mock.calls.find(c=>String(c[0]).endsWith('/commit'))?.[2];expect(commit).toEqual({delivery_date:day,preview_revision:1});
});

it('专车可单独展开轨迹并在总览筛选车辆',async()=>{
 const original=call.getMockImplementation()!;const base=plan();
 const p={...base,result:{...base.result,routes:[{...base.result.routes[0],segments:[{source:'LIVE',verified:false,polyline:[[117.2,31.8],[117.21,31.81]],crs:'GCJ02',meters:1000,seconds:60,evidence_hash:'fixture'}]}]}};
 call.mockImplementation(async(path:string,method='GET',body?:unknown)=>path==='/planning/plans/P1'?p:original(path,method,body));
 render(<PlanPanel day={day} demand={demand} notify={()=>{}}/>);await screen.findByRole('heading',{name:'方案 V1'});
 fireEvent.click(screen.getByRole('button',{name:'查看行驶轨迹 测试车辆'}));
 expect(screen.getByLabelText('测试车辆行驶轨迹').getAttribute('data-lines')).toBe('1');
 expect(screen.getByText('已返回 1/2 段道路，缺失路段未绘制。')).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'地图与行驶轨迹'}));fireEvent.change(screen.getByLabelText('查看车辆轨迹'),{target:{value:'V'}});
 expect(screen.getByLabelText('方案配送地图').getAttribute('data-lines')).toBe('1');
});

it('驾车参考方案显示说明和参考费用，保持确认按钮禁用',async()=>{
 const original=call.getMockImplementation()!;const base=plan();
 const p={...base,result:{...base.result,road_source:'DRIVING',confirmable:false}};
 call.mockImplementation(async(path:string,method='GET',body?:unknown)=>path==='/planning/plans/P1'?p:path==='/planning/capabilities'?{route_mode:'DRIVING',road_source:'LIVE',live_calls_enabled:true,truck_key_configured:true,max_calls:null,qps:3}:original(path,method,body));
 render(<PlanPanel day={day} demand={demand} notify={()=>{}}/>);await screen.findByRole('heading',{name:'方案 V1'});
 expect(screen.getByText(/驾车参考方案：未核验货车限高/)).toBeTruthy();expect(screen.getByText('驾车参考费用')).toBeTruthy();
 expect(screen.getByRole('button',{name:'确认此方案'}).hasAttribute('disabled')).toBe(true);
 expect(screen.getByLabelText('高德算路调用上限').textContent).toBe('无限制');expect(screen.getByText('每秒最多 3 次，可取消任务')).toBeTruthy();
});

it('驾车参考坐标换算有明确提示，原始位置仍用于标记来源',async()=>{
 const original=call.getMockImplementation()!;const base=plan();
 const p={...base,result:{...base.result,road_source:'DRIVING',routes:[{...base.result.routes[0],segments:[{source:'DRIVING',verified:false,polyline:[[117,31],[118,32]],crs:'GCJ02',meters:1000,seconds:60,evidence_hash:'conversion-test',request:{coordinate_policy:'EXPLICIT_REFERENCE_CONVERSION_TO_GCJ02'}}]}]}};
 call.mockImplementation(async(path:string,method='GET',body?:unknown)=>path==='/planning/plans/P1'?p:original(path,method,body));
 render(<PlanPanel day={day} demand={demand} notify={()=>{}}/>);await screen.findByRole('heading',{name:'方案 V1'});
 expect(screen.getByText('部分位置采用坐标换算，原资料保留；请核对实际位置。')).toBeTruthy();
 expect(screen.getByRole('button',{name:'确认此方案'}).hasAttribute('disabled')).toBe(true);
});

it('当日最晚到店显示次日闭店时间，保存默认值仍跟随营业结束',async()=>{
 const task={id:'D',store_id:'S1',store_code:'T01',store_name:'夜间门店',warehouse_id:'W',warehouse_name:'仓库',pieces:6,source_rows:[],configuration_warnings:[],day_revision:0,
  latest_arrival_at:'2026-11-12T02:00:00+08:00',latest_arrival_source:'BUSINESS_CLOSE' as const,business_close_at:'2026-11-12T02:00:00+08:00'};
 call.mockResolvedValue({});const reload=vi.fn();
 render(<DemandPanel day={day} data={{...demand,items:[task]}} loading={false} onReload={reload} notify={()=>{}}/>);
 expect(screen.getByRole('table').textContent).toContain('2026-11-12 02:00');expect(screen.getByText('随营业结束')).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'收货限制'}));
 expect((screen.getByLabelText('最晚实际到店时间（北京时间）') as HTMLInputElement).value).toBe('');
 expect(screen.getByText(/营业结束时间：2026-11-12 02:00/)).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'保存收货限制'}));await waitFor(()=>expect(reload).toHaveBeenCalled());
 expect(call).toHaveBeenCalledWith('/planning/days/'+day+'/stores/S1','PUT',{expected_revision:0,latest_arrival_at:null});
});

it('已有特殊收货时间可一键恢复按营业结束时间',async()=>{
 const task={id:'D',store_id:'S1',store_code:'T01',store_name:'一店',warehouse_id:'W',warehouse_name:'仓库',pieces:6,source_rows:[],configuration_warnings:[],day_revision:2,
  latest_arrival_at:day+'T12:30:00+08:00',latest_arrival_source:'DAY' as const,business_close_at:day+'T18:00:00+08:00'};
 call.mockResolvedValue({});const reload=vi.fn();
 render(<DemandPanel day={day} data={{...demand,items:[task]}} loading={false} onReload={reload} notify={()=>{}}/>);
 fireEvent.click(screen.getByRole('button',{name:'收货限制'}));
 expect((screen.getByLabelText('最晚实际到店时间（北京时间）') as HTMLInputElement).value).toBe(day+'T12:30');
 fireEvent.click(screen.getByRole('button',{name:'按营业结束时间'}));fireEvent.click(screen.getByRole('button',{name:'保存收货限制'}));
 await waitFor(()=>expect(reload).toHaveBeenCalled());
 expect(call).toHaveBeenCalledWith('/planning/days/'+day+'/stores/S1','PUT',{expected_revision:2,latest_arrival_at:null});
});
