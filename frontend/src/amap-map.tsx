import React,{useEffect,useId,useRef,useState} from 'react';
import {MapPin,Search} from 'lucide-react';
import {api} from './api';
import {Action,Input,Field,ErrorMessage,Spinner} from './ui';
import {AMapConfig,AMapSDK,MapInstance,LngLat,Overlay,loadAMap} from './amap-sdk';
import {gcjPath,gcjPoint} from './map-coordinates';
import {locationFields} from './location-value';
import {MapViewport,mapCanvasStyle,mapColor,decorateMapMarker} from './map-viewport';
import {VStack} from '@astryxdesign/core/VStack';
import {HStack} from '@astryxdesign/core/HStack';
import {Section} from '@astryxdesign/core/Section';
import {Text} from '@astryxdesign/core/Text';
import type {LocationMapProps,LocationValue,MapPoint} from './location-map';

type Place=LocationValue&{name:string;address:string;match_level?:string};
export function AMapLocationMap({config,points=[],lines=[],value,onChange,disabled=false,label='位置地图'}:LocationMapProps&{config:AMapConfig}){
  const container=useRef<HTMLDivElement>(null);const map=useRef<MapInstance|null>(null);const sdk=useRef<AMapSDK|null>(null);
  const onChangeRef=useRef(onChange);onChangeRef.current=disabled?undefined:onChange;
  const valueRef=useRef(value);valueRef.current=value;const generation=useRef(0);const lastFocus=useRef('');
  const [ready,setReady]=useState(0);const [reload,setReload]=useState(0);const [loaded,setLoaded]=useState(false);
  const [mapError,setMapError]=useState('');const [error,setError]=useState('');
  const [query,setQuery]=useState('');const [results,setResults]=useState<Place[]|null>(null);const [searching,setSearching]=useState(false);const searchId=useId();
  function choose(position:LngLat,source:string){
    const lng=position.getLng(),lat=position.getLat();if(!Number.isFinite(lng)||!Number.isFinite(lat))return;
    onChangeRef.current?.({longitude:lng.toFixed(8),latitude:lat.toFixed(8),crs:'GCJ02',location_source:source,...(valueRef.current?.address?{address:valueRef.current.address}:{})});
  }
  useEffect(()=>{
    let active=true;let instance:MapInstance|null=null;let timer:number|undefined;
    setMapError('');setLoaded(false);lastFocus.current='';
    loadAMap(config).then(AMap=>{
      if(!active||!container.current)return;
      sdk.current=AMap;
      instance=new AMap.Map(container.current,{viewMode:'2D',center:[117.2272,31.8206],zoom:6,resizeEnable:true,scrollWheel:false});map.current=instance;
      instance.on('click',event=>{if(event.lnglat)choose(event.lnglat,'高德地图点击选点');});
      timer=window.setTimeout(()=>{if(active)setMapError('高德底图连接超时，请重新加载地图；若持续失败，请联系管理员检查网络和地图服务权限。已有坐标会保留。');},45000);
      instance.on('complete',()=>{if(active){window.clearTimeout(timer);setLoaded(true);setMapError('');}});
      setReady(n=>n+1);
    }).catch(e=>{if(active)setMapError(e instanceof Error?e.message:'高德地图未能加载。');});
    return()=>{active=false;generation.current++;window.clearTimeout(timer);instance?.destroy();map.current=null;sdk.current=null;};
  },[config,reload]);
  useEffect(()=>{
    const instance=map.current,AMap=sdk.current;if(!instance||!AMap)return;
    const overlays:Overlay[]=[];const info=new AMap.InfoWindow({offset:new AMap.Pixel(0,-16)});
    const displayed:MapPoint[]=value?[{id:'editing',name:'当前选择位置',...value}]:points;
    const positions:number[][]=[];
    for(const point of displayed){
      const position=gcjPoint(point);if(!position)continue;positions.push(position);
      const icon=document.createElement('button');icon.type='button';icon.className='amap-location-pin';icon.setAttribute('aria-label',point.name);icon.title=point.name;
      decorateMapMarker(icon,point.color);icon.textContent=point.markerLabel??'●';
      const marker=new AMap.Marker({position,content:icon,anchor:'center',draggable:Boolean(onChange)&&!disabled,title:point.name,bubble:false});
      marker.on('dragend',()=>choose(marker.getPosition(),'高德地图拖拽校正'));
      if(!onChange)marker.on('click',()=>{
        const popup=document.createElement('div');popup.className='amap-location-info';Object.assign(popup.style,{font:'inherit',maxWidth:'18rem',color:'var(--color-text-primary)'});const title=document.createElement('strong');title.textContent=(point.code?point.code+' · ':'')+point.name;
        const address=document.createElement('p');address.textContent=String(point.address??'');popup.append(title,address);info.setContent(popup);info.open(instance,position);
      });
      overlays.push(marker);
    }
    for(const line of lines){
      const path=gcjPath(line.coordinates,line.crs);if(path.length<2)continue;positions.push(...path);
      const polyline=new AMap.Polyline({path,strokeColor:mapColor(container.current,line.color||'--color-accent'),strokeWeight:6,strokeOpacity:.85,showDir:true,isOutline:true,outlineColor:mapColor(container.current,'--color-background-surface'),borderWeight:1,bubble:false});
      polyline.on('click',event=>{if(event.lnglat){const title=document.createElement('div');title.textContent=line.label;info.setContent(title);info.open(instance,event.lnglat);}});
      overlays.push(polyline);
    }
    instance.add(overlays);
    const focus=positions.map(position=>position.join(',')).join('|');
    if(focus!==lastFocus.current&&positions.length){lastFocus.current=focus;if(positions.length===1)instance.setZoomAndCenter(15,positions[0]);else instance.setFitView(overlays,false,[35,35,35,35],16);}
    return()=>{if(map.current===instance){info.close();instance.remove(overlays);}};
  },[ready,points,lines,value,onChange,disabled]);
  async function search(){
    if(query.trim().length<2)return;
    const request=++generation.current;setSearching(true);setResults(null);setError('');
    try{const response=await api<{items:Place[];notice?:string}>('/maps/search?q='+encodeURIComponent(query.trim()));if(request===generation.current){setResults(response.items);if(response.notice)setError(response.notice);}}
    catch(e){if(request===generation.current)setError(e instanceof Error?e.message:'高德位置搜索失败，请重试。');}
    finally{if(request===generation.current)setSearching(false);}
  }
  const associated=points.filter(point=>gcjPoint(point)!==null).length;
  const pathCount=lines.filter(line=>gcjPath(line.coordinates,line.crs).length>1).length;
  return <Section padding={3} className="location-widget" style={{minWidth:0,gridColumn:"1 / -1"}} aria-label={label}><VStack gap={3}>
    {onChange&&<><Field label="高德文字搜索位置" inputID={searchId}><HStack gap={2} wrap="wrap" className="location-search">
      <Input style={{flex:"1 1 12rem"}} id={searchId} maxLength={200} placeholder="输入完整城市、街道、门牌或地点名称" value={query} disabled={disabled||searching} onChange={e=>{setQuery(e.target.value);setResults(null);}} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();void search();}}}/>
      <Action type="button" className="button secondary" disabled={disabled||searching||query.trim().length<2||!config.geocoder_ready} onClick={()=>void search()}>{searching?<Spinner/>:<Search size={15}/>} 搜索位置</Action>
    </HStack></Field><Text as="p" type="supporting" color="secondary" className="map-privacy">搜索文字将发送至高德地图，选择结果后保存关联位置。</Text>{!config.geocoder_ready&&<ErrorMessage text="高德地址搜索尚未配置，请联系管理员。"/>}
    {results&&<VStack gap={1} isScrollable className="place-results" aria-live="polite" style={{maxHeight:"16rem"}}>{results.length?results.map((place,i)=><Action key={i} type="button" style={{height:"auto",whiteSpace:"normal",justifyContent:"flex-start",padding:"var(--spacing-3)"}} disabled={disabled} onClick={()=>{onChangeRef.current?.(locationFields(place));setQuery(place.name);setResults(null);}}><MapPin size={15}/><VStack gap={1} style={{flex:1,minWidth:0,textAlign:"left"}}><Text weight="semibold">{place.name}</Text><Text type="supporting" color="secondary">{place.address}{place.match_level?' · '+place.match_level:''}</Text></VStack><Text color="accent">选用</Text></Action>):<Text as="p" color="secondary">未找到匹配地点，请补充完整地址，或在地图上选点。</Text>}</VStack>}</>}
    <ErrorMessage text={error}/><ErrorMessage text={mapError}/>
    <MapViewport label={label} ready={Boolean(map.current)} onZoomIn={()=>map.current?.zoomIn()} onZoomOut={()=>map.current?.zoomOut()} onResize={()=>map.current?.resize?.()}><VStack style={mapCanvasStyle} className="location-map-canvas amap-map-canvas" role="region" aria-label={label+'画布'} ref={container}/></MapViewport>
    {!loaded&&!mapError&&<HStack gap={2} className="map-loading"><Spinner/> <Text color="secondary">正在加载高德地图，连接较慢时会自动重试</Text></HStack>}
    {mapError&&<Action type="button" className="text-button" onClick={()=>setReload(n=>n+1)}>重新加载地图</Action>}
    <HStack gap={3} wrap="wrap" hAlign="between" className="map-caption">{onChange?<><Text type="supporting" color="secondary">拖动地图、点击选点或拖拽标记；调整后请核对文字地址。</Text><Action type="button" className="text-button" disabled={disabled||!map.current} onClick={()=>{if(map.current)choose(map.current.getCenter(),'高德地图中心选点');}}>使用地图中心</Action></>:<Text type="supporting" color="secondary">{loaded?'已显示':'线路数据包含'} {associated} 个位置、{pathCount} 段道路轨迹{points.length>associated?`，${points.length-associated} 条资料尚未关联坐标`:''}。箭头表示行驶方向。</Text>}</HStack>
    {value&&<Text as="p" type="supporting" color="accent" className="map-coordinates" style={{overflowWrap:"anywhere"}}>{value.longitude&&value.latitude?`${value.longitude}, ${value.latitude} · ${value.crs}`:'尚未关联位置'}{value.crs!=='GCJ02'&&value.longitude?'（地图显示已转换，重新选点后保存为GCJ02）':''}</Text>}
  </VStack></Section>;
}
