import React, {useEffect, useId, useRef, useState} from 'react';
import L from 'leaflet';
import coordtransform from 'coordtransform';
import {MapPin, Search} from 'lucide-react';
import {api} from './api';
import {ErrorMessage, Spinner} from './ui';
import {AMapLocationMap} from './amap-map';
import type {AMapConfig} from './amap-sdk';
import {locationFields} from './location-value';
import {MapViewport} from './map-viewport';
import type {LocationValue} from './location-value';
import 'leaflet/dist/leaflet.css';
import './location-map.css';

export type {LocationValue} from './location-value';
export type MapPoint={id:string;name:string;code?:string;longitude:unknown;latitude:unknown;crs:unknown;address?:unknown;markerLabel?:string;color?:string};
export type MapLine={id:string;color:string;coordinates:number[][];crs:string;label:string};
type Place=LocationValue&{name:string;address:string};
type OpenMapConfig={provider?:'OPEN';tile_url:string;attribution:string;crs:string};
export type LocationMapProps={points?:MapPoint[];lines?:MapLine[];value?:LocationValue;onChange?:(v:LocationValue)=>void;disabled?:boolean;label?:string};
export function LocationMap(props:LocationMapProps){
  const [config,setConfig]=useState<OpenMapConfig|AMapConfig|null>(null);const [error,setError]=useState('');
  useEffect(()=>{let active=true;api<OpenMapConfig|AMapConfig>('/maps/config').then(value=>{if(active)setConfig(value);}).catch(e=>{if(active)setError(e instanceof Error?e.message:'地图配置读取失败。');});return()=>{active=false;};},[]);
  if(!config)return <section className="location-widget"><ErrorMessage text={error}/>{!error&&<div className="map-loading"><Spinner/> 正在读取地图配置</div>}</section>;
  return config.provider==='AMAP'?<AMapLocationMap {...props} config={config}/>:<OpenLocationMap {...props} config={config}/>;
}
const fallbackCenter:L.LatLngTuple=[31.30,120.58];
function wgsPoint(p:Pick<MapPoint,'longitude'|'latitude'|'crs'>):L.LatLngTuple|null{
  if(p.longitude===null||p.latitude===null||String(p.longitude).trim()===''||String(p.latitude).trim()==='')return null;
  let lng=Number(p.longitude),lat=Number(p.latitude);
  if(!Number.isFinite(lng)||!Number.isFinite(lat)||Math.abs(lng)>180||Math.abs(lat)>90)return null;
  if(p.crs==='BD09')[lng,lat]=coordtransform.bd09togcj02(lng,lat);
  if(p.crs==='GCJ02'||p.crs==='BD09')[lng,lat]=coordtransform.gcj02towgs84(lng,lat);
  if(!['WGS84','GCJ02','BD09'].includes(String(p.crs)))return null;
  return [Math.max(-85.0511,Math.min(85.0511,lat)),lng];
}
const pinIcon=()=>L.divIcon({className:'location-marker',html:'<span class="location-pin"></span>',iconSize:[30,38],iconAnchor:[15,36]});

function OpenLocationMap({config,points=[],lines=[],value,onChange,disabled=false,label='位置地图'}:LocationMapProps&{config:OpenMapConfig}){
  const container=useRef<HTMLDivElement>(null);const map=useRef<L.Map|null>(null);const layer=useRef<L.LayerGroup|null>(null);
  const onChangeRef=useRef(onChange);onChangeRef.current=disabled?undefined:onChange;
  const [error,setError]=useState('');const [tileError,setTileError]=useState(false);
  const [ready,setReady]=useState(false);
  const [query,setQuery]=useState('');const [results,setResults]=useState<Place[]|null>(null);const [searching,setSearching]=useState(false);
  const searchId=useId();const generation=useRef(0);const lastFocus=useRef('');const valueRef=useRef(value);valueRef.current=value;
  useEffect(()=>()=>{generation.current++;},[]);
  function choose(latlng:L.LatLng,source:string){const current=valueRef.current;onChangeRef.current?.({longitude:latlng.lng.toFixed(8),latitude:latlng.lat.toFixed(8),crs:'WGS84',location_source:source,...(current?.address?{address:current.address}:{})});}
  useEffect(()=>{
    if(!container.current||!config)return;
    const instance=L.map(container.current,{center:fallbackCenter,zoom:11,scrollWheelZoom:false});map.current=instance;
    setReady(true);
    const tiles=L.tileLayer(config.tile_url,{attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>',maxZoom:19,keepBuffer:1}).addTo(instance);
    tiles.on('tileerror',()=>setTileError(true));tiles.on('load',()=>{if(container.current?.querySelector('.leaflet-tile-loaded'))setTileError(false);});
    layer.current=L.layerGroup().addTo(instance);
    instance.on('click',(e:L.LeafletMouseEvent)=>choose(e.latlng,'OpenStreetMap地图选点'));
    const resize=new ResizeObserver(()=>instance.invalidateSize({pan:false}));resize.observe(container.current);
    return()=>{resize.disconnect();instance.remove();map.current=null;layer.current=null;};
  },[config]);
  useEffect(()=>{
    const instance=map.current,group=layer.current;if(!instance||!group)return;
    group.clearLayers();
    const displayed:MapPoint[]=value?[{id:'editing',name:'当前选择位置',...value}]:points;
    const positions:L.LatLngTuple[]=[];
    for(const point of displayed){const position=wgsPoint(point);if(!position)continue;positions.push(position);
      let icon=pinIcon();
      if(point.markerLabel||point.color){const label=document.createElement('span');label.style.background=point.color??'#d66a25';label.textContent=point.markerLabel??'·';icon=L.divIcon({className:'location-order-marker',html:label,iconSize:[26,26],iconAnchor:[13,13]});}
      const marker=L.marker(position,{icon,draggable:Boolean(onChange)&&!disabled,title:point.name,alt:point.name}).addTo(group);
      if(onChange){marker.on('dragend',()=>choose(marker.getLatLng(),'OpenStreetMap拖拽校正'));}
      else{const popup=document.createElement('div');const title=document.createElement('strong');title.textContent=(point.code?point.code+' · ':'')+point.name;popup.append(title);const address=document.createElement('p');address.textContent=String(point.address??'');popup.append(address);marker.bindPopup(popup);}
    }
    for(const line of lines){const path=line.coordinates.map(p=>wgsPoint({longitude:p[0],latitude:p[1],crs:line.crs})).filter((p):p is L.LatLngTuple=>p!==null);if(path.length<2)continue;positions.push(...path);const name=document.createElement('span');name.textContent=line.label;L.polyline(path,{color:line.color,weight:4,opacity:.8}).bindTooltip(name).addTo(group);}
    const focus=positions.map(p=>p.join(',')).join('|');
    if(focus!==lastFocus.current&&positions.length){lastFocus.current=focus;if(positions.length===1)instance.setView(positions[0],Math.max(instance.getZoom(),14),{animate:false});else instance.fitBounds(positions,{padding:[32,32],maxZoom:15,animate:false});}
  },[points,lines,value,config,disabled,onChange]);
  async function search(){if(query.trim().length<2){setError('请输入至少2字的城市、地址或地点。');return;}const request=++generation.current;setSearching(true);setError('');setResults(null);try{const data=await api<{items:Place[]}>('/maps/search?q='+encodeURIComponent(query.trim()));if(request===generation.current)setResults(data.items);}catch(e){if(request===generation.current)setError(e instanceof Error?e.message:'搜索失败，请重试。');}finally{if(request===generation.current)setSearching(false);}}
  const associated=points.filter(p=>wgsPoint(p)!==null).length;
  return <section className="location-widget" aria-label={label}>
    {onChange&&<><label htmlFor={searchId}>文字搜索位置</label><div className="location-search"><input id={searchId} value={query} disabled={disabled||searching} maxLength={200} placeholder="输入城市、街道或地点" onChange={e=>{setQuery(e.target.value);setResults(null);}} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();void search();}}}/><button type="button" className="button secondary" disabled={disabled||searching||query.trim().length<2} onClick={()=>void search()}>{searching?<Spinner/>:<Search size={15}/>} 搜索位置</button></div><p className="map-privacy">点击搜索时，将输入的地点文字发送至 Photon 开放地图搜索。</p>{results&&<div className="place-results" aria-live="polite">{results.length?results.map((place,i)=><button type="button" key={i} disabled={disabled} onClick={()=>{onChangeRef.current?.(locationFields(place));setResults(null);setQuery(place.name);}}><MapPin size={15}/><span><b>{place.name}</b><small>{place.address}</small></span><span>选用</span></button>):<p>未找到匹配地点，可补充城市名称，或直接在地图上选点。</p>}</div>}</>}
    <ErrorMessage text={error}/><MapViewport label={label} ready={ready} onZoomIn={()=>{map.current?.zoomIn();}} onZoomOut={()=>{map.current?.zoomOut();}} onResize={()=>{map.current?.invalidateSize({pan:false});}}><div className="location-map-canvas" role="region" aria-label={label+'画布'} ref={container}/></MapViewport>
    {!config&&!error&&<div className="map-loading"><Spinner/> 正在加载地图</div>}
    {tileError&&<p className="map-service-error" role="status">地图底图加载失败，请检查网络后重新打开；已有坐标仍会保留。</p>}
    <div className="map-caption">{onChange?<><span>拖动地图、点击选点或拖拽标记；调整后请核对文字地址。</span><button type="button" className="text-button" disabled={disabled||!config} onClick={()=>{if(map.current)choose(map.current.getCenter(),'OpenStreetMap地图中心选点');}}>使用地图中心</button></>:<span>已显示 {associated} 个位置{points.length>associated?`，${points.length-associated} 条资料尚未关联坐标`:''}。点击标记查看详情。</span>}</div>
    {value&&<p className="map-coordinates">{value.longitude&&value.latitude?`${value.longitude}, ${value.latitude} · ${value.crs}`:'尚未关联位置'}{value.crs!=='WGS84'&&value.longitude?'（地图显示已转换，重新选点后保存为WGS84）':''}</p>}
  </section>;
}
