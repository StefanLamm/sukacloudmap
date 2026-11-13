import React, {FormEvent, useEffect, useId, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {ArrowRight, ArrowUpRight, Check, CheckCircle2, ChevronRight, CircleHelp, ClipboardList, Clock3, Eye, EyeOff, FileText, LayoutDashboard, LoaderCircle, LogOut, MapPin, MoreHorizontal, Plus, RefreshCw, Search, ShieldCheck, Truck, Users, X} from 'lucide-react';
import {Account, api, ApiError, AuditEvent, setCsrf, SharedRecord} from './api';
import './styles.css';
import {Modal, Spinner, ErrorMessage} from './ui';
import {Pagination,PageSize,pageIndex} from './pagination';
import {MasterDataPage, MasterKind} from './master-data';
import {Warehouse, Store, Tags, Route} from 'lucide-react';
import {PlanningPage} from './planning';
import './theme.css';
import {canLeaveEditor,DiscardConfirmation} from './unsaved';

type Page = 'workspace' | 'accounts' | 'activity' | 'planning' | MasterKind;
const pageNames: Record<Page, string> = {workspace:'共享工作区', planning:'物流线路规划', accounts:'账号管理', activity:'操作记录', warehouses:'仓库管理', stores:'门店管理', vehicles:'车辆管理', quotes:'报价方案'};
function pageFromPath(): Page {const value=location.pathname.slice(1);return Object.hasOwn(pageNames,value) ? value as Page : 'workspace';}
const time = (value: string) => new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(value));
const message = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试。';

function Brand({light = false}: {light?: boolean}) {
  return <div className={'brand ' + (light ? 'brand-light' : '')}><span className="brand-mark"><MapPin size={24}/><i/></span><span>苏咔云图<small>SUKA CLOUDMAP</small></span></div>;
}

function Login({onLogin}: {onLogin: (account: Account) => void}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError('');
    try {
      const result = await api<{account: Account; csrf_token: string}>('/auth/login', 'POST', {username, password});
      setCsrf(result.csrf_token); setPassword(''); onLogin(result.account);
    } catch (e) {setError(message(e));} finally {setBusy(false);}
  }
  return <div className="login-layout">
    <section className="login-story"><Brand light/><div className="story-content"><span className="eyebrow">门店配送协作平台</span><h1>每一份安排，<br/>都有清晰的起点。</h1><p>连接仓库、门店与团队，<br/>让每日配送有序开展。</p>
      <div className="route-art" aria-hidden="true"><svg viewBox="0 0 540 270"><defs><pattern id="dots" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1" fill="#ffffff" opacity=".13"/></pattern></defs><rect width="540" height="270" fill="url(#dots)"/><path d="M65 191H191Q211 191 211 171V100Q211 80 231 80H390" fill="none" stroke="#d4c0b2" strokeWidth="2" strokeDasharray="5 6"/><path d="M65 191H130Q150 191 150 211V234H320Q340 234 340 214V154Q340 134 360 134H471" fill="none" stroke="#9d6d4b" strokeWidth="2"/><circle cx="390" cy="80" r="8" fill="#e0b86e"/><circle cx="471" cy="134" r="6" fill="#e4d2c5"/><circle cx="320" cy="234" r="6" fill="#e4d2c5"/><rect x="45" y="171" width="40" height="40" rx="11" fill="#eee8e4"/><path d="M56 185l9-5 9 5v13H56zM62 198v-7h6v7" fill="none" stroke="#683c1d" strokeWidth="2"/></svg><span className="art-label art-warehouse">仓库</span><span className="art-label art-store">门店</span><span className="art-note"><CheckCircle2 size={14}/> 团队信息，共享同步</span></div>
    </div><footer>苏咔云图 · 内部工作平台</footer></section>
    <main className="login-main"><div className="login-mobile-brand"><Brand/></div><div className="login-form-wrap"><div className="login-symbol"><ShieldCheck size={25}/></div><h2>登录工作区</h2><p className="muted login-subtitle">使用您的内部账号，开始今日工作。</p><form onSubmit={submit}>
      <label htmlFor="username">账号</label><input id="username" name="username" autoComplete="username" placeholder="请输入账号" required maxLength={64} value={username} onChange={e => setUsername(e.target.value)} autoFocus/>
      <label htmlFor="password">密码</label><div className="password-input"><input id="password" name="password" type={visible ? 'text' : 'password'} autoComplete="current-password" placeholder="请输入密码" required maxLength={128} value={password} onChange={e => setPassword(e.target.value)}/><button type="button" className="icon-button" aria-label={visible ? '隐藏密码' : '显示密码'} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={18}/> : <Eye size={18}/>}</button></div>
      <ErrorMessage text={error}/><button className="button primary login-submit" disabled={busy}>{busy ? <Spinner/> : null}{busy ? '正在登录' : '登录'}{!busy && <ArrowRight size={17}/>}</button>
    </form><p className="login-help"><CircleHelp size={15}/> 无法登录？请联系管理员开通或重置账号。</p></div><div className="login-footer"><span><ShieldCheck size={13}/> 仅限内部授权人员使用</span><span>苏咔云图</span></div></main>
  </div>;
}

const beijingDay=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
type TaskView='pending'|'completed'|'overdue'|'all';
export function Workspace({notify,isAdmin=false}: {notify: (text: string) => void;isAdmin?:boolean}) {
  const [records,setRecords]=useState<SharedRecord[]>([]);const [loading,setLoading]=useState(true);const [busyRecord,setBusyRecord]=useState('');
  const [error,setError]=useState('');const [query,setQuery]=useState('');const [view,setView]=useState<TaskView>('pending');const [taskDate,setTaskDate]=useState(beijingDay);
  const [editing,setEditing]=useState<SharedRecord|'new'|null>(null);const [recordPage,setRecordPage]=useState(0);const [recordSize,setRecordSize]=useState<PageSize>(10);const [recordTotal,setRecordTotal]=useState(0);const [recordAll,setRecordAll]=useState(0);const [counts,setCounts]=useState({pending:0,completed:0,overdue:0});const [latestRecordAt,setLatestRecordAt]=useState<string|null>(null);const recordGeneration=useRef(0);
  async function load(){const generation=++recordGeneration.current;setLoading(true);setError('');try{
    const params=new URLSearchParams({q:query,status:view,offset:String(recordPage*recordSize),limit:String(recordSize)});if(view==='pending')params.set('task_date',taskDate);
    const r=await api<{items:SharedRecord[];total:number;total_all:number;latest_updated_at:string|null;counts:{pending:number;completed:number;overdue:number}}>('/workspace/records?'+params);
    if(generation!==recordGeneration.current)return;setRecords(r.items);setRecordTotal(r.total);if(recordPage>Math.max(0,Math.ceil(r.total/recordSize)-1))setRecordPage(Math.max(0,Math.ceil(r.total/recordSize)-1));setRecordAll(r.total_all);setLatestRecordAt(r.latest_updated_at);if(r.counts)setCounts(r.counts);
  }catch(e){if(generation===recordGeneration.current)setError(message(e));}finally{if(generation===recordGeneration.current)setLoading(false);}}
  useEffect(()=>{const timer=setTimeout(()=>void load(),150);return()=>{clearTimeout(timer);recordGeneration.current++;};},[query,recordPage,recordSize,view,taskDate]);
  useEffect(()=>{const timer=setInterval(()=>void load(),60000);return()=>clearInterval(timer);},[query,recordPage,recordSize,view,taskDate]);
  async function complete(record:SharedRecord){setBusyRecord(record.id);setError('');try{await api('/workspace/records/'+record.id+'/completion','POST',{expected_revision:record.revision,completed:record.status!=='COMPLETED'});notify(record.status==='COMPLETED'?'任务已恢复待办':'任务已归入已完成任务');await load();}catch(e){setError(message(e));}finally{setBusyRecord('');}}
  return <><div className="page-heading"><div><div className="section-kicker">TEAM WORKSPACE</div><h1>共享工作区</h1><p className="muted">按任务日期安排团队事项，及时跟进完成情况。</p></div><button className="button primary" onClick={()=>setEditing('new')}><Plus size={17}/> 新建记录</button></div>
    <div className="workspace-banner"><div className="banner-icon"><ClipboardList size={26}/></div><div><h3>按日安排，及时跟进</h3><p>管理员可标记任务完成；超过任务日期仍未完成的事项自动归入已超时。</p></div><span className="banner-badge"><span className="status-dot"/> 团队共享</span></div>
    <div className="stat-row"><div className="stat-item"><span>共享任务</span><strong>{recordAll}<small> 条</small></strong></div><div className="stat-item"><span>已完成 / 已超时</span><strong>{counts.completed}<small> / {counts.overdue} 条</small></strong></div><div className="stat-item"><span>最近更新</span><strong className="stat-time">{latestRecordAt?time(latestRecordAt):'暂无记录'}</strong></div></div>
    <section className="panel"><div className="workspace-task-toolbar"><div className="tabs" aria-label="任务分类">{([['pending','日期任务'],['completed','已完成任务'],['overdue','已超时'],['all','全部任务']] as const).map(([key,label])=><button key={key} className={view===key?'selected':''} onClick={()=>{setView(key);setRecordPage(0);}}>{label}{key==='completed'?' '+counts.completed:key==='overdue'?' '+counts.overdue:''}</button>)}</div>
      {view==='pending'&&<label className="workspace-task-date">任务日期<input aria-label="筛选任务日期" type="date" required value={taskDate} onChange={e=>{if(e.target.value){setTaskDate(e.target.value);setRecordPage(0);}}}/></label>}</div>
      <div className="panel-toolbar"><div className="panel-title"><h2>{view==='pending'?taskDate+' · 团队任务':view==='completed'?'已完成任务':view==='overdue'?'已超时':'全部团队任务'}</h2><span className="count">{recordTotal}</span></div><div className="toolbar-actions"><div className="search"><Search size={16}/><input aria-label="搜索共享记录" placeholder="搜索任务" value={query} onChange={e=>{setQuery(e.target.value);setRecordPage(0);}}/></div><button className="icon-button bordered" title="刷新记录" aria-label="刷新记录" onClick={()=>void load()}><RefreshCw size={16}/></button></div></div>
    <ErrorMessage text={error}/>{loading?<div className="empty-state"><Spinner/><p>正在读取团队任务</p></div>:!records.length?<div className="empty-state"><div className="empty-illustration"><FileText size={32}/></div><h3>暂无匹配任务</h3><p>{view==='pending'?'已完成的任务归入已完成任务，过期未完成的任务归入已超时。':'可以切换分类或搜索其他关键词。'}</p></div>:<div className="record-grid">{records.map(r=><article className={'record-card task-card '+r.status.toLowerCase()} key={r.id}><button className="task-card-open" aria-label={'编辑任务 '+r.title} onClick={()=>setEditing(r)}><div className="record-card-top"><span className="record-icon"><FileText size={17}/></span><span className={'task-status '+r.status.toLowerCase()}>{r.status==='COMPLETED'?'已完成':r.status==='OVERDUE'?'已超时':'待完成'}</span><ArrowUpRight size={17}/></div><h3>{r.title}</h3><p>{r.content}</p><div className="task-due">任务日期 · {r.task_date}</div><div className="record-meta"><span>{r.updated_by}</span><span>{time(r.updated_at)}</span></div></button>{r.completed_at&&<p className="task-completion-note">{r.completed_by} · {time(r.completed_at)} 完成</p>}{isAdmin&&<button className="text-button task-complete" disabled={Boolean(busyRecord)} onClick={()=>void complete(r)}>{busyRecord===r.id?<Spinner/>:<CheckCircle2 size={15}/>} {r.status==='COMPLETED'?'恢复待办':'标记已完成'}</button>}</article>)}</div>}<Pagination label="共享记录" page={recordPage} total={recordTotal} pageSize={recordSize} disabled={loading} onPageChange={setRecordPage} onPageSizeChange={size=>{setRecordSize(size);setRecordPage(0);}}/></section>
    <p className="section-footnote"><Users size={14}/> 团队成员可新增和编辑任务，完成状态由管理员维护。日期以北京时间为准。</p>
    {editing&&<RecordEditor record={editing} initialDate={view==='pending'?taskDate:beijingDay()} onClose={()=>setEditing(null)} onSave={()=>{setEditing(null);notify('共享任务已保存');void load();}}/>}
  </>;
}

function RecordEditor({record,initialDate,onClose,onSave}: {record:SharedRecord|'new';initialDate:string;onClose:()=>void;onSave:()=>void}){
  const [title,setTitle]=useState(record==='new'?'':record.title);const [content,setContent]=useState(record==='new'?'':record.content);const [taskDate,setTaskDate]=useState(record==='new'?initialDate:record.task_date);
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');
  async function save(e:FormEvent){e.preventDefault();setBusy(true);setError('');try{await api(record==='new'?'/workspace/records':'/workspace/records/'+record.id,record==='new'?'POST':'PATCH',{title,content,task_date:taskDate,...(record==='new'?{}:{expected_revision:record.revision})});onSave();}catch(e){setError(message(e)+(e instanceof ApiError&&e.status===409?' 您的输入已保留，请关闭窗口查看最新记录后再编辑。':''));}finally{setBusy(false);}}
  return <Modal title={record==='new'?'新建共享记录':'编辑共享记录'} description="保存后，所有团队成员均可查看。" onClose={onClose} busy={busy}><form onSubmit={save}><label htmlFor="record-date">任务日期</label><input id="record-date" type="date" required value={taskDate} onChange={e=>setTaskDate(e.target.value)}/><label htmlFor="record-title">标题</label><input id="record-title" required maxLength={120} autoFocus value={title} onChange={e=>setTitle(e.target.value)} placeholder="例如：今日配送注意事项"/><label htmlFor="record-content">内容</label><textarea id="record-content" rows={6} required maxLength={5000} value={content} onChange={e=>setContent(e.target.value)} placeholder="填写需要团队了解的具体安排"/><ErrorMessage text={error}/><div className="modal-actions"><button type="button" className="button secondary" onClick={onClose} disabled={busy}>取消</button><button className="button primary" disabled={busy}>{busy&&<Spinner/>} 保存记录</button></div></form></Modal>;
}

export function Accounts({current, notify, onOwnInvalidated}: {current: Account; notify: (text: string) => void; onOwnInvalidated: () => void}) {
  const [items, setItems] = useState<Account[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState('');
  const [query, setQuery] = useState(''); const [filter, setFilter] = useState('all');
  const [page,setPage]=useState(0);const [pageSize,setPageSize]=useState<PageSize>(10);
  const [action, setAction] = useState<{type: 'create' | 'reset' | 'toggle' | 'delete'; account?: Account} | null>(null);
  async function load(){setError('');try {setItems((await api<{items: Account[]}>('/accounts')).items);} catch(e){setError(message(e));}finally{setLoading(false);}}
  useEffect(() => {void load();}, []);
  const matching = items.filter(a => (filter === 'all' || a.active === (filter === 'active')) && (a.username + a.display_name).toLowerCase().includes(query.toLowerCase()));
  const currentPage=pageIndex(page,matching.length,pageSize);const visible=matching.slice(currentPage*pageSize,currentPage*pageSize+pageSize);
  return <><div className="page-heading"><div><div className="section-kicker">ACCESS MANAGEMENT</div><h1>账号管理</h1><p className="muted">管理内部成员的访问权限与账号状态。</p></div><button className="button primary" onClick={() => setAction({type:'create'})}><Plus size={17}/> 新建账号</button></div>
    <div className="stat-row account-stats"><div className="stat-item"><span>内部账号</span><strong>{items.length}<small> 位</small></strong></div><div className="stat-item"><span>已启用</span><strong>{items.filter(a => a.active).length}<small> 位</small></strong></div><div className="stat-item"><span>管理员</span><strong>{items.filter(a => a.is_admin && a.active).length}<small> 位</small></strong></div></div>
    <section className="panel"><div className="panel-toolbar"><div className="tabs" aria-label="账号状态">{[['all','全部账号'],['active','已启用'],['inactive','已停用']].map(([key,label]) => <button key={key} className={filter===key ? 'selected' : ''} onClick={() => {setFilter(key);setPage(0);}}>{label}</button>)}</div><div className="toolbar-actions"><div className="search"><Search size={16}/><input aria-label="搜索账号或姓名" placeholder="搜索账号或姓名" value={query} onChange={e => {setQuery(e.target.value);setPage(0);}}/></div><button className="icon-button bordered" aria-label="刷新账号" onClick={() => void load()}><RefreshCw size={16}/></button></div></div>
    <ErrorMessage text={error}/><div className="table-scroll"><table className="accounts-table"><thead><tr><th>成员</th><th>角色</th><th>状态</th><th>最近登录</th><th className="action-column">操作</th></tr></thead><tbody>{visible.map(a => <tr key={a.id}><td><div className="member"><span className={'avatar '+(a.is_admin?'admin-avatar':'')}>{a.display_name.slice(-2)}</span><div><b>{a.display_name}{a.id===current.id && <span className="self-label">我</span>}</b><small>{a.username}</small></div></div></td><td><span className={'role-tag '+(a.is_admin?'admin-role':'')}>{a.is_admin && <ShieldCheck size={12}/>} {a.is_admin?'管理员':'业务人员'}</span></td><td><span className={'status '+(!a.active?'inactive':'')}><span className="status-dot"/>{a.active?'已启用':'已停用'}</span></td><td className="muted">{a.last_login_at ? time(a.last_login_at) : '尚未登录'}</td><td className="action-column"><button className="text-button" onClick={() => setAction({type:'reset',account:a})}>重置密码</button><button className={'text-button '+(a.active?'danger-text':'')} onClick={() => setAction({type:'toggle',account:a})}>{a.active?'停用':'启用'}</button><button className="text-button danger-text" disabled={a.id===current.id||a.is_admin&&a.active&&items.filter(item=>item.is_admin&&item.active).length<=1} title={a.id===current.id?'不能删除当前登录账号':undefined} onClick={()=>setAction({type:'delete',account:a})}>删除</button></td></tr>)}</tbody></table></div>{loading ? <div className="table-empty"><Spinner/> 正在读取账号</div> : !visible.length && <div className="table-empty">没有匹配的账号</div>}<div className="table-footer">共 {matching.length} 个账号<span><ShieldCheck size={13}/> 仅管理员可管理账号</span></div><Pagination label="账号" page={currentPage} total={matching.length} pageSize={pageSize} disabled={loading} onPageChange={setPage} onPageSizeChange={size=>{setPageSize(size);setPage(0);}}/></section>
    <div className="info-note"><ShieldCheck size={19}/><div><b>账号权限说明</b><p>业务人员可使用共享工作区。停用账号或重置密码后，该账号需重新登录；已保存的团队资料会保留。</p></div></div>
    {action && <AccountAction key={action.type+(action.account?.id||'')} action={action} onClose={() => setAction(null)} onSave={() => {const own=action.account?.id===current.id;setAction(null);notify(action.type==='create'?'账号已创建':action.type==='reset'?'密码已重置，原会话已注销':action.type==='delete'?'账号已删除':'账号状态已更新'); if(own)onOwnInvalidated();else void load();}}/>}
  </>;
}

function AccountAction({action, onClose, onSave}: {action: {type: 'create'|'reset'|'toggle'|'delete'; account?: Account}; onClose:()=>void; onSave:()=>void}) {
  const [username,setUsername]=useState('');const [name,setName]=useState('');const [password,setPassword]=useState('');const [repeat,setRepeat]=useState('');const [role,setRole]=useState(false);
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');
  const target=action.account;const toggle=action.type==='toggle';const deleting=action.type==='delete';
  async function submit(e:FormEvent){e.preventDefault();setError('');if(!toggle&&!deleting && password!==repeat){setError('两次输入的密码不一致。');return;}setBusy(true);try{
    if(action.type==='create') await api('/accounts','POST',{username,display_name:name,password,is_admin:role});
    else if(action.type==='reset') await api('/accounts/'+target!.id+'/reset-password','POST',{password,expected_revision:target!.revision});
    else if(deleting) await api('/accounts/'+target!.id+'?expected_revision='+target!.revision,'DELETE');
    else await api('/accounts/'+target!.id,'PATCH',{active:!target!.active,expected_revision:target!.revision});
    setPassword('');setRepeat('');onSave();
  }catch(e){setError(message(e));}finally{setBusy(false);}}
  const title=action.type==='create'?'新建内部账号':action.type==='reset'?'重置密码':deleting?'删除账号':(target!.active?'停用账号':'启用账号');
  return <Modal title={title} description={target ? `${target.display_name} · ${target.username}` : '创建后即可使用此账号登录共享工作区。'} onClose={onClose} busy={busy}><form onSubmit={submit}>
    {action.type==='create' && <><label htmlFor="account-name">姓名</label><input id="account-name" autoFocus required maxLength={80} value={name} onChange={e=>setName(e.target.value)} placeholder="请输入成员姓名"/><label htmlFor="account-username">登录账号</label><input id="account-username" required minLength={3} maxLength={64} pattern="[a-zA-Z0-9][a-zA-Z0-9._\-]{2,63}" autoComplete="off" value={username} onChange={e=>setUsername(e.target.value)} placeholder="3至64位字母、数字或 . _ -"/><label htmlFor="account-role">角色</label><select id="account-role" value={String(role)} onChange={e=>setRole(e.target.value==='true')}><option value="false">业务人员</option><option value="true">管理员</option></select></>}
    {!toggle&&!deleting && <><label htmlFor="new-password">{action.type==='reset'?'新密码':'初始密码'}</label><input id="new-password" type="password" required minLength={12} maxLength={128} autoComplete="new-password" value={password} onChange={e=>setPassword(e.target.value)} placeholder="12至128位密码" autoFocus={action.type==='reset'}/><label htmlFor="repeat-password">确认密码</label><input id="repeat-password" type="password" required minLength={12} maxLength={128} autoComplete="new-password" value={repeat} onChange={e=>setRepeat(e.target.value)} placeholder="再次输入密码"/></>}
    {deleting&&<p className="confirm-text">删除后此账号将从账号列表移除，所有登录会话立即失效。其历史团队资料及操作记录会保留，原账号名不能重复使用。</p>}{toggle && <p className="confirm-text">{target!.active?'停用后，该成员的当前会话将立即失效，无法继续访问工作区。已有共享记录会保留。':'启用后，该成员可以使用已有密码重新登录。'}</p>}
    {action.type==='reset' && <p className="field-hint">密码重置后，原密码和所有已登录会话将失效。</p>}<ErrorMessage text={error}/><div className="modal-actions"><button className="button secondary" type="button" onClick={onClose} disabled={busy}>取消</button><button className={'button '+(deleting||toggle && target!.active?'danger':'primary')} disabled={busy}>{busy && <Spinner/>}{action.type==='create'?'创建账号':title}</button></div></form></Modal>;
}

export function Activity(){
  const [items,setItems]=useState<AuditEvent[]>([]);const [error,setError]=useState('');const [loading,setLoading]=useState(true);
  const [page,setPage]=useState(0);const [pageSize,setPageSize]=useState<PageSize>(10);const [total,setTotal]=useState(0);const generation=useRef(0);
  async function load(){const request=++generation.current;setLoading(true);try{const result=await api<{items:AuditEvent[];total:number}>('/audit-events?offset='+page*pageSize+'&limit='+pageSize);if(request===generation.current){setItems(result.items);setTotal(result.total??result.items.length);setError('');}}catch(e){if(request===generation.current)setError(message(e));}finally{if(request===generation.current)setLoading(false);}}
  useEffect(()=>{void load();return()=>{generation.current++;};},[page,pageSize]);
  return <><div className="page-heading"><div><div className="section-kicker">WORKSPACE ACTIVITY</div><h1>操作记录</h1><p className="muted">查看团队的登录、账号及共享资料变更。</p></div><button className="button secondary" disabled={loading} onClick={()=>void load()}><RefreshCw size={16}/> 刷新记录</button></div><section className="panel"><div className="panel-toolbar"><div className="panel-title"><h2>操作记录</h2><span className="count">{total}</span></div><span className="muted small">北京时间</span></div><ErrorMessage text={error}/>{loading?<div className="table-empty"><Spinner/> 正在读取操作记录</div>:<div className="activity-list">{items.map(e=><div className="activity-item" key={e.id}><span className="activity-icon"><Clock3 size={16}/></span><div><b>{e.summary}</b><span>{e.actor}</span></div><time>{time(e.occurred_at)}</time></div>)}{!items.length&&<div className="table-empty">暂无操作记录</div>}</div>}<Pagination label="操作记录" page={page} total={total} pageSize={pageSize} disabled={loading} onPageChange={setPage} onPageSizeChange={size=>{setPageSize(size);setPage(0);}}/></section></>;
}

function App(){
  const [runtime,setRuntime]=useState<{test_fixture:boolean;label:string;default_delivery_date?:string}|null>(null);
  useEffect(()=>{api<{test_fixture:boolean;label:string;default_delivery_date?:string}>('/runtime').then(r=>{setRuntime(r);document.title=r.label?'苏咔云图 · 测试版':'苏咔云图';}).catch(()=>{});},[]);
  const [account,setAccount]=useState<Account|null>(null);const [loading,setLoading]=useState(true);const [bootError,setBootError]=useState('');
  const [page,setPage]=useState<Page>(pageFromPath);const [toast,setToast]=useState('');
  function signedOut(){setAccount(null);setCsrf('');history.replaceState({},'', '/login');setPage('workspace');}
  async function restore(){setLoading(true);setBootError('');try{const result=await api<{account:Account;csrf_token:string}>('/auth/me');setCsrf(result.csrf_token);setAccount(result.account);if(location.pathname==='/login')history.replaceState({},'','/');}catch(e){if(e instanceof ApiError&&e.status===401)signedOut();else setBootError(message(e));}finally{setLoading(false);}}
  useEffect(()=>{void restore();const expired=()=>signedOut();window.addEventListener('suka-session-expired',expired);return()=>window.removeEventListener('suka-session-expired',expired);},[]);
  useEffect(()=>{if(!toast)return;const timer=setTimeout(()=>setToast(''),4500);return()=>clearTimeout(timer);},[toast]);
  useEffect(()=>{const onPop=async()=>{if(!await canLeaveEditor()){history.pushState({},'',page==='workspace'?'/':'/'+page);return;}setPage(pageFromPath());};window.addEventListener('popstate',onPop);return()=>window.removeEventListener('popstate',onPop);},[page]);
  async function navigate(next:Page){if(next!==page&&!await canLeaveEditor())return;setPage(next);history.pushState({},'',next==='workspace'?'/':'/'+next);}
  if(loading)return <div className="app-loading"><Brand/><Spinner/><p>正在打开工作区</p></div>;
  if(bootError)return <div className="app-loading"><Brand/><ErrorMessage text={bootError}/><button className="button secondary" onClick={()=>void restore()}>重新连接</button></div>;
  if(!account)return <>{runtime?.label&&<div className="runtime-banner">{runtime.label}</div>}<Login onLogin={a=>{setAccount(a);history.replaceState({},'','/');setPage('workspace');}}/></>;
  const activePage:Page=!account.is_admin&&['accounts','activity'].includes(page)?'workspace':page;
  return <div className="app-shell"><aside className="sidebar"><Brand/><div className="workspace-label"><span className="workspace-letter">苏</span><div><b>苏咔配送团队</b><span>内部共享空间</span></div><ChevronRight size={14}/></div><div className="nav-caption">工作空间</div><nav><button className={activePage==='workspace'?'active':''} onClick={()=>navigate('workspace')}><LayoutDashboard size={18}/>共享工作区{activePage==='workspace'&&<span/>}</button>{<button title="物流线路规划" className={activePage==='planning'?'active':''} onClick={()=>navigate('planning')}><Route size={18}/>物流线路规划{activePage==='planning'&&<span/>}</button>}{([['warehouses',Warehouse],['stores',Store],['vehicles',Truck],['quotes',Tags]] as const).map(([key,Icon])=><button key={key} title={pageNames[key]} className={activePage===key?'active':''} onClick={()=>navigate(key)}><Icon size={18}/>{pageNames[key]}{activePage===key&&<span/>}</button>)}{account.is_admin&&<><button className={activePage==='accounts'?'active':''} onClick={()=>navigate('accounts')}><Users size={18}/>账号管理{activePage==='accounts'&&<span/>}</button><button className={activePage==='activity'?'active':''} onClick={()=>navigate('activity')}><Clock3 size={18}/>操作记录{activePage==='activity'&&<span/>}</button></>}</nav><div className="sidebar-bottom"><div className="sidebar-help"><ShieldCheck size={18}/><div><b>内部工作平台</b><p>账号问题请联系管理员</p></div></div><div className="sidebar-version">SUKA CLOUDMAP<span>v0.16.0</span></div></div></aside>
    <div className="app-main"><header className="topbar"><div className="breadcrumb">工作空间<ChevronRight size={13}/><b>{pageNames[activePage]}</b></div><div className="topbar-right">{!['warehouses','stores','vehicles','quotes'].includes(activePage)&&<span className="today">{new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'long',day:'numeric',weekday:'short'}).format(new Date())}</span>}<span className="topbar-divider"/><span className="user-chip"><span className="avatar small-avatar">{account.display_name.slice(-2)}</span><span>{account.display_name}<small>{account.is_admin?'管理员':'业务人员'}</small></span></span><button className="icon-button logout" title="退出登录" aria-label="退出登录" onClick={async()=>{if(!await canLeaveEditor())return;try{await api('/auth/logout','POST');signedOut();}catch(e){setToast(message(e));}}}><LogOut size={17}/></button></div></header>
    {runtime?.label&&<div className="runtime-banner">{runtime.label}</div>}<main className="page-content">{activePage==='workspace'?<Workspace notify={setToast} isAdmin={account.is_admin}/>:activePage==='accounts'?<Accounts current={account} notify={setToast} onOwnInvalidated={signedOut}/>:activePage==='activity'?<Activity/>:activePage==='planning'?<PlanningPage notify={setToast} initialDay={runtime?.default_delivery_date}/>:<MasterDataPage key={activePage} kind={activePage} notify={setToast}/>}</main><footer className="app-footer"><span>苏咔云图</span><span>团队共用资料 · 北京时间</span></footer></div>{toast&&<div className="toast" role="status"><CheckCircle2 size={18}/>{toast}<button className="icon-button" aria-label="关闭提示" onClick={()=>setToast('')}><X size={14}/></button></div>}</div>;
}

const rootElement=document.getElementById('root');
if(rootElement)createRoot(rootElement).render(<><App/><DiscardConfirmation/></>);

