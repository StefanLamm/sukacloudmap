import {expect,it} from 'vitest';
import {buildPlanMap,MapRoute,roadSegments} from './plan-map';
import {gcjPath,gcjPoint} from './map-coordinates';

const warehouses=[{id:'W1',name:'出货仓',longitude:'117.2',latitude:'31.8',crs:'GCJ02'},{id:'W2',name:'其他仓',longitude:'118',latitude:'32',crs:'GCJ02'}];
const tasks=new Map(['A','B','C'].map((id,i)=>[id,{id,name:id,warehouse_id:i===2?'W2':'W1',longitude:117.21+i*.01,latitude:31.81+i*.01,crs:'GCJ02'}]));
const segment={source:'LIVE',verified:false,polyline:[[117.2,31.8],[117.21,31.81],[117.22,31.82]],crs:'GCJ02'};
const routes:MapRoute[]=[{vehicle_id:'V1',plate:'测试一车',warehouse_id:'W1',task_ids:['A','B'],segments:[segment,{...segment,source:'FIXTURE'}]},
 {vehicle_id:'V2',plate:'测试二车',warehouse_id:'W2',task_ids:['C'],segments:[{...segment,polyline:[[118,32],[118.1,32.1]]}]}];

it('仅绘制服务返回道路，不跨缺失路段连直线或使用模拟折线',()=>{
 const result=buildPlanMap(warehouses,tasks,routes,[]);
 expect(result.lines.length).toBe(2);expect(result.lines[0].coordinates).toEqual(segment.polyline);
 expect(result.lines[0].label).toContain('出货仓 → A');expect(result.lines[0].label).toContain('待核验');
 expect(roadSegments(routes[0]).length).toBe(1);
});
it('单车轨迹保留对应仓库和配送编号，隐藏其他车辆及共配',()=>{
 const result=buildPlanMap(warehouses,tasks,routes,['C'],'V1');
 expect(result.lines.length).toBe(1);expect(result.points.map(p=>p.id)).toEqual(['A','B','W1']);
 expect(result.points.map(p=>p.markerLabel)).toEqual(['1','2','仓']);
});
it('非法坐标使整段不绘制，保持缺失状态',()=>{
 const broken={...routes[0],segments:[{...segment,polyline:[[117,31],[999,99],[118,32]]}]};
 expect(roadSegments(broken)).toEqual([]);
 expect(gcjPath(segment.polyline,'UNKNOWN')).toEqual([]);
});
it('GCJ02不重复偏移，WGS84与BD09按声明坐标系转换',()=>{
 expect(gcjPoint({longitude:117.2,latitude:31.8,crs:'GCJ02'})).toEqual([117.2,31.8]);
 expect(gcjPoint({longitude:117.2,latitude:31.8,crs:'WGS84'})).not.toEqual([117.2,31.8]);
 expect(gcjPoint({longitude:117.2,latitude:31.8,crs:'BD09'})).not.toEqual([117.2,31.8]);
 expect(gcjPoint({longitude:null,latitude:null,crs:'GCJ02'})).toBe(null);
});
it('普通驾车轨迹可显示，标明参考性质且不依赖货车核验标志',()=>{
 const reference={...routes[0],segments:[{...segment,source:'DRIVING'}]};
 const result=buildPlanMap(warehouses,tasks,[reference],[]);
 expect(result.lines).toHaveLength(1);expect(result.lines[0].label).toContain('驾车参考，未核验货车限行');
});
