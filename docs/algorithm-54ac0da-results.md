费用目标为完整覆盖且满足全部硬约束后的整单实际费用。新旧固定输入、道路、历史提示、seed、哈希环境、计算预算和总等待截止，串行交替运行。以下保留全部样本；冷启动差额与单阶段直接降费分别报告，不按最低 seed 挑选。

| 报告 | 完成 | 已记录运行 | 处置 | 基线源码 | 候选源码 |
| --- | --- | ---: | --- | --- | --- |
| phase2-cold | True | 4 | NO_POSITIVE_COLD_START_EVIDENCE_NOT_DEFAULT | 8bdefbc77b6f | 85eb8f2c1c97 |
| search-screen | True | 16 | EXPLORATORY_OLDER_SOURCE_NOT_ADOPTED | 8bdefbc77b6f | 85eb8f2c1c97 |
| production-600 | True | 8 | RESIDUAL_LOOP_VALIDATION | 8bdefbc77b6f | 0c6b00e5b913 |
| critical-graph-paired | True | 2 | SEPARATE_GRAPH_NO_BENEFIT | 8bdefbc77b6f | 5c848e3cbdeb |
| month-regression-15 | True | 60 | ACTUAL_SHORT_BUDGET_DEFAULT_REGRESSION | 8bdefbc77b6f | 5c848e3cbdeb |

| 报告 | 日标签 | 单项开关 | 预算秒 | seed | 基线费用 | 候选费用 | 旧减新 | 结果 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| phase2-cold | september-9.22 | deep_batch | 60.0 | 0 | 16439.47 | 16442.18 | -2.71 | REGRESSED |
| phase2-cold | september-9.22 | deep_diversity | 60.0 | 0 | 16890.47 | 16890.47 | 0.00 | EQUAL |
| search-screen | september-9.11 | joint | 30.0 | 0 | 11646.42 | 12089.17 | -442.75 | REGRESSED |
| search-screen | september-9.22 | joint | 30.0 | 0 | 16852.44 | 16439.09 | 413.35 | IMPROVED |
| search-screen | september-9.11 | quote_rotation | 30.0 | 0 | 11764.81 | 11600.54 | 164.27 | IMPROVED |
| search-screen | september-9.22 | quote_rotation | 30.0 | 0 | 16440.22 | 16394.53 | 45.69 | IMPROVED |
| search-screen | september-9.11 | selection_loop | 30.0 | 0 | 11658.94 | 11509.95 | 148.99 | IMPROVED |
| search-screen | september-9.22 | selection_loop | 30.0 | 0 | 16441.39 | 16359.99 | 81.40 | IMPROVED |
| search-screen | september-9.11 | gain_allocation | 30.0 | 0 | 11561.35 | 11509.95 | 51.40 | IMPROVED |
| search-screen | september-9.22 | gain_allocation | 30.0 | 0 | 16473.68 | 16403.45 | 70.23 | IMPROVED |
| production-600 | september-9.22 | remaining_budget_loop | 600.0 | 0 | 16405.57 | 16337.77 | 67.80 | IMPROVED |
| production-600 | september-9.22 | remaining_budget_loop | 600.0 | 1 | 16403.28 | 16343.15 | 60.13 | IMPROVED |
| production-600 | 2026-10-20 | remaining_budget_loop | 600.0 | 0 | 14474.04 | 14453.80 | 20.24 | IMPROVED |
| production-600 | 2026-10-20 | remaining_budget_loop | 600.0 | 1 | 14329.61 | 14329.61 | 0.00 | EQUAL |
| critical-graph-paired | critical-collected | actual_short_budget_default | 60.0 | 0 | 16423.24 | 16423.24 | 0.00 | EQUAL |
| month-regression-15 | september-9.01 | actual_short_budget_default | 15.0 | 0 | 19447.14 | 19447.14 | 0.00 | EQUAL |
| month-regression-15 | september-9.02 | actual_short_budget_default | 15.0 | 0 | 10143.20 | 10221.37 | -78.17 | REGRESSED |
| month-regression-15 | september-9.03 | actual_short_budget_default | 15.0 | 0 | 8367.58 | 8367.58 | 0.00 | EQUAL |
| month-regression-15 | september-9.04 | actual_short_budget_default | 15.0 | 0 | 12737.95 | 12784.28 | -46.33 | REGRESSED |
| month-regression-15 | september-9.05 | actual_short_budget_default | 15.0 | 0 | 11128.16 | 11128.16 | 0.00 | EQUAL |
| month-regression-15 | september-9.06 | actual_short_budget_default | 15.0 | 0 | 3019.00 | 3019.00 | 0.00 | EQUAL |
| month-regression-15 | september-9.07 | actual_short_budget_default | 15.0 | 0 | 7738.21 | 7738.21 | 0.00 | EQUAL |
| month-regression-15 | september-9.08 | actual_short_budget_default | 15.0 | 0 | 16461.40 | 16389.79 | 71.61 | IMPROVED |
| month-regression-15 | september-9.09 | actual_short_budget_default | 15.0 | 0 | 11417.52 | 11458.76 | -41.24 | REGRESSED |
| month-regression-15 | september-9.10 | actual_short_budget_default | 15.0 | 0 | 9903.23 | 9888.23 | 15.00 | IMPROVED |
| month-regression-15 | september-9.11 | actual_short_budget_default | 15.0 | 0 | 11981.76 | 11961.46 | 20.30 | IMPROVED |
| month-regression-15 | september-9.12 | actual_short_budget_default | 15.0 | 0 | 10445.71 | 10410.91 | 34.80 | IMPROVED |
| month-regression-15 | september-9.13 | actual_short_budget_default | 15.0 | 0 | 769.08 | 769.08 | 0.00 | EQUAL |
| month-regression-15 | september-9.14 | actual_short_budget_default | 15.0 | 0 | 8458.71 | 8473.71 | -15.00 | REGRESSED |
| month-regression-15 | september-9.15 | actual_short_budget_default | 15.0 | 0 | 16872.85 | 16714.23 | 158.62 | IMPROVED |
| month-regression-15 | september-9.16 | actual_short_budget_default | 15.0 | 0 | 11974.19 | 11974.19 | 0.00 | EQUAL |
| month-regression-15 | september-9.17 | actual_short_budget_default | 15.0 | 0 | 9050.15 | 9050.15 | 0.00 | EQUAL |
| month-regression-15 | september-9.18 | actual_short_budget_default | 15.0 | 0 | 13354.26 | 13354.26 | 0.00 | EQUAL |
| month-regression-15 | september-9.19 | actual_short_budget_default | 15.0 | 0 | 9210.68 | 9199.15 | 11.53 | IMPROVED |
| month-regression-15 | september-9.20 | actual_short_budget_default | 15.0 | 0 | 2640.00 | 2640.00 | 0.00 | EQUAL |
| month-regression-15 | september-9.21 | actual_short_budget_default | 15.0 | 0 | 6936.95 | 6936.95 | 0.00 | EQUAL |
| month-regression-15 | september-9.22 | actual_short_budget_default | 15.0 | 0 | 17616.94 | 17616.94 | 0.00 | EQUAL |
| month-regression-15 | september-9.23 | actual_short_budget_default | 15.0 | 0 | 10453.10 | 10453.10 | 0.00 | EQUAL |
| month-regression-15 | september-9.24 | actual_short_budget_default | 15.0 | 0 | 13596.53 | 13596.53 | 0.00 | EQUAL |
| month-regression-15 | september-9.25 | actual_short_budget_default | 15.0 | 0 | 12904.20 | 12904.20 | 0.00 | EQUAL |
| month-regression-15 | september-9.26 | actual_short_budget_default | 15.0 | 0 | 12072.18 | 12082.28 | -10.10 | REGRESSED |
| month-regression-15 | september-9.28 | actual_short_budget_default | 15.0 | 0 | 10879.31 | 10879.31 | 0.00 | EQUAL |
| month-regression-15 | september-9.29 | actual_short_budget_default | 15.0 | 0 | 16837.69 | 16837.69 | 0.00 | EQUAL |
| month-regression-15 | september-9.30 | actual_short_budget_default | 15.0 | 0 | 15798.79 | 15798.79 | 0.00 | EQUAL |
| month-regression-15 | 2026-06-21 | actual_short_budget_default | 15.0 | 0 | 3349.55 | 3349.55 | 0.00 | EQUAL |

候选阶段的直接降费（父阶段与嵌套方法重复覆盖，不能叠加求和）：

| 报告/日/seed | 开关 | 阶段 | 计算秒 | 阶段直接降费 | 依据 |
| --- | --- | --- | ---: | ---: | --- |
| phase2-cold/september-9.22/s0 | deep_batch | 1:CURRENT_INPUT_COLD_SEED | 2.8588 | 2379.54 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| phase2-cold/september-9.22/s0 | deep_batch | 2:MULTI_START:round1 | 8.917 | 1316.79 | EXPLICIT_RECORDED_FEE_REDUCTION |
| phase2-cold/september-9.22/s0 | deep_batch | 3:MULTI_START:round2 | 8.5845 | 102.61 | EXPLICIT_RECORDED_FEE_REDUCTION |
| phase2-cold/september-9.22/s0 | deep_batch | 4:MULTI_START:round3 | 8.8745 | 24.17 | EXPLICIT_RECORDED_FEE_REDUCTION |
| phase2-cold/september-9.22/s0 | deep_batch | 5:MULTI_START:round4 | 8.9025 | 640.71 | EXPLICIT_RECORDED_FEE_REDUCTION |
| phase2-cold/september-9.22/s0 | deep_batch | 6:COLUMN_SELECTION | 7.8138 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| phase2-cold/september-9.22/s0 | deep_batch | 7:SMALL_GROUP_INTENSIFICATION | 2.5078 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| phase2-cold/september-9.22/s0 | deep_diversity | 1:CURRENT_INPUT_COLD_SEED | 2.7649 | 2871.17 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| phase2-cold/september-9.22/s0 | deep_diversity | 2:MULTI_START:round1 | 8.8096 | 1075.21 | EXPLICIT_RECORDED_FEE_REDUCTION |
| phase2-cold/september-9.22/s0 | deep_diversity | 3:MULTI_START:round2 | 8.2956 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| phase2-cold/september-9.22/s0 | deep_diversity | 4:MULTI_START:round3 | 9.1312 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| phase2-cold/september-9.22/s0 | deep_diversity | 5:MULTI_START:round4 | 9.2684 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| phase2-cold/september-9.22/s0 | deep_diversity | 6:COLUMN_SELECTION | 9.9671 | 68.78 | EXPLICIT_RECORDED_FEE_REDUCTION |
| phase2-cold/september-9.22/s0 | deep_diversity | 7:SMALL_GROUP_INTENSIFICATION | 1.3459 | 0.37 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.11/s0 | joint | 1:CURRENT_INPUT_COLD_SEED | 1.7231 | 1407.01 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.11/s0 | joint | 2:MULTI_START:round1 | 4.3023 | 741.11 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | joint | 3:MULTI_START:round2 | 3.9925 | 40.31 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | joint | 4:MULTI_START:round3 | 4.5536 | 10.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | joint | 5:MULTI_START:round4 | 4.3887 | 238.40 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | joint | 6:COLUMN_SELECTION | 2.0729 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | joint | 7:JOINT_REOPTIMIZATION | 3.286671 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | joint | 8:SMALL_GROUP_INTENSIFICATION | 0.4689 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.22/s0 | joint | 1:CURRENT_INPUT_COLD_SEED | 1.4986 | 1990.05 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.22/s0 | joint | 2:MULTI_START:round1 | 4.3043 | 698.62 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | joint | 3:MULTI_START:round2 | 4.2713 | 1054.06 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | joint | 4:MULTI_START:round3 | 4.3575 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | joint | 5:MULTI_START:round4 | 4.5268 | 711.08 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | joint | 6:COLUMN_SELECTION | 2.0828 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | joint | 7:JOINT_REOPTIMIZATION | 3.037543 | 13.10 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | joint | 8:SMALL_GROUP_INTENSIFICATION | 0.4556 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.11/s0 | quote_rotation | 1:CURRENT_INPUT_COLD_SEED | 1.7235 | 1322.46 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.11/s0 | quote_rotation | 2:MULTI_START:round1 | 4.2285 | 823.41 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | quote_rotation | 3:MULTI_START:round2 | 4.0041 | 42.56 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | quote_rotation | 4:MULTI_START:round3 | 4.5746 | 10.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | quote_rotation | 5:MULTI_START:round4 | 4.3965 | 238.40 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | quote_rotation | 6:COLUMN_SELECTION | 2.0035 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | quote_rotation | 7:CANDIDATE_EXPANSION | 3.310399 | 468.63 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | quote_rotation | 8:SMALL_GROUP_INTENSIFICATION | 0.2591 | 20.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.22/s0 | quote_rotation | 1:CURRENT_INPUT_COLD_SEED | 1.5328 | 2206.38 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.22/s0 | quote_rotation | 2:MULTI_START:round1 | 4.2779 | 444.80 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | quote_rotation | 3:MULTI_START:round2 | 4.1773 | 810.94 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | quote_rotation | 4:MULTI_START:round3 | 4.3971 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | quote_rotation | 5:MULTI_START:round4 | 4.589 | 1003.66 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | quote_rotation | 6:COLUMN_SELECTION | 1.9662 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | quote_rotation | 7:CANDIDATE_EXPANSION | 3.500422 | 45.69 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | quote_rotation | 8:SMALL_GROUP_INTENSIFICATION | 0.1355 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.11/s0 | selection_loop | 1:CURRENT_INPUT_COLD_SEED | 1.7262 | 1445.07 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.11/s0 | selection_loop | 2:MULTI_START:round1 | 4.3157 | 870.90 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | selection_loop | 3:MULTI_START:round2 | 3.8974 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | selection_loop | 4:MULTI_START:round3 | 4.4843 | 82.25 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | selection_loop | 5:MULTI_START:round4 | 4.5969 | 481.36 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | selection_loop | 6:COLUMN_SELECTION | 2.0784 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | selection_loop | 7:CANDIDATE_EXPANSION | 3.410855 | 136.47 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | selection_loop | 8:SMALL_GROUP_INTENSIFICATION | 0.1341 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.22/s0 | selection_loop | 1:CURRENT_INPUT_COLD_SEED | 1.5328 | 2206.38 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.22/s0 | selection_loop | 2:MULTI_START:round1 | 4.2757 | 444.80 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | selection_loop | 3:MULTI_START:round2 | 4.1456 | 810.94 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | selection_loop | 4:MULTI_START:round3 | 4.4085 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | selection_loop | 5:MULTI_START:round4 | 4.6127 | 929.82 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | selection_loop | 6:COLUMN_SELECTION | 2.003 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | selection_loop | 7:CANDIDATE_EXPANSION | 3.552472 | 154.07 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | selection_loop | 8:SMALL_GROUP_INTENSIFICATION | 0.0 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.11/s0 | gain_allocation | 1:CURRENT_INPUT_COLD_SEED | 1.7217 | 1407.01 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.11/s0 | gain_allocation | 2:MULTI_START:round1 | 4.2921 | 869.63 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | gain_allocation | 3:MULTI_START:round2 | 3.9515 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | gain_allocation | 4:MULTI_START:round3 | 4.5584 | 219.22 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | gain_allocation | 5:MULTI_START:round4 | 4.4515 | 373.04 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | gain_allocation | 6:COLUMN_SELECTION | 2.1151 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | gain_allocation | 7:CANDIDATE_EXPANSION | 3.459391 | 147.15 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.11/s0 | gain_allocation | 8:SMALL_GROUP_INTENSIFICATION | 0.1303 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.22/s0 | gain_allocation | 1:CURRENT_INPUT_COLD_SEED | 1.539 | 2206.38 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| search-screen/september-9.22/s0 | gain_allocation | 2:MULTI_START:round1 | 4.2788 | 444.80 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | gain_allocation | 3:MULTI_START:round2 | 4.1521 | 1402.20 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | gain_allocation | 4:MULTI_START:round3 | 4.5021 | 0.47 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | gain_allocation | 5:MULTI_START:round4 | 4.6775 | 368.47 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | gain_allocation | 6:COLUMN_SELECTION | 1.9847 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | gain_allocation | 7:CANDIDATE_EXPANSION | 3.342992 | 80.23 | EXPLICIT_RECORDED_FEE_REDUCTION |
| search-screen/september-9.22/s0 | gain_allocation | 8:SMALL_GROUP_INTENSIFICATION | 0.2386 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| production-600/september-9.22/s0 | remaining_budget_loop | 1:CURRENT_INPUT_COLD_SEED | 2.8097 | 2677.45 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| production-600/september-9.22/s0 | remaining_budget_loop | 2:MULTI_START:round1 | 42.2262 | 1400.83 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s0 | remaining_budget_loop | 3:MULTI_START:round2 | 29.4655 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s0 | remaining_budget_loop | 4:MULTI_START:round3 | 44.2041 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s0 | remaining_budget_loop | 5:MULTI_START:round4 | 44.0692 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s0 | remaining_budget_loop | 6:MULTI_START:round5 | 29.3492 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s0 | remaining_budget_loop | 7:MULTI_START:round6 | 29.6731 | 208.07 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s0 | remaining_budget_loop | 8:MULTI_START:round7 | 54.0606 | 66.65 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s0 | remaining_budget_loop | 9:MULTI_START:round8 | 29.4044 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s0 | remaining_budget_loop | 10:MULTI_START:round9 | 78.24 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s0 | remaining_budget_loop | 11:COLUMN_SELECTION | 58.2196 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s0 | remaining_budget_loop | 12:SMALL_GROUP_INTENSIFICATION | 2.3381 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| production-600/september-9.22/s0 | remaining_budget_loop | 13:CANDIDATE_EXPANSION | 53.191732 | 215.23 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s1 | remaining_budget_loop | 1:CURRENT_INPUT_COLD_SEED | 2.6845 | 2871.17 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| production-600/september-9.22/s1 | remaining_budget_loop | 2:MULTI_START:round1 | 42.3574 | 1075.21 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s1 | remaining_budget_loop | 3:MULTI_START:round2 | 29.648 | 121.04 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s1 | remaining_budget_loop | 4:MULTI_START:round3 | 44.2628 | 285.63 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s1 | remaining_budget_loop | 5:MULTI_START:round4 | 44.0744 | 73.35 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s1 | remaining_budget_loop | 6:MULTI_START:round5 | 29.6488 | 8.92 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s1 | remaining_budget_loop | 7:MULTI_START:round6 | 29.3803 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s1 | remaining_budget_loop | 8:MULTI_START:round7 | 53.7126 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s1 | remaining_budget_loop | 9:MULTI_START:round8 | 29.4005 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s1 | remaining_budget_loop | 10:MULTI_START:round9 | 78.4495 | 50.93 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s1 | remaining_budget_loop | 11:COLUMN_SELECTION | 5.3169 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/september-9.22/s1 | remaining_budget_loop | 12:SMALL_GROUP_INTENSIFICATION | 2.2513 | 7.21 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| production-600/september-9.22/s1 | remaining_budget_loop | 13:CANDIDATE_EXPANSION | 59.812137 | 69.39 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s0 | remaining_budget_loop | 1:CURRENT_INPUT_COLD_SEED | 2.7731 | 2248.41 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| production-600/2026-10-20/s0 | remaining_budget_loop | 2:MULTI_START:round1 | 42.1909 | 1831.29 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s0 | remaining_budget_loop | 3:MULTI_START:round2 | 29.4401 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s0 | remaining_budget_loop | 4:MULTI_START:round3 | 44.0464 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s0 | remaining_budget_loop | 5:MULTI_START:round4 | 44.0428 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s0 | remaining_budget_loop | 6:MULTI_START:round5 | 29.3372 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s0 | remaining_budget_loop | 7:MULTI_START:round6 | 29.3815 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s0 | remaining_budget_loop | 8:MULTI_START:round7 | 54.0674 | 43.45 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s0 | remaining_budget_loop | 9:MULTI_START:round8 | 29.5796 | 18.50 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s0 | remaining_budget_loop | 10:MULTI_START:round9 | 78.7419 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s0 | remaining_budget_loop | 11:COLUMN_SELECTION | 6.5735 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s0 | remaining_budget_loop | 12:SMALL_GROUP_INTENSIFICATION | 1.5044 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| production-600/2026-10-20/s0 | remaining_budget_loop | 13:CANDIDATE_EXPANSION | 59.875793 | 10.55 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s1 | remaining_budget_loop | 1:CURRENT_INPUT_COLD_SEED | 2.8493 | 2248.41 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| production-600/2026-10-20/s1 | remaining_budget_loop | 2:MULTI_START:round1 | 42.1876 | 1826.13 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s1 | remaining_budget_loop | 3:MULTI_START:round2 | 29.4694 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s1 | remaining_budget_loop | 4:MULTI_START:round3 | 44.181 | 9.79 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s1 | remaining_budget_loop | 5:MULTI_START:round4 | 44.0048 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s1 | remaining_budget_loop | 6:MULTI_START:round5 | 29.3541 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s1 | remaining_budget_loop | 7:MULTI_START:round6 | 29.3633 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s1 | remaining_budget_loop | 8:MULTI_START:round7 | 54.0013 | 17.31 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s1 | remaining_budget_loop | 9:MULTI_START:round8 | 29.5078 | 78.52 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s1 | remaining_budget_loop | 10:MULTI_START:round9 | 78.7548 | 96.23 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s1 | remaining_budget_loop | 11:COLUMN_SELECTION | 58.1814 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| production-600/2026-10-20/s1 | remaining_budget_loop | 12:SMALL_GROUP_INTENSIFICATION | 1.7785 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| production-600/2026-10-20/s1 | remaining_budget_loop | 13:CANDIDATE_EXPANSION | 53.797568 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| critical-graph-paired/critical-collected/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 2.8647 | 3054.18 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| critical-graph-paired/critical-collected/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 8.8281 | 963.32 | EXPLICIT_RECORDED_FEE_REDUCTION |
| critical-graph-paired/critical-collected/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 8.513 | 168.02 | EXPLICIT_RECORDED_FEE_REDUCTION |
| critical-graph-paired/critical-collected/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 8.9227 | 234.25 | EXPLICIT_RECORDED_FEE_REDUCTION |
| critical-graph-paired/critical-collected/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 8.9947 | 62.99 | EXPLICIT_RECORDED_FEE_REDUCTION |
| critical-graph-paired/critical-collected/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 9.8426 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| critical-graph-paired/critical-collected/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 1.4871 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.01/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.8536 | 1744.32 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.01/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1517 | 975.32 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.01/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.0721 | 433.11 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.01/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2159 | 25.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.01/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.1919 | 0.86 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.01/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.7729 | 784.25 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.01/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.0 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.02/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.8598 | 767.43 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.02/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1101 | 290.09 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.02/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.939 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.02/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.234 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.02/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.4139 | 9.63 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.02/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 1.3811 | 66.73 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.02/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.7268 | 16.25 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.03/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7053 | 720.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.03/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1614 | 572.68 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.03/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9571 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.03/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2936 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.03/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3518 | 5.24 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.03/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.5061 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.03/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.4809 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.04/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.8664 | 1797.91 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.04/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1406 | 803.80 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.04/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.168 | 18.67 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.04/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2465 | 171.33 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.04/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.0576 | 420.03 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.04/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.6822 | 149.46 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.04/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.1622 | 9.52 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.05/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7301 | 110.12 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.05/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.099 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.05/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9606 | 301.72 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.05/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2796 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.05/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3813 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.05/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 1.0844 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.05/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.2733 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.06/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.0373 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.06/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.2754 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.06/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.0746 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.06/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.4771 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.06/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.576 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.06/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 0.6275 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.06/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.5475 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.07/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7954 | 168.70 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.07/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1344 | 268.77 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.07/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.934 | 67.53 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.07/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2609 | 40.43 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.07/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3242 | 16.12 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.07/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.5868 | 114.24 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.07/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.3476 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.08/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7402 | 1685.95 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.08/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.169 | 730.35 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.08/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.0827 | 1466.87 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.08/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2611 | 264.38 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.08/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.2187 | 55.58 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.08/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.7222 | 215.58 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.08/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.1264 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.09/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7886 | 557.48 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.09/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1294 | 131.99 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.09/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9486 | 351.50 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.09/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2347 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.09/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3259 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.09/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 1.2828 | 138.77 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.09/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.5962 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.10/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7833 | 393.45 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.10/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1223 | 315.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.10/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9636 | 186.55 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.10/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2718 | 166.77 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.10/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3266 | 15.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.10/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.5363 | 0.50 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.10/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.4021 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.11/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7184 | 1310.70 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.11/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1563 | 430.56 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.11/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.1701 | 391.56 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.11/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.1687 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.11/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.2575 | 134.27 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.11/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.6186 | 271.32 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.11/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.2454 | 26.13 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.12/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.8148 | 345.72 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.12/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1327 | 267.75 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.12/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9478 | 579.20 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.12/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2436 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.12/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.2987 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.12/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 1.4828 | 25.99 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.12/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.7317 | 38.93 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.13/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.1469 | 130.92 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.13/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.2503 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.13/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.0561 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.13/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.4455 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.13/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.5562 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.13/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 0.5764 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.13/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.5516 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.14/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7088 | 63.70 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.14/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1504 | 269.71 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.14/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9567 | 306.59 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.14/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.284 | 29.43 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.14/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3558 | 23.12 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.14/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.5657 | 21.15 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.14/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.3176 | 2.09 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.15/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.71 | 1516.21 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.15/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1875 | 811.07 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.15/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.1309 | 569.87 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.15/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.1942 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.15/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.2716 | 9.68 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.15/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.6701 | 163.94 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.15/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.1525 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.16/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7882 | 641.06 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.16/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.097 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.16/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9764 | 204.50 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.16/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2495 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.16/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3196 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.16/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 1.1491 | 282.75 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.16/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.6736 | 82.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.17/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7686 | 321.36 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.17/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1579 | 636.17 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.17/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9345 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.17/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.3008 | 95.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.17/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3122 | 5.37 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.17/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.6972 | 75.95 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.17/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.1914 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.18/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.725 | 613.93 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.18/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1688 | 85.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.18/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.0337 | 615.17 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.18/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2127 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.18/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.2912 | 85.41 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.18/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 1.8575 | 5.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.18/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.5888 | 15.73 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.19/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.8501 | 126.07 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.19/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1025 | 935.65 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.19/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9591 | 74.83 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.19/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2585 | 83.67 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.19/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3016 | 144.36 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.19/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 1.2721 | 67.53 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.19/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.7276 | 59.24 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.20/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.0379 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.20/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.2754 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.20/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.0681 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.20/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.4798 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.20/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.5979 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.20/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 0.6408 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.20/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.0097 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.21/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7059 | 84.55 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.21/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1287 | 400.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.21/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9606 | 120.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.21/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.3067 | 10.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.21/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3081 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.21/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.235 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.21/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.424 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.22/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7215 | 1379.05 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.22/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1698 | 588.49 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.22/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.1929 | 1316.52 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.22/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.1604 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.22/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.1777 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.22/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.7199 | 5.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.22/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.178 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.23/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.8481 | 1380.22 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.23/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1232 | 587.83 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.23/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9352 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.23/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2237 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.23/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.2908 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.23/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 1.4929 | 72.17 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.23/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.728 | 163.18 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.24/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.785 | 417.38 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.24/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1276 | 75.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.24/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9236 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.24/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2771 | 110.81 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.24/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3659 | 136.28 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.24/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 1.3664 | 115.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.24/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.611 | 5.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.25/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.8661 | 1945.83 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.25/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1234 | 675.18 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.25/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.1196 | 534.07 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.25/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.1397 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.25/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.1883 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.25/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.6463 | 35.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.25/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.2281 | 278.72 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.26/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.8518 | 1049.40 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.26/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1028 | 49.38 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.26/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9057 | 140.42 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.26/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2292 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.26/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3209 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.26/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 1.7445 | 18.43 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.26/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.4607 | 2.59 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.28/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7084 | 391.85 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.28/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1429 | 156.30 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.28/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9856 | 551.61 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.28/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.2557 | 53.07 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.28/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.2767 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.28/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.6037 | 116.36 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.28/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.3169 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.29/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.8707 | 1640.90 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.29/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1222 | 1183.01 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.29/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.1921 | 1160.95 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.29/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.1347 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.29/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.1315 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.29/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 2.648 | 917.95 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.29/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.219 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.30/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.7518 | 326.06 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/september-9.30/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.1371 | 399.21 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.30/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 1.9803 | 439.75 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.30/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.248 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.30/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.3117 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.30/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 1.3324 | 58.35 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/september-9.30/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.6312 | 110.84 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/2026-06-21/s0 | actual_short_budget_default | 1:CURRENT_INPUT_COLD_SEED | 0.146 | 530.45 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |
| month-regression-15/2026-06-21/s0 | actual_short_budget_default | 2:MULTI_START:round1 | 2.2552 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/2026-06-21/s0 | actual_short_budget_default | 3:MULTI_START:round2 | 2.0563 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/2026-06-21/s0 | actual_short_budget_default | 4:MULTI_START:round3 | 2.4416 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/2026-06-21/s0 | actual_short_budget_default | 5:MULTI_START:round4 | 2.5464 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/2026-06-21/s0 | actual_short_budget_default | 6:COLUMN_SELECTION | 0.6856 | 0.00 | EXPLICIT_RECORDED_FEE_REDUCTION |
| month-regression-15/2026-06-21/s0 | actual_short_budget_default | 7:SMALL_GROUP_INTENSIFICATION | 0.0697 | 0.00 | SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER |

全部失败及超时样本：

| 报告 | 日 | 开关 | 侧 | seed | 状态 | 异常类型 |
| --- | --- | --- | --- | ---: | --- | --- |
| — | — | — | — | — | 无 | — |

全部升费样本 7 对，全部超时/失败 0 次，尚未成对 0 次。首次完整可行时间、每轮原生缺边、候选计数、费用轨迹及最终道路复核价变见本机忽略目录 `data/algorithm-20261115/anonymous-results.json`，原始运行工件不随代码提交。

上述重复试验费用不能解释为实际运输支出。未记录的基线诊断显示为空，不能按零处理。不同源码版本的结果分开保留；处置为拒绝的试验仍完整列出。

9月单独回归为29日、每侧15秒、seed 0：基线 322216.47 元，候选 322095.45 元，差额 121.02 元；6日降低、18日持平、5日升高。月报另含预锁6/21小日，双方3349.55元持平。此处不是600秒生产预算收益，也不能归为单一新增阶段效果。

精确核查仅限小规模冻结道路模型：

| 样本 | 任务数 | 独立费用 | 枚举完成 | 最优证明 | 遇到的缺边方向数 |
| --- | ---: | ---: | --- | --- | ---: |
| heldout-2026-06-21 | 12 | 3349.55 | True | False | 42 |
| huaian-five-regression | 5 | 269.10 | True | True | 0 |

五店模型证明269.10元；12店新日虽完成已知道路枚举，遇到42条相关缺边，未取得最优证明。不能把这些证明扩展到完整配送日或真实货车执行。关键缺边扩图后的独立固定候选实验直接降费0.00元，选中新增边0条；额外预算的同一起点联合重优化与选线循环实验直接降费也均为0.00元，分别保留，没有混入冷启动收益。
