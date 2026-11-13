import React,{useEffect,useRef,useState} from 'react';
import {Maximize,Minimize,Minus,Plus} from 'lucide-react';
import {VStack} from '@astryxdesign/core/VStack';
import {HStack} from '@astryxdesign/core/HStack';
import {Action} from './ui';

export const mapCanvasStyle:React.CSSProperties={height:'100%',minHeight:0,borderRadius:'var(--radius-container)',background:'var(--color-background-muted)',isolation:'isolate',position:'relative',zIndex:0};

// Map SDK overlays live outside React. Resolve theme colors to concrete values
// for their canvas renderers while retaining normal CSS tokens on DOM markers.
export function mapColor(element:HTMLElement|null,value:string){
  if(!element)return '';
  if(!value.startsWith('--')&&!value.includes('var('))return value;
  const probe=document.createElement('i');
  probe.style.color=value.startsWith('--')?`var(${value})`:value;
  probe.style.display='none';element.append(probe);
  const resolved=getComputedStyle(probe).color;probe.remove();
  return resolved;
}
export function decorateMapMarker(element:HTMLElement,color?:string){
  Object.assign(element.style,{
    width:'var(--spacing-8)',height:'var(--spacing-8)',border:`solid ${color||'var(--color-accent)'}`,
    borderWidth:'var(--spacing-0-5)',borderRadius:'50%',background:'var(--color-background-surface)',
    color:'var(--color-text-primary)',font:'inherit',fontWeight:'600',display:'flex',alignItems:'center',
    justifyContent:'center',padding:'0',cursor:'pointer',boxShadow:'var(--shadow-low)'
  });
}

export function MapViewport({children,label,ready,onZoomIn,onZoomOut,onResize}:{children:React.ReactNode;label:string;ready:boolean;onZoomIn:()=>void;onZoomOut:()=>void;onResize:()=>void}){
  const wrapper=useRef<HTMLElement>(null);const [expanded,setExpanded]=useState(false);const expandedRef=useRef(expanded);expandedRef.current=expanded;
  useEffect(()=>{
    const change=()=>{setExpanded(document.fullscreenElement===wrapper.current);};
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape'&&expandedRef.current&&!document.fullscreenElement){event.preventDefault();event.stopPropagation();setExpanded(false);}};
    document.addEventListener('fullscreenchange',change);window.addEventListener('keydown',escape,true);
    return()=>{document.removeEventListener('fullscreenchange',change);window.removeEventListener('keydown',escape,true);};
  },[]);
  useEffect(()=>{const id=window.requestAnimationFrame(onResize);return()=>window.cancelAnimationFrame(id);},[expanded,onResize]);
  useEffect(()=>{
    if(!expanded)return;
    // Preserve the live SDK canvas, including inside top-layer dialogs. Restore
    // only the overridden properties so unrelated React updates are retained.
    const restores:Array<()=>void>=[];
    for(let element=wrapper.current?.parentElement;element;element=element.parentElement){
      const ancestor=element;const hadClass=ancestor.classList.contains('map-fullscreen-ancestor');
      ancestor.classList.add('map-fullscreen-ancestor');
      const overrides:Record<string,string>={transform:'none',filter:'none','backdrop-filter':'none',perspective:'none',contain:'none','will-change':'auto',overflow:'visible'};
      if(ancestor.tagName==='DIALOG')Object.assign(overrides,{position:'fixed',inset:'0',margin:'0',width:'100vw',height:'100dvh','max-width':'none','max-height':'none',border:'0','border-radius':'0'});
      if(ancestor===document.body)overrides.overflow='hidden';
      const saved=Object.keys(overrides).map(name=>[name,ancestor.style.getPropertyValue(name),ancestor.style.getPropertyPriority(name)]);
      for(const [name,value] of Object.entries(overrides))ancestor.style.setProperty(name,value,'important');
      restores.push(()=>{for(const [name,value,priority] of saved){if(value)ancestor.style.setProperty(name,value,priority);else ancestor.style.removeProperty(name);}if(!hadClass)ancestor.classList.remove('map-fullscreen-ancestor');});
    }
    return()=>{for(const restore of restores)restore();};
  },[expanded]);
  async function toggle(){
    if(expanded){try{if(document.fullscreenElement===wrapper.current)await document.exitFullscreen();}finally{setExpanded(false);}}
    else{setExpanded(true);try{await wrapper.current?.requestFullscreen?.();}catch{/* The fixed viewport also supports browsers that deny native fullscreen. */}}
  }
  return <VStack className={'map-viewport'+(expanded?' expanded':'')} ref={wrapper} style={expanded?{position:'fixed',inset:0,zIndex:20000,padding:'var(--spacing-3)',background:'var(--color-background-surface)',minWidth:0}:{position:'relative',height:'22rem',minWidth:0}}>
    {children}<HStack gap={1} padding={1} className="map-controls" role="group" aria-label={label+'操作'} style={{position:'absolute',top:'var(--spacing-3)',right:'var(--spacing-3)',zIndex:500,background:'var(--color-background-surface)',borderRadius:'var(--radius-container)',boxShadow:'var(--shadow-low)'}}>
      <Action type="button" variant="secondary" title="放大地图" aria-label="放大地图" disabled={!ready} onClick={onZoomIn}><Plus size={17}/></Action>
      <Action type="button" variant="secondary" title="缩小地图" aria-label="缩小地图" disabled={!ready} onClick={onZoomOut}><Minus size={17}/></Action>
      <Action type="button" variant="secondary" title={expanded?'退出全屏':'全屏地图'} aria-label={expanded?'退出全屏':'全屏地图'} aria-pressed={expanded} onClick={()=>void toggle()}>{expanded?<Minimize size={17}/>:<Maximize size={17}/>}</Action>
    </HStack>
  </VStack>;
}
