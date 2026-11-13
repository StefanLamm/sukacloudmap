import React,{useEffect,useRef,useState} from 'react';
import {Maximize,Minimize,Minus,Plus} from 'lucide-react';

export function MapViewport({children,label,ready,onZoomIn,onZoomOut,onResize}:{children:React.ReactNode;label:string;ready:boolean;onZoomIn:()=>void;onZoomOut:()=>void;onResize:()=>void}){
  const wrapper=useRef<HTMLDivElement>(null);const [expanded,setExpanded]=useState(false);const expandedRef=useRef(expanded);expandedRef.current=expanded;
  useEffect(()=>{
    const change=()=>{if(document.fullscreenElement===wrapper.current)setExpanded(true);else setExpanded(false);};
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape'&&expandedRef.current&&!document.fullscreenElement){event.preventDefault();event.stopPropagation();setExpanded(false);}};
    document.addEventListener('fullscreenchange',change);window.addEventListener('keydown',escape,true);
    return()=>{document.removeEventListener('fullscreenchange',change);window.removeEventListener('keydown',escape,true);};
  },[]);
  useEffect(()=>{const id=window.requestAnimationFrame(onResize);return()=>window.cancelAnimationFrame(id);},[expanded,onResize]);
  useEffect(()=>{
    if(!expanded)return;
    // Preserve the live SDK container while neutralizing containing blocks that
    // would otherwise clip the CSS fallback inside a transformed modal/panel.
    const ancestors:HTMLElement[]=[];
    for(let element=wrapper.current?.parentElement;element;element=element.parentElement){
      if(!element.classList.contains('map-fullscreen-ancestor')){element.classList.add('map-fullscreen-ancestor');ancestors.push(element);}
    }
    return()=>{for(const element of ancestors)element.classList.remove('map-fullscreen-ancestor');};
  },[expanded]);
  async function toggle(){
    if(expanded){try{if(document.fullscreenElement===wrapper.current)await document.exitFullscreen();}finally{setExpanded(false);}}
    else{setExpanded(true);try{await wrapper.current?.requestFullscreen?.();}catch{/* In-app browsers can deny native fullscreen; the viewport still expands. */}}
  }
  return <div className={'map-viewport'+(expanded?' expanded':'')} ref={wrapper}>
    {children}<div className="map-controls" role="group" aria-label={label+'操作'}>
      <button type="button" title="放大地图" aria-label="放大地图" disabled={!ready} onClick={onZoomIn}><Plus size={17}/></button>
      <button type="button" title="缩小地图" aria-label="缩小地图" disabled={!ready} onClick={onZoomOut}><Minus size={17}/></button>
      <button type="button" title={expanded?'退出全屏':'全屏地图'} aria-label={expanded?'退出全屏':'全屏地图'} aria-pressed={expanded} onClick={()=>void toggle()}>{expanded?<Minimize size={17}/>:<Maximize size={17}/>}</button>
    </div>
  </div>;
}
