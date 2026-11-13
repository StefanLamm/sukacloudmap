import {Theme} from '@astryxdesign/core/theme';
import {InternationalizationProvider} from '@astryxdesign/core/i18n';
import zhCN from '@astryxdesign/core/locales/zh-CN.generated.js';
import {VStack, HStack} from '@astryxdesign/core/Stack';
import {Grid} from '@astryxdesign/core/Grid';
import {Section} from '@astryxdesign/core/Section';
import {Text} from '@astryxdesign/core/Text';
import {Heading} from '@astryxdesign/core/Heading';
import {Banner} from '@astryxdesign/core/Banner';
import {AppShell} from '@astryxdesign/core/AppShell';
import {TopNav} from '@astryxdesign/core/TopNav';
import {SideNav, SideNavSection, SideNavItem} from '@astryxdesign/core/SideNav';
import {Table, TableHeader, TableBody, TableRow, TableHeaderCell, TableCell} from '@astryxdesign/core/Table';
import {themes, defaultThemeSlug} from './astryx-themes';
import '@astryxdesign/core/reset.css';
import '@astryxdesign/core/astryx.css';
import React, {FormEvent, useEffect, useId, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {ArrowRight, ArrowUpRight, Check, CheckCircle2, ChevronRight, CircleHelp, ClipboardList, Clock3, Eye, EyeOff, FileText, LayoutDashboard, LoaderCircle, LogOut, MapPin, MoreHorizontal, Plus, RefreshCw, Search, ShieldCheck, Truck, Users, X} from 'lucide-react';
import {Account, api, ApiError, AuditEvent, setCsrf, SharedRecord} from './api';

import {Modal, Spinner, ErrorMessage, Action, Input, Select, TextArea, Field} from './ui';
import {Pagination,PageSize,pageIndex} from './pagination';
import {MasterDataPage, MasterKind} from './master-data';
import {Warehouse, Store, Tags, Route} from 'lucide-react';
import {PlanningPage} from './planning';

import {canLeaveEditor,DiscardConfirmation} from './unsaved';

type Page = 'workspace' | 'accounts' | 'activity' | 'planning' | MasterKind;
const pageNames: Record<Page, string> = {workspace:'共享工作区', planning:'物流线路规划', accounts:'账号管理', activity:'操作记录', warehouses:'仓库管理', stores:'门店管理', vehicles:'车辆管理', quotes:'报价方案'};
function pageFromPath(): Page {const value=location.pathname.slice(1);return Object.hasOwn(pageNames,value) ? value as Page : 'workspace';}
const time = (value: string) => new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(value));
const message = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试。';

function Brand() {
  return <HStack gap={3} vAlign="center"><VStack padding={2} style={{background:'var(--color-accent)',color:'var(--color-on-accent)',borderRadius:'var(--radius-container)'}}><MapPin size={24}/></VStack><VStack gap={0}><Text weight="bold">苏咔云图</Text><Text type="supporting" color="secondary">SUKA CLOUDMAP</Text></VStack></HStack>;
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
  return <Grid columns={{minWidth: 320, max: 2}} gap={0} style={{minHeight:'100dvh',background:'var(--color-background-body)'}}>
    <VStack gap={8} padding={8} style={{background:'var(--color-background-muted)',justifyContent:'space-between',display:'var(--suka-login-story-display)' as React.CSSProperties['display']}}>
      <Brand/>
      <VStack gap={5} maxWidth="32rem">
        <Text type="label" color="accent">门店配送协作平台</Text>
        <Heading level={1}>每一份安排，<br/>都有清晰的起点。</Heading>
        <Text as="p" type="large" color="secondary">连接仓库、门店与团队，让每日配送有序开展。</Text>
        <Section padding={5}>
          <HStack gap={4} hAlign="between" vAlign="center" aria-hidden="true"><Warehouse size={32}/><ArrowRight size={24}/><Truck size={32}/><ArrowRight size={24}/><Store size={32}/></HStack>
          <HStack gap={3} vAlign="center" style={{marginTop:'var(--spacing-5)'}}><CheckCircle2 size={18}/><Text color="secondary">资料同步 · 线路规划 · 团队协作</Text></HStack>
        </Section>
      </VStack>
      <Text type="supporting" color="secondary">苏咔云图 · 内部工作平台</Text>
    </VStack>
    <VStack as="main" gap={6} padding={6} vAlign="center" hAlign="center">
      <VStack gap={5} width="100%" maxWidth="25rem"><Brand/>
        <VStack gap={2}><ShieldCheck size={28} color="var(--color-accent)"/><Heading level={2}>登录工作区</Heading><Text color="secondary">使用您的内部账号，开始今日工作。</Text></VStack>
        <VStack as="form" onSubmit={submit} gap={4}>
          <Field label="账号" inputID="username"><Input id="username" name="username" autoComplete="username" placeholder="请输入账号" required maxLength={64} value={username} onChange={e=>setUsername(e.target.value)} autoFocus/></Field>
          <Field label="密码" inputID="password"><HStack gap={2}><Input id="password" name="password" type={visible?'text':'password'} autoComplete="current-password" placeholder="请输入密码" required maxLength={128} value={password} onChange={e=>setPassword(e.target.value)}/><Action type="button" variant="ghost" aria-label={visible?'隐藏密码':'显示密码'} onClick={()=>setVisible(!visible)}>{visible?<EyeOff size={18}/>:<Eye size={18}/>}</Action></HStack></Field>
          <ErrorMessage text={error}/><Action variant="primary" size="lg" disabled={busy}>{busy?<Spinner/>:<ArrowRight size={18}/>} {busy?'正在登录':'登录'}</Action>
        </VStack>
        <HStack gap={2} vAlign="start"><CircleHelp size={18}/><Text type="supporting" color="secondary">无法登录？请联系管理员开通或重置账号。</Text></HStack>
      </VStack>
    </VStack>
  </Grid>;
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
  return <><HStack gap={3} wrap="wrap" hAlign="between" className="page-heading"><VStack gap={3}><Text type="supporting" color="accent">TEAM WORKSPACE</Text><Heading level={1}>共享工作区</Heading><Text as="p" color="secondary" className="muted">按任务日期安排团队事项，及时跟进完成情况。</Text></VStack><Action className="button primary" onClick={()=>setEditing('new')}><Plus size={17}/> 新建记录</Action></HStack>
    <Banner status="info" title="按日安排，及时跟进" description="管理员可标记任务完成；超过任务日期仍未完成的事项自动归入已超时。"/>
    <Grid columns={{minWidth: 240, max: 3}} gap={4} className="stat-row"><Section padding={4} className="stat-item"><VStack gap={1} style={{minWidth:0}}><Text color="secondary" type="supporting">共享任务</Text><Heading level={2}>{recordAll}<Text type="supporting" color="secondary"> 条</Text></Heading></VStack></Section><Section padding={4} className="stat-item"><VStack gap={1} style={{minWidth:0}}><Text color="secondary" type="supporting">已完成 / 已超时</Text><Heading level={2}>{counts.completed}<Text type="supporting" color="secondary"> / {counts.overdue} 条</Text></Heading></VStack></Section><Section padding={4} className="stat-item"><VStack gap={1} style={{minWidth:0}}><Text color="secondary" type="supporting">最近更新</Text><Text weight="semibold" className="stat-time">{latestRecordAt?time(latestRecordAt):'暂无记录'}</Text></VStack></Section></Grid>
    <Section padding={4} className="panel"><VStack gap={4} style={{minWidth:0}}><HStack gap={3} wrap="wrap" className="workspace-task-toolbar"><HStack gap={3} wrap="wrap" className="tabs" aria-label="任务分类">{([['pending','日期任务'],['completed','已完成任务'],['overdue','已超时'],['all','全部任务']] as const).map(([key,label])=><Action key={key} variant={view===key?'primary':'ghost'} aria-pressed={view===key} onClick={()=>{setView(key);setRecordPage(0);}}>{label}{key==='completed'?' '+counts.completed:key==='overdue'?' '+counts.overdue:''}</Action>)}</HStack>
      {view==='pending'&&<label className="workspace-task-date">任务日期<Input aria-label="筛选任务日期" type="date" required value={taskDate} onChange={e=>{if(e.target.value){setTaskDate(e.target.value);setRecordPage(0);}}}/></label>}</HStack>
      <HStack gap={3} wrap="wrap" hAlign="between" className="panel-toolbar"><HStack gap={3} wrap="wrap" className="panel-title"><Heading level={2}>{view==='pending'?taskDate+' · 团队任务':view==='completed'?'已完成任务':view==='overdue'?'已超时':'全部团队任务'}</Heading><Text className="count">{recordTotal}</Text></HStack><HStack gap={3} wrap="wrap" className="toolbar-actions"><HStack gap={2} vAlign="center" style={{maxWidth:"100%",minWidth:0}} className="search"><Search size={16}/><Input aria-label="搜索共享记录" placeholder="搜索任务" value={query} onChange={e=>{setQuery(e.target.value);setRecordPage(0);}}/></HStack><Action className="icon-button bordered" title="刷新记录" aria-label="刷新记录" onClick={()=>void load()}><RefreshCw size={16}/></Action></HStack></HStack>
    <ErrorMessage text={error}/>{loading?<VStack gap={3} padding={6} hAlign="center" className="empty-state"><Spinner/><Text as="p">正在读取团队任务</Text></VStack>:!records.length?<VStack gap={3} padding={6} hAlign="center" className="empty-state"><VStack gap={3} className="empty-illustration"><FileText size={32}/></VStack><Heading level={3}>暂无匹配任务</Heading><Text as="p">{view==='pending'?'已完成的任务归入已完成任务，过期未完成的任务归入已超时。':'可以切换分类或搜索其他关键词。'}</Text></VStack>:<Grid columns={{minWidth: 240, max: 3}} gap={4} className="record-grid">{records.map(r=><Section padding={4} className={'record-card task-card '+r.status.toLowerCase()} key={r.id}><VStack gap={4} style={{minWidth:0}}><HStack gap={2} hAlign="between"><Text type="supporting" color={r.status==='OVERDUE'?'accent':'secondary'}>{r.status==='COMPLETED'?'已完成':r.status==='OVERDUE'?'已超时':'待完成'}</Text><Action type="button" variant="ghost" aria-label={'编辑任务 '+r.title} onClick={()=>setEditing(r)}><ArrowUpRight size={18}/>编辑</Action></HStack><Heading level={3}>{r.title}</Heading><Text as="p" style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{r.content}</Text><Text type="supporting" color="secondary">任务日期 · {r.task_date}</Text><HStack gap={2} wrap="wrap" hAlign="between"><Text type="supporting">{r.updated_by}</Text><Text type="supporting">{time(r.updated_at)}</Text></HStack>{r.completed_at&&<Text as="p" className="task-completion-note">{r.completed_by} · {time(r.completed_at)} 完成</Text>}{isAdmin&&<Action className="text-button task-complete" disabled={Boolean(busyRecord)} onClick={()=>void complete(r)}>{busyRecord===r.id?<Spinner/>:<CheckCircle2 size={15}/>} {r.status==='COMPLETED'?'恢复待办':'标记已完成'}</Action>}</VStack></Section>)}</Grid>}<Pagination label="共享记录" page={recordPage} total={recordTotal} pageSize={recordSize} disabled={loading} onPageChange={setRecordPage} onPageSizeChange={size=>{setRecordSize(size);setRecordPage(0);}}/></VStack></Section>
    <Text as="p" type="supporting" color="secondary" className="section-footnote"> 团队成员可新增和编辑任务，完成状态由管理员维护。日期以北京时间为准。</Text>
    {editing&&<RecordEditor record={editing} initialDate={view==='pending'?taskDate:beijingDay()} onClose={()=>setEditing(null)} onSave={()=>{setEditing(null);notify('共享任务已保存');void load();}}/>}
  </>;
}

function RecordEditor({record,initialDate,onClose,onSave}: {record:SharedRecord|'new';initialDate:string;onClose:()=>void;onSave:()=>void}){
  const [title,setTitle]=useState(record==='new'?'':record.title);const [content,setContent]=useState(record==='new'?'':record.content);const [taskDate,setTaskDate]=useState(record==='new'?initialDate:record.task_date);
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');
  async function save(e:FormEvent){e.preventDefault();setBusy(true);setError('');try{await api(record==='new'?'/workspace/records':'/workspace/records/'+record.id,record==='new'?'POST':'PATCH',{title,content,task_date:taskDate,...(record==='new'?{}:{expected_revision:record.revision})});onSave();}catch(e){setError(message(e)+(e instanceof ApiError&&e.status===409?' 您的输入已保留，请关闭窗口查看最新记录后再编辑。':''));}finally{setBusy(false);}}
  return <Modal title={record==='new'?'新建共享记录':'编辑共享记录'} description="保存后，所有团队成员均可查看。" onClose={onClose} busy={busy}><VStack as="form" gap={3} onSubmit={save}><label htmlFor="record-date">任务日期</label><Input id="record-date" type="date" required value={taskDate} onChange={e=>setTaskDate(e.target.value)}/><label htmlFor="record-title">标题</label><Input id="record-title" required maxLength={120} autoFocus value={title} onChange={e=>setTitle(e.target.value)} placeholder="例如：今日配送注意事项"/><label htmlFor="record-content">内容</label><TextArea id="record-content" rows={6} required maxLength={5000} value={content} onChange={e=>setContent(e.target.value)} placeholder="填写需要团队了解的具体安排"/><ErrorMessage text={error}/><HStack gap={3} wrap="wrap" hAlign="end" className="modal-actions"><Action type="button" className="button secondary" onClick={onClose} disabled={busy}>取消</Action><Action className="button primary" disabled={busy}>{busy&&<Spinner/>} 保存记录</Action></HStack></VStack></Modal>;
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
  return <><HStack gap={3} wrap="wrap" hAlign="between" className="page-heading"><VStack gap={3}><Text type="supporting" color="accent">ACCESS MANAGEMENT</Text><Heading level={1}>账号管理</Heading><Text as="p" color="secondary" className="muted">管理内部成员的访问权限与账号状态。</Text></VStack><Action className="button primary" onClick={() => setAction({type:'create'})}><Plus size={17}/> 新建账号</Action></HStack>
    <Grid columns={{minWidth: 240, max: 3}} gap={4} className="stat-row account-stats"><Section padding={4} className="stat-item"><VStack gap={1} style={{minWidth:0}}><Text color="secondary" type="supporting">内部账号</Text><Heading level={2}>{items.length}<Text type="supporting" color="secondary"> 位</Text></Heading></VStack></Section><Section padding={4} className="stat-item"><VStack gap={1} style={{minWidth:0}}><Text color="secondary" type="supporting">已启用</Text><Heading level={2}>{items.filter(a => a.active).length}<Text type="supporting" color="secondary"> 位</Text></Heading></VStack></Section><Section padding={4} className="stat-item"><VStack gap={1} style={{minWidth:0}}><Text color="secondary" type="supporting">管理员</Text><Heading level={2}>{items.filter(a => a.is_admin && a.active).length}<Text type="supporting" color="secondary"> 位</Text></Heading></VStack></Section></Grid>
    <Section padding={4} className="panel"><VStack gap={4} style={{minWidth:0}}><HStack gap={3} wrap="wrap" hAlign="between" className="panel-toolbar"><HStack gap={3} wrap="wrap" className="tabs" aria-label="账号状态">{[['all','全部账号'],['active','已启用'],['inactive','已停用']].map(([key,label]) => <Action key={key} variant={filter===key?'primary':'ghost'} aria-pressed={filter===key} onClick={() => {setFilter(key);setPage(0);}}>{label}</Action>)}</HStack><HStack gap={3} wrap="wrap" className="toolbar-actions"><HStack gap={2} vAlign="center" style={{maxWidth:"100%",minWidth:0}} className="search"><Search size={16}/><Input aria-label="搜索账号或姓名" placeholder="搜索账号或姓名" value={query} onChange={e => {setQuery(e.target.value);setPage(0);}}/></HStack><Action className="icon-button bordered" aria-label="刷新账号" onClick={() => void load()}><RefreshCw size={16}/></Action></HStack></HStack>
    <ErrorMessage text={error}/><VStack style={{minWidth: 0}} className="table-scroll"><Table density="balanced" dividers="rows" hasHover className="accounts-table"><TableHeader><TableRow><TableHeaderCell>成员</TableHeaderCell><TableHeaderCell>角色</TableHeaderCell><TableHeaderCell>状态</TableHeaderCell><TableHeaderCell>最近登录</TableHeaderCell><TableHeaderCell className="action-column">操作</TableHeaderCell></TableRow></TableHeader><TableBody>{visible.map(a => <TableRow key={a.id}><TableCell><HStack gap={3} wrap="wrap" className="member"><VStack gap={1}><Text weight="semibold">{a.display_name}{a.id===current.id && <Text type="supporting" color="accent"> · 我</Text>}</Text><Text type="supporting" color="secondary">{a.username}</Text></VStack></HStack></TableCell><TableCell><Text style={{display:"inline-flex",gap:"var(--spacing-1)",alignItems:"center"}} className={'role-tag '+(a.is_admin?'admin-role':'')}>{a.is_admin && <ShieldCheck size={12}/>} {a.is_admin?'管理员':'业务人员'}</Text></TableCell><TableCell><Text className={'status '+(!a.active?'inactive':'')}>{a.active?'已启用':'已停用'}</Text></TableCell><TableCell className="muted">{a.last_login_at ? time(a.last_login_at) : '尚未登录'}</TableCell><TableCell className="action-column"><Action className="text-button" onClick={() => setAction({type:'reset',account:a})}>重置密码</Action><Action className={'text-button '+(a.active?'danger-text':'')} onClick={() => setAction({type:'toggle',account:a})}>{a.active?'停用':'启用'}</Action><Action className="text-button danger-text" disabled={a.id===current.id||a.is_admin&&a.active&&items.filter(item=>item.is_admin&&item.active).length<=1} title={a.id===current.id?'不能删除当前登录账号':undefined} onClick={()=>setAction({type:'delete',account:a})}>删除</Action></TableCell></TableRow>)}</TableBody></Table></VStack>{loading ? <VStack gap={3} padding={6} hAlign="center" className="table-empty"><Spinner/> 正在读取账号</VStack> : !visible.length && <VStack gap={3} padding={6} hAlign="center" className="table-empty">没有匹配的账号</VStack>}<HStack gap={3} wrap="wrap" hAlign="between" className="table-footer">共 {matching.length} 个账号<Text><ShieldCheck size={13}/> 仅管理员可管理账号</Text></HStack><Pagination label="账号" page={currentPage} total={matching.length} pageSize={pageSize} disabled={loading} onPageChange={setPage} onPageSizeChange={size=>{setPageSize(size);setPage(0);}}/></VStack></Section>
    <HStack gap={3} wrap="wrap" className="info-note"><ShieldCheck size={19}/><VStack gap={3}><Text weight="semibold">账号权限说明</Text><Text as="p">业务人员可使用共享工作区。停用账号或重置密码后，该账号需重新登录；已保存的团队资料会保留。</Text></VStack></HStack>
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
  return <Modal title={title} description={target ? `${target.display_name} · ${target.username}` : '创建后即可使用此账号登录共享工作区。'} onClose={onClose} busy={busy}><VStack as="form" gap={3} onSubmit={submit}>
    {action.type==='create' && <><label htmlFor="account-name">姓名</label><Input id="account-name" autoFocus required maxLength={80} value={name} onChange={e=>setName(e.target.value)} placeholder="请输入成员姓名"/><label htmlFor="account-username">登录账号</label><Input id="account-username" required minLength={3} maxLength={64} pattern="[a-zA-Z0-9][a-zA-Z0-9._\-]{2,63}" autoComplete="off" value={username} onChange={e=>setUsername(e.target.value)} placeholder="3至64位字母、数字或 . _ -"/><label htmlFor="account-role">角色</label><Select id="account-role" value={String(role)} onChange={e=>setRole(e.target.value==='true')}><option value="false">业务人员</option><option value="true">管理员</option></Select></>}
    {!toggle&&!deleting && <><label htmlFor="new-password">{action.type==='reset'?'新密码':'初始密码'}</label><Input id="new-password" type="password" required minLength={12} maxLength={128} autoComplete="new-password" value={password} onChange={e=>setPassword(e.target.value)} placeholder="12至128位密码" autoFocus={action.type==='reset'}/><label htmlFor="repeat-password">确认密码</label><Input id="repeat-password" type="password" required minLength={12} maxLength={128} autoComplete="new-password" value={repeat} onChange={e=>setRepeat(e.target.value)} placeholder="再次输入密码"/></>}
    {deleting&&<Text as="p" className="confirm-text">删除后此账号将从账号列表移除，所有登录会话立即失效。其历史团队资料及操作记录会保留，原账号名不能重复使用。</Text>}{toggle && <Text as="p" className="confirm-text">{target!.active?'停用后，该成员的当前会话将立即失效，无法继续访问工作区。已有共享记录会保留。':'启用后，该成员可以使用已有密码重新登录。'}</Text>}
    {action.type==='reset' && <Text as="p" color="secondary" className="field-hint">密码重置后，原密码和所有已登录会话将失效。</Text>}<ErrorMessage text={error}/><HStack gap={3} wrap="wrap" hAlign="end" className="modal-actions"><Action className="button secondary" type="button" onClick={onClose} disabled={busy}>取消</Action><Action className={'button '+(deleting||toggle && target!.active?'danger':'primary')} disabled={busy}>{busy && <Spinner/>}{action.type==='create'?'创建账号':title}</Action></HStack></VStack></Modal>;
}

export function Activity(){
  const [items,setItems]=useState<AuditEvent[]>([]);const [error,setError]=useState('');const [loading,setLoading]=useState(true);
  const [page,setPage]=useState(0);const [pageSize,setPageSize]=useState<PageSize>(10);const [total,setTotal]=useState(0);const generation=useRef(0);
  async function load(){const request=++generation.current;setLoading(true);try{const result=await api<{items:AuditEvent[];total:number}>('/audit-events?offset='+page*pageSize+'&limit='+pageSize);if(request===generation.current){setItems(result.items);setTotal(result.total??result.items.length);setError('');}}catch(e){if(request===generation.current)setError(message(e));}finally{if(request===generation.current)setLoading(false);}}
  useEffect(()=>{void load();return()=>{generation.current++;};},[page,pageSize]);
  return <><HStack gap={3} wrap="wrap" hAlign="between" className="page-heading"><VStack gap={3}><Text type="supporting" color="accent">WORKSPACE ACTIVITY</Text><Heading level={1}>操作记录</Heading><Text as="p" color="secondary" className="muted">查看团队的登录、账号及共享资料变更。</Text></VStack><Action className="button secondary" disabled={loading} onClick={()=>void load()}><RefreshCw size={16}/> 刷新记录</Action></HStack><Section padding={4} className="panel"><VStack gap={4} style={{minWidth:0}}><HStack gap={3} wrap="wrap" hAlign="between" className="panel-toolbar"><HStack gap={3} wrap="wrap" className="panel-title"><Heading level={2}>操作记录</Heading><Text className="count">{total}</Text></HStack><Text color="secondary" className="muted small">北京时间</Text></HStack><ErrorMessage text={error}/>{loading?<VStack gap={3} padding={6} hAlign="center" className="table-empty"><Spinner/> 正在读取操作记录</VStack>:<VStack gap={3} className="activity-list">{items.map(e=><HStack gap={3} wrap="wrap" className="activity-item" key={e.id}><Text className="activity-icon"><Clock3 size={16}/></Text><VStack gap={3}><Text weight="semibold">{e.summary}</Text><Text>{e.actor}</Text></VStack><time>{time(e.occurred_at)}</time></HStack>)}{!items.length&&<VStack gap={3} padding={6} hAlign="center" className="table-empty">暂无操作记录</VStack>}</VStack>}<Pagination label="操作记录" page={page} total={total} pageSize={pageSize} disabled={loading} onPageChange={setPage} onPageSizeChange={size=>{setPageSize(size);setPage(0);}}/></VStack></Section></>;
}

function App(){
  const [runtime,setRuntime]=useState<{test_fixture:boolean;label:string;default_delivery_date?:string}|null>(null);
  useEffect(()=>{api<{test_fixture:boolean;label:string;default_delivery_date?:string}>('/runtime').then(r=>{setRuntime(r);document.title=r.label?'苏咔云图 · 测试版':'苏咔云图';}).catch(()=>{});},[]);
  const [account,setAccount]=useState<Account|null>(null);const [loading,setLoading]=useState(true);const [bootError,setBootError]=useState('');
  const [page,setPage]=useState<Page>(pageFromPath);const [toast,setToast]=useState('');const [navOpen,setNavOpen]=useState(false);
  function signedOut(){setAccount(null);setCsrf('');history.replaceState({},'', '/login');setPage('workspace');}
  async function restore(){setLoading(true);setBootError('');try{const result=await api<{account:Account;csrf_token:string}>('/auth/me');setCsrf(result.csrf_token);setAccount(result.account);if(location.pathname==='/login')history.replaceState({},'','/');}catch(e){if(e instanceof ApiError&&e.status===401)signedOut();else setBootError(message(e));}finally{setLoading(false);}}
  useEffect(()=>{void restore();const expired=()=>signedOut();window.addEventListener('suka-session-expired',expired);return()=>window.removeEventListener('suka-session-expired',expired);},[]);
  useEffect(()=>{if(!toast)return;const timer=setTimeout(()=>setToast(''),4500);return()=>clearTimeout(timer);},[toast]);
  useEffect(()=>{const onPop=async()=>{if(!await canLeaveEditor()){history.pushState({},'',page==='workspace'?'/':'/'+page);return;}setPage(pageFromPath());};window.addEventListener('popstate',onPop);return()=>window.removeEventListener('popstate',onPop);},[page]);
  async function navigate(next:Page){if(next!==page&&!await canLeaveEditor())return;setPage(next);setNavOpen(false);history.pushState({},'',next==='workspace'?'/':'/'+next);}
  if(loading)return <VStack gap={5} padding={8} hAlign="center" vAlign="center" minHeight="100dvh"><Brand/><Spinner/><Text>正在打开工作区</Text></VStack>;
  if(bootError)return <VStack gap={5} padding={8} hAlign="center" vAlign="center" minHeight="100dvh"><Brand/><ErrorMessage text={bootError}/><Action onClick={()=>void restore()}>重新连接</Action></VStack>;
  if(!account)return <>{runtime?.label&&<Banner status="warning" title={runtime.label}/>}<Login onLogin={a=>{setAccount(a);history.replaceState({},'','/');setPage('workspace');}}/></>;
  const activePage:Page=!account.is_admin&&['accounts','activity'].includes(page)?'workspace':page;
  const navItem=(key:Page,icon:typeof Warehouse)=><SideNavItem key={key} label={pageNames[key]} icon={icon} href={key==='workspace'?'/':'/'+key} isSelected={activePage===key} onClick={e=>{e.preventDefault();void navigate(key);}}/>;
  return <AppShell contentPadding={0} variant="section" mobileNav={{isOpen:navOpen,onOpenChange:setNavOpen}}
    sideNav={<SideNav aria-label="主导航" header={<Section padding={4} variant="transparent"><Text type="supporting" color="secondary">苏咔配送团队</Text></Section>}
      footer={<VStack gap={2} padding={4}><HStack gap={2}><ShieldCheck size={16}/><Text type="supporting">内部工作平台</Text></HStack><Text type="supporting" color="secondary">账号问题请联系管理员</Text><Text type="supporting" color="secondary">v0.16.0</Text></VStack>}>
      <SideNavSection title="工作空间">{navItem('workspace',LayoutDashboard)}{navItem('planning',Route)}</SideNavSection>
      <SideNavSection title="基础资料">{navItem('warehouses',Warehouse)}{navItem('stores',Store)}{navItem('vehicles',Truck)}{navItem('quotes',Tags)}</SideNavSection>
      {account.is_admin&&<SideNavSection title="团队管理">{navItem('accounts',Users)}{navItem('activity',Clock3)}</SideNavSection>}
    </SideNav>}
    topNav={<TopNav label="顶部导航" heading={<Brand/>} startContent={<Text color="secondary">{pageNames[activePage]}</Text>} endContent={<HStack gap={3} vAlign="center"><Text>{account.display_name}</Text><Action type="button" variant="ghost" title="退出登录" aria-label="退出登录" onClick={async()=>{if(!await canLeaveEditor())return;try{await api('/auth/logout','POST');signedOut();}catch(e){setToast(message(e));}}}><LogOut size={18}/></Action></HStack>}/>}

    banner={runtime?.label?<Banner status="warning" title={runtime.label}/>:undefined}>
      <VStack gap={5} padding={5} style={{minWidth:0,maxWidth:'110rem',marginInline:'auto'}}>
        {activePage==='workspace'?<Workspace notify={setToast} isAdmin={account.is_admin}/>:activePage==='accounts'?<Accounts current={account} notify={setToast} onOwnInvalidated={signedOut}/>:activePage==='activity'?<Activity/>:activePage==='planning'?<PlanningPage notify={setToast} initialDay={runtime?.default_delivery_date}/>:<MasterDataPage key={activePage} kind={activePage} notify={setToast}/>}
        <HStack as="footer" hAlign="between" gap={3} wrap="wrap"><Text type="supporting" color="secondary">苏咔云图</Text><Text type="supporting" color="secondary">团队共用资料 · 北京时间</Text></HStack>
      </VStack>
      {toast&&<Section padding={3} role="status" style={{position:'fixed',bottom:'var(--spacing-5)',right:'var(--spacing-5)',maxWidth:'calc(100vw - var(--spacing-10))',zIndex:100}}><HStack gap={3}><CheckCircle2 size={18}/><Text>{toast}</Text><Action type="button" variant="ghost" aria-label="关闭提示" onClick={()=>setToast('')}><X size={16}/></Action></HStack></Section>}
  </AppShell>;
}

const rootElement=document.getElementById('root');
if(rootElement)createRoot(rootElement).render(<InternationalizationProvider locale="zh-CN" messages={{'zh-CN':zhCN}}><Theme theme={themes[defaultThemeSlug]} mode="light"><App/><DiscardConfirmation/></Theme></InternationalizationProvider>);

