import React from 'react';
import {HStack} from '@astryxdesign/core/Stack';
import {Text} from '@astryxdesign/core/Text';
import {Action,Input,Select} from './ui';

export type PageSize=10|20|50|100;
export function pageIndex(page:number,total:number,pageSize:number){return Math.max(0,Math.min(page,Math.max(0,Math.ceil(total/pageSize)-1)));}

export function Pagination({page,total,pageSize,onPageChange,onPageSizeChange,disabled=false,label='列表'}:{page:number;total:number;pageSize:PageSize;onPageChange:(page:number)=>void;onPageSizeChange:(size:PageSize)=>void;disabled?:boolean;label?:string}){
  const pages=Math.ceil(total/pageSize),current=pageIndex(page,total,pageSize);
  const numbers=[...new Set([0,current-1,current,current+1,pages-1])].filter(n=>n>=0&&n<pages).sort((a,b)=>a-b);
  return <HStack as="nav" gap={3} padding={3} wrap="wrap" hAlign="between" aria-label={label+'分页'}>
    <Text type="supporting" color="secondary" aria-live="polite">共 {total} 条 · 第 {pages?current+1:0} / {pages} 页</Text>
    <HStack gap={2} wrap="wrap"><Text type="supporting">每页显示</Text><Select aria-label="每页显示条数" style={{width:'auto'}} value={pageSize} disabled={disabled} onChange={e=>onPageSizeChange(Number(e.target.value) as PageSize)}>{([10,20,50,100] as const).map(size=><option key={size} value={size}>{size}条</option>)}</Select></HStack>
    <HStack gap={1} wrap="wrap"><Action type="button" variant="ghost" disabled={disabled||current===0} onClick={()=>onPageChange(current-1)}>上一页</Action>
      {numbers.map((n,i)=><React.Fragment key={n}>{i>0&&n>numbers[i-1]+1&&<Text aria-hidden="true">…</Text>}<Action type="button" variant={n===current?'primary':'ghost'} aria-label={'第 '+(n+1)+' 页'} aria-current={n===current?'page':undefined} disabled={disabled} onClick={()=>onPageChange(n)}>{n+1}</Action></React.Fragment>)}
      <Action type="button" variant="ghost" disabled={disabled||current+1>=pages} onClick={()=>onPageChange(current+1)}>下一页</Action></HStack>
    {pages>5&&<HStack gap={2}><Text type="supporting">跳至</Text><Input type="number" aria-label="跳转页码" style={{width:'5rem'}} min={1} max={pages} value={current+1} disabled={disabled} onChange={e=>{const n=Number(e.target.value);if(Number.isInteger(n)&&n>=1&&n<=pages)onPageChange(n-1);}}/><Text type="supporting">页</Text></HStack>}
  </HStack>;
}
