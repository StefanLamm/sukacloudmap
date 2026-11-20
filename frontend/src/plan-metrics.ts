export function fleetSummary(routes:{vehicle_id:string;pieces:number;meters:number}[],vehicles:{id:string;max_pieces:number}[]){
 const capacities=new Map(vehicles.map(v=>[v.id,v.max_pieces]));
 const meters=routes.reduce((sum,r)=>sum+r.meters,0);const pieces=routes.reduce((sum,r)=>sum+r.pieces,0);
 const known=routes.every(r=>{const size=capacities.get(r.vehicle_id);return typeof size==='number'&&Number.isFinite(size)&&size>0;});
 const capacity=known?routes.reduce((sum,r)=>sum+(capacities.get(r.vehicle_id)??0),0):null;
 return {meters,pieces,capacity,loadedPercent:capacity?100*pieces/capacity:null};
}
export function optimizationLabel(mode:string,optimal?:boolean){
 if(mode==='DAILY_DEEP_PORTFOLIO')return '日常深度规划 · 多轮搜索与联合选优';
 if(mode==='HYBRID_COST_PORTFOLIO')return '多算法联合优化 · 按总支出择优';
 if(mode==='ADAPTIVE_LARGE_NEIGHBORHOOD')return '大邻域费用优化';
 if(mode==='MULTI_START_JOINT_FLEET')return '多起点组合排线 · 按总费用择优';
 if(mode==='EXACT_SMALL')return '小规模联合枚举';
 if(mode==='MANUAL')return '按人工顺序核算';
 if(mode==='MANUAL_AUTO_DEDICATED')return optimal?'人工约束内专车最低费用分配':'人工约束内专车自动分车排线';
 if(mode==='MANUAL_OPTIMIZED')return optimal?'人工分车内最低费用顺序':'人工分车内限时费用优化';
 return '限时费用优化';
}
export function optimizationStop(reason:string){
 return ({TIME_BUDGET:'已达到优化时限，保留当前最佳方案',WALL_BUDGET:'已达到道路核验总时限，保留当前最佳方案',NO_AVAILABLE_ROAD_EVIDENCE:'暂无完整实路可用于专车核算',CANDIDATE_BUDGET:'已达到本次计算上限，保留当前最佳方案',CANCELLED:'已停止，保留当前方案',SEARCH_COMPLETED:'本轮搜索完成',DEEP_COMPLETED:'深度规划与可行性复核完成',DEEP_VALIDATED:'深度规划与可行性复核完成',FINAL_VALIDATED:'最终可行方案已复核',NO_DEDICATED_CANDIDATES:'无可用专车候选',MANUAL_RECALCULATED:'已按人工顺序核算',SELECTED_ROADS_REFRESHED:'已复核最新道路并重新报价',EXHAUSTIVE:'约束内枚举完成'} as Record<string,string>)[reason]??'本轮核算完成';
}
export type PlanningProgress={stage?:string;message?:string;round_index?:number;round_total?:number;seed?:number;best_fee?:string|null;elapsed_seconds?:number;candidate_count?:number};
export const deepPlanningStages=['准备道路','多轮搜索','汇总与联合选优','道路复核与修复','保存方案'];
export function planningStageIndex(stage?:string){
 const value=(stage??'').toUpperCase();
 if(['SEARCH','ROUND_SEARCH','SEARCH_ROUNDS','MULTI_SEED_SEARCH','MULTI_START','OPTIMIZE'].includes(value))return 1;
 if(['POOL','MERGE','SELECT','JOINT_SELECTION','COLUMN_SELECTION','PORTFOLIO_SELECTION','FINAL_SELECTION'].includes(value))return 2;
 if(['VERIFY','VALIDATE','ROAD_VERIFY','ROAD_VALIDATION','FINAL_VALIDATION','REFRESH','REPAIR','VERIFY_ROADS','FEASIBILITY_REPAIR','FINAL_CHECK'].includes(value))return 3;
 if(['SAVE','SAVING','COMPLETE','COMPLETED'].includes(value))return 4;
 return 0;
}
export function planningProgressDetails(progress:PlanningProgress){
 const details:string[]=[];const finite=(n:unknown):n is number=>typeof n==='number'&&Number.isFinite(n)&&n>=0;
 if(finite(progress.round_index)&&progress.round_index>0&&finite(progress.round_total)&&progress.round_total>0)details.push(`第 ${Math.min(Math.floor(progress.round_index),Math.floor(progress.round_total))} / ${Math.floor(progress.round_total)} 轮`);
 if(finite(progress.seed))details.push(`搜索起点 ${Math.floor(progress.seed)}`);
 return details.join(' · ');
}
export function elapsedTimeText(seconds: number) {
 const whole = Math.max(0, Math.floor(seconds));
 return whole >= 60 ? `${Math.floor(whole / 60)}分${whole % 60}秒` : `${whole}秒`;
}
export function progressFee(value?:string|null){
 if(typeof value!=='string'||!/^\d+(?:\.\d+)?$/.test(value))return null;
 const amount=Number(value);return Number.isFinite(amount)?amount.toFixed(2):null;
}
export type FinalRoadValidation={mode?:string;fresh?:boolean;selected_segments?:number;fresh_segments?:number};
export function deepValidationLabel(validation?:FinalRoadValidation){
 if(!validation)return '最终道路核验状态未记录';
 if(validation.fresh===true&&validation.mode==='FRESH_DRIVING_VALIDATED')return '已按本轮高德道路复核并重新报价';
 if(validation.mode==='OFFLINE_REFERENCE_VALIDATED')return '已用已有道路参考复核，未实时更新道路';
 if(validation.mode?.includes('FIXTURE')||validation.mode?.includes('TEST'))return '固定道路已复核，仅供隔离测试';
 if(validation.mode?.includes('COMMON'))return '全共配方案已复核，无专车道路调用';
 return '已有道路参考已复核，实时道路状态请查看采集时间';
}
export function roadReferenceSummary(routes:{segments:{source:string;received_at?:number;time_status?:string}[]}[],nowSeconds=Date.now()/1000){
 const segments=routes.flatMap(r=>r.segments).filter(s=>s.source==='DRIVING');
 const times=segments.map(s=>s.received_at).filter((t):t is number=>typeof t==='number'&&Number.isFinite(t)&&t>=0);
 const oldest=times.length?Math.min(...times):null;const newest=times.length?Math.max(...times):null;
 return {count:segments.length,oldest,newest,historical:segments.some(s=>s.time_status==='HISTORICAL_TRAFFIC_REFERENCE')||(oldest!==null&&nowSeconds-oldest>=3600),missingTime:times.length!==segments.length};
}
export function beijingReferenceTime(seconds:number|null){
 return seconds===null?'采集时间未记录':new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(seconds*1000));
}
