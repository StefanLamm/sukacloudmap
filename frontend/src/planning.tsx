import React, {FormEvent, useEffect, useRef, useState} from 'react';
import {ArrowRight, FileSpreadsheet, GitCompareArrows, MapPin, RefreshCw, Route, Truck, Warehouse} from 'lucide-react';
import {api} from './api';
import {Catalog, dateToday, quoteDescription, Row, QuoteVersion} from './master-data';
import {ErrorMessage, Modal, Spinner} from './ui';
import {LocationMap} from './location-map';
import {Pagination,PageSize} from './pagination';
import './planning.css';
import {DemandPanel, DayData} from './demand-panel';
import {PlanPanel} from './plan-panel';
import {editorLeaveDecision} from './unsaved';

type DemandGroup={id:string;name:string;delivery_date:string;revision:number;task_count:number;total_pieces:number;snapshot_count:number;plan_count:number};
const empty:Catalog={warehouses:[],stores:[],vehicles:[],quotes:[],master_revision:0};
export function PlanningPage({notify,initialDay}:{notify:(message:string)=>void;initialDay?:string}){
  const [groups,setGroups]=useState<DemandGroup[]>([]);const [groupId,setGroupId]=useState('');const [creating,setCreating]=useState(false);const [deleting,setDeleting]=useState(false);const [groupName,setGroupName]=useState('');const [groupBusy,setGroupBusy]=useState(false);const [groupError,setGroupError]=useState('');
  const group=groups.find(g=>g.id===groupId);
  const [day,setDay]=useState(()=>initialDay??dateToday());const [demand,setDemand]=useState<DayData|null>(null);const [section,setSection]=useState<'demand'|'resources'|'checks'|'plans'>('demand');const [data,setData]=useState<Catalog>(empty);const [loading,setLoading]=useState(true);const [error,setError]=useState('');
  const [warehouse,setWarehouse]=useState('');const [query,setQuery]=useState('');const [page,setPage]=useState(0);const [pageSize,setPageSize]=useState<PageSize>(10);
  const [selected,setSelected]=useState<string[]>([]);const [comparing,setComparing]=useState(false);const [showMap,setShowMap]=useState(false);
  const [editing,setEditing]=useState<{kind:'warehouses'|'vehicles';row:Row}|null>(null);const generation=useRef(0);
  const dayRef=useRef(day);dayRef.current=day;const pendingDay=useRef(false);
  async function load(){const request=++generation.current;setLoading(true);setError('');try{
    const [groupList,result,daily]=await Promise.all([api<{items:DemandGroup[]}>('/planning/days/'+day+'/groups'),api<Catalog>(groupId?'/planning/groups/'+groupId+'/catalog':'/master-data/catalog?delivery_date='+day),groupId?api<DayData>('/planning/days/'+day+'?group_id='+groupId):Promise.resolve(null)]);
    if(request===generation.current){setGroups(groupList.items);setData(result);setDemand(daily);}
  }catch(e){if(request===generation.current){setError(e instanceof Error?e.message:'读取业务资料失败。');setData(empty);setDemand(null);}}finally{if(request===generation.current)setLoading(false);}}
  useEffect(()=>{setSelected([]);void load();return()=>{generation.current++;};},[day,groupId]);
  async function chooseGroup(value:string){if(value===groupId)return;const decision=editorLeaveDecision();if(await decision){setGroupId(value);setDemand(null);setSelected([]);}}
  async function createGroup(e:FormEvent){e.preventDefault();setGroupBusy(true);setGroupError('');try{const item=await api<DemandGroup>('/planning/days/'+day+'/groups','POST',{name:groupName});setGroups(v=>[...v,item]);setCreating(false);setGroupName('');setGroupId(item.id);notify('已创建需求分组 '+item.name);}catch(e){setGroupError(errorText(e));}finally{setGroupBusy(false);}}
  async function removeGroup(){if(!group)return;setGroupBusy(true);setGroupError('');try{await api('/planning/groups/'+group.id+'?expected_revision='+group.revision,'DELETE');setDeleting(false);setGroups(v=>v.filter(g=>g.id!==group.id));setGroupId('');setDemand(null);notify('分组及组内需求、快照和方案已删除');}catch(e){setGroupError(errorText(e));}finally{setGroupBusy(false);}}
  const warehouses=data.warehouses.filter(w=>!warehouse||w.id===warehouse);
  const vehicles=data.vehicles.filter(v=>!warehouse||v.day_warehouse_id===warehouse);
  const demanded=new Set(demand?.delivery_date===day?demand.items.map(t=>t.store_id):[]);
  const stores=data.stores.filter(s=>demanded.has(s.id)&&(!warehouse||s.warehouse_id===warehouse)&&[s.code,s.name,s.city].some(v=>String(v??'').includes(query)));
  const pages=Math.ceil(stores.length/pageSize);const current=Math.min(page,Math.max(pages-1,0));const visible=stores.slice(current*pageSize,current*pageSize+pageSize);
  function finish(){setEditing(null);notify('当日安排已保存，输入有变化时请重新冻结快照');void load();}
  function changeDay(input:HTMLInputElement){
    const value=input.value;const previous=dayRef.current;
    if(!value||value===previous){if(!value)input.value=previous;return;}
    if(pendingDay.current){input.value=previous;return;}
    const commit=()=>{dayRef.current=value;setDay(value);setGroupId('');setDemand(null);};
    const decision=editorLeaveDecision();
    // A controlled date input must update synchronously when no editor is
    // dirty. The ref deduplicates input/change before React commits its render.
    if(typeof decision==='boolean'){if(decision)commit();else input.value=previous;}
    else{
      pendingDay.current=true;input.value=previous;
      void decision.then(accepted=>{pendingDay.current=false;if(accepted)commit();else input.value=dayRef.current;});
    }
  }
  return <>
    <div className="page-heading"><div><div className="section-kicker">LOGISTICS PLANNING</div><h1>物流线路规划</h1><p className="muted">导入每日需求，核对资源，联合排线并保留人工调整版本。</p></div><button className="button secondary" onClick={()=>void load()} disabled={loading}><RefreshCw size={15}/> 刷新业务资料</button></div>
    <div className="planning-controls"><div><label htmlFor="planning-date">配送日期</label><input id="planning-date" type="date" required value={day} onInput={e=>changeDay(e.currentTarget)} onChange={e=>changeDay(e.currentTarget)}/></div><div><label htmlFor="planning-warehouse">出货仓库</label><select id="planning-warehouse" value={warehouse} onChange={e=>{setWarehouse(e.target.value);setPage(0);}}><option value="">全部仓库</option>{data.warehouses.map(w=><option key={w.id} value={w.id}>{w.code} · {w.name}</option>)}</select></div><p>本页的安排按配送日期保存，基础资料继续统一维护。</p></div>
    <section className="panel demand-group-picker"><div><label htmlFor="demand-group">需求分组</label><select id="demand-group" value={groupId} disabled={loading} onChange={e=>void chooseGroup(e.target.value)}><option value="">请选择需求分组</option>{groups.map(g=><option key={g.id} value={g.id}>{g.name} · {g.task_count}票 / {g.total_pieces}件</option>)}</select></div><button className="button primary" onClick={()=>{setGroupError('');setCreating(true);}}>新建需求分组</button><button className="button secondary" disabled={!group||loading} onClick={()=>{setGroupError('');setDeleting(true);}}>删除分组</button><p>需求、收货限制、资源安排、快照和方案均按分组保存。</p></section>
    <div className="planning-flow"><span><FileSpreadsheet size={15}/> 导入每日需求</span><ArrowRight size={13}/><span><Truck size={15}/> 核对当日资源</span><ArrowRight size={13}/><span><Route size={15}/> 冻结规划输入</span></div>
    <ErrorMessage text={error}/>{loading&&<div className="table-empty"><Spinner/> 正在读取 {day} 的业务资料</div>}
    <nav className="planning-sections" aria-label="物流业务板块"><button className={section==='demand'?'selected':''} onClick={()=>setSection('demand')}><FileSpreadsheet size={15}/> 每日需求与快照</button><button className={section==='resources'?'selected':''} onClick={()=>setSection('resources')}><Truck size={15}/> 仓库与车辆安排</button><button className={section==='checks'?'selected':''} onClick={()=>setSection('checks')}><GitCompareArrows size={15}/> 门店与报价校验</button><button className={section==='plans'?'selected':''} onClick={()=>setSection('plans')}><Route size={15}/> 方案规划与调整</button></nav>
    {!groupId&&<div className="panel table-empty">请先选择或新建需求分组，再创建需求、校验报价或规划线路。</div>}
    {groupId&&<React.Fragment key={day+groupId}>
    {section!=='demand'&&demand?.latest_snapshot?.input_changed&&<div className="planning-input-alert"><span>输入已变更：{demand.latest_snapshot.changed_reasons.join('；')}</span><button className="text-button" onClick={()=>setSection('demand')}>查看并重新冻结</button></div>}
    {section==='demand'&&<DemandPanel key={day+groupId} groupId={groupId} catalog={data} day={day} data={demand} loading={loading} onReload={()=>void load()} notify={notify}/>}
    <div hidden={section!=='plans'}><PlanPanel key={day+groupId} groupId={groupId} day={day} demand={demand} notify={notify}/></div>
    {(section==='resources'||section==='checks')&&<>
    <div className="stat-row account-stats"><div className="stat-item"><span>出货仓库</span><strong>{warehouses.length}<small> 个</small></strong></div><div className="stat-item"><span>当日可用车辆</span><strong>{vehicles.filter(v=>v.day_available).length}<small> 辆</small></strong></div><div className="stat-item"><span>关联门店</span><strong>{stores.length}<small> 家</small></strong></div></div>
    {section==='resources'&&<div className="planning-resource-grid">
      <section className="panel"><div className="panel-toolbar"><div className="panel-title"><Warehouse size={17}/><h2>仓库发车安排</h2></div></div><div className="planning-resource-list">{warehouses.map(w=><div className="planning-resource" key={w.id}><div><b>{w.name}</b><small>{w.code} · {w.day_override?'当日单独安排':'沿用默认'}</small></div><strong>{String(w.day_departure_time)}</strong><button className="text-button" disabled={loading} onClick={()=>setEditing({kind:'warehouses',row:w})}>设置发车</button></div>)}{!warehouses.length&&!loading&&<p className="table-empty">请先在仓库管理维护仓库资料。</p>}</div></section>
      <section className="panel"><div className="panel-toolbar"><div className="panel-title"><Truck size={17}/><h2>当日车辆安排</h2></div></div><div className="planning-resource-list">{vehicles.map(v=><div className="planning-resource" key={v.id}><div><b>{v.plate}</b><small>{String(v.day_warehouse_name)} · {String(v.max_pieces)} 件 · {v.day_available?'可用':'不可用'}</small></div><span className={'status '+(!v.day_available?'inactive':'')}><span className="status-dot"/>{v.day_override?'单独安排':'默认安排'}</span><button className="text-button" disabled={loading} onClick={()=>setEditing({kind:'vehicles',row:v})}>安排车辆</button></div>)}{!vehicles.length&&!loading&&<p className="table-empty">当前仓库没有车辆，请检查默认归仓或切换全部仓库。</p>}</div></section>
    </div>
    }{section==='checks'&&<section className="panel planning-store-panel"><div className="panel-toolbar"><div className="panel-title"><h2>门店与报价校验</h2><span className="count">{stores.length}</span></div><div className="toolbar-actions"><button className="button secondary" onClick={()=>setShowMap(!showMap)}><MapPin size={14}/>{showMap?'收起位置地图':'查看位置地图'}</button><input className="planning-search" aria-label="搜索规划门店" placeholder="搜索编号、门店或城市" value={query} onChange={e=>{setQuery(e.target.value);setPage(0);}}/></div></div>
    {showMap&&<div className="location-panel"><LocationMap points={[...warehouses,...stores].map(r=>({id:r.id,name:String(r.name),code:String(r.code),address:r.address,longitude:r.longitude,latitude:r.latitude,crs:r.crs}))} label="规划仓库与门店位置"/></div>}
    <div className="planning-selection"><span>已选择 {selected.length} 家门店（含其他页）</span>{selected.length>10000&&<span>每次最多校验10000家门店，请减少选择。</span>}<button className="button primary" disabled={loading||selected.length<2||selected.length>10000} onClick={()=>setComparing(true)}><GitCompareArrows size={15}/> 判断报价兼容</button>{selected.length>0&&<button className="text-button" onClick={()=>setSelected([])}>清空选择</button>}</div>
    <div className="table-scroll"><table className="accounts-table master-table"><thead><tr><th><div className="bulk-selection-head"><input type="checkbox" className="row-check" aria-label="选择本页规划门店" checked={visible.length>0&&visible.every(s=>selected.includes(s.id))} disabled={loading||!visible.length} onChange={e=>setSelected(e.target.checked?Array.from(new Set([...selected,...visible.map(s=>s.id)])):selected.filter(id=>!visible.some(s=>s.id===id)))}/><button type="button" className="text-button" aria-label={'全选所有匹配规划门店（'+stores.length+'条）'} disabled={loading||!stores.length} onClick={()=>setSelected(previous=>Array.from(new Set([...previous,...stores.map(s=>s.id)])))}>全选所有（{stores.length}条匹配）</button></div></th><th>门店 / 出货仓</th><th>专车报价</th><th>共配每件价</th><th>配置提示</th></tr></thead><tbody>{visible.map(s=><tr key={s.id}><td><input type="checkbox" className="row-check" aria-label={'选择规划门店 '+s.code} checked={selected.includes(s.id)} onChange={e=>setSelected(e.target.checked?[...selected,s.id]:selected.filter(id=>id!==s.id))}/></td><td><b className="entity-name">{s.name}</b><small className="cell-secondary">{s.code} · {String(s.warehouse_name)}</small></td><td>{s.dedicated_quote_available?quoteDescription(s.dedicated_quote_version):'未配置有效报价'}</td><td>{s.common_unit_price===null?'未配置':s.common_unit_price+' 元'}</td><td className="planning-status">{s.dedicated_config_ready?'基础配置齐全':(s.dedicated_config_missing??[]).join('；')}{!s.common_available&&<small className="cell-secondary">共配缺价</small>}</td></tr>)}</tbody></table></div>
    {!stores.length&&!loading&&<div className="table-empty">当日没有匹配的需求门店，请先导入每日需求。</div>}<Pagination label="规划门店" total={stores.length} page={current} pageSize={pageSize} disabled={loading} onPageChange={setPage} onPageSizeChange={size=>{setPageSize(size);setPage(0);}}/>
    </section>}<p className="planning-note">报价兼容只表示同仓与计费参数一致。进入“方案规划与调整”进行排线、道路复核及人工确认。</p></>}
    </React.Fragment>}
    {creating&&<Modal title="新建需求分组" description={day} onClose={()=>setCreating(false)} busy={groupBusy}><form onSubmit={createGroup}><label htmlFor="new-group-name">分组名称</label><input id="new-group-name" required maxLength={120} value={groupName} onChange={e=>setGroupName(e.target.value)}/><ErrorMessage text={groupError}/><div className="modal-actions"><button type="button" className="button secondary" onClick={()=>setCreating(false)}>取消</button><button className="button primary" disabled={groupBusy}>创建分组</button></div></form></Modal>}
    {deleting&&group&&<Modal title="删除需求分组" description={`${day} · ${group.name}`} onClose={()=>setDeleting(false)} busy={groupBusy}><p>将删除本组 {group.task_count} 票需求、{group.snapshot_count} 份快照、{group.plan_count} 个方案版本和组内安排。基础资料和其他分组保留。此操作无法在页面撤销。</p><ErrorMessage text={groupError}/><div className="modal-actions"><button className="button secondary" onClick={()=>setDeleting(false)}>取消</button><button className="button danger" disabled={groupBusy} onClick={()=>void removeGroup()}>删除分组及全部数据</button></div></Modal>}
    {editing&&<DayEditor groupId={groupId} kind={editing.kind} row={editing.row} data={data} day={day} onClose={()=>setEditing(null)} onSave={finish}/>}
    {comparing&&<CompatibilityDialog ids={selected} data={data} day={day} onClose={()=>setComparing(false)}/>}
  </>;
}

type Compatibility = {compatible:boolean; reasons:string[]; groups:{warehouse_name:string;store_ids:string[];parameters:QuoteVersion}[];notice:string};
const errorText=(e:unknown)=>e instanceof Error?e.message:'操作未完成，请重试。';
function DayEditor({kind,row,data,day,groupId,onClose,onSave}:{groupId?:string;kind:'warehouses'|'vehicles';row:Row;data:Catalog;day:string;onClose:()=>void;onSave:()=>void}){
  const [departure,setDeparture]=useState(String(row.day_departure_time??'08:00'));const [warehouse,setWarehouse]=useState(String(row.day_warehouse_id??''));const [available,setAvailable]=useState(String(row.day_available));
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');
  async function save(e:FormEvent){e.preventDefault();setBusy(true);try{await api(groupId?'/planning/groups/'+groupId+'/'+kind+'/'+row.id:'/'+kind+'/'+row.id+'/days/'+day,'PUT',{expected_revision:row.day_revision,...(kind==='warehouses'?{departure_time:departure}:{warehouse_id:warehouse,available:available==='true'})});onSave();}catch(e){setError(errorText(e));}finally{setBusy(false);}}
  return <Modal title="当日安排" description={`${day} · ${row.name??row.plate} · 北京时间`} onClose={onClose} busy={busy}><form onSubmit={save}>{kind==='warehouses'?<><label htmlFor="day-departure">当天最早发车时间</label><input id="day-departure" type="time" required value={departure} onInput={e=>setDeparture(e.currentTarget.value)} onChange={e=>setDeparture(e.target.value)}/></>:<><label htmlFor="day-warehouse">当天出货仓</label><select id="day-warehouse" value={warehouse} required onChange={e=>setWarehouse(e.target.value)}>{data.warehouses.map(w=><option key={w.id} value={w.id}>{w.code} · {w.name}</option>)}</select><label htmlFor="day-available">当天是否可用</label><select id="day-available" value={available} onChange={e=>setAvailable(e.target.value)}><option value="true">可用</option><option value="false">不可用</option></select></>}<p className="field-hint">仅影响所选日期；默认资料和其他日期的单独安排保持原值。</p><ErrorMessage text={error}/><div className="modal-actions"><button type="button" className="button secondary" onClick={onClose} disabled={busy}>取消</button><button className="button primary" disabled={busy}>{busy&&<Spinner/>} 保存当日安排</button></div></form></Modal>;
}

function CompatibilityDialog({ids,data,day,onClose}:{ids:string[];data:Catalog;day:string;onClose:()=>void}){
  const [result,setResult]=useState<Compatibility|null>(null);const [error,setError]=useState('');
  useEffect(()=>{api<Compatibility>('/master-data/compatibility','POST',{store_ids:ids,delivery_date:day}).then(setResult).catch(e=>setError(errorText(e)));},[]);
  return <Modal wide title="报价兼容判断" description={`${day} · 已选择 ${ids.length} 家门店`} onClose={onClose}><ErrorMessage text={error}/>{result?<><div className={'import-result '+(result.compatible?'valid':'invalid')}><GitCompareArrows size={19}/>{result.compatible?'同仓且完整报价参数一致，报价兼容':'所选门店不能合为同一报价线路'}</div>{result.reasons.map((r,i)=><p className="compat-reason" key={i}>{r}</p>)}<div className="version-list">{result.groups.map((g,i)=><div className="version-item" key={i}><div><b>{g.warehouse_name}</b><span className="version-badge">兼容组 {i+1}</span></div><p>{quoteDescription(g.parameters)}</p><small className="muted">{g.store_ids.map(id=>data.stores.find(s=>s.id===id)?.name).join('、')}</small></div>)}</div><p className="form-guidance">{result.notice}</p></>:!error&&<div className="table-empty"><Spinner/> 正在比较完整参数</div>}</Modal>;
}
