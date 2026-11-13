export type AMapConfig={provider:'AMAP';crs:'GCJ02';attribution:string;js_ready:boolean;geocoder_ready:boolean;js_key:string|null;security_proxy:string};
export type LngLat={getLng():number;getLat():number};
type MapEvent={lnglat?:LngLat};
export type Overlay={on(event:string,handler:(event:MapEvent)=>void):void};
export type MapInstance={on(event:string,handler:(event:MapEvent)=>void):void;add(overlays:Overlay[]):void;remove(overlays:Overlay[]):void;destroy():void;getCenter():LngLat;zoomIn():void;zoomOut():void;resize?():void;setZoomAndCenter(zoom:number,center:number[]):void;setFitView(overlays?:Overlay[],immediately?:boolean,padding?:number[],maxZoom?:number):void};
export type AMapSDK={Map:new(container:HTMLElement,options:Record<string,unknown>)=>MapInstance;
  Marker:new(options:Record<string,unknown>)=>Overlay&{getPosition():LngLat};
  Polyline:new(options:Record<string,unknown>)=>Overlay;
  InfoWindow:new(options:Record<string,unknown>)=>{setContent(content:HTMLElement):void;open(map:MapInstance,position:number[]|LngLat):void;close():void};
  Pixel:new(x:number,y:number)=>unknown};
declare global{interface Window{AMap?:AMapSDK;_AMapSecurityConfig?:{serviceHost:string};[callback:`__sukaAmapReady_${number}`]:(()=>void)|undefined;}}
let loading:Promise<AMapSDK>|null=null;
let readySDK:AMapSDK|null=null;
let callbackSequence=0;
const LOAD_TIMEOUT=45000;
function loadAttempt(key:string):Promise<AMapSDK>{
  return new Promise((resolve,reject)=>{
    const script=document.createElement('script');script.async=true;
    const callback=`__sukaAmapReady_${++callbackSequence}` as const;
    let settled=false;
    const cleanup=()=>{
      window.clearTimeout(timer);script.onerror=null;
      // An expired JSONP response can still arrive after the script is removed.
      // Its callback must neither throw nor settle a newer loading attempt.
      const ignore=()=>{};window[callback]=ignore;
      window.setTimeout(()=>{if(window[callback]===ignore)delete window[callback];},60000);
    };
    const fail=(reason:string)=>{
      if(settled)return;
      settled=true;cleanup();script.remove();reject(new Error(reason));
    };
    const timer=window.setTimeout(()=>fail('高德地图连接超时'),LOAD_TIMEOUT);
    window[callback]=()=>{
      if(settled)return;
      if(!window.AMap?.Map){fail('高德地图初始化未完成');return;}
      settled=true;cleanup();resolve(window.AMap);
    };
    script.onerror=()=>fail('高德地图脚本连接失败');
    script.src='https://webapi.amap.com/maps?'+new URLSearchParams({v:'2.0',key,callback});
    document.head.appendChild(script);
  });
}
export function loadAMap(config:AMapConfig):Promise<AMapSDK>{
  if(!config.js_ready||!config.js_key)return Promise.reject(new Error('高德地图尚未配置完整，请联系管理员。'));
  window._AMapSecurityConfig={serviceHost:window.location.origin+config.security_proxy};
  // The SDK can expose Map before its asynchronous initialization completes.
  if(loading)return loading;
  if(readySDK&&window.AMap===readySDK)return Promise.resolve(readySDK);
  const request=(async()=>{
    for(let attempt=0;attempt<2;attempt++){
      try{readySDK=await loadAttempt(config.js_key!);return readySDK;}
      catch(error){
        if(attempt===1)throw new Error(`${error instanceof Error?error.message:'高德地图加载失败'}，自动重试后仍未恢复。请稍后点击「重新加载地图」；已有位置会保留。`);
        await new Promise<void>(resolve=>window.setTimeout(resolve,1000));
      }
    }
    throw new Error('高德地图加载失败，请重新加载。');
  })();
  loading=request;
  const clear=()=>{if(loading===request)loading=null;};
  void request.then(clear,clear);
  return request;
}
