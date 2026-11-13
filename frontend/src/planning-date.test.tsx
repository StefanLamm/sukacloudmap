import React from 'react';
import {act,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import {PlanningPage} from './planning';
const request=vi.hoisted(()=>vi.fn(async(path:string):Promise<any>=>path.endsWith('/groups')?{items:[]}:path.startsWith('/master-data/')?{warehouses:[],stores:[],vehicles:[],quotes:[],master_revision:1}:{delivery_date:path.split('/').pop(),items:[],revision:0,latest_snapshot:null}));
vi.mock('./api',()=>({api:request}));
vi.mock('./plan-panel',()=>({PlanPanel:({day}:{day:string})=><div data-testid="day-in-plan">{day}</div>}));
vi.mock('./demand-panel',()=>({DemandPanel:()=>null}));
vi.mock('./location-map',()=>({LocationMap:()=>null}));
beforeEach(()=>{request.mockClear();});

it('无未保存修改时同步接受日期，避免原生日期控件回跳',async()=>{
 render(<PlanningPage initialDay="2026-11-12" notify={()=>{}}/>);
 const input=screen.getByLabelText('配送日期') as HTMLInputElement;
 act(()=>{
  fireEvent.change(input,{target:{value:'2026-10-31'}});
  expect(input.value).toBe('2026-10-31');
 });
 await waitFor(()=>expect(request.mock.calls.some(([path])=>path==='/planning/days/2026-10-31/groups')).toBe(true));
 expect(screen.queryByTestId('day-in-plan')).toBeNull();
 expect((screen.getByLabelText('需求分组') as HTMLSelectElement).value).toBe('');
});

it('有未保存修改时取消保留日期，确认后才切换业务输入',async()=>{
 let answer!:(accepted:boolean)=>void;
 const guard=(event:Event)=>(event as CustomEvent<Promise<boolean>[]>).detail.push(new Promise<boolean>(resolve=>{answer=resolve;}));
 window.addEventListener('suka-before-leave',guard);
 try{
  render(<PlanningPage initialDay="2026-11-12" notify={()=>{}}/>);
  const input=screen.getByLabelText('配送日期') as HTMLInputElement;
  fireEvent.change(input,{target:{value:'2026-10-31'}});expect(input.value).toBe('2026-11-12');
  await act(async()=>answer(false));expect(input.value).toBe('2026-11-12');
  fireEvent.change(input,{target:{value:'2026-10-31'}});
  await act(async()=>answer(true));expect(input.value).toBe('2026-10-31');
 }finally{window.removeEventListener('suka-before-leave',guard);}
});

it('原生input事件同步日期状态，读取对应分组并在刷新后保持新日期',async()=>{
 const original=request.getMockImplementation()!;
 request.mockImplementation(async(path:string)=>{
  if(path.endsWith('/groups')){
   const day=path.includes('2026-11-12')?'2026-11-12':'2026-11-13';
   return {items:[{id:day,name:day==='2026-11-12'?'前日需求':'今日需求',delivery_date:day,revision:0,task_count:2,total_pieces:4,snapshot_count:0,plan_count:0}]};
  }
  if(path.endsWith('/catalog'))return {warehouses:[],stores:[],vehicles:[],quotes:[],master_revision:1};
  if(path.includes('?group_id='))return {delivery_date:path.split('/').pop()!.split('?')[0],items:[],revision:0,latest_snapshot:null};
  return original(path);
 });
 try{
  render(<PlanningPage initialDay="2026-11-13" notify={()=>{}}/>);
  await screen.findByRole('option',{name:/今日需求/});
  await waitFor(()=>expect(screen.getByLabelText('需求分组').hasAttribute('disabled')).toBe(false));
  fireEvent.change(screen.getByLabelText('需求分组'),{target:{value:'2026-11-13'}});
  await screen.findByTestId('day-in-plan');
  await waitFor(()=>expect(screen.getByLabelText('需求分组').hasAttribute('disabled')).toBe(false));
  const input=screen.getByLabelText('配送日期') as HTMLInputElement;
  // Some embedded/native accessibility inputs set the DOM property first.
  // React's change-value tracker then suppresses synthetic onChange; the
  // real input event must still update the business date, not only the DOM.
  act(()=>{input.value='2026-11-12';fireEvent.input(input);});
  await screen.findByRole('option',{name:/前日需求/});
  expect(screen.queryByRole('option',{name:/今日需求/})).toBeNull();
  expect(input.value).toBe('2026-11-12');
  expect((screen.getByLabelText('需求分组') as HTMLSelectElement).value).toBe('');
  expect(screen.queryByTestId('day-in-plan')).toBeNull();
  await waitFor(()=>expect(screen.getByRole('button',{name:'刷新业务资料'}).hasAttribute('disabled')).toBe(false));
  request.mockClear();fireEvent.click(screen.getByRole('button',{name:'刷新业务资料'}));
  await waitFor(()=>expect(request.mock.calls.some(([path])=>path==='/planning/days/2026-11-12/groups')).toBe(true));
  expect(input.value).toBe('2026-11-12');
 }finally{request.mockImplementation(original);}
});

it('原生input和change双事件只询问一次，取消恢复原日，确认才清空分组并切日',async()=>{
 let answer!:(accepted:boolean)=>void;let questions=0;
 const guard=(event:Event)=>{questions++;(event as CustomEvent<Promise<boolean>[]>).detail.push(new Promise<boolean>(resolve=>{answer=resolve;}));};
 window.addEventListener('suka-before-leave',guard);
 try{
  render(<PlanningPage initialDay="2026-11-13" notify={()=>{}}/>);
  await waitFor(()=>expect(screen.getByRole('button',{name:'刷新业务资料'}).hasAttribute('disabled')).toBe(false));
  const input=screen.getByLabelText('配送日期') as HTMLInputElement;
  act(()=>{input.value='2026-11-12';fireEvent.input(input);fireEvent.change(input,{target:{value:'2026-11-12'}});});
  expect(questions).toBe(1);expect(input.value).toBe('2026-11-13');
  await act(async()=>answer(false));expect(input.value).toBe('2026-11-13');
  act(()=>{input.value='2026-11-12';fireEvent.input(input);});
  expect(questions).toBe(2);
  await act(async()=>answer(true));expect(input.value).toBe('2026-11-12');
  await waitFor(()=>expect(request.mock.calls.some(([path])=>path==='/planning/days/2026-11-12/groups')).toBe(true));
  expect((screen.getByLabelText('需求分组') as HTMLSelectElement).value).toBe('');
 }finally{window.removeEventListener('suka-before-leave',guard);}
});
