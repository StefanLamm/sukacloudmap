import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import type {AMapConfig,AMapSDK} from './amap-sdk';
const config:AMapConfig={provider:'AMAP',crs:'GCJ02',attribution:'高德地图',js_ready:true,geocoder_ready:true,js_key:'PUBLIC_BROWSER_KEY',security_proxy:'/_AMapService'};
const sdk=()=>({Map:class{}} as unknown as AMapSDK);
const script=()=>document.head.querySelector('script')!;
const callback=(element:HTMLScriptElement=script())=>{
 const key=new URL(element.src).searchParams.get('callback')! as `__sukaAmapReady_${number}`;
 return window[key]!;
};
const complete=()=>{window.AMap=sdk();callback()();};
beforeEach(()=>{vi.resetModules();delete window.AMap;delete window._AMapSecurityConfig;vi.useFakeTimers();});
afterEach(()=>{document.head.querySelectorAll('script').forEach(s=>s.remove());delete window.AMap;delete window._AMapSecurityConfig;for(const key of Object.keys(window)){if(key.startsWith('__sukaAmapReady_'))delete window[key as `__sukaAmapReady_${number}`];}vi.clearAllTimers();vi.useRealTimers();});
it('SDK在线加载并只使用浏览器Key，安全密钥仍走同源代理',async()=>{
 const {loadAMap}=await import('./amap-sdk');const pending=loadAMap(config);
 expect(new URL(script().src).hostname).toBe('webapi.amap.com');
 expect(new URL(script().src).searchParams.get('key')).toBe('PUBLIC_BROWSER_KEY');
 expect(window._AMapSecurityConfig).toEqual({serviceHost:window.location.origin+'/_AMapService'});
 complete();await expect(pending).resolves.toBe(window.AMap);
 await expect(loadAMap(config)).resolves.toBe(window.AMap);expect(document.head.querySelectorAll('script')).toHaveLength(1);
});
it('缺少配置时不会发出SDK脚本请求',async()=>{
 const {loadAMap}=await import('./amap-sdk');await expect(loadAMap({...config,js_ready:false})).rejects.toThrow('尚未配置');
 expect(document.head.querySelector('script')).toBe(null);
});
it('多个地图共享初始化，不能提前使用只加载了一部分的SDK',async()=>{
 const {loadAMap}=await import('./amap-sdk');const first=loadAMap(config);window.AMap=sdk();
 const second=loadAMap(config);expect(first).toBe(second);expect(document.head.querySelectorAll('script')).toHaveLength(1);
 let finished=false;void second.then(()=>{finished=true;});await Promise.resolve();expect(finished).toBe(false);
 callback()();await first;expect(finished).toBe(true);
});
it('慢连接超过原20秒仍等待初始化完成',async()=>{
 const {loadAMap}=await import('./amap-sdk');const pending=loadAMap(config);
 const first=script();await vi.advanceTimersByTimeAsync(25000);expect(script()).toBe(first);
 complete();await expect(pending).resolves.toBe(window.AMap);
});
it('首次连接失败自动重试，多个组件不会各自重复请求',async()=>{
 const {loadAMap}=await import('./amap-sdk');const first=loadAMap(config);const oldScript=script();
 oldScript.dispatchEvent(new Event('error'));expect(loadAMap(config)).toBe(first);
 await vi.advanceTimersByTimeAsync(1000);expect(script()).not.toBe(oldScript);
 complete();await expect(first).resolves.toBe(window.AMap);
});
it('两次失败后停止自动重试，允许人工重新加载',async()=>{
 const {loadAMap}=await import('./amap-sdk');const first=loadAMap(config);
 const rejected=expect(first).rejects.toThrow('自动重试后仍未恢复');
 script().dispatchEvent(new Event('error'));await vi.advanceTimersByTimeAsync(1000);
 script().dispatchEvent(new Event('error'));await rejected;
 await vi.advanceTimersByTimeAsync(100000);expect(document.head.querySelector('script')).toBeNull();
 const second=loadAMap(config);complete();await expect(second).resolves.toBe(window.AMap);
});
it('超时请求的迟到回调和错误不会清除或完成新请求',async()=>{
 const {loadAMap}=await import('./amap-sdk');const pending=loadAMap(config);const firstCallback=callback();const firstScript=script();
 let finished=false;void pending.then(()=>{finished=true;});
 await vi.advanceTimersByTimeAsync(46000);const currentScript=script();expect(currentScript).not.toBe(firstScript);
 window.AMap=sdk();firstCallback();firstScript.dispatchEvent(new Event('error'));await Promise.resolve();
 expect(finished).toBe(false);expect(script()).toBe(currentScript);
 callback()();await expect(pending).resolves.toBe(window.AMap);
});
it('连续超时有明确边界，失败后不会把半初始化SDK当作可用地图',async()=>{
 const {loadAMap}=await import('./amap-sdk');const first=loadAMap(config);const rejected=expect(first).rejects.toThrow('连接超时');
 window.AMap=sdk();await vi.advanceTimersByTimeAsync(91000);await rejected;
 const second=loadAMap(config);expect(document.head.querySelector('script')).not.toBeNull();
 complete();await expect(second).resolves.toBe(window.AMap);
});
