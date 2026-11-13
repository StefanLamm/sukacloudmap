import React from 'react';
import './pagination.css';

export type PageSize=10|20|50|100;
export function pageIndex(page:number,total:number,pageSize:number){return Math.max(0,Math.min(page,Math.max(0,Math.ceil(total/pageSize)-1)));}

export function Pagination({page,total,pageSize,onPageChange,onPageSizeChange,disabled=false,label='列表'}:{page:number;total:number;pageSize:PageSize;onPageChange:(page:number)=>void;onPageSizeChange:(size:PageSize)=>void;disabled?:boolean;label?:string}){
  const pages=Math.ceil(total/pageSize),current=pageIndex(page,total,pageSize);
  const numbers=[...new Set([0,current-1,current,current+1,pages-1])].filter(n=>n>=0&&n<pages).sort((a,b)=>a-b);
  return <nav className="list-pagination" aria-label={label+'分页'}>
    <span className="pagination-summary" aria-live="polite">共 {total} 条 · 第 {pages?current+1:0} / {pages} 页</span>
    <label>每页显示 <select aria-label="每页显示条数" value={pageSize} disabled={disabled} onChange={e=>onPageSizeChange(Number(e.target.value) as PageSize)}>{([10,20,50,100] as const).map(size=><option key={size} value={size}>{size}条</option>)}</select></label>
    <div className="page-number-buttons"><button type="button" className="text-button" disabled={disabled||current===0} onClick={()=>onPageChange(current-1)}>上一页</button>
      {numbers.map((n,i)=><React.Fragment key={n}>{i>0&&n>numbers[i-1]+1&&<span aria-hidden="true">…</span>}<button type="button" className="page-number" aria-label={'第 '+(n+1)+' 页'} aria-current={n===current?'page':undefined} disabled={disabled} onClick={()=>onPageChange(n)}>{n+1}</button></React.Fragment>)}
      <button type="button" className="text-button" disabled={disabled||current+1>=pages} onClick={()=>onPageChange(current+1)}>下一页</button></div>
    {pages>5&&<label>跳至 <input type="number" aria-label="跳转页码" min={1} max={pages} value={current+1} disabled={disabled} onChange={e=>{const n=Number(e.target.value);if(Number.isInteger(n)&&n>=1&&n<=pages)onPageChange(n-1);}}/> 页</label>}
  </nav>;
}
