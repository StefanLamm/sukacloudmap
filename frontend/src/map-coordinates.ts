import coordtransform from 'coordtransform';

export function gcjPoint(point:{longitude:unknown;latitude:unknown;crs:unknown}):number[]|null{
  if(point.longitude===null||point.latitude===null||String(point.longitude).trim()===''||String(point.latitude).trim()==='')return null;
  let lng=Number(point.longitude),lat=Number(point.latitude);
  if(!Number.isFinite(lng)||!Number.isFinite(lat)||Math.abs(lng)>180||Math.abs(lat)>90)return null;
  if(point.crs==='WGS84')[lng,lat]=coordtransform.wgs84togcj02(lng,lat);
  else if(point.crs==='BD09')[lng,lat]=coordtransform.bd09togcj02(lng,lat);
  else if(point.crs!=='GCJ02')return null;
  return [lng,lat];
}

export function gcjPath(coordinates:number[][],crs:string):number[][]{
  if(coordinates.length<2)return [];
  const path=coordinates.map(point=>Array.isArray(point)&&point.length===2?gcjPoint({longitude:point[0],latitude:point[1],crs}):null);
  return path.some(point=>point===null)?[]:path as number[][];
}
