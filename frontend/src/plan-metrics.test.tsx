import {expect,it} from 'vitest';
import {fleetSummary,optimizationLabel,optimizationStop,planningStageIndex,planningProgressDetails,progressFee,deepValidationLabel} from './plan-metrics';
it('uses total pieces over total used capacity rather than mean percentages',()=>{
 const summary=fleetSummary([{vehicle_id:'A',pieces:100,meters:10000},{vehicle_id:'B',pieces:100,meters:20000}],[{id:'A',max_pieces:100},{id:'B',max_pieces:400},{id:'UNUSED',max_pieces:900}]);
 expect(summary).toEqual({meters:30000,pieces:200,capacity:500,loadedPercent:40});
});
it('does not report full loading for missing capacity or an all-common plan',()=>{
 expect(fleetSummary([{vehicle_id:'MISSING',pieces:100,meters:12000}],[]).loadedPercent).toBeNull();
 expect(fleetSummary([],[]).loadedPercent).toBeNull();
});
it('describes the cost priority and stopping condition without claiming global optimality',()=>{
 expect(optimizationLabel('HYBRID_COST_PORTFOLIO')).toContain('总支出');
 expect(optimizationStop('TIME_BUDGET')).toContain('保留当前最佳方案');
 expect(optimizationStop('SELECTED_ROADS_REFRESHED')).toBe('已复核最新道路并重新报价');
 expect(optimizationStop('SOME_FUTURE_REASON')).toBe('本轮核算完成');
});
it('preserves the road capture interval and labels historical references explicitly',async()=>{
 const {roadReferenceSummary,beijingReferenceTime}=await import('./plan-metrics');
 const summary=roadReferenceSummary([{segments:[{source:'DRIVING',received_at:1000,time_status:'CURRENT_TRAFFIC_REFERENCE'},{source:'DRIVING',received_at:1500,time_status:'HISTORICAL_TRAFFIC_REFERENCE'},{source:'FIXTURE',received_at:0}]}],1600);
 expect(summary).toEqual({count:2,oldest:1000,newest:1500,historical:true,missingTime:false});
 expect(beijingReferenceTime(null)).toBe('采集时间未记录');
 expect(roadReferenceSummary([{segments:[{source:'DRIVING'}]}],5000).missingTime).toBe(true);
 expect(roadReferenceSummary([{segments:[{source:'DRIVING',received_at:1000}]}],4600).historical).toBe(true);
});
it('describes daily depth and its real workflow stages without exposing unknown stage codes',()=>{
 expect(optimizationLabel('DAILY_DEEP_PORTFOLIO')).toBe('日常深度规划 · 多轮搜索与联合选优');
 expect(optimizationStop('DEEP_COMPLETED')).toBe('深度规划与可行性复核完成');
 expect(planningStageIndex('MULTI_START')).toBe(1);
 expect(planningStageIndex('COLUMN_SELECTION')).toBe(2);
 expect(planningStageIndex('ROAD_VALIDATION')).toBe(3);
 expect(planningStageIndex('FEASIBILITY_REPAIR')).toBe(3);
 expect(planningStageIndex('FINAL_SELECTION')).toBe(2);
 expect(planningStageIndex('FINAL_CHECK')).toBe(3);
 expect(planningStageIndex('COMPLETE')).toBe(4);
 expect(planningStageIndex('FUTURE_INTERNAL_CODE')).toBe(0);
});
it('never formats missing or invalid progress values into a claimed candidate price',()=>{
 expect(progressFee('0.00')).toBe('0.00');expect(progressFee('23595.33')).toBe('23595.33');
 for(const value of [undefined,null,'NaN','Infinity','-1','bad'])expect(progressFee(value)).toBeNull();
 expect(planningProgressDetails({elapsed_seconds:NaN,candidate_count:Infinity,round_index:-1,round_total:0})).toBe('');
 expect(deepValidationLabel({mode:'FRESH_DRIVING_VALIDATED',fresh:false})).not.toContain('已按本轮高德');
 expect(deepValidationLabel({mode:'OFFLINE_REFERENCE_VALIDATED',fresh:true})).toContain('未实时更新');
});
