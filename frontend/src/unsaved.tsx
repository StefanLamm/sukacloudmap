import React,{useEffect,useRef,useState} from 'react';
import {Modal,Action} from './ui';
import {HStack} from '@astryxdesign/core/Stack';

type Request={message:string;resolve:(accepted:boolean)=>void};
export function confirmDiscard(message:string):Promise<boolean>{
  return new Promise(resolve=>window.dispatchEvent(new CustomEvent<Request>('suka-confirm-discard',{detail:{message,resolve}})));
}
export function editorLeaveDecision():boolean|Promise<boolean>{
  const checks:Promise<boolean>[]=[];
  window.dispatchEvent(new CustomEvent('suka-before-leave',{detail:checks}));
  return checks.length?Promise.all(checks).then(values=>values.every(Boolean)):true;
}
export async function canLeaveEditor(){return editorLeaveDecision();}

/** App-owned dialog avoids blocking the embedded browser with window.confirm. */
export function DiscardConfirmation(){
  const [pending,setPending]=useState<Request|null>(null);const request=useRef<Request|null>(null);
  useEffect(()=>{const listen=(e:Event)=>{const next=(e as CustomEvent<Request>).detail;if(request.current){next.resolve(false);return;}request.current=next;setPending(next);};window.addEventListener('suka-confirm-discard',listen);return()=>{window.removeEventListener('suka-confirm-discard',listen);request.current?.resolve(false);};},[]);
  function finish(accepted:boolean){request.current?.resolve(accepted);request.current=null;setPending(null);}
  return pending?<Modal title="人工调整尚未保存" description={pending.message} onClose={()=>finish(false)}><HStack gap={3} hAlign="end" wrap="wrap"><Action className="button secondary" onClick={()=>finish(false)}>继续编辑</Action><Action className="button primary" onClick={()=>finish(true)}>放弃修改并继续</Action></HStack></Modal>:null;
}
