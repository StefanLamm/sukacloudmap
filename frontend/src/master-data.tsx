import React, {FormEvent, useEffect, useRef, useState} from 'react';
import {Download, FileSpreadsheet, History, MapPin, Plus, RefreshCw, Trash2, Upload} from 'lucide-react';
import {api, ApiError} from './api';
import {ErrorMessage, Modal, Spinner, Input, Select} from './ui';
import {VStack} from '@astryxdesign/core/VStack';
import {HStack} from '@astryxdesign/core/HStack';
import {Grid} from '@astryxdesign/core/Grid';
import {Section} from '@astryxdesign/core/Section';
import {Text} from '@astryxdesign/core/Text';
import {Heading} from '@astryxdesign/core/Heading';
import {Button} from '@astryxdesign/core/Button';
import {Link} from '@astryxdesign/core/Link';
import {Badge} from '@astryxdesign/core/Badge';
import {Banner} from '@astryxdesign/core/Banner';
import {Table,TableHeader,TableHeaderCell,TableBody,TableRow,TableCell} from '@astryxdesign/core/Table';
import {LocationMap} from './location-map';
import {Pagination,PageSize} from './pagination';
import {locationFields} from './location-value';
import type {LocationValue} from './location-value';
import {RegionPicker,RegionSurchargeEditor,RegionChoice,RegionSurcharge} from './region-picker';

export type MasterKind = 'warehouses' | 'stores' | 'vehicles' | 'quotes';
export type QuoteVersion = {id:string; quote_id:string; version:number; name:string; mode:'KM'|'BASE_STOPS'|'KM_REGION'|'BASE_STOPS_REGION'; region_surcharge?:RegionSurcharge; per_km:string|null; base_price:string|null; included_stops:number|null; extra_stop_price:string|null; effective_from:string; signature:string; created_at:string};
export type Row = {location_status?:'LINKED'|'PENDING'|'MANUAL'; location_note?:string; id:string; revision:number; code?:string; name?:string; plate?:string; latest_version?:QuoteVersion; effective_version?:QuoteVersion|null; dedicated_quote_version?:QuoteVersion|null; road_profile_missing?:string[]; dedicated_config_missing?:string[]; [key:string]:unknown};
export type Catalog = Record<MasterKind, Row[]> & {delivery_date?:string; master_revision:number};
type Issue = {field:string; message:string; row?:number};
type Field = {key:string; label:string; type?:'text'|'integer'|'decimal'|'time'|'date'|'datetime-local'|'select'|'textarea'; required?:boolean; options?:[string,string][]; group?:string; hint?:string};
type Preview = {preview_id:string; can_commit:boolean; row_count:number; valid_rows:number; error_count:number; create_count:number; update_count:number; errors:Issue[]; rows:{source_row:number;action:string;code:string;name:string}[]};
const names:Record<MasterKind,string>={warehouses:'仓库',stores:'门店',vehicles:'车辆',quotes:'报价方案'};
const descriptions:Record<MasterKind,string>={warehouses:'维护仓库资料、地址与关联位置。',stores:'维护门店位置、经营时间、出货仓与运输报价。',vehicles:'维护车牌、默认归属仓、容量与车辆资料。',quotes:'维护可复用报价，保留每次修改的生效版本。'};
const emptyCatalog:Catalog={warehouses:[],stores:[],vehicles:[],quotes:[],delivery_date:'',master_revision:0};
const text=(value:unknown)=>value===null||value===undefined||value===''?'—':String(value);
export const dateToday=()=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
const errorText=(e:unknown)=>e instanceof Error?e.message:'操作未完成，请重试。';
const money=(value:unknown)=>value===null||value===undefined?'未配置':`${value} 元`;
export function quoteDescription(v:QuoteVersion|null|undefined){if(!v)return '该日期未生效';const base=v.mode.startsWith('KM')?`${v.per_km} 元 / 公里`:`起送 ${v.base_price} 元 · 含 ${v.included_stops} 店 · 串店 ${v.extra_stop_price} 元`;return base+(v.mode.endsWith('_REGION')?` · 特殊地区${v.region_surcharge?.billing_mode==='PER_STORE'?'每家门店':v.region_surcharge?.billing_mode==='PER_REGION'?'每区县':'一次性'} +${v.region_surcharge?.amount??'0'} 元`:'');}
const issueList=(error:unknown):Issue[]=>error instanceof ApiError&&Array.isArray(error.details)?error.details as Issue[]:[];

function fields(kind:MasterKind, catalog:Catalog):Field[]{
  const coded:Field[]=[{key:'code',label:'稳定编号',required:true},{key:'name',label:kind==='quotes'?'方案名称':names[kind]+'名称',required:true}];
  const warehouses=catalog.warehouses.map(w=>[w.id,`${w.code} · ${w.name}`] as [string,string]);
  const quotes=catalog.quotes.map(q=>[q.id,`${q.code} · ${q.name}`] as [string,string]);
  const location:Field[]=[{key:'longitude',label:'经度',type:'decimal',group:'位置与坐标'},{key:'latitude',label:'纬度',type:'decimal'},
    {key:'crs',label:'坐标系',type:'select',required:true,options:[['GCJ02','GCJ02 · 高德坐标'],['WGS84','WGS84 · GPS坐标'],['BD09','BD09 · 百度坐标']]},
    {key:'location_source',label:'位置来源',required:true,hint:'记录地图校正、人工录入或资料来源。'}];
  if(kind==='warehouses')return [...coded,{key:'address',label:'地址',required:true},{key:'departure_time',label:'默认最早发车时间',type:'time',required:true},...location];
  if(kind==='stores')return [...coded,{key:'province',label:'省份'},{key:'city',label:'城市'},{key:'district',label:'区县'},{key:'region_code',label:'行政区编码'},{key:'warehouse_id',label:'出货仓库',type:'select',required:true,options:warehouses},
    {key:'address',label:'门店地址',required:true},{key:'business_open',label:'营业开始',type:'time',required:true,group:'经营时间与收货'},
    {key:'business_close',label:'营业结束',type:'time',required:true},{key:'close_day_offset',label:'营业跨日',type:'select',required:true,options:[['0','当日闭店'],['1','次日闭店（明确跨日）']]},
    {key:'unload_minutes',label:'卸货时长（分钟）',type:'integer',required:true},
    {key:'dedicated_quote_id',label:'专车报价方案',type:'select',options:quotes,group:'运输报价'},{key:'common_unit_price',label:'共配每件单价（元）',type:'decimal',hint:'留空表示共配不可用，明确填写0为有效零价。'},...location];
  if(kind==='vehicles')return [{key:'plate',label:'完整车牌',required:true},{key:'warehouse_id',label:'默认归属仓',type:'select',required:true,options:warehouses},
    {key:'max_pieces',label:'最大可装件数',type:'integer',required:true},{key:'available',label:'默认可用性',type:'select',required:true,options:[['true','可用'],['false','不可用']]},
    {key:'size',label:'车型大小',type:'select',group:'货车道路资料',options:[['1','微型'],['2','轻型'],['3','中型'],['4','重型']]},
    {key:'cartype',label:'车辆类型',type:'select',options:[['0','普通货车'],['1','纯电动货车'],['2','插电混动货车']]},
    {key:'plate_color',label:'车牌颜色',type:'select',options:[['BLUE','蓝牌'],['YELLOW','黄牌'],['GREEN','绿牌'],['YELLOW_GREEN','黄绿牌'],['WHITE','白牌'],['BLACK','黑牌'],['OTHER','其他']]},
    {key:'length_m',label:'车长（米）',type:'decimal'},{key:'width_m',label:'车宽（米）',type:'decimal'},{key:'height_m',label:'车高（米）',type:'decimal'},
    {key:'rated_load_t',label:'核定载重（吨）',type:'decimal'},{key:'full_gross_t',label:'满载总重（吨）',type:'decimal'},{key:'axles',label:'轴数（个）',type:'integer'}];
  return [...coded,{key:'mode',label:'计费模式',type:'select',required:true,options:[['KM','按公里'],['BASE_STOPS','起送加串店'],['KM_REGION','按公里 + 特殊地区附加费'],['BASE_STOPS_REGION','起送加串店 + 特殊地区附加费']]},
    {key:'per_km',label:'每公里单价（元）',type:'decimal',required:true,group:'计费参数'},
    {key:'base_price',label:'起送价（元）',type:'decimal',required:true},{key:'included_stops',label:'起送价包含门店数',type:'integer',required:true},
    {key:'extra_stop_price',label:'串店费（元）',type:'decimal',required:true}];
}

export function MasterDataPage({kind,notify}:{kind:MasterKind;notify:(s:string)=>void}){
  const [data,setData]=useState<Catalog>(emptyCatalog);
  const [query,setQuery]=useState(''); const [page,setPage]=useState(0);const [pageSize,setPageSize]=useState<PageSize>(10);
  const [loading,setLoading]=useState(true); const [error,setError]=useState('');
  const [view,setView]=useState<'list'|'map'>('list');
  const [locationFilter,setLocationFilter]=useState('all');
  const [selected,setSelected]=useState<Set<string>>(new Set());
  const [deleting,setDeleting]=useState<Row[]|null>(null);
  const [locating,setLocating]=useState(false);
  const [action,setAction]=useState<{type:'edit'|'location'|'history';row:Row|'new'}|null>(null);
  const [importing,setImporting]=useState(false);const [bulkEditing,setBulkEditing]=useState(false);const [exporting,setExporting]=useState(false);
  const [regionFilter,setRegionFilter]=useState<RegionChoice>({province:'',city:'',district:'',region_code:''});
  const generation=useRef(0); const hasLocations=kind==='warehouses'||kind==='stores';
  async function load(quiet=false){
    const request=++generation.current;
    if(!quiet){setLoading(true);setError('');}
    try{
      const result=await api<Catalog>('/master-data/management');
      if(request===generation.current){
        setData(result);
        setSelected(previous=>new Set([...previous].filter(id=>result[kind].some(r=>r.id===id))));
      }
    }catch(e){if(request===generation.current)setError(errorText(e));}
    finally{if(request===generation.current&&!quiet)setLoading(false);}
  }
  useEffect(()=>{
    setPage(0);setSelected(new Set());setLocationFilter('all');setRegionFilter({province:'',city:'',district:'',region_code:''});setAction(null);setDeleting(null);setImporting(false);
    void load();return()=>{generation.current++;};
  },[kind]);
  const hasPending=hasLocations&&data[kind].some(r=>r.location_status==='PENDING');
  useEffect(()=>{
    if(!hasPending||loading)return;
    let stopped=false;let timer:number;
    async function poll(){await load(true);if(!stopped)timer=window.setTimeout(()=>void poll(),3000);}
    timer=window.setTimeout(()=>void poll(),3000);
    return()=>{stopped=true;window.clearTimeout(timer);};
  },[kind,hasPending,loading]);
  const rows=data[kind].filter(r=>(locationFilter==='all'||r.location_status===locationFilter)&&(kind!=='stores'||(!regionFilter.province||r.province===regionFilter.province)&&(!regionFilter.city||String(r.city??'').replace(/市$/,'')===regionFilter.city.replace(/市$/,''))&&(!regionFilter.district||r.district===regionFilter.district))&&
    [r.code,r.name,r.plate,r.province,r.city,r.district,r.warehouse_name,r.address].some(v=>String(v??'').toLowerCase().includes(query.toLowerCase())));
  const count=Math.ceil(rows.length/pageSize);const current=Math.min(page,Math.max(count-1,0));const visible=rows.slice(current*pageSize,current*pageSize+pageSize);
  const selectedRows=data[kind].filter(r=>selected.has(r.id));
  const allVisible=visible.length>0&&visible.every(r=>selected.has(r.id));
  const located=data[kind].filter(r=>r.longitude!==null&&r.longitude!==undefined&&r.latitude!==null&&r.latitude!==undefined).length;
  function toggle(id:string){setSelected(previous=>{const next=new Set(previous);if(next.has(id))next.delete(id);else next.add(id);return next;});}
  function togglePage(){setSelected(previous=>{const next=new Set(previous);visible.forEach(r=>{if(allVisible)next.delete(r.id);else next.add(r.id);});return next;});}
  function selectAllMatching(){setSelected(previous=>new Set([...previous,...rows.map(row=>row.id)]));}
  function finish(message='资料已保存'){setAction(null);setImporting(false);notify(message);void load();}
  async function retryLocations(){
    setLocating(true);setError('');
    try{
      const result=await api<{queued:number}>('/master-data/'+kind+'/locate','POST',{
        items:selectedRows.map(r=>({id:r.id,expected_revision:r.revision}))});
      notify(result.queued?`已安排 ${result.queued} 条地址重新自动关联`:'所选资料已有位置，无需重新关联');
      await load();
    }catch(e){setError(errorText(e));}finally{setLocating(false);}
  }
  async function exportSelected(){setExporting(true);setError('');try{const {download}=await import('./store-export');await download(selectedRows);notify(`已导出 ${selectedRows.length} 家门店`);}catch(e){setError(errorText(e));}finally{setExporting(false);}}
  function locationStatus(row:Row){
    return <Text type="supporting" color={row.location_status==='MANUAL'?'accent':'secondary'}>
      {row.location_status==='LINKED'?'已关联位置':row.location_status==='PENDING'?'自动关联中':'需手动关联'}
    </Text>;
  }
  return <VStack gap={6} style={{minWidth:0}}>
    <HStack gap={4} wrap="wrap" hAlign="between" vAlign="center">
      <VStack gap={1}>
        <Text type="supporting" color="accent">基础资料 / MASTER DATA</Text>
        <Heading level={1}>{names[kind]}{kind==='quotes'?'':'管理'}</Heading>
        <Text color="secondary">{descriptions[kind]}</Text>
      </VStack>
      <HStack gap={2} wrap="wrap">
        <Link href={'/api/master-data/templates/'+kind+'.xlsx'} download isStandalone><HStack gap={1.5} vAlign="center"><Download size={16}/><Text type="label" color="inherit">下载模板</Text></HStack></Link>
        <Button label="批量导入" icon={<Upload size={16}/>} onClick={()=>setImporting(true)}/>
        <Button label={'新增'+names[kind]} variant="primary" isDisabled={loading} icon={<Plus size={16}/>} onClick={()=>setAction({type:'edit',row:'new'})}/>
      </HStack>
    </HStack>
    <Grid columns={{minWidth:180,max:3}} gap={4}>
      <VStack gap={1}><Text type="supporting">{names[kind]}总数</Text><HStack gap={1} vAlign="center"><Heading level={2}>{data[kind].length}</Heading><Text type="supporting">{kind==='vehicles'?'辆':kind==='quotes'?'份':kind==='warehouses'?'个':'家'}</Text></HStack></VStack>
      <VStack gap={1}><Text type="supporting">{hasLocations?'已关联位置':kind==='vehicles'?'车辆资料齐全':'已关联门店'}</Text><HStack gap={1} vAlign="center"><Heading level={2}>{hasLocations?located:kind==='vehicles'?data.vehicles.filter(v=>v.road_profile_complete).length:data.stores.filter(s=>s.dedicated_quote_id!==null).length}</Heading><Text type="supporting">{kind==='vehicles'?'辆':kind==='warehouses'?'个':'家'}</Text></HStack></VStack>
      <VStack gap={1}><Text type="supporting">{kind==='vehicles'?'默认可用':kind==='quotes'?'计费模式':kind==='warehouses'?'关联门店':'关联仓库'}</Text><HStack gap={1} vAlign="center"><Heading level={2}>{kind==='vehicles'?data.vehicles.filter(v=>v.available).length:kind==='quotes'?new Set(data.quotes.map(q=>q.latest_version?.mode)).size:kind==='warehouses'?data.stores.length:new Set(data.stores.map(s=>s.warehouse_id)).size}</Heading><Text type="supporting">{kind==='vehicles'?'辆':kind==='quotes'?'种':kind==='warehouses'?'家':'个'}</Text></HStack></VStack>
    </Grid>
    {selectedRows.length>0&&<Section variant="muted" padding={3} role="region" aria-label="已选基础资料">
      <VStack gap={3}>
        <Text type="label">已选择 {selectedRows.length} 条{names[kind]}（含其他页）</Text>
        <HStack gap={2} wrap="wrap">
          {kind==='stores'&&<><Button label="批量修改" isDisabled={loading||selectedRows.length>10000} onClick={()=>setBulkEditing(true)}/><Button label="导出所选门店" isDisabled={loading||exporting||selectedRows.length>10000} icon={exporting?<Spinner/>:<Download size={16}/>} onClick={()=>void exportSelected()}/></>}
          {hasLocations&&<Button label="重试未关联地址" isDisabled={loading||locating||selectedRows.length>10000} icon={locating?<Spinner/>:<MapPin size={16}/>} onClick={()=>void retryLocations()}/>}
          <Button label="批量删除" variant="destructive" isDisabled={loading||locating||selectedRows.length>10000} icon={<Trash2 size={16}/>} onClick={()=>setDeleting([...selectedRows])}/>
          <Button label="清空选择" variant="ghost" isDisabled={locating} onClick={()=>setSelected(new Set())}/>
        </HStack>
        {selectedRows.length>10000&&<Text type="supporting">每次最多操作10000条，请减少选择。</Text>}
      </VStack>
    </Section>}
    <Section padding={0} style={{minWidth:0,borderRadius:'var(--radius-container)',overflow:'hidden'}}>
      <VStack gap={0} style={{minWidth:0}}>
        <VStack gap={4} padding={4}>
          <HStack gap={2} hAlign="between" vAlign="center" wrap="wrap">
            <HStack gap={2} vAlign="center" style={{flexShrink:0}}><Heading level={2}>{names[kind]}资料</Heading><Badge label={rows.length}/></HStack>
            <HStack gap={2} wrap="wrap" vAlign="center" hAlign="end" style={{flex:'1 1 34rem',minWidth:0}}>
              {hasLocations&&<>
                <Select aria-label="筛选关联位置状态" style={{width:'auto',flex:'0 1 12rem',minWidth:'10rem'}} value={locationFilter} onChange={e=>{setLocationFilter(e.target.value);setPage(0);}}>
                  <option value="all">全部位置状态</option><option value="PENDING">自动关联中</option><option value="MANUAL">需手动关联</option><option value="LINKED">已关联位置</option>
                </Select>
                <HStack gap={1}>
                  <Button label="列表" size="sm" variant={view==='list'?'secondary':'ghost'} aria-pressed={view==='list'} onClick={()=>setView('list')}/>
                  <Button label="地图" size="sm" variant={view==='map'?'secondary':'ghost'} aria-pressed={view==='map'} icon={<MapPin size={16}/>} onClick={()=>setView('map')}/>
                </HStack>
              </>}
              <Input aria-label="搜索基础资料" style={{width:'auto',flex:'1 1 16rem',minWidth:'10rem',maxWidth:'22rem'}} placeholder="搜索编号、名称、仓库或地址" value={query} onChange={e=>{setQuery(e.target.value);setPage(0);}}/>
              <Button label="刷新基础资料" variant="ghost" isIconOnly icon={<RefreshCw size={16}/>} onClick={()=>void load()}/>
            </HStack>
          </HStack>
          <ErrorMessage text={error}/>
          {kind==='stores'&&<RegionPicker label="筛选" value={regionFilter} onChange={value=>{setRegionFilter(value);setPage(0);}}/>}
        </VStack>
        {view==='map'&&hasLocations?<Section padding={3}><LocationMap points={rows.map(r=>({id:r.id,name:String(r.name),code:String(r.code),address:r.address,longitude:r.longitude,latitude:r.latitude,crs:r.crs}))} label={names[kind]+'关联位置'}/></Section>:<>
          <VStack isScrollable width="100%" style={{minWidth:0}}>
            <Table density="balanced" hasHover dividers="rows" style={{minWidth:kind==='stores'?'62rem':'48rem'}}>
              <TableHeader><TableRow>
                <TableHeaderCell><HStack gap={2} vAlign="center"><Input type="checkbox" aria-label={'选择本页全部'+names[kind]} checked={allVisible} disabled={loading||!visible.length} onChange={togglePage}/><Button type="button" label={'全选所有（'+rows.length+'条匹配）'} aria-label={'全选所有匹配'+names[kind]+'（'+rows.length+'条）'} size="sm" variant="ghost" isDisabled={loading||!rows.length} onClick={selectAllMatching}/></HStack></TableHeaderCell>
                <TableHeaderCell>{kind==='vehicles'?'车牌':kind==='quotes'?'报价方案':names[kind]}</TableHeaderCell>
                {kind==='warehouses'?<><TableHeaderCell>地址与位置</TableHeaderCell><TableHeaderCell>关联门店</TableHeaderCell></>:kind==='stores'?<><TableHeaderCell>城市 / 出货仓</TableHeaderCell><TableHeaderCell>营业与卸货</TableHeaderCell><TableHeaderCell>报价资料</TableHeaderCell><TableHeaderCell>关联位置</TableHeaderCell></>:kind==='vehicles'?<><TableHeaderCell>默认归属仓</TableHeaderCell><TableHeaderCell>件数容量</TableHeaderCell><TableHeaderCell>默认状态</TableHeaderCell><TableHeaderCell>车辆资料</TableHeaderCell></>:<><TableHeaderCell>计费参数</TableHeaderCell><TableHeaderCell>版本</TableHeaderCell><TableHeaderCell>引用门店</TableHeaderCell></>}
                <TableHeaderCell>操作</TableHeaderCell>
              </TableRow></TableHeader>
              <TableBody>{visible.map(row=><TableRow key={row.id}>
                <TableCell><Input type="checkbox" aria-label={'选择'+(row.code??row.plate)} checked={selected.has(row.id)} disabled={loading} onChange={()=>toggle(row.id)}/></TableCell>
                <TableCell><VStack gap={0.5} style={{minWidth:'8rem'}}><Text type="label">{text(kind==='vehicles'?row.plate:row.name)}</Text>{kind!=='vehicles'&&<Text type="supporting">{text(row.code)}</Text>}</VStack></TableCell>
                {kind==='warehouses'?<>
                  <TableCell><VStack gap={1} style={{maxWidth:'24rem'}}><Text>{text(row.address)}</Text>{locationStatus(row)}<Text type="supporting">{row.location_status==='LINKED'?`${row.longitude}, ${row.latitude} · ${row.crs}`:text(row.location_note)}</Text></VStack></TableCell>
                  <TableCell>{text(row.store_count)} 家</TableCell>
                </>:kind==='stores'?<>
                  <TableCell><VStack gap={1}><Text>{[row.province,row.city,row.district].filter(Boolean).join(' / ')||'未维护行政区'}</Text><Text type="supporting">{text(row.warehouse_name)}</Text></VStack></TableCell>
                  <TableCell><VStack gap={1}><Text hasTabularNumbers>{text(row.business_open)}–{Number(row.close_day_offset)===1?'次日 ':''}{text(row.business_close)}</Text><Text type="supporting">卸货 {text(row.unload_minutes)} 分钟</Text></VStack></TableCell>
                  <TableCell><VStack gap={1}><Text>{row.quote_name?text(row.quote_name):'专车报价未配置'}</Text><Text type="supporting">共配：{money(row.common_unit_price)} / 件</Text></VStack></TableCell>
                  <TableCell><VStack gap={1}>{locationStatus(row)}{row.location_status!=='LINKED'&&<Text type="supporting">{text(row.location_note)}</Text>}</VStack></TableCell>
                </>:kind==='vehicles'?<>
                  <TableCell>{text(row.warehouse_name)}</TableCell><TableCell>{text(row.max_pieces)} 件</TableCell><TableCell><Text>{row.available?'可用':'不可用'}</Text></TableCell><TableCell><Text color={row.road_profile_complete?'secondary':'accent'}>{row.road_profile_complete?'资料齐全':`缺 ${(row.road_profile_missing??[]).length} 项`}</Text></TableCell>
                </>:<><TableCell>{quoteDescription(row.latest_version)}</TableCell><TableCell>V{row.latest_version?.version}</TableCell><TableCell>{text(row.store_count)} 家</TableCell></>}
                <TableCell><HStack gap={1} wrap="wrap" style={{minWidth:'10rem'}}>
                  <Button label="编辑" size="sm" variant="ghost" isDisabled={loading} onClick={()=>setAction({type:'edit',row})}/>
                  {hasLocations&&<Button label="选择位置" size="sm" variant="ghost" isDisabled={loading} icon={<MapPin size={14}/>} onClick={()=>setAction({type:'location',row})}/>}
                  {kind==='quotes'&&<Button label="版本" size="sm" variant="ghost" isDisabled={loading} icon={<History size={14}/>} onClick={()=>setAction({type:'history',row})}/>}
                  <Button label="删除" size="sm" variant="ghost" aria-label={'删除'+(row.code??row.plate)} isDisabled={loading} onClick={()=>setDeleting([row])}/>
                </HStack></TableCell>
              </TableRow>)}</TableBody>
            </Table>
          </VStack>
          {loading?<HStack gap={2} padding={6} hAlign="center"><Spinner/><Text>正在读取资料</Text></HStack>:!visible.length&&<VStack gap={2} padding={8} hAlign="center"><FileSpreadsheet size={30}/><Heading level={3}>{query||locationFilter!=='all'?'没有匹配的资料':'还没有'+names[kind]+'资料'}</Heading><Text color="secondary">{query||locationFilter!=='all'?'请调整关键词或位置状态。':'可以手工新增，或下载模板批量导入。'}</Text></VStack>}
          <Section padding={3} dividers={['top']}><Pagination label={names[kind]} total={rows.length} page={current} pageSize={pageSize} disabled={loading} onPageChange={setPage} onPageSizeChange={size=>{setPageSize(size);setPage(0);}}/></Section>
        </>}
      </VStack>
    </Section>
    <Text type="supporting">基础资料统一维护；配送日期、当日安排和业务校验在「物流线路规划」中操作。</Text>
    {action?.type==='edit'&&<EntityEditor kind={kind} row={action.row} data={data} onClose={()=>setAction(null)} onSave={finish}/>}
    {action?.type==='location'&&action.row!=='new'&&<EntityEditor kind={kind} row={action.row} data={data} locationOnly onClose={()=>setAction(null)} onSave={finish}/>}
    {action?.type==='history'&&action.row!=='new'&&<VersionHistory row={action.row} onClose={()=>setAction(null)}/>}
    {bulkEditing&&<StoreBulkEditor rows={selectedRows} data={data} onClose={()=>setBulkEditing(false)} onSave={()=>{setBulkEditing(false);setSelected(new Set());finish('门店批量修改已保存');}}/>}
    {importing&&<ImportDialog kind={kind} onClose={()=>setImporting(false)} onSave={finish}/>}
    {deleting&&<DeleteDialog kind={kind} rows={deleting} onClose={()=>setDeleting(null)} onDeleted={deleted=>{setDeleting(null);setSelected(new Set());notify(`已删除 ${deleted} 条${names[kind]}资料`);void load();}}/>}
  </VStack>;
}

function DeleteDialog({kind,rows,onClose,onDeleted}:{kind:MasterKind;rows:Row[];onClose:()=>void;onDeleted:(n:number)=>void}){
  const [busy,setBusy]=useState(false); const [error,setError]=useState('');
  const [details,setDetails]=useState<{id:string;name?:string;message:string}[]>([]);
  async function remove(){
    setBusy(true);setError('');setDetails([]);
    try{
      const result=rows.length===1?
        await api<{deleted:number}>(`/${kind}/${rows[0].id}?expected_revision=${rows[0].revision}`,'DELETE'):
        await api<{deleted:number}>(`/master-data/${kind}/bulk-delete`,'POST',{items:rows.map(r=>({id:r.id,expected_revision:r.revision}))});
      onDeleted(result.deleted);
    }catch(e){setError(errorText(e));if(e instanceof ApiError&&Array.isArray(e.details))setDetails(e.details);}
    finally{setBusy(false);}
  }
  return <Modal title={'删除'+names[kind]} description={`将删除已选择的 ${rows.length} 条资料。`} busy={busy} onClose={onClose}>
    <VStack gap={4}>
      <Text color="secondary">请核对下列资料。有关联门店、车辆或配送需求时须先解除关联；本次操作整批完成或全部保留。历史规划快照与方案保留。</Text>
      <Section variant="muted" padding={3}><VStack gap={2} isScrollable style={{maxHeight:'14rem'}}>
        {rows.slice(0,50).map(row=><Text key={row.id}>{row.code??row.plate} · {row.name}</Text>)}
        {rows.length>50&&<Text type="supporting">另有 {rows.length-50} 条已选资料</Text>}
      </VStack></Section>
      <ErrorMessage text={error}/>
      {details.length>0&&<VStack gap={2}>{details.map(item=><ErrorMessage key={item.id} text={(item.name??rows.find(r=>r.id===item.id)?.code??item.id)+'：'+item.message}/>)}</VStack>}
      <HStack gap={2} hAlign="end" wrap="wrap"><Button label="取消" isDisabled={busy} onClick={onClose}/><Button label="确认删除" variant="destructive" isDisabled={busy} icon={busy?<Spinner/>:undefined} onClick={()=>void remove()}/></HStack>
    </VStack>
  </Modal>;
}

function EntityEditor({kind,row,data,onClose,onSave,locationOnly=false}:{kind:MasterKind;row:Row|'new';data:Catalog;onClose:()=>void;onSave:()=>void;locationOnly?:boolean}){
  const definitions=fields(kind,data).filter(f=>!locationOnly||['address','longitude','latitude','crs','location_source'].includes(f.key));
  const initial:Record<string,unknown>=row==='new'?{}:{...row,...(kind==='quotes'?row.latest_version:{})};
  const defaults:Record<string,string>={crs:'GCJ02',location_source:'手工录入',business_open:'08:00',business_close:'18:00',close_day_offset:'0',unload_minutes:'20',available:'true',mode:'KM',departure_time:'08:00'};
  const [values,setValues]=useState<Record<string,string>>(()=>Object.fromEntries(definitions.map(f=>{const value=initial[f.key]??defaults[f.key]??'';return [f.key,f.type==='datetime-local'?String(value).slice(0,16):String(value)];})));
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [issues,setIssues]=useState<Issue[]>([]);
  const [regionRule,setRegionRule]=useState<RegionSurcharge>(()=>(initial.region_surcharge as RegionSurcharge)?.targets?(initial.region_surcharge as RegionSurcharge):{amount:'',targets:[]});
  const shown=definitions.filter(f=>!(kind==='stores'&&['province','city','district','region_code'].includes(f.key))).filter(f=>kind!=='quotes'||!['per_km','base_price','included_stops','extra_stop_price'].includes(f.key)||(values.mode.startsWith('KM')?f.key==='per_km':f.key!=='per_km'));
  function change(key:string,value:string){setValues(v=>({...v,[key]:value,...(key==='mode'?{per_km:'',base_price:'',included_stops:'',extra_stop_price:''}:{})}));}
  function changeLocation(location:LocationValue){setValues(v=>({...v,...locationFields(location)}));}
  async function save(e:FormEvent){e.preventDefault();setBusy(true);setError('');setIssues([]);try{
    const payload:Record<string,unknown>={};
    for(const f of definitions){const value=values[f.key];payload[f.key]=f.key==='available'?value==='true':['integer'].includes(f.type??'')||['size','cartype','close_day_offset'].includes(f.key)?value===''?null:Number(value):f.type==='datetime-local'?value?value+':00+08:00':null:value===''&&!f.required?null:value;}
    if(kind==='quotes'){payload.effective_from=dateToday();payload.region_surcharge=values.mode.endsWith('_REGION')?regionRule:{};if(values.mode.startsWith('KM')){payload.base_price=null;payload.included_stops=null;payload.extra_stop_price=null;}else payload.per_km=null;}
    if(kind==='stores'&&!locationOnly)for(const key of ['province','city','district','region_code'])payload[key]=values[key]??'';
    if(row!=='new')payload.expected_revision=row.revision;
    await api('/'+kind+(row==='new'?'':'/'+row.id),row==='new'?'POST':'PATCH',payload);onSave();
  }catch(e){setError(errorText(e));setIssues(issueList(e));}finally{setBusy(false);}}
  return <Modal wide title={locationOnly?'选择位置':(row==='new'?'新增':'编辑')+names[kind]} description={locationOnly?'输入文字搜索、点击地图或拖拽标记，保存关联位置。':kind==='quotes'?'完整计费参数决定是否兼容，保存修改会产生新版本。':kind==='vehicles'?'维护车辆基本信息；当天安排在物流线路规划中设置。':'所有登录成员共用同一套基础资料。'} busy={busy} onClose={onClose}>
    <VStack as="form" gap={5} onSubmit={save}>
      <Grid columns={{minWidth:240,max:2}} gap={4}>
        {kind==='stores'&&!locationOnly&&<VStack gap={2} style={{gridColumn:'1 / -1'}}>
          <Heading level={3}>门店省市区</Heading>
          <RegionPicker label="门店" disabled={busy} value={{province:values.province??'',city:values.city??'',district:values.district??'',region_code:values.region_code??''}} onChange={choice=>setValues(v=>({...v,...choice}) as Record<string,string>)}/>
          <Text type="supporting">现有城市：{values.city||'未维护'}。选择上一级后会清空下一级，请按实际地址核对。</Text>
        </VStack>}
        {kind==='quotes'&&values.mode.endsWith('_REGION')&&<VStack style={{gridColumn:'1 / -1'}}><RegionSurchargeEditor value={regionRule} onChange={setRegionRule} disabled={busy}/></VStack>}
        {locationOnly&&<VStack style={{gridColumn:'1 / -1'}}><LocationMap value={{longitude:values.longitude??'',latitude:values.latitude??'',crs:values.crs??'WGS84',location_source:values.location_source??'',address:values.address??''}} disabled={busy} onChange={changeLocation} label={names[kind]+'位置选择'}/></VStack>}
        {shown.map((f,index)=><React.Fragment key={f.key}>
          {f.group&&index>0&&<VStack paddingBlockStart={4} style={{gridColumn:'1 / -1'}}><Heading level={3}>{f.group}</Heading></VStack>}
          <VStack gap={1.5} style={f.key==='address'?{gridColumn:'1 / -1'}:undefined}>
            <label htmlFor={'master-'+f.key}><Text type="label">{f.label}{f.required?' *':''}</Text></label>
            {f.type==='select'?<Select id={'master-'+f.key} required={f.required} value={values[f.key]??''} onChange={e=>change(f.key,e.target.value)}>
              <option value="">{f.required?'请选择':'未配置'}</option>{f.options?.map(([v,l])=><option key={v} value={v}>{l}</option>)}
            </Select>:<Input id={'master-'+f.key} value={values[f.key]??''} required={f.required} readOnly={row!=='new'&&f.key==='code'} type={f.type==='integer'?'number':f.type==='decimal'||f.type==='textarea'?'text':f.type??'text'} step={f.type==='integer'?'1':undefined} min={f.type==='integer'?'0':undefined} inputMode={f.type==='decimal'?'decimal':undefined} maxLength={f.key==='address'?300:f.key==='code'?40:undefined} onInput={e=>{if(['date','time','datetime-local'].includes(f.type??''))change(f.key,e.currentTarget.value);}} onChange={e=>change(f.key,e.target.value)}/>}
            {(f.hint||(row!=='new'&&f.key==='code'))&&<Text type="supporting">{row!=='new'&&f.key==='code'?'编号保持稳定，不可修改。':f.hint}</Text>}
            {issues.filter(i=>i.field===f.key).map((i,n)=><ErrorMessage key={n} text={i.message}/>)}
          </VStack>
        </React.Fragment>)}
        {!locationOnly&&(kind==='warehouses'||kind==='stores')&&<VStack style={{gridColumn:'1 / -1'}}><LocationMap value={{longitude:values.longitude??'',latitude:values.latitude??'',crs:values.crs??'WGS84',location_source:values.location_source??'',address:values.address??''}} disabled={busy} onChange={changeLocation} label={names[kind]+'位置选择'}/></VStack>}
      </Grid>
      {kind==='vehicles'&&<Text type="supporting">道路参数可暂时留空，但资料未补全前不能用于正式货车线路校验。最大件数不随车型额外加价。</Text>}
      {locationOnly&&<Text type="supporting">地图选点保存明确的坐标系，并记录位置来源。</Text>}
      <ErrorMessage text={error}/>{issues.filter(i=>i.field==='整行').map((i,n)=><ErrorMessage key={n} text={i.message}/>)}
      <HStack gap={2} hAlign="end" wrap="wrap"><Button label="取消" type="button" onClick={onClose} isDisabled={busy}/><Button label={'保存'+(kind==='quotes'&&row!=='new'?'新版本':'资料')} type="submit" variant="primary" isDisabled={busy} icon={busy?<Spinner/>:undefined}/></HStack>
    </VStack>
  </Modal>;
}


function VersionHistory({row,onClose}:{row:Row;onClose:()=>void}){
  const [versions,setVersions]=useState<QuoteVersion[]>([]);const [error,setError]=useState('');
  useEffect(()=>{api<{items:QuoteVersion[]}>('/quotes/'+row.id+'/versions').then(r=>setVersions(r.items)).catch(e=>setError(errorText(e)));},[row.id]);
  return <Modal wide title="报价版本" description={row.code+' · '+row.name} onClose={onClose}>
    <VStack gap={4}>
      <ErrorMessage text={error}/>
      {versions.map(v=><Section key={v.id} variant="muted" padding={3}><VStack gap={2}>
        <HStack gap={2} wrap="wrap" vAlign="center"><Text type="supporting">V{v.version}</Text><Heading level={3}>{v.name}</Heading></HStack>
        <Text>{quoteDescription(v)}</Text>
        {!!v.region_surcharge?.targets?.length&&<Text type="supporting">适用地区：{v.region_surcharge.targets.map(target=>[target.province_name,target.city_name,target.district_name||'全市'].join('/')).join('、')}</Text>}
      </VStack></Section>)}
      {!versions.length&&!error&&<HStack gap={2}><Spinner/><Text>正在读取版本</Text></HStack>}
      <Text type="supporting">旧版本只读。保存修改会增加版本，保留原报价资料。</Text>
    </VStack>
  </Modal>;
}

function ImportDialog({kind,onClose,onSave}:{kind:MasterKind;onClose:()=>void;onSave:(message?:string)=>void}){
  const [file,setFile]=useState<File|null>(null);const [result,setResult]=useState<Preview|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState('');
  async function preview(e:FormEvent){e.preventDefault();if(!file)return;setBusy(true);setError('');setResult(null);try{if(file.size>5*1024*1024)throw new Error('文件不能超过5MiB。');setResult(await api<Preview>('/master-data/imports/'+kind+'/preview?filename='+encodeURIComponent(file.name),'POST',file));}catch(e){setError(errorText(e));}finally{setBusy(false);}}
  async function commit(){if(!result)return;setBusy(true);setError('');try{const saved=await api<{geocoding?:{queued:number}}>('/master-data/imports/'+result.preview_id+'/commit','POST');onSave(saved.geocoding?.queued?`资料已保存，${saved.geocoding.queued} 条地址正在自动关联`:'资料已保存');}catch(e){setError(errorText(e));if(e instanceof ApiError&&e.status===409)setResult(null);}finally{setBusy(false);}}
  return <Modal wide title={'批量导入'+names[kind]} description="按编号或车牌新增、整行更新；任一错误会阻止整批提交。" onClose={onClose} busy={busy}>
    <VStack gap={4}>
      <Section variant="muted" padding={3}><HStack gap={3} wrap="wrap" vAlign="start">
        <FileSpreadsheet size={24}/><VStack gap={1} style={{flex:1,minWidth:0}}><Text type="label">先下载模板，再填写资料</Text><Text type="supporting">空白可选字段会清空原值，不删除文件中未出现的资料。</Text>{(kind==='warehouses'||kind==='stores')&&<Text type="supporting">坐标留空时，导入成功后按地址后台自动关联地图；无法精确关联时标记为「需手动关联」。</Text>}</VStack>
        <Link href={'/api/master-data/templates/'+kind+'.xlsx'} download isStandalone>下载模板</Link>
      </HStack></Section>
      <VStack as="form" gap={3} onSubmit={preview}>
        <label htmlFor="xlsx-file"><Text type="label">选择 xlsx 文件</Text></label>
        <Input id="xlsx-file" type="file" accept=".xlsx" required disabled={busy} onChange={e=>{setFile(e.target.files?.[0]??null);setResult(null);setError('');}}/>
        <HStack gap={3} wrap="wrap" vAlign="center"><Button label="上传并校验" type="submit" isDisabled={busy||!file} icon={busy?<Spinner/>:<Upload size={16}/>}/><Text type="supporting">最多5MiB / 10000行</Text></HStack>
      </VStack>
      <ErrorMessage text={error}/>
      {result&&<VStack gap={3}>
        <Banner status={result.can_commit?'success':'warning'} title={result.can_commit?'校验通过，尚未保存':'存在错误，整批尚未保存'}/>
        <HStack gap={4} wrap="wrap"><Text type="supporting">资料 {result.row_count} 行</Text><Text type="supporting">新增 {result.create_count}</Text><Text type="supporting">更新 {result.update_count}</Text><Text type="supporting">错误 {result.error_count} 项</Text></HStack>
        <VStack isScrollable style={{maxHeight:'20rem',minWidth:0}}>
          {result.errors.length>0?<Table density="compact"><TableHeader><TableRow><TableHeaderCell>来源行</TableHeaderCell><TableHeaderCell>字段</TableHeaderCell><TableHeaderCell>错误原因</TableHeaderCell></TableRow></TableHeader><TableBody>{result.errors.map((i,n)=><TableRow key={n}><TableCell>{i.row}</TableCell><TableCell>{i.field}</TableCell><TableCell>{i.message}</TableCell></TableRow>)}</TableBody></Table>:<Table density="compact"><TableHeader><TableRow><TableHeaderCell>来源行</TableHeaderCell><TableHeaderCell>编号 / 车牌</TableHeaderCell><TableHeaderCell>名称</TableHeaderCell><TableHeaderCell>操作</TableHeaderCell></TableRow></TableHeader><TableBody>{result.rows.map(r=><TableRow key={r.source_row}><TableCell>{r.source_row}</TableCell><TableCell>{r.code}</TableCell><TableCell>{r.name}</TableCell><TableCell>{r.action==='create'?'新增':'整行更新'}</TableCell></TableRow>)}</TableBody></Table>}
        </VStack>
        <Text type="supporting">预览最多展示前100条有效资料，确认时按整批提交。资料变化后需重新预览。</Text>
      </VStack>}
      <HStack gap={2} hAlign="end" wrap="wrap"><Button label="取消" onClick={onClose} isDisabled={busy}/><Button label="确认整批导入" variant="primary" isDisabled={busy||!result?.can_commit} icon={busy?<Spinner/>:undefined} onClick={()=>void commit()}/></HStack>
    </VStack>
  </Modal>;
}


function StoreBulkEditor({rows,data,onClose,onSave}:{rows:Row[];data:Catalog;onClose:()=>void;onSave:()=>void}){
 const keys=['warehouse_id','business_open','business_close','close_day_offset','unload_minutes','dedicated_quote_id','common_unit_price'];
 const definitions=fields('stores',data).filter(field=>keys.includes(field.key));
 const [enabled,setEnabled]=useState<Set<string>>(new Set());const [values,setValues]=useState<Record<string,string>>({business_open:'08:00',business_close:'18:00',close_day_offset:'0',unload_minutes:'20',warehouse_id:'',dedicated_quote_id:'',common_unit_price:''});
 const [busy,setBusy]=useState(false);const [error,setError]=useState('');
 // Read event values in handlers; React clears currentTarget before queued updates run.
 function change(key:string,value:string){setValues(previous=>({...previous,[key]:value}));}
 function toggleField(key:string,checked:boolean){setEnabled(previous=>{const next=new Set(previous);if(checked)next.add(key);else next.delete(key);return next;});}
 async function save(e:FormEvent){e.preventDefault();setBusy(true);setError('');try{
  const changes=Object.fromEntries([...enabled].map(key=>[key,['close_day_offset','unload_minutes'].includes(key)?Number(values[key]):values[key]===''?null:values[key]]));
  await api('/master-data/stores/bulk-update','POST',{items:rows.map(row=>({id:row.id,expected_revision:row.revision})),changes});onSave();
 }catch(e){setError(errorText(e));}finally{setBusy(false);}}
 return <Modal wide title="批量修改门店" description={`已选择 ${rows.length} 家门店（包含跨页选择）`} onClose={onClose} busy={busy}>
  <VStack as="form" gap={4} onSubmit={save}>
   <Text color="secondary">勾选需要修改的字段。未勾选字段保留原值；已勾选的专车报价或共配单价留空表示清空。全部门店校验通过后整批保存。</Text>
   <VStack gap={3}>
    {definitions.map(field=><Section key={field.key} variant={enabled.has(field.key)?'muted':'transparent'} padding={3} dividers={['bottom']}>
      <VStack gap={3}>
        <HStack as="label" gap={2} vAlign="center"><Input type="checkbox" aria-label={'修改'+field.label} checked={enabled.has(field.key)} disabled={busy} onChange={e=>toggleField(field.key,e.currentTarget.checked)}/><Text type="label">{field.label}</Text></HStack>
        {enabled.has(field.key)&&(field.type==='select'?<Select aria-label={'批量'+field.label} disabled={busy} required={field.key==='warehouse_id'||field.key==='close_day_offset'} value={values[field.key]} onChange={e=>change(field.key,e.currentTarget.value)}><option value="">{field.required?'请选择':'清空 / 未配置'}</option>{field.options?.map(([value,label])=><option key={value} value={value}>{label}</option>)}</Select>:<Input aria-label={'批量'+field.label} disabled={busy} type={field.type==='integer'?'number':field.type==='time'?'time':'text'} inputMode={field.type==='decimal'?'decimal':undefined} min={field.type==='integer'?0:undefined} step={field.type==='integer'?1:undefined} required={field.key!=='common_unit_price'} value={values[field.key]} onInput={e=>{if(field.type==='time')change(field.key,e.currentTarget.value);}} onChange={e=>change(field.key,e.currentTarget.value)}/>)}
      </VStack>
    </Section>)}
   </VStack>
   <ErrorMessage text={error}/>
   <HStack gap={2} hAlign="end" wrap="wrap"><Button type="button" label="取消" isDisabled={busy} onClick={onClose}/><Button type="submit" label="保存批量修改" variant="primary" isDisabled={busy||enabled.size===0} icon={busy?<Spinner/>:undefined}/></HStack>
  </VStack>
 </Modal>;
}
