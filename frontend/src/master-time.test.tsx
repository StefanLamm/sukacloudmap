import React from 'react';
import {act,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import {MasterDataPage,Catalog,Row} from './master-data';

const request=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:request,ApiError:class extends Error{}}));
vi.mock('./location-map',()=>({LocationMap:()=>null}));

let data:Catalog;
beforeEach(()=>{
 const store=(id:string):Row=>({id,code:id,name:id+'门店',revision:2,warehouse_id:'W1',warehouse_name:'测试仓',address:'测试地址',business_open:'08:00',business_close:'18:00',close_day_offset:0,unload_minutes:20,crs:'GCJ02',location_source:'手工录入',location_status:'MANUAL'});
 data={master_revision:1,warehouses:[{id:'W1',revision:1,code:'W1',name:'测试仓'}],stores:[store('S1'),store('S2')],vehicles:[],quotes:[]};
 request.mockReset();request.mockImplementation(async(path:string,method='GET',body?:Record<string,any>)=>{
  if(path==='/master-data/management')return structuredClone(data);
  if(path==='/master-data/regions')return {items:[]};
  if(path==='/master-data/stores/bulk-update'&&method==='POST'){
   for(const item of body!.items){const row=data.stores.find(row=>row.id===item.id)!;Object.assign(row,body!.changes,{revision:row.revision+1});}
   return {updated:body!.items.length};
  }
  if(path==='/stores/S1'&&method==='PATCH'){Object.assign(data.stores[0],body,{revision:3});return structuredClone(data.stores[0]);}
  if(path==='/stores'&&method==='POST'){const row={...body,id:'NEW',revision:1} as Row;data.stores.push(row);return structuredClone(row);}
  throw Error('Unhandled '+path);
 });
});

// Native time widgets may update the DOM first, suppressing React's change
// tracker. Exercise input independently so onChange cannot mask stale state.
function inputOnly(input:HTMLInputElement,value:string){act(()=>{input.value=value;fireEvent.input(input);});}
async function openBulk(){
 render(<React.StrictMode><MasterDataPage kind="stores" notify={()=>{}}/></React.StrictMode>);
 fireEvent.click(await screen.findByRole('checkbox',{name:'选择本页全部门店'}));
 fireEvent.click(screen.getByRole('button',{name:'批量修改'}));
 return screen.getByRole('dialog');
}
async function editStore(id='S1'){
 const row=(await screen.findByRole('checkbox',{name:'选择'+id})).closest('tr')!;
 fireEvent.click(within(row).getByRole('button',{name:'编辑'}));
 return screen.getByRole('dialog');
}

it.each([{key:'business_open',label:'营业开始',values:['07:00','07:15','','07:45']},{key:'business_close',label:'营业结束',values:['19:00','19:15','','19:45']}])('批量 $label 连续原生输入和清空重填不白屏，保存后重新编辑可回显',async({key,label,values})=>{
 const dialog=await openBulk();
 fireEvent.click(within(dialog).getByRole('checkbox',{name:'修改'+label}));
 const input=within(dialog).getByLabelText('批量'+label) as HTMLInputElement;
 for(const value of values){
  inputOnly(input,value);
  expect(screen.getByRole('dialog')).toBe(dialog);
  expect((within(dialog).getByLabelText('批量'+label) as HTMLInputElement).value).toBe(value);
  if(!value){
   expect(input.validity.valueMissing).toBe(true);
   fireEvent.click(within(dialog).getByRole('button',{name:'保存批量修改'}));
   expect(request.mock.calls.some(c=>c[1]==='POST')).toBe(false);
  }
 }
 fireEvent.click(within(dialog).getByRole('button',{name:'保存批量修改'}));
 await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());
 expect(request).toHaveBeenCalledWith('/master-data/stores/bulk-update','POST',{items:[{id:'S1',expected_revision:2},{id:'S2',expected_revision:2}],changes:{[key]:values.at(-1)}});
 const other=key==='business_open'?'business_close':'business_open';
 expect(data.stores.map(row=>row[other])).toEqual(key==='business_open'?['18:00','18:00']:['08:00','08:00']);
 const reopened=await editStore('S2');
 expect((within(reopened).getByLabelText(new RegExp('^'+label)) as HTMLInputElement).value).toBe(values.at(-1));
});

it('批量同时修改开闭店和跨日，input/change双事件保留最后值和勾选范围',async()=>{
 const dialog=await openBulk();
 for(const label of ['营业开始','营业结束','营业跨日','卸货时长（分钟）'])fireEvent.click(within(dialog).getByRole('checkbox',{name:'修改'+label}));
 const open=within(dialog).getByLabelText('批量营业开始') as HTMLInputElement;
 const close=within(dialog).getByLabelText('批量营业结束') as HTMLInputElement;
 act(()=>{
  fireEvent.input(open,{target:{value:'20:00'}});fireEvent.change(open,{target:{value:'20:30'}});
  fireEvent.input(close,{target:{value:'02:00'}});fireEvent.change(close,{target:{value:'02:15'}});
 });
 fireEvent.change(within(dialog).getByLabelText('批量营业跨日'),{target:{value:'1'}});
 fireEvent.change(within(dialog).getByLabelText('批量卸货时长（分钟）'),{target:{value:'35'}});
 // Toggling a field off must preserve its original server value.
 fireEvent.click(within(dialog).getByRole('checkbox',{name:'修改卸货时长（分钟）'}));
 expect(within(dialog).queryByLabelText('批量卸货时长（分钟）')).toBeNull();
 expect(open.value).toBe('20:30');expect(close.value).toBe('02:15');
 fireEvent.click(within(dialog).getByRole('button',{name:'保存批量修改'}));
 await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());
 expect(request).toHaveBeenCalledWith('/master-data/stores/bulk-update','POST',{items:[{id:'S1',expected_revision:2},{id:'S2',expected_revision:2}],changes:{business_open:'20:30',business_close:'02:15',close_day_offset:1}});
 expect(data.stores.every(row=>row.unload_minutes===20)).toBe(true);
 expect(screen.getByRole('table').textContent).toContain('20:30–次日 02:15');
});

it.each(['edit','new'])('单店%s修改两个营业时间，清空重填并保存后回显',async mode=>{
 render(<React.StrictMode><MasterDataPage kind="stores" notify={()=>{}}/></React.StrictMode>);
 await screen.findByRole('checkbox',{name:'选择S1'});
 let dialog:HTMLElement;
 if(mode==='edit')dialog=await editStore();
 else{
  fireEvent.click(screen.getByRole('button',{name:'新增门店'}));dialog=screen.getByRole('dialog');
  for(const [label,value] of [[/稳定编号/,'S3'],[/门店名称/,'新增门店'],[/门店地址/,'新地址'] ] as const)fireEvent.change(within(dialog).getByLabelText(label),{target:{value}});
  fireEvent.change(within(dialog).getByLabelText(/出货仓库/),{target:{value:'W1'}});
 }
 const open=within(dialog).getByLabelText(/^营业开始/) as HTMLInputElement;
 const close=within(dialog).getByLabelText(/^营业结束/) as HTMLInputElement;
 for(const [input,values] of [[open,['07:00','','07:45']],[close,['19:00','','19:45']]] as const){
  for(const value of values){
   inputOnly(input,value);expect(input.value).toBe(value);expect(screen.getByRole('dialog')).toBe(dialog);
   if(!value){fireEvent.click(within(dialog).getByRole('button',{name:'保存资料'}));expect(request.mock.calls.some(c=>['POST','PATCH'].includes(c[1]))).toBe(false);}
  }
 }
 fireEvent.click(within(dialog).getByRole('button',{name:'保存资料'}));
 await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());
 const saved=request.mock.calls.find(c=>c[1]===(mode==='edit'?'PATCH':'POST'))!;
 expect(saved[2]).toMatchObject({business_open:'07:45',business_close:'19:45',close_day_offset:0});
 if(mode==='edit')expect(saved[2].expected_revision).toBe(2);
 const reopened=await editStore(mode==='edit'?'S1':'S3');
 expect((within(reopened).getByLabelText(/^营业开始/) as HTMLInputElement).value).toBe('07:45');
 expect((within(reopened).getByLabelText(/^营业结束/) as HTMLInputElement).value).toBe('19:45');
});
