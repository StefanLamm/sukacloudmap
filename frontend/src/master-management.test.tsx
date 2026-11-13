import React from 'react';
import {act,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {MasterDataPage,Catalog,MasterKind,Row} from './master-data';

const call=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:call,ApiError:class extends Error{constructor(public status:number,message:string,public code:string,public requestId?:string,public details?:unknown){super(message);}}}));
vi.mock('./location-map',()=>({LocationMap:({onChange}:{onChange?:(value:unknown)=>void})=> <div>地图预览{onChange&&<button type="button" onClick={()=>onChange({name:'高德测试POI名称',code:'POI_CODE',warehouse_id:'POI_WAREHOUSE',address:'合肥市测试路100号',longitude:'117.227185',latitude:'31.820824',crs:'GCJ02',location_source:'高德地图地点搜索'})}>选用测试POI</button>}</div>}));

let data:Catalog;
const name={warehouses:'仓库',stores:'门店',vehicles:'车辆',quotes:'报价方案'};
function row(id:string,kind:MasterKind='warehouses',state:'MANUAL'|'PENDING'|'LINKED'='MANUAL'):Row{
  return {id,revision:2,code:id,name:id+'名称',plate:kind==='vehicles'?id:undefined,warehouse_name:'出货仓',city:'合肥市',address:'合肥市地址',
    longitude:state==='LINKED'?'117.2':null,latitude:state==='LINKED'?'31.8':null,crs:'WGS84',location_status:state,location_note:state==='MANUAL'?'未找到精确地址，需手动关联':'正在根据地址自动关联',
    store_count:0,business_open:'08:00',business_close:'18:00',unload_minutes:5,available:true,max_pieces:400,
    latest_version:kind==='quotes'?{id:'QV',quote_id:id,version:1,name:id,mode:'KM',per_km:'5.8',base_price:null,included_stops:null,extra_stop_price:null,effective_from:'2026-11-12',signature:'sig',created_at:'2026-11-12'}:undefined};
}
beforeEach(()=>{
  data={warehouses:[row('W1'),row('W2')],stores:[row('S1','stores'),row('S2','stores')],vehicles:[row('沪A12345','vehicles'),row('沪A12346','vehicles')],quotes:[row('Q1','quotes'),row('Q2','quotes')],master_revision:1};
  call.mockReset();call.mockImplementation(async(path:string,method='GET',body?:{items:{id:string}[]})=>{
    if(path==='/master-data/management')return structuredClone(data);
    if(method==='DELETE'){
      const [,kind,id]=path.split(/[/?]/) as [string,MasterKind,string];data[kind]=data[kind].filter(r=>r.id!==id);return {deleted:1};
    }
    if(path.endsWith('/bulk-delete')){
      const kind=path.split('/')[2] as MasterKind;const ids=body!.items.map(r=>r.id);data[kind]=data[kind].filter(r=>!ids.includes(r.id));return {deleted:ids.length};
    }
    if(path.endsWith('/locate'))return {queued:1};
    throw Error('Unhandled '+path);
  });
});
afterEach(()=>vi.useRealTimers());

it.each((['warehouses','stores'] as const).flatMap(kind=>(['edit','new','location','rename'] as const).map(mode=>({kind,mode}))))('$kind的$mode表单选用地图POI不会覆盖业务名称',async({kind,mode})=>{
  const first=data[kind][0];const original=call.getMockImplementation()!;
  call.mockImplementation(async(path:string,method='GET',body?:Record<string,unknown>)=>{
    if(path==='/'+kind+'/'+first.id&&method==='PATCH'){
      Object.assign(first,body);return {...first};
    }
    if(path==='/'+kind&&method==='POST'){
      const created={...body,id:'NEW',revision:1} as Row;data[kind].push(created);return created;
    }
    return original(path,method,body);
  });
  render(<MasterDataPage kind={kind} notify={()=>{}}/>);
  const selector=await screen.findByRole('checkbox',{name:'选择'+first.code});
  const tableRow=selector.closest('tr')!;
  fireEvent.click(mode==='new'?screen.getByRole('button',{name:'新增'+name[kind]}):within(tableRow).getByRole('button',{name:mode==='location'?'选择位置':'编辑'}));
  const dialog=screen.getByRole('dialog');
  const expectedName=mode==='new'?'自定义新增'+name[kind]:mode==='rename'?'人工修改'+name[kind]+'名称':first.name;
  if(mode==='new')fireEvent.change(within(dialog).getByLabelText(/稳定编号/),{target:{value:'CUSTOM01'}});
  if(mode==='new'||mode==='rename')fireEvent.change(within(dialog).getByLabelText(new RegExp(name[kind]+'名称')),{target:{value:expectedName}});
  if(kind==='stores'&&mode!=='location')fireEvent.change(within(dialog).getByLabelText(/出货仓库/),{target:{value:'W1'}});
  fireEvent.click(within(dialog).getByRole('button',{name:'选用测试POI'}));
  if(mode!=='location')expect((within(dialog).getByLabelText(new RegExp(name[kind]+'名称')) as HTMLInputElement).value).toBe(expectedName);
  expect((within(dialog).getByLabelText(kind==='stores'?/门店地址/:/^地址/) as HTMLInputElement).value).toBe('合肥市测试路100号');
  fireEvent.click(within(dialog).getByRole('button',{name:'保存资料'}));
  await waitFor(()=>expect(call.mock.calls.some(c=>c[1]===(mode==='new'?'POST':'PATCH'))).toBe(true));
  const payload=call.mock.calls.find(c=>c[1]===(mode==='new'?'POST':'PATCH'))![2];
  expect(payload).toMatchObject({address:'合肥市测试路100号',longitude:'117.227185',latitude:'31.820824',crs:'GCJ02',location_source:'高德地图地点搜索'});
  if(mode==='location'){
    expect(payload).not.toHaveProperty('name');expect(payload).not.toHaveProperty('code');expect(first.name).toBe(expectedName);
  }else{
    expect(payload.name).toBe(expectedName);expect(payload.code).toBe(mode==='new'?'CUSTOM01':first.code);
  }
  if(kind==='stores'&&mode!=='location')expect(payload.warehouse_id).toBe('W1');
  await waitFor(()=>expect(screen.queryByRole('dialog')).toBe(null));
  expect(within(screen.getByRole('table')).getByText(String(expectedName))).toBeTruthy();
});

it.each(['warehouses','stores','vehicles','quotes'] as MasterKind[])('单条删除%s需要确认，取消不会提交',async(kind)=>{
  const notify=vi.fn();render(<MasterDataPage kind={kind} notify={notify}/>);
  const first=data[kind][0];await screen.findByRole('button',{name:'删除'+(first.code??first.plate)});
  fireEvent.click(screen.getByRole('button',{name:'删除'+(first.code??first.plate)}));
  expect(screen.getByRole('heading',{name:'删除'+name[kind]})).toBeTruthy();
  expect(call.mock.calls.some(c=>c[1]==='DELETE')).toBe(false);
  fireEvent.click(screen.getByRole('button',{name:'取消'}));
  expect(call.mock.calls.some(c=>c[1]==='DELETE')).toBe(false);
  fireEvent.click(screen.getByRole('button',{name:'删除'+(first.code??first.plate)}));
  fireEvent.click(screen.getByRole('button',{name:'确认删除'}));
  await waitFor(()=>expect(notify).toHaveBeenCalledWith(`已删除 1 条${name[kind]}资料`));
  expect(call).toHaveBeenCalledWith(`/${kind}/${first.id}?expected_revision=2`,'DELETE');
  await waitFor(()=>expect(screen.queryByRole('button',{name:'删除'+(first.code??first.plate)})).toBe(null));
});

it('分页勾选保留其他页，批量删除只提交已选择的ID和修订',async()=>{
  data.warehouses=Array.from({length:11},(_,i)=>row('W'+String(i+1).padStart(2,'0')));
  render(<MasterDataPage kind="warehouses" notify={()=>{}}/>);
  await screen.findByRole('checkbox',{name:'选择W01'});
  fireEvent.click(screen.getByRole('checkbox',{name:'选择本页全部仓库'}));
  fireEvent.click(screen.getByRole('button',{name:'下一页'}));
  fireEvent.click(screen.getByRole('checkbox',{name:'选择W11'}));
  expect(screen.getByText('已选择 11 条仓库（含其他页）')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'批量删除'}));
  fireEvent.click(screen.getByRole('button',{name:'确认删除'}));
  await waitFor(()=>expect(data.warehouses.length).toBe(0));
  const sent=call.mock.calls.find(c=>String(c[0]).endsWith('/bulk-delete'));
  expect(sent?.[2].items).toHaveLength(11);
  expect(sent?.[2].items[10]).toEqual({id:'W11',expected_revision:2});
});

it('基础资料切换每页条数和页码时保留跨页选择',async()=>{
  data.stores=Array.from({length:105},(_,i)=>row('S'+String(i+1).padStart(3,'0'),'stores'));
  render(<MasterDataPage kind="stores" notify={()=>{}}/>);
  await screen.findByRole('checkbox',{name:'选择S001'});
  fireEvent.click(screen.getByRole('checkbox',{name:'选择S001'}));
  const nav=screen.getByRole('navigation',{name:'门店分页'});
  fireEvent.change(within(nav).getByLabelText('每页显示条数'),{target:{value:'50'}});
  expect(screen.getByRole('checkbox',{name:'选择S050'})).toBeTruthy();expect(screen.queryByRole('checkbox',{name:'选择S051'})).toBe(null);
  expect((screen.getByRole('checkbox',{name:'选择S001'}) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(within(nav).getByRole('button',{name:'第 3 页'}));
  fireEvent.click(screen.getByRole('checkbox',{name:'选择S105'}));
  expect(screen.getByText('已选择 2 条门店（含其他页）')).toBeTruthy();
  fireEvent.change(within(nav).getByLabelText('每页显示条数'),{target:{value:'100'}});
  expect(screen.getByRole('checkbox',{name:'选择S100'})).toBeTruthy();expect(screen.getByText('已选择 2 条门店（含其他页）')).toBeTruthy();
});

it('关联阻止时保留选择并展示具体原因',async()=>{
  const {ApiError}=await import('./api');const original=call.getMockImplementation()!;
  call.mockImplementation(async(path:string,method='GET',body?:unknown)=>{
    if(path.endsWith('/bulk-delete'))throw new ApiError(409,'有关联资料不能删除','MASTER_IN_USE',undefined,[{id:'W1',name:'W1名称',message:'关联门店2条，请先解除关联后删除'}]);
    return original(path,method,body);
  });
  render(<MasterDataPage kind="warehouses" notify={()=>{}}/>);
  await screen.findByRole('checkbox',{name:'选择W1'});
  fireEvent.click(screen.getByRole('checkbox',{name:'选择本页全部仓库'}));
  fireEvent.click(screen.getByRole('button',{name:'批量删除'}));fireEvent.click(screen.getByRole('button',{name:'确认删除'}));
  await screen.findByText('W1名称：关联门店2条，请先解除关联后删除');
  expect(data.warehouses.length).toBe(2);
  fireEvent.click(screen.getByRole('button',{name:'取消'}));
  expect(screen.getByText('已选择 2 条仓库（含其他页）')).toBeTruthy();
});

it('需手动关联有单独标记和筛选，并可批量重试',async()=>{
  data.warehouses[1]=row('W2','warehouses','LINKED');const notify=vi.fn();
  render(<MasterDataPage kind="warehouses" notify={notify}/>);
  await screen.findByRole('checkbox',{name:'选择W1'});
  fireEvent.change(screen.getByLabelText('筛选关联位置状态'),{target:{value:'MANUAL'}});
  expect(screen.queryByRole('checkbox',{name:'选择W2'})).toBe(null);
  expect(screen.getByRole('table').textContent).toContain('需手动关联');
  fireEvent.click(screen.getByRole('checkbox',{name:'选择W1'}));fireEvent.click(screen.getByRole('button',{name:'重试未关联地址'}));
  await waitFor(()=>expect(notify).toHaveBeenCalledWith('已安排 1 条地址重新自动关联'));
  expect(call).toHaveBeenCalledWith('/master-data/warehouses/locate','POST',{items:[{id:'W1',expected_revision:2}]});
});

it('后台定位完成后自动刷新到已关联状态',async()=>{
  vi.useFakeTimers();data.warehouses=[row('W1','warehouses','PENDING')];
  render(<MasterDataPage kind="warehouses" notify={()=>{}}/>);
  await act(async()=>{});
  expect(within(screen.getByRole('table')).getByText('自动关联中')).toBeTruthy();
  data.warehouses=[row('W1','warehouses','LINKED')];
  await act(async()=>{await vi.advanceTimersByTimeAsync(3000);});
  expect(screen.getByRole('table').textContent).toContain('已关联位置');
  expect(within(screen.getByRole('table')).queryByText('自动关联中')).toBe(null);
});

it('导入提示后台自动关联，提交成功反馈准确排队数量',async()=>{
  const original=call.getMockImplementation()!;const notify=vi.fn();
  call.mockImplementation(async(path:string,method='GET',body?:unknown)=>{
    if(path.includes('/preview?'))return {preview_id:'B',can_commit:true,row_count:2,valid_rows:2,error_count:0,create_count:2,update_count:0,errors:[],rows:[]};
    if(path.endsWith('/B/commit'))return {geocoding:{queued:2}};
    return original(path,method,body);
  });
  render(<MasterDataPage kind="warehouses" notify={notify}/>);
  await screen.findByRole('checkbox',{name:'选择W1'});fireEvent.click(screen.getByRole('button',{name:'批量导入'}));
  const dialog=screen.getByRole('dialog');expect(dialog.textContent).toContain('需手动关联');
  fireEvent.change(within(dialog).getByLabelText('选择 xlsx 文件'),{target:{files:[new File(['test'],'import.xlsx')]}});
  fireEvent.submit(within(dialog).getByLabelText('选择 xlsx 文件').closest('form')!);
  await screen.findByText('校验通过，尚未保存');fireEvent.click(screen.getByRole('button',{name:'确认整批导入'}));
  await waitFor(()=>expect(notify).toHaveBeenCalledWith('资料已保存，2 条地址正在自动关联'));
});


it.each(['warehouses','stores','vehicles','quotes'] as MasterKind[])('%s全选所有跨越全部筛选页且保留筛选外已选资料',async(kind)=>{
 const outside=row('H00',kind);outside.name='筛选外资料';
 const matching=Array.from({length:22},(_,i)=>{const item=row('M'+String(i+1).padStart(2,'0'),kind);item.name='筛选命中'+i;return item;});
 data[kind]=[outside,...matching];
 render(<MasterDataPage kind={kind} notify={()=>{}}/>);
 await screen.findByRole('checkbox',{name:'选择H00'});
 const nav=screen.getByRole('navigation',{name:name[kind]+'分页'});
 expect((within(nav).getByLabelText('每页显示条数') as HTMLSelectElement).value).toBe('10');
 expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(11);
 fireEvent.click(screen.getByRole('checkbox',{name:'选择H00'}));
 fireEvent.change(screen.getByLabelText('搜索基础资料'),{target:{value:'筛选命中'}});
 expect(screen.queryByRole('checkbox',{name:'选择H00'})).toBeNull();
 fireEvent.click(screen.getByRole('button',{name:'全选所有匹配'+name[kind]+'（22条）'}));
 expect(screen.getByText('已选择 23 条'+name[kind]+'（含其他页）')).toBeTruthy();
 fireEvent.click(within(nav).getByRole('button',{name:'第 3 页'}));
 expect((screen.getByRole('checkbox',{name:'选择M22'}) as HTMLInputElement).checked).toBe(true);
 fireEvent.click(screen.getByRole('button',{name:'批量删除'}));fireEvent.click(screen.getByRole('button',{name:'确认删除'}));
 await waitFor(()=>expect(data[kind]).toHaveLength(0));
 const sent=call.mock.calls.find(c=>String(c[0]).endsWith('/bulk-delete'));
 expect(sent?.[2].items).toHaveLength(23);expect(new Set(sent?.[2].items.map((item:{id:string})=>item.id))).toEqual(new Set(['H00',...matching.map(r=>r.id)]));
});
