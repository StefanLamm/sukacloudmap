import type {MapLine,MapPoint} from './location-map';
import {gcjPath} from './map-coordinates';

type GeoTask={id:string;code?:string;name:string;warehouse_id:string;longitude?:unknown;latitude?:unknown;crs?:unknown};
type Segment={source:string;verified:boolean;polyline?:number[][];crs:string};
export type MapRoute={vehicle_id:string;plate?:string;warehouse_id?:string;task_ids:string[];segments:Segment[]};
export const routeColors=['#d66a25','#3378b5','#985bc3','#439178','#b14761','#8a7828'];
export function roadSegments(route:MapRoute){
  return route.segments.slice(0,route.task_ids.length).map((segment,index)=>({segment,index}))
    .filter(({segment})=>['LIVE','DRIVING'].includes(segment.source)&&Array.isArray(segment.polyline)&&gcjPath(segment.polyline,segment.crs).length>1);
}

export function buildPlanMap(warehouses:Record<string,unknown>[],tasks:Map<string,GeoTask>,routes:MapRoute[],commonIds:string[],vehicleId?:string){
  const points:MapPoint[]=[],lines:MapLine[]=[];const warehouseIds=new Set<string>();
  for(const [i,route] of routes.entries()){
    if(vehicleId&&route.vehicle_id!==vehicleId)continue;
    const color=routeColors[i%routeColors.length];const plate=route.plate??route.vehicle_id;
    const warehouseId=route.warehouse_id??tasks.get(route.task_ids[0])?.warehouse_id;
    if(warehouseId)warehouseIds.add(warehouseId);
    const origin=warehouses.find(w=>String(w.id)===warehouseId);
    route.task_ids.forEach((id,n)=>{const task=tasks.get(id);if(task)points.push({...task,name:`${plate} · ${n+1} ${task.name}`,markerLabel:String(n+1),color,longitude:task.longitude,latitude:task.latitude,crs:task.crs});});
    for(const {segment,index} of roadSegments(route)){
      const from=index===0?String(origin?.name??'出货仓'):tasks.get(route.task_ids[index-1])?.name??route.task_ids[index-1];
      const to=tasks.get(route.task_ids[index])?.name??route.task_ids[index];
      lines.push({id:route.vehicle_id+'-'+index,color,coordinates:segment.polyline!,crs:segment.crs,
        label:`${plate} · 第${index+1}段 ${from} → ${to}${segment.source==='DRIVING'?'（驾车参考，未核验货车限行）':segment.verified?'':'（待核验）'}`});
    }
  }
  if(!vehicleId){
    for(const id of commonIds){const task=tasks.get(id);if(task){warehouseIds.add(task.warehouse_id);points.push({...task,name:'共配 · '+task.name,color:'#898078',longitude:task.longitude,latitude:task.latitude,crs:task.crs});}}
  }
  for(const warehouse of warehouses){
    if(!warehouseIds.has(String(warehouse.id)))continue;
    points.push({id:String(warehouse.id),name:String(warehouse.name??warehouse.id),longitude:warehouse.longitude,latitude:warehouse.latitude,crs:warehouse.crs,markerLabel:'仓',color:'#693819'});
  }
  return {points,lines};
}
