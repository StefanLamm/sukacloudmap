import {api,ApiError} from './api';
import type {Row} from './master-data';
export async function download(rows:Row[]){
 const session=await api<{csrf_token:string}>('/auth/me');
 const response=await fetch('/api/master-data/stores/export.xlsx',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf_token},body:JSON.stringify({items:rows.map(row=>({id:row.id,expected_revision:row.revision}))})});
 if(!response.ok){const error=await response.json().catch(()=>({}));throw new ApiError(response.status,error.message||'门店导出失败。',error.code||'EXPORT_FAILED');}
 const blob=await response.blob();const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download='门店资料.xlsx';document.body.appendChild(link);link.click();link.remove();window.setTimeout(()=>URL.revokeObjectURL(url),1000);
}
