import React from 'react';
import {fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import {PlanningPage} from './planning';
const call=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:call,ApiError:class extends Error{status=409;}}));
vi.mock('./plan-panel',()=>({PlanPanel:({day,groupId}:{day:string;groupId:string})=><div data-testid="group-plan">{day}:{groupId}</div>}));
vi.mock('./location-map',()=>({LocationMap:()=>null}));
const day='2026-11-12';
let groups:any[];
const catalog={master_revision:1,warehouses:[{id:'W',code:'W',name:'合肥仓',day_departure_time:'08:00',day_revision:0}],vehicles:[],quotes:[],stores:[{id:'S',code:'S',name:'一店',warehouse_name:'合肥仓',warehouse_id:'W',common_unit_price:'2',dedicated_config_missing:[]}]};
const totals={task_count:0,total_pieces:0,active_warehouse_count:0,warehouses:[]};
const daily={delivery_date:day,revision:0,input_fingerprint:'abc',latest_import_id:null,items:[],totals,latest_snapshot:null,snapshot_count:0};
beforeEach(()=>{groups=[{id:'G1',name:'早班',revision:0,task_count:0,total_pieces:0,snapshot_count:0,plan_count:0},{id:'G2',name:'晚班',revision:0,task_count:0,total_pieces:0,snapshot_count:0,plan_count:0}];call.mockReset();call.mockImplementation(async(path:string,method='GET',body?:any)=>{
 if(path===`/planning/days/${day}/groups`){if(method==='POST'){const g={...groups[0],id:'G3',name:body.name};groups.push(g);return g;}return {items:groups};}
 if(path.startsWith('/master-data/catalog')||path.endsWith('/catalog'))return catalog;
 if(path.startsWith(`/planning/days/${day}?group_id=`))return {...daily,group_id:path.split('=').pop()};
 if(path.includes('/groups/')&&method==='DELETE'){groups=groups.filter(g=>!path.includes(g.id));return {deleted:true};}
 if(path==='/planning/groups/G1/demands'&&method==='PUT')return {};
 if(path==='/planning/groups/G1/warehouses/W'&&method==='PUT')return {};
 throw Error('Unhandled '+method+' '+path);
});});
async function selectGroup(id='G1'){await waitFor(()=>expect(screen.getByLabelText('需求分组').hasAttribute('disabled')).toBe(false));fireEvent.change(screen.getByLabelText('需求分组'),{target:{value:id}});await waitFor(()=>expect(screen.getByTestId('group-plan').textContent).toBe(day+':'+id));await waitFor(()=>expect(call.mock.calls.some(c=>c[0]==='/planning/groups/'+id+'/catalog')).toBe(true));await waitFor(()=>expect(screen.getByLabelText('需求分组').hasAttribute('disabled')).toBe(false));}
it('未选组时不能进入规划，切组后需求和方案都绑定新组',async()=>{
 render(<PlanningPage initialDay={day} notify={()=>{}}/>);expect(screen.queryByTestId('group-plan')).toBeNull();
 await selectGroup();expect(call.mock.calls.some(c=>c[0]===`/planning/days/${day}?group_id=G1`)).toBe(true);
 await selectGroup('G2');expect(call.mock.calls.some(c=>c[0]==='/planning/groups/G2/catalog')).toBe(true);
});
it('同日新建分组后进入新组，删除确认仅发送选中组并退出内容区',async()=>{
 render(<PlanningPage initialDay={day} notify={()=>{}}/>);fireEvent.click(screen.getByRole('button',{name:'新建需求分组'}));
 fireEvent.change(screen.getByLabelText('分组名称'),{target:{value:'加急配送'}});fireEvent.click(screen.getByRole('button',{name:'创建分组'}));
 await waitFor(()=>expect(screen.getByTestId('group-plan').textContent).toContain('G3'));
 await waitFor(()=>expect(screen.getByRole('button',{name:'删除分组'}).hasAttribute('disabled')).toBe(false));fireEvent.click(screen.getByRole('button',{name:'删除分组'}));
 fireEvent.click(screen.getByRole('button',{name:'删除分组及全部数据'}));
 await waitFor(()=>expect(screen.queryByTestId('group-plan')).toBeNull());expect(call).toHaveBeenCalledWith('/planning/groups/G3?expected_revision=0','DELETE');
 expect(screen.getByRole('option',{name:/晚班/})).toBeTruthy();
});
it('人工新建需求向当前组提交门店与件数',async()=>{
 const notify=vi.fn();render(<PlanningPage initialDay={day} notify={notify}/>);await selectGroup();
 fireEvent.click(screen.getByRole('button',{name:/^新建需求$/}));
 fireEvent.change(screen.getByLabelText('门店'),{target:{value:'S'}});fireEvent.change(screen.getByLabelText('配送件数'),{target:{value:'25'}});
 fireEvent.click(screen.getByRole('button',{name:'保存需求'}));
 await waitFor(()=>expect(call).toHaveBeenCalledWith('/planning/groups/G1/demands','PUT',{expected_revision:0,items:[{store_id:'S',pieces:25}]}));
});
it('分组仓库安排使用组内端点，不写全日安排',async()=>{
 render(<PlanningPage initialDay={day} notify={()=>{}}/>);await selectGroup();fireEvent.click(screen.getByRole('button',{name:'仓库与车辆安排'}));
 fireEvent.click(screen.getByRole('button',{name:'设置发车'}));fireEvent.change(screen.getByLabelText('当天最早发车时间'),{target:{value:'09:30'}});
 fireEvent.click(screen.getByRole('button',{name:'保存当日安排'}));await waitFor(()=>expect(call).toHaveBeenCalledWith('/planning/groups/G1/warehouses/W','PUT',{expected_revision:0,departure_time:'09:30'}));
});

it('规划门店全选所有匹配跨页，切仓后保留其他仓已选门店',async()=>{
 const outside={...catalog.stores[0],id:'H',code:'H',name:'另一仓门店',warehouse_id:'W2',warehouse_name:'淮安仓'};
 const matching=Array.from({length:22},(_,i)=>({...catalog.stores[0],id:'S'+i,code:'S'+i,name:'筛选门店'+i}));
 const result={...catalog,warehouses:[...catalog.warehouses,{...catalog.warehouses[0],id:'W2',code:'W2',name:'淮安仓'}],stores:[outside,...matching]};
 const original=call.getMockImplementation()!;
 call.mockImplementation(async(path:string,method='GET',body?:any)=>{
  if(path.startsWith('/master-data/catalog')||path.endsWith('/catalog'))return result;
  if(path.startsWith(`/planning/days/${day}?group_id=`))return {...daily,group_id:'G1',items:result.stores.map(s=>({id:'D'+s.id,store_id:s.id,store_code:s.code,store_name:s.name,warehouse_id:s.warehouse_id,warehouse_name:s.warehouse_name,pieces:1,source_rows:[],configuration_warnings:[],latest_arrival_at:null,day_revision:0}))};
  if(path==='/master-data/compatibility')return {compatible:false,reasons:['不同仓'],groups:[],notice:'固定测试'};
  return original(path,method,body);
 });
 render(<PlanningPage initialDay={day} notify={()=>{}}/>);await selectGroup();fireEvent.click(screen.getByRole('button',{name:'门店与报价校验'}));
 await screen.findByRole('checkbox',{name:'选择规划门店 H'});fireEvent.click(screen.getByRole('checkbox',{name:'选择规划门店 H'}));
 fireEvent.change(screen.getByLabelText('出货仓库'),{target:{value:'W'}});
 fireEvent.click(screen.getByRole('button',{name:'全选所有匹配规划门店（22条）'}));
 expect(screen.getByText('已选择 23 家门店（含其他页）')).toBeTruthy();
 const nav=screen.getByRole('navigation',{name:'规划门店分页'});expect((within(nav).getByLabelText('每页显示条数') as HTMLSelectElement).value).toBe('10');
 fireEvent.click(within(nav).getByRole('button',{name:'第 3 页'}));expect((screen.getByRole('checkbox',{name:'选择规划门店 S21'}) as HTMLInputElement).checked).toBe(true);
 fireEvent.click(screen.getByRole('button',{name:'判断报价兼容'}));
 await waitFor(()=>expect(call).toHaveBeenCalledWith('/master-data/compatibility','POST',{store_ids:['H',...matching.map(s=>s.id)],delivery_date:day}));
});
