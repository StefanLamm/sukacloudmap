import React from 'react';
import {fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import type {Account,SharedRecord} from './api';
const mocks=vi.hoisted(()=>({api:vi.fn()}));
vi.mock('./api',()=>({api:mocks.api,ApiError:class extends Error{status=409;},setCsrf:vi.fn()}));
vi.mock('./master-data',()=>({MasterDataPage:()=>null}));
vi.mock('./planning',()=>({PlanningPage:()=>null}));
import {Accounts,Workspace} from './main';
const record:SharedRecord={id:'R1',title:'核对门店配送',content:'请检查明日门店',task_date:'2026-11-12',status:'PENDING',completed_at:null,completed_by:null,revision:1,created_by:'业务员',updated_by:'业务员',created_at:'2026-11-12T08:00:00+08:00',updated_at:'2026-11-12T08:00:00+08:00'};
const admin:Account={id:'ADMIN',username:'admin',display_name:'管理员',is_admin:true,active:true,revision:1,created_at:record.created_at,updated_at:record.updated_at,last_login_at:null};
const member:Account={...admin,id:'MEMBER',username:'member',display_name:'业务成员',is_admin:false};
let done=false;let deleted=false;
beforeEach(()=>{done=false;deleted=false;mocks.api.mockReset();mocks.api.mockImplementation(async(path:string,method='GET',body?:any)=>{
 if(path.startsWith('/workspace/records?')){const params=new URLSearchParams(path.split('?')[1]);const show=params.get('status')==='completed'?done:!done;return {items:show?[{...record,status:done?'COMPLETED':'PENDING',completed_at:done?record.updated_at:null,completed_by:done?'管理员':null}]:[],total:show?1:0,total_all:1,latest_updated_at:record.updated_at,counts:{pending:done?0:1,completed:done?1:0,overdue:0}};}
 if(path==='/workspace/records/R1/completion'){done=body.completed;return {...record,status:done?'COMPLETED':'PENDING'};}
 if(path==='/workspace/records'&&method==='POST')return {...record,...body};
 if(path==='/accounts')return {items:deleted?[admin]:[admin,member]};
 if(path==='/accounts/MEMBER?expected_revision=1'&&method==='DELETE'){deleted=true;return;}
 throw new Error('Unexpected API '+path);
 });});
it('新建共享任务必填任务日期，并把选定日期提交到服务端',async()=>{
 render(<Workspace notify={()=>{}} isAdmin/>);await screen.findByRole('button',{name:'编辑任务 '+record.title});
 fireEvent.change(screen.getByLabelText('筛选任务日期'),{target:{value:'2026-12-31'}});
 fireEvent.click(screen.getByRole('button',{name:'新建记录'}));
 const date=within(screen.getByRole('dialog')).getByLabelText('任务日期') as HTMLInputElement;expect(date.required).toBe(true);expect(date.value).toBe('2026-12-31');
 fireEvent.change(screen.getByLabelText('标题'),{target:{value:'年末配送'}});fireEvent.change(screen.getByLabelText('内容'),{target:{value:'安排配送'}});
 fireEvent.click(screen.getByRole('button',{name:'保存记录'}));
 await waitFor(()=>expect(mocks.api).toHaveBeenCalledWith('/workspace/records','POST',{title:'年末配送',content:'安排配送',task_date:'2026-12-31'}));
});
it('管理员可完成任务归档，也可以恢复待办，日期筛选会传入接口',async()=>{
 render(<Workspace notify={()=>{}} isAdmin/>);await screen.findByRole('button',{name:'编辑任务 '+record.title});
 fireEvent.click(screen.getByRole('button',{name:'标记已完成'}));
 await waitFor(()=>expect(mocks.api).toHaveBeenCalledWith('/workspace/records/R1/completion','POST',{expected_revision:1,completed:true}));
 fireEvent.click(screen.getByRole('button',{name:/^已完成任务/}));await screen.findByRole('button',{name:'恢复待办'});
 expect(mocks.api.mock.calls.some(c=>String(c[0]).includes('status=completed'))).toBe(true);
 fireEvent.click(screen.getByRole('button',{name:'恢复待办'}));await waitFor(()=>expect(done).toBe(false));
 fireEvent.click(screen.getByRole('button',{name:/^已超时/}));await waitFor(()=>expect(mocks.api.mock.calls.some(c=>String(c[0]).includes('status=overdue'))).toBe(true));
});
it('业务人员只能编辑记录，完成状态操作不显示',async()=>{
 render(<Workspace notify={()=>{}}/>);await screen.findByRole('button',{name:'编辑任务 '+record.title});
 expect(screen.queryByRole('button',{name:'标记已完成'})).toBeNull();
});
it('删除账号需在应用弹窗确认，不显示密码表单，并保护当前账号',async()=>{
 render(<Accounts current={admin} notify={()=>{}} onOwnInvalidated={()=>{}}/>);
 const memberName=await screen.findByText('业务成员');const row=memberName.closest('tr')!;
 const self=screen.getByText('admin').closest('tr')!;expect(within(self).getByRole('button',{name:'删除'}).hasAttribute('disabled')).toBe(true);
 fireEvent.click(within(row).getByRole('button',{name:'删除'}));await screen.findByRole('heading',{name:'删除账号'});
 expect(screen.queryByLabelText('初始密码')).toBeNull();expect(screen.getByText(/历史团队资料及操作记录会保留/)).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'删除账号'}));
 await waitFor(()=>expect(mocks.api).toHaveBeenCalledWith('/accounts/MEMBER?expected_revision=1','DELETE'));
 await waitFor(()=>expect(screen.queryByText('业务成员')).toBeNull());
});

it('账号默认10条，可跨页切换并在搜索时回到首页',async()=>{
 const members=Array.from({length:24},(_,i)=>({...member,id:'M'+i,username:'user'+String(i+1).padStart(2,'0'),display_name:'成员'+(i+1)}));
 mocks.api.mockResolvedValue({items:[admin,...members]});
 render(<Accounts current={admin} notify={()=>{}} onOwnInvalidated={()=>{}}/>);
 await screen.findByText('成员1');const nav=screen.getByRole('navigation',{name:'账号分页'});
 expect((within(nav).getByLabelText('每页显示条数') as HTMLSelectElement).value).toBe('10');
 expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(11);expect(screen.queryByText('成员10')).toBeNull();
 fireEvent.click(within(nav).getByRole('button',{name:'第 3 页'}));expect(screen.getByText('成员24')).toBeTruthy();
 fireEvent.change(screen.getByLabelText('搜索账号或姓名'),{target:{value:'user24'}});
 expect(screen.getByText('成员24')).toBeTruthy();expect(within(nav).getByText('共 1 条 · 第 1 / 1 页')).toBeTruthy();
 fireEvent.change(screen.getByLabelText('搜索账号或姓名'),{target:{value:''}});
 fireEvent.change(within(nav).getByLabelText('每页显示条数'),{target:{value:'20'}});
 expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(21);
});

it('操作记录按10条请求，可翻页及切换显示条数',async()=>{
 const {Activity}=await import('./main');
 const events=Array.from({length:25},(_,i)=>({id:'E'+i,summary:'操作'+(i+1),actor:'管理员',action:'TEST',occurred_at:record.created_at}));
 mocks.api.mockImplementation(async(path:string)=>{const query=new URLSearchParams(path.split('?')[1]);const offset=Number(query.get('offset')),limit=Number(query.get('limit'));return {items:events.slice(offset,offset+limit),total:events.length};});
 render(<Activity/>);await screen.findByText('操作1');const nav=screen.getByRole('navigation',{name:'操作记录分页'});
 expect(mocks.api).toHaveBeenCalledWith('/audit-events?offset=0&limit=10');expect(screen.queryByText('操作11')).toBeNull();
 fireEvent.click(within(nav).getByRole('button',{name:'第 2 页'}));await screen.findByText('操作11');expect(mocks.api).toHaveBeenCalledWith('/audit-events?offset=10&limit=10');
 fireEvent.change(within(nav).getByLabelText('每页显示条数'),{target:{value:'50'}});await screen.findByText('操作25');expect(mocks.api).toHaveBeenCalledWith('/audit-events?offset=0&limit=50');
});
