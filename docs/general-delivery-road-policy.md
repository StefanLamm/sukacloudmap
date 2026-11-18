# 日常规划道路调用策略审查

本轮 `test` 将生产规划的高德总调用次数固定为无限。显式启用开关、账号共享 QPS、有限网络超时、取消和运行总等待截止仍然有效。无限总次数不等于无限并发、无限重试或无限等待。

本文审查调用功能，不代表费用采用。原source59及唯一候选bf58/app92a正式都NOT_ACCEPTED、四层全False，**尚未完成通用优化**。新功能源full2e/appd2e按7ba独立冻结，五性能位关闭，要求的无限调用/critical/diagnostics/截止保留；自身300/2000真实页面及原始/同选中回执审计均已PASS，只证明功能接入，不冒认旧候选或332性能通过。

## 生产入口检查表

| 入口 | 请求及最终适配器 | 总次数 | 等待、取消和禁网依据 |
| --- | --- | --- | --- |
| 页面“开始规划”，包括新日期、新需求分组 | `frontend/src/plan-panel.tsx` 一律提交 `call_limit:null`；`NewJob`、`PlanningRunner.run` | `None` | 以 `job.created_at` 受理起算含队列的截止，Worker 换算剩余绝对 `wall_deadline`；服务显式 enabled 开关 |
| 同快照再次开始、继续优化 | 同一 jobs API / Worker；历史方案只作当前输入的提示 | `None` | 同一生产适配器控制，不继承旧任务次数额度 |
| 人工调整后核算、`optimize_routes=true` | `MANUAL` jobs API / `allocate_dedicated` | `None` | 人工核算控制与道路适配器共享绝对截止、取消状态 |
| 人工“刷新所选道路” | 相同 Worker，显式 `force_refresh` | `None` | enabled、Key、道路模式先校验；沿用同一个截止 |
| 初始缺边发现、搜索、联合重分配和降费清理 | 使用 Worker 创建的同一个生产道路对象 | `None` | `production_planning=True` 时不使用阶段临时次数 cap；有限计算阶段仍受预算控制 |
| 最终道路复核、失败线路修复、新增桥接 | `DrivingCollector` 委托同一生产道路对象 | `None` | 生产 bridge 总额度和小邻域份额均无限；仍受全局/可选阶段截止控制 |
| 应用道路修复工具 | `scripts/refresh_application_roads.py` 显式生产标记 | `None` | 绝对截止取本次等待预算与授权结束预留的较早值 |
| 输入预锁定的未见日道路冻结 | `scripts/freeze_unseen_roads.py` 显式生产标记 | `None` | 共享 QPS、SIGINT/SIGTERM/外部取消、显式有限等待截止；只写新 graph/cache |

API 接受旧客户端的非负有限 `call_limit`，包括 0、10、350，统一归一为 `None`；负值、布尔值、字符串等非法严格类型仍拒绝。0 不再暗示禁网。没有显式传参也保持无限默认值。

`Settings` 兼容旧 `SUKA_TRUCK_JOB_MAX_CALLS` / `SUKA_DRIVING_JOB_MAX_CALLS` 环境字段，但有效值统一为 `None`；`road_job_max_calls` 不会恢复有限额度。`prepare-test-runtime.py` 在新建或更新隔离运行快照时把这两个字段显式写成 `unlimited`，保留独立 Key、enabled 和优化配置，不修改稳定版配置。

适配器另有防漏：生产对象 `call_limit` getter 恒为 `None`，有限 setter 赋值被忽略并计入 `ignored_call_limit_assignments`。实际启用与否只看 `road_calls_enabled`；关闭后新 HTTP 返回 `LIVE_CALLS_DISABLED`，现有缓存仍可读取。启用但缺 Key 时接口返回 `LIVE_KEY_MISSING`，不以旧 0 请求绕过。

## 预算与控制

默认任务总等待预算是：

`compute_seconds + min(1800, max(120, 2 * compute_seconds))`

因此默认计算 300 秒对应总等待 900 秒；计算 2000 秒对应 3800 秒。API 也接受显式 `wall_budget_seconds`。两种情况均从 `job.created_at` 受理时间起算，包含队列、准备、搜索、人工核算、道路请求、修复、最终验证和保存门禁。

Worker 开始执行时先计算 `queue_wait_seconds`，以 `max(0, wall_budget_seconds - queue_wait_seconds)` 换算剩余的绝对 monotonic 截止。受理时间为秒精度下界，换算采取保守值；`started_at` 不用于重新起算或延长期限。已超期的队列任务在构造道路适配器及调用优化器前停止。排队不扣优化阶段的计算预算，道路等待仍消耗同一个总等待期限。

结果记录 `queue_wait_seconds`、`job_elapsed_seconds`、`total_wait_elapsed_seconds` 和 `deadline_overrun_seconds`。取得最终写锁后及追加方案前刷新计时并检查截止；截止时已验证的完整结果保留在任务中，状态为失败，不追加业务方案。页面通过 capabilities 的默认政策和任务实际 `wall_budget_seconds` 分别显示计算上限、含排队/道路/复核的总等待上限，调用次数的无限值另行显示。

TruckRoad 和 DrivingRoad 都将 QPS 排队及 HTTP 等待（包含失败、重试和中止前已经等待的部分）累积到 `io_seconds`。DrivingRoad 继承统一排队计时，不重复计入父类等待；计算预算扣除此项。缓存命中和调用适配器以外的时间不计为道路等待。默认时钟为 monotonic，可注入 `timer` 做确定性验证。

每次 GET 前、QPS 等待循环中、响应返回后及网络异常处理时检查取消/等待截止；`CANCELLED` 与 `WALL_BUDGET` 分别记录。HTTPX 单阶段超时为 `min(6 秒, 剩余等待)`；响应过截止不接受、不缓存，也不再发新 GET。同步 HTTPX 的阶段超时不构成操作系统级硬中断承诺，实际时间及截止超出量仍应记录。

共享限流仍由 `AMapRateLimiter` 执行。道路服务按账号配置每秒最多 3 次，多个实例/进程共享 pacing 文件；限流等待的 10 秒上限是单次排队等待，单个连接最多两次尝试是异常重试策略，两者均不是任务总调用次数上限。

## 生产搜索的候选计数与时间边界

生产人工顺序优化、自动分车、小组 legacy 与显式 reference 搜索不再因累计评估达到 10000/20000 或旧 `max_evaluations` 提前结束。每次候选或递归仍检查原计算控制、取消及有限绝对 `wall_deadline`，全缓存搜索也有截止。日常入口继承从 `job.created_at` 受理时间计算并扣除排队的同一截止；重试和内部优化不延长等待。缺少有限绝对截止的直接 production helper 调用明确拒绝。

未显式标 production 的旧 Frozen/Fixture/独立研究路径保留原计数边界。修正后的评估器可以对同一个无 HTTP、只读 Frozen 对象显式设置 `production-search-policy`、原 worker 的有限绝对 deadline 和 monotonic timer；网络仍禁用、图仍冻结、缺边仍拒绝，不会单边补图。需核实际 marker/deadline/timer 及各版本原生 count 分支，不能由类名或统计值推断政策已生效。旧4bc基线本身的无条件限制保留，不由新harness替它删改。这项功能政策修复不单独证明费用改善。

原formal-v1漏此标记而在21/64停止，是协议错误，原报告保留。后续修正政策的source59及bf58两个正式64格均完整、条件和失败/超时列表为空，却费用门槛未达；不能把此次费用失败混成旧协议错误。[全部正式层/日中位/升费](general-delivery-improvements.md#正式未见验收结果与非采用)分别保留；54ac历史与本轮4bc也不混合。

## 保留的离线与研究边界

`FixtureRoad`、`SyntheticRoad`、`FrozenEvidenceRoad`、`CollectedDrivingRoad` 的离线证据角色不发 HTTP；部分对象或 stats 中的 `call_limit=0` 只描述此角色，不等于原生产规划0次，也不能据此推断对象具有同名配额属性或生产搜索政策已应用。离线审计不关闭原生产 `DrivingCollector` 的新增连接补采。网络次数政策与候选搜索计数政策是两个独立维度。

显式低层研究适配器可保留有限调用实验，用于验证额度/重试语义；`collect_critical_algorithm_roads.py` 等旧隔离研究脚本的 12/24/96 清单不属于日常规划入口。生产对象必须显式使用 `production_planning=True`。新的未见日道路冻结脚本没有继承这些清单限制。

10000 条候选保留/缓存预读、96 条几何内存缓存、近邻数量和有限邻域形状是存储与搜索覆盖限制，不构成累计评估次数停机或生产道路总次数 cap。有限时间内不保证穷举全部线路；其算法效果须单独比较，不能用数量增加替代费用改善证明。

## 已完成验证

模拟提供方完成以下检查，均未调用真实网络：

- Truck/Driving 各 361 个唯一方向持续执行，旧 350/10/0 初始值或后续赋值无法恢复生产总额度；共享 QPS、缓存和取消仍有效。
- QPS 等待中达到截止、响应后取消、响应超过截止、网络超时恰好超过截止，分别保留正确停止原因，且不继续发 HTTP。
- 自动、再次自动/刷新、人工核算、人工优化及人工刷新 API 路径最终适配器均为生产 `None`；缺 Key 不允许用旧 0 请求绕过。
- 缺边冻结脚本通过大于旧 96 次边界、同仓/完整报价签名、跨城相容组合、双向连接、原输入/道路/提示不变及取消/截止验证。
- Truck/Driving 等待计时测试覆盖成功、缓存、失败重试、排队取消和截止，注入同一时钟验证只累计一次。
- 生产人工搜索越过 10000、自动分车及小组旧搜索越过 20000 次评估后继续，直至计算或等待截止；研究路径保留原边界。10 项定向测试还覆盖缓存搜索取消、禁止延长截止及完整方案保护。

前端相关 36 项检查通过；隔离运行准备工具 25 项检查通过。道路策略各轮测试有重叠，不将测试数量相加当成效果证明。

实际高德任务使用开发前预锁定的 4 个完整日期、8 个有/无提示条件，只按输入和坐标补路，未读取 baseline 或算法费用输出：

| 项目 | 结果 |
| --- | --- |
| 全局去重目标方向 | 2037 |
| 只读复用并预冻结的原有回执 | 1249 |
| 真正缺边/真实成功 GET | 788 / 788 |
| 真实重试、失败 | 0 / 0 |
| 完成 350 次后继续 | 是，持续到 788 次 |
| 网络方向采集阶段 | 283.6436 秒 |
| 含图复制与最终核查总时间 | 305.8995 秒 |
| 等待截止/超出量 | 1200 秒 / 0 秒 |
| 最终目标未采到的方向 | 0 |
| 原有测量 | 26620 条逐行保留 |
| 新增测量 | 23 条已有验证图导入 + 788 条新采集 |
| 最终共享测量池 | 27431 条 |
| 原历史提示 | 6400 条 / 0 条，各条件原样复制 |

新道路批次 hash：`1c458fa913fb3b84e5bb8ff764213cfc3c6eecbf4d8db72e7ea1a45427595906`。

隔离数据入口：`data/general-delivery-20261115/acceptance/roads-collected-v2/frozen-cases-hash-schema-v2.json`。此版本仅修正索引 hash 字段语义：`hint_hash` 对齐评估器的 `route_seed_samples` 字典规范 hash，原提示表逐行 hash 保存在 `hint_rows_hash`。旧索引原样保留，道路、回执、输入和提示内容均未改。有/无提示的两个图共享相同测量批次，分别保留自己的提示；新旧算法应使用此索引做同图对照。原输入、旧道路图、历史方案、稳定配置和业务数据库没有写入。

另一次 `roads-collected-v1` 在默认沙箱中发生网络失败，25 次尝试没有取得新测量，已主动取消并保留 `INCOMPLETE/CANCELLED` 报告。该失败批次与 v2 完整批次分别保存，不混入验收费用或节省率。

## 被拒绝候选页面与功能证据归属

历史候选bf58/app92a相对旧f978有57个app文件字节一致，route_deep三个默认布尔关闭；这只说明这些历史版本中API/QPS/取消/截止证据的字节关联。mock和真实788保留原标签，不改写为新版本重跑。功能源full2e/appd2e另有自身300/2000页面审计，不直接转标旧证据。

下列8003页面使用当时92a候选真实默认，无实验覆盖/初始方案，正常提示仍有；不是另行验证的五性能位全False功能源：

| 被拒绝92a页面 | 300秒，11/18分组 | 2000秒，11/19分组 |
| --- | --- | --- |
| 实际计算 / 总等待 | 248.0641 / 310.958121秒 | 1308.4281 / 1379.789058秒 |
| 计算 / 总等待上限 | 300 / 900秒 | 2000 / 3800秒 |
| HTTP尝试 / 选中实际回执 | 140 / 130 | 159 / 135 |
| 总次数政策 / 账号QPS | None、无限 / 3 | None、无限 / 3 |
| 同组选中实际回执独立核价及重放 | PASS | PASS |

这两次有用任务没有达到350次，不能以其调用数重复声称越过阈值；超过350的真实证据仍是上述788方向补采，模拟边界另列。页面新任务不是人工核算/刷新所有按钮都已真实操作的证明；那些入口的覆盖仍按各自mock和静态链范围披露。QPS3的共享/逐秒边界由pacer与mock证明，不用平均调用速度替代。

两页面均为 LIVE/DRIVING 参考 DRAFT，不能确认执行，不称货车通行已经核验。最终复核费用分别变化+16.49、+85.58元，属于道路批次变化，不是开关收益。最终UI审计及截图见 `qa-audits/actual-page-{300,2000}-selected-v4/`、`qa-inputs/ui{300,2000}-result-selected-v4.jpg`；完整阶段与费用边界见[已知案例说明](general-delivery-known-case-findings.md)。离线配对矩阵另外锁定915/3815秒进程等待（含15秒管理余量），不与页面900/3800混作同一等待条件。

无限调用及补采有上述历史证据，不证明通用降费或全局最优。新功能源300/2000秒分别121/147次HTTP、114/131个选中回执，总次数None、QPS3；计算199.0435/1357.6057秒，总等待258.081358/900及1424.791687/3800秒，原始及同选中回执回放均PASS，审计器未启动优化器、HTTP或旧reference graph。详细身份与SHA见[功能版页面](general-delivery-known-case-findings.md#独立功能保留版页面交付状态)。不称332性能通过，不再用解封日求过。

完整332有34个升费pair、8个预期不可行负例且无非预期失败/超时；两份正式64格全部费用层拒绝。244 generic+4 UI诊断有效与0gap不能扩写到controlled88条：其中80条缺supervisor_seconds/wait_overrun_seconds，8条负例另有首完整/compute的N/A。controlled旗标supervisor_timeout=False、可测正overrun=0，不等于未记录的总监控等待数值已证明零超限；worker读取输入和构图之后的elapsed也不能补它。3次fresh复核升费与同图最低完整费用保护分开，[全坏样本与缺测CSV](general-delivery-improvements.md#全部332条记录与计时缺测范围)全部保留。

上述UI4和3次fresh升费只属原332附件，不含新功能页面。新功能300的23625.62→23623.50（-2.12）与2000的23266.57→23267.25（+0.68）均为自身fresh道路变化，后者升费也完整保留，不当性能开关收益。2000旧loop实际59.863472秒、9cycles、上报candidate_count896、直接降费0；critical已执行但2003方向均已知、新HTTP0，joint SWITCH_DISABLED。有限池费用OPTIMAL不证明全局。V5审计33b阈值不混入1c4/fresh图，见[已知案例](general-delivery-known-case-findings.md#v5最终独立冷启动核对)。私有附件仅相对定位，不提交`data/`或真实配置。
