import React from 'react';
import {act,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import {AMapLocationMap} from './amap-map';
import type {AMapConfig} from './amap-sdk';
const mocks=vi.hoisted(()=>({load:vi.fn(),api:vi.fn()}));
vi.mock('./amap-sdk',()=>({loadAMap:mocks.load}));
vi.mock('./api',()=>({api:mocks.api}));
const config:AMapConfig={provider:'AMAP',crs:'GCJ02',attribution:'高德地图',js_ready:true,geocoder_ready:true,js_key:'PUBLIC_TEST',security_proxy:'/_AMapService'};
let maps:FakeMap[]=[],markers:FakeMarker[]=[],polylines:FakeLine[]=[];
class FakeMap{
 events:Record<string,(event:any)=>void>={};destroyed=false;options:Record<string,unknown>;
 constructor(_element:HTMLElement,options:Record<string,unknown>){this.options=options;maps.push(this);}
 on(name:string,handler:(event:any)=>void){this.events[name]=handler;if(name==='complete')queueMicrotask(()=>handler({}));}
 add=vi.fn();remove=vi.fn(()=>{if(this.destroyed)throw Error('remove after destroy');});destroy=vi.fn(()=>{this.destroyed=true;});
 setFitView=vi.fn();setZoomAndCenter=vi.fn();getCenter(){return {getLng:()=>117.2,getLat:()=>31.8};}
}
class FakeMarker{
 events:Record<string,(event:any)=>void>={};constructor(public options:Record<string,unknown>){markers.push(this);}
 on(name:string,handler:(event:any)=>void){this.events[name]=handler;}
 getPosition(){return {getLng:()=>117.22,getLat:()=>31.82};}
}
class FakeLine{
 constructor(public options:Record<string,unknown>){polylines.push(this);}on(){}
}
class FakeInfo{setContent(){}open(){}close(){}}
beforeEach(()=>{maps=[];markers=[];polylines=[];mocks.load.mockReset();mocks.api.mockReset();mocks.load.mockResolvedValue({Map:FakeMap,Marker:FakeMarker,Polyline:FakeLine,InfoWindow:FakeInfo,Pixel:class{}});});
it('高德地图展示带方向箭头的道路折线及配送编号',async()=>{
 const {unmount}=render(<AMapLocationMap config={config} points={[{id:'A',name:'一店',longitude:117.2,latitude:31.8,crs:'GCJ02',markerLabel:'1'}]} lines={[{id:'L',color:'#d66a25',coordinates:[[117.2,31.8],[117.22,31.82]],crs:'GCJ02',label:'道路'}]}/>);
 await waitFor(()=>expect(polylines.length).toBe(1));
 expect(polylines[0].options.showDir).toBe(true);expect(polylines[0].options.path).toEqual([[117.2,31.8],[117.22,31.82]]);
 expect((markers[0].options.content as HTMLElement).textContent).toBe('1');
 unmount();expect(maps[0].destroy).toHaveBeenCalledOnce();
});
it('选点、拖拽和地图中心以GCJ02保存并保留文字地址',async()=>{
 const change=vi.fn();render(<AMapLocationMap config={config} value={{longitude:'117.2',latitude:'31.8',crs:'GCJ02',location_source:'原始资料',address:'文字地址'}} onChange={change}/>);
 await waitFor(()=>expect(markers.length).toBe(1));
 act(()=>maps[0].events.click({lnglat:{getLng:()=>117.21,getLat:()=>31.81}}));
 expect(change.mock.calls[0][0]).toMatchObject({longitude:'117.21000000',latitude:'31.81000000',crs:'GCJ02',address:'文字地址'});
 act(()=>markers[0].events.dragend({}));expect(change.mock.lastCall?.[0].location_source).toBe('高德地图拖拽校正');
 fireEvent.click(screen.getByRole('button',{name:'使用地图中心'}));expect(change.mock.lastCall?.[0].location_source).toBe('高德地图中心选点');
});
it('高德搜索结果只回填位置字段，POI名称与其他元数据不会传入业务表单',async()=>{
 const place={name:'合肥市人民政府',address:'合肥市东流路100号',longitude:'117.227185',latitude:'31.820824',crs:'GCJ02',location_source:'高德地图地址解析'};
 mocks.api.mockResolvedValue({items:[place]});const change=vi.fn();
 render(<AMapLocationMap config={config} onChange={change}/>);
 fireEvent.change(screen.getByLabelText('高德文字搜索位置'),{target:{value:'合肥市人民政府'}});fireEvent.click(screen.getByRole('button',{name:'搜索位置'}));
 fireEvent.click(await screen.findByRole('button',{name:/选用/}));
 const {name:poiName,...location}=place;expect(change).toHaveBeenCalledWith(location);
 expect((screen.getByLabelText('高德文字搜索位置') as HTMLInputElement).value).toBe(poiName);
});
it('未配置完整凭证时给出明确错误且不会生成假地图',async()=>{
 mocks.load.mockRejectedValue(Error('高德地图尚未配置完整，请联系管理员。'));
 render(<AMapLocationMap config={{...config,js_ready:false,geocoder_ready:false,js_key:null}} onChange={()=>{}}/>);
 await screen.findByText('高德地图尚未配置完整，请联系管理员。');
 expect(maps.length).toBe(0);expect(screen.getByRole('button',{name:'搜索位置'}).hasAttribute('disabled')).toBe(true);
});
it('地点搜索配额受限时仍保留可用的地址解析候选',async()=>{
 const place={name:'可用地址解析候选',address:'合肥市测试路1号',longitude:'117.2',latitude:'31.8',crs:'GCJ02',location_source:'高德地图地址解析'};
 const notice='高德地点搜索额度受限，地址解析候选已保留，请核对或在地图上选点。';
 mocks.api.mockResolvedValue({items:[place],notice});const change=vi.fn();
 render(<AMapLocationMap config={config} onChange={change}/>);
 fireEvent.change(screen.getByLabelText('高德文字搜索位置'),{target:{value:'合肥市测试路1号'}});fireEvent.click(screen.getByRole('button',{name:'搜索位置'}));
 await screen.findByText(notice);fireEvent.click(await screen.findByRole('button',{name:/选用/}));
 const {name:poiName,...location}=place;expect(change).toHaveBeenCalledWith(location);
});

it('加载中或失败仅说明线路数据数量，底图完成后才声称已经显示',async()=>{
 mocks.load.mockRejectedValueOnce(Error('高德地图测试连接超时'));
 render(<AMapLocationMap config={config} points={[{id:'A',name:'一店',longitude:117.2,latitude:31.8,crs:'GCJ02'}]} lines={[{id:'L',color:'#d66a25',coordinates:[[117.2,31.8],[117.22,31.82]],crs:'GCJ02',label:'道路'}]}/>);
 expect(screen.getByText(/线路数据包含\s*1\s*个位置、\s*1\s*段道路轨迹/)).toBeTruthy();
 await screen.findByText('高德地图测试连接超时');
 expect(screen.queryByText(/已显示\s*1\s*个位置/)).toBeNull();
 fireEvent.click(screen.getByRole('button',{name:'重新加载地图'}));
 expect(await screen.findByText(/已显示\s*1\s*个位置、\s*1\s*段道路轨迹/)).toBeTruthy();
});
