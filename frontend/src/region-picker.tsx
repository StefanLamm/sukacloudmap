import React,{useEffect,useState} from 'react';
import {api} from './api';
import {ErrorMessage,Spinner,Input,Select} from './ui';
import {VStack} from '@astryxdesign/core/VStack';
import {HStack} from '@astryxdesign/core/HStack';
import {Grid} from '@astryxdesign/core/Grid';
import {Section} from '@astryxdesign/core/Section';
import {Text} from '@astryxdesign/core/Text';
import {Heading} from '@astryxdesign/core/Heading';
import {Button} from '@astryxdesign/core/Button';
export type RegionNode={code:string;name:string;children?:RegionNode[]};
export type RegionChoice={province:string;city:string;district:string;region_code:string;province_code?:string;city_code?:string;district_code?:string};
export type RegionTarget={province_code:string;province_name:string;city_code:string;city_name:string;district_code?:string;district_name?:string};
export type RegionSurcharge={amount:string;targets:RegionTarget[];billing_mode?:'ONCE'|'PER_REGION'|'PER_STORE'};
let cached:Promise<RegionNode[]>|null=null;
function choices(){if(!cached){cached=api<{items:RegionNode[]}>('/master-data/regions').then(v=>v.items).catch(error=>{cached=null;throw error;});}return cached;}
export function RegionPicker({value,onChange,label='行政区',disabled=false}:{value:RegionChoice;onChange:(v:RegionChoice)=>void;label?:string;disabled?:boolean}){
 const [items,setItems]=useState<RegionNode[]>([]);const [error,setError]=useState('');const [loading,setLoading]=useState(true);const [retry,setRetry]=useState(0);
 useEffect(()=>{let active=true;setLoading(true);setError('');choices().then(rows=>{if(active)setItems(rows);}).catch(e=>{if(active)setError(e instanceof Error?e.message:'行政区选项加载失败。');}).finally(()=>{if(active)setLoading(false);});return()=>{active=false;};},[retry]);
 const province=items.find(p=>p.name===value.province||p.code===value.province_code);
 const cities=province?.children??[];const city=cities.find(c=>c.name===value.city||c.code===value.city_code||c.name.replace(/市$/,'')===value.city);
 const districts=city?.children??[];const district=districts.find(d=>d.name===value.district||d.code===value.district_code);
 function choose(p:RegionNode|undefined,c?:RegionNode,d?:RegionNode){onChange({province:p?.name??'',city:c?.name??'',district:d?.name??'',region_code:d?.code??c?.code??p?.code??'',province_code:p?.code??'',city_code:c?.code??'',district_code:d?.code??''});}
 return <VStack gap={2} style={{minWidth:0}}>
  <Grid columns={{minWidth:180,max:3}} gap={3}>
   <VStack as="label" gap={1.5}><Text type="label">{label}省份</Text><Select aria-label={label+'省份'} disabled={disabled||loading} value={province?.code??''} onChange={e=>choose(items.find(p=>p.code===e.target.value))}><option value="">全部 / 未选择省份</option>{items.map(p=><option key={p.code} value={p.code}>{p.name}</option>)}</Select></VStack>
   <VStack as="label" gap={1.5}><Text type="label">{label}城市</Text><Select aria-label={label+'城市'} disabled={disabled||loading||!province} value={city?.code??''} onChange={e=>choose(province,cities.find(c=>c.code===e.target.value))}><option value="">全部 / 未选择城市</option>{cities.map(c=><option key={c.code} value={c.code}>{c.name}</option>)}</Select></VStack>
   <VStack as="label" gap={1.5}><Text type="label">{label}区县</Text><Select aria-label={label+'区县'} disabled={disabled||loading||!city} value={district?.code??''} onChange={e=>choose(province,city,districts.find(d=>d.code===e.target.value))}><option value="">全市 / 未选择区县</option>{districts.map(d=><option key={d.code} value={d.code}>{d.name}</option>)}</Select></VStack>
  </Grid>
  {loading&&<HStack gap={2} vAlign="center"><Spinner/><Text type="supporting">正在读取省市区选项</Text></HStack>}
  <ErrorMessage text={error}/>{error&&<Button type="button" label="重试加载行政区" variant="ghost" size="sm" onClick={()=>setRetry(n=>n+1)}/>}
 </VStack>;
}
export function RegionSurchargeEditor({value,onChange,disabled=false}:{value:RegionSurcharge;onChange:(v:RegionSurcharge)=>void;disabled?:boolean}){
 const [target,setTarget]=useState<RegionChoice>({province:'',city:'',district:'',region_code:''});
 const [items,setItems]=useState<RegionNode[]>([]);const [selectedCities,setSelectedCities]=useState<string[]>([]);const [selectedDistricts,setSelectedDistricts]=useState<string[]>([]);
 const [error,setError]=useState('');const [retry,setRetry]=useState(0);
 useEffect(()=>{let active=true;setError('');choices().then(rows=>{if(active)setItems(rows);}).catch(e=>{if(active)setError(e instanceof Error?e.message:'行政区选项加载失败。');});return()=>{active=false;};},[retry]);
 const province=items.find(p=>p.code===target.province_code);const cities=province?.children??[];const city=cities.find(c=>c.code===target.city_code);const districts=city?.children??[];
 const perRegion=value.billing_mode==='PER_REGION';const perStore=value.billing_mode==='PER_STORE';
 function changeTarget(next:RegionChoice){
  if(next.province_code!==target.province_code)setSelectedCities([]);
  if(next.city_code!==target.city_code)setSelectedDistricts(next.district_code?[next.district_code]:[]);
  else if(next.district_code)setSelectedDistricts(old=>Array.from(new Set([...old,next.district_code!])));
  setTarget(next);
 }
 function add(){
  if(!province)return;
  const additions:RegionTarget[]=[];
  const make=(c:RegionNode,d?:RegionNode)=>({province_code:province!.code,province_name:province!.name,city_code:c.code,city_name:c.name,district_code:d?.code??'',district_name:d?.name??''});
  for(const c of cities)if(selectedCities.includes(c.code))additions.push(make(c));
  if(city){
   const chosen=districts.filter(d=>selectedDistricts.includes(d.code));
   if(chosen.length)for(const d of chosen)additions.push(make(city,d));
   else if(!selectedCities.length)additions.push(make(city));
  }
  const targets=[...value.targets];
  for(const item of additions)if(!targets.some(t=>t.city_code===item.city_code&&(t.district_code??'')===item.district_code))targets.push(item);
  onChange({...value,targets});setSelectedCities([]);setSelectedDistricts([]);setTarget({...target,district:'',district_code:'',region_code:target.city_code??''});
 }
 function multi(kind:'city'|'district',rows:RegionNode[],selected:string[],change:(next:string[])=>void){
  return <details open={kind==='district'}>
   <summary><Text type="label">{kind==='city'?'多选城市':'多选区县'}（{selected.length}个待添加）</Text></summary>
   <VStack gap={3} paddingBlockStart={3}>
    <HStack gap={2} wrap="wrap"><Button type="button" label={kind==='city'?'全选所有城市':'全选所有区县'} variant="ghost" size="sm" isDisabled={disabled||!rows.length} onClick={()=>change(rows.map(r=>r.code))}/><Button type="button" label="清空勾选" variant="ghost" size="sm" isDisabled={disabled||!selected.length} onClick={()=>change([])}/></HStack>
    <VStack isScrollable style={{maxHeight:'14rem'}}><Grid columns={{minWidth:160,max:4}} gap={3}>
     {rows.map(row=><HStack as="label" key={row.code} gap={2} vAlign="center"><Input type="checkbox" disabled={disabled} aria-label={(kind==='city'?'附加费城市 ':'附加费区县 ')+row.name} checked={selected.includes(row.code)} onChange={e=>change(e.target.checked?[...selected,row.code]:selected.filter(code=>code!==row.code))}/><Text>{row.name}</Text></HStack>)}
    </Grid></VStack>
   </VStack>
  </details>;
 }
 return <Section variant="muted" padding={4}><VStack gap={4}>
  <Heading level={3}>特殊地区附加费</Heading>
  <Grid columns={{minWidth:240,max:2}} gap={3}>
   <VStack as="label" gap={1.5}><Text type="label">附加费收取方式</Text><Select aria-label="附加费收取方式" value={value.billing_mode??'ONCE'} disabled={disabled} onChange={e=>onChange({...value,billing_mode:e.target.value as 'ONCE'|'PER_REGION'|'PER_STORE'})}><option value="ONCE">整条线路一次性附加费</option><option value="PER_REGION">每个送达区县一次性收取附加费</option><option value="PER_STORE">每家命中门店分别收取附加费</option></Select></VStack>
   <VStack gap={1.5}><label htmlFor="region-surcharge-amount"><Text type="label">{perStore?'每家门店附加费（元）':perRegion?'每个区县附加费（元）':'一次性增加金额（元）'}</Text></label><Input id="region-surcharge-amount" aria-label={perStore?'每家门店附加费（元）':perRegion?'每个区县附加费（元）':'一次性增加金额（元）'} inputMode="decimal" value={value.amount} disabled={disabled} required onChange={e=>onChange({...value,amount:e.target.value})}/></VStack>
  </Grid>
  <RegionPicker label="特殊地区" value={target} onChange={changeTarget} disabled={disabled}/>
  {province&&multi('city',cities,selectedCities,setSelectedCities)}
  {city&&multi('district',districts,selectedDistricts,setSelectedDistricts)}
  <ErrorMessage text={error}/>{error&&<Button type="button" label="重试加载多选地区" variant="ghost" size="sm" onClick={()=>setRetry(n=>n+1)}/>}
  <HStack><Button type="button" label="添加所选城市 / 区县" isDisabled={disabled||(!target.city_code&&!selectedCities.length)} onClick={add}/></HStack>
  <Text type="supporting">可勾选多个城市或区县后一起添加；切换城市后可以继续添加其他地区。未勾选区县时可添加全市。</Text>
  <VStack gap={0}>
   {value.targets.map((t,i)=><HStack key={t.city_code+'-'+t.district_code} gap={3} paddingBlock={2} vAlign="center" hAlign="between" ><Text>{t.province_name} / {t.city_name} / {t.district_name||'全市'}</Text><Button type="button" label="移除" variant="ghost" size="sm" isDisabled={disabled} aria-label={'移除附加费地区 '+[t.province_name,t.city_name,t.district_name||'全市'].join('/')} onClick={()=>onChange({...value,targets:value.targets.filter((_,j)=>i!==j)})}/></HStack>)}
  </VStack>
  <Text type="supporting">{perStore?`每家实际送达且命中上述范围的门店增加 ${value.amount||'0'} 元；同一区县的多家门店分别计费，城市与区县范围重叠时同一家门店只收一次。`:perRegion?`每个实际送达且命中上述范围的区县增加 ${value.amount||'0'} 元；同区县多店只收一次。选择全市时按该市实际送达的区县分别收费，城市与区县重叠不重复，区县资料不足时费用待核对。`:`同一专车线路到达上述任一城市或区县时，一次性增加 ${value.amount||'0'} 元；多个门店、多个地区或城市与区县重叠均只加一次。`}</Text>
 </VStack></Section>;
}
