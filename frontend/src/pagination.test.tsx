import React,{useState} from 'react';
import {fireEvent,render,screen,within} from '@testing-library/react';
import {expect,it} from 'vitest';
import {Pagination,PageSize,pageIndex} from './pagination';

function List({total=121}:{total?:number}){
 const [page,setPage]=useState(0),[size,setSize]=useState<PageSize>(10);const current=pageIndex(page,total,size);
 return <><p aria-label="可见条目">{Array.from({length:total},(_,n)=>n+1).slice(current*size,current*size+size).join(',')}</p>
 <Pagination label="测试列表" total={total} page={current} pageSize={size} onPageChange={setPage} onPageSizeChange={n=>{setSize(n);setPage(0);}}/></>;
}
it('分页显示当前页码并支持10、20、50、100条，切换条数回到首页',()=>{
 render(<List/>);const nav=screen.getByRole('navigation',{name:'测试列表分页'});
 expect(within(nav).getByText('共 121 条 · 第 1 / 13 页')).toBeTruthy();
 fireEvent.click(within(nav).getByRole('button',{name:'下一页'}));
 expect(within(nav).getByRole('button',{name:'第 2 页'}).getAttribute('aria-current')).toBe('page');
 expect(screen.getByLabelText('可见条目').textContent?.split(',')[0]).toBe('11');
 fireEvent.change(within(nav).getByLabelText('每页显示条数'),{target:{value:'20'}});
 expect(screen.getByLabelText('可见条目').textContent?.split(',')).toHaveLength(20);
 fireEvent.change(within(nav).getByLabelText('每页显示条数'),{target:{value:'50'}});
 expect(within(nav).getByText('共 121 条 · 第 1 / 3 页')).toBeTruthy();expect(screen.getByLabelText('可见条目').textContent?.split(',')).toHaveLength(50);
 fireEvent.click(within(nav).getByRole('button',{name:'第 3 页'}));expect(screen.getByLabelText('可见条目').textContent?.split(',')[0]).toBe('101');
 fireEvent.change(within(nav).getByLabelText('每页显示条数'),{target:{value:'100'}});
 expect(screen.getByLabelText('可见条目').textContent?.split(',')).toHaveLength(100);expect(within(nav).getByText('共 121 条 · 第 1 / 2 页')).toBeTruthy();
});
it('可跳转具体页码，空列表的翻页按钮禁用',()=>{
 const {unmount}=render(<List/>);fireEvent.change(screen.getByLabelText('跳转页码'),{target:{value:'13'}});
 expect(screen.getByLabelText('可见条目').textContent).toBe('121');expect((screen.getByRole('button',{name:'下一页'}) as HTMLButtonElement).disabled).toBe(true);
 unmount();render(<List total={0}/>);expect(screen.getByText('共 0 条 · 第 0 / 0 页')).toBeTruthy();
 expect((screen.getByRole('button',{name:'上一页'}) as HTMLButtonElement).disabled).toBe(true);expect((screen.getByRole('button',{name:'下一页'}) as HTMLButtonElement).disabled).toBe(true);
});
