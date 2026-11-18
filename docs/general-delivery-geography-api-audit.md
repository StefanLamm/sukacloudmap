# 地理资料与地图服务的调用范围审计

本次拓展审计本身仅只读审查隔离 `test` 源码及调用关系，没有运行真实 API、修改地点资料、队列、账号或业务数据库。没有修改或发布稳定服务；此前道路补采的真实 API 证据另行报告。

结论：除 TruckRoad/DrivingRoad 外，当前地理编码、POI、地点后台队列、地图代理及行政区查询也没有 350 次、10 次等累计调用总额度。发现的 10、256、1、2、3 等数值各有超时、结果数、缓存、并发或单次重试含义，不能混为总次数上限。以下结论是源码审计，不把它冒称为这些独立服务都完成了超过 350 次真实网络验收。

## 入口与规划的关系

| 入口 | 调用链与副作用 | 与规划流程的实际关系 | 累计总次数限制 |
| --- | --- | --- | --- |
| 开始规划、同快照再次规划、新日期/分组 | `POST /api/planning/jobs` → PlanningRunner → 算法 → TruckRoad/DrivingRoad | 直接规划链；沿用生产 `None`、共享 QPS、取消及绝对等待截止 | 无，生产 `None` |
| 人工核算、线路优化、道路刷新/复核、连接补采 | 同一 PlanningRunner 道路对象及 DrivingCollector/补边生成器 | 直接规划链；没有嵌套地理编码调用 | 无，生产 `None` |
| 已声明坐标的参考转换 | `coordinates.provider_point`，WGS84/BD09 → GCJ02 | 道路适配器直接调用，但它是纯数学转换；不查地址、不发 HTTP、不补写原坐标 | 不适用，无 HTTP |
| 基础资料位置编辑中的“搜索位置” | `GET /api/maps/search?q=…` → MapSearchService → AMapSearchService | 独立资料编辑入口。搜索本身只返回候选；选点后保存才写资料。规划只读地图未传 `onChange`，不显示文字搜索控件 | 无累计 cap |
| 仓库/门店资料导入提交 | `master_imports.commit_import` → `enqueue_location` → LocationTask | 导入无坐标记录会自动入队，不仅是手工点按钮。队列独立于 PlanningJob | 无累计 cap |
| 基础资料“关联缺失位置” | `POST /api/master-data/{warehouses|stores}/locate` → enqueue_location | 显式资料维护入口。单个选择请求最多 10000 条是资料批量请求大小，不是服务 lifetime API 次数额度 | 无累计 cap |
| 后台地址关联 | LocationRunner → MapSearchService → geocode/POI，成功后 `save_entity(... automatic_location=True)` | 会写仓库/门店坐标、位置来源和资料版本；与规划输入有间接联系，但不由开始规划/继续/人工核算触发或等待 | 无累计 cap；见下述失败转人工机制 |
| 规划位置图、方案轨迹图 | LocationMap → AMap JS SDK → 受限地图 styles/maps 代理；直接绘制已有点与折线 | 是规划页面的显示依赖，不能笼统称“无关”。没有调用浏览器 Driving/Geocoder/PlaceSearch 来替后台算路或自动补点 | 无累计 cap |
| 行政区选择器 | `GET /api/master-data/regions` → region_tree → 高德 district tree | 基础资料/附加费编辑、行政区筛选的辅助入口；缓存刷新写的是行政区公共缓存，不是仓库门店记录 | 无累计 cap |
| catalog 中行政区补全 | `master_service.catalog/management_catalog` → `infer_region` → cached_tree | 可能影响返回的派生行政区字段，故与冻结输入有关；明确只读本地缓存，不会现场调用高德，也不保存人工资料 | 不适用，无 HTTP |
| OPEN 地图地点搜索 | 同一 maps/search → OpenMapSearchService → Photon | 非高德备用资料搜索入口；不参与道路核价，不把搜索结果自动写入规划快照 | 无累计 cap |

调用关系由以下源码交叉核对，而不是仅凭服务命名分类：

- [PlanningRunner](../app/plan_service.py)、[规划 API](../app/plan_api.py)、[冻结输入](../app/planning_inputs.py)、[道路适配器](../app/road_service.py)没有调用 MapSearchService、LocationRunner 或 enqueue_location。
- enqueue_location 的生产调用点是[基础资料批量关联](../app/master_api.py)和[基础资料导入提交](../app/master_imports.py)，需求导入仅复用读取表格的工具，没有调用地理编码入队函数。
- [后台队列](../app/location_tasks.py)与规划队列都在[应用生命周期](../app/main.py)启动，各有自己的任务、租约和控制状态。后台关联可与规划并行，不能把“独立调用链”理解为“不会影响规划”。
- [规划页面](../frontend/src/planning.tsx)、[方案页面](../frontend/src/plan-panel.tsx)的 LocationMap 均不传位置修改回调；[AMap 地图组件](../frontend/src/amap-map.tsx)只在有 `onChange` 时显示文字搜索/选点编辑。

## HTTP 与数值限制逐项核对

生产 `app/` 内找到 6 处 HTTPX Client 构造：Truck、Driving、AMap fetch、AMap JS 代理、行政区树、Photon。未找到另一条以 requests/urlopen/AsyncClient 绕开这些服务的生产规划路径。脚本里的本机 HTTP 管理/验收客户端仍调用既有应用 API；已知有限 collect/replay 研究工具另有明确隔离用途，不作为日常服务入口。

| 位置 | 实际限制 | 是否是累计 API 次数 cap |
| --- | --- | --- |
| `AMapSearchService.fetch` | 一次 geocode 或 POI 请求最多 3 次尝试（最多 2 次重试）；每次 HTTPX 阶段超时 10 秒，响应大小 2 MiB | 否；没有总调用计数器或 call_limit/max_calls |
| `AMapSearchService.search` | geocode 最多处理 5 条、POI offset=5/page=1，合并最多返回 10 条建议 | 否；这是一次搜索的候选数限制，可能导致转人工，不代表允许调用 10 次 |
| 同上 | LRU 最多 256 个 query；常规缓存 86400 秒，有 notice 时 30 秒 | 否；满后淘汰旧缓存，仍可请求新地址 |
| 同上 | POI 上游配额/认证错误冷却 900 秒，其他不可用错误冷却 30 秒；保留 geocode 候选 | 否；由真实上游错误触发的次级端点降级，不是内部累计额度耗尽 |
| `LocationRunner.claim` | SQL limit(1)、同一时刻一个地址任务、最近尝试间隔至少 2 秒、任务租约 60 秒 | 否；是全局串行领取与节流，健康服务下队列可持续处理后续任意数量地址 |
| `LocationRunner.run_once` | 单地址精确匹配失败转 MANUAL；上游 502/503 时未处理队列统一转人工，避免继续发送失败地址请求 | 否；但确实会停止该批自动关联，需要明确披露，不能称“队列永不停止” |
| AMap JS security_proxy | 仅允许 styles/maps 两条上游路径，2 次单请求尝试、10 秒 HTTP 阶段超时、8 MiB 响应限制 | 否；更不能借该代理绕过后台道路接口 |
| AMap JS SDK 加载 | 一次加载最多自动尝试 2 次，每次 45 秒；用户可重新加载 | 否；显示组件的加载策略，不限制道路规划累计次数 |
| `region_tree` | 30 天缓存、一次全树查询、20 秒 HTTP 阶段超时；失败保留旧树；30 省/300 城是完整性检查 | 否；完整性门槛不是 300/350 次请求额度 |
| `OpenMapSearchService` | 每秒一次文字搜索，单次取 5 条，10 秒阶段超时，256 条 LRU | 否；且不是高德道路服务 |
| `AMapRateLimiter` | web 服务每秒最多配置 3 次，JS 每秒最多配置 10 次；单次领取最多等待 10 秒 | 否；共享 pacing，没有累计使用量/总次数计数器 |

Key、provider、enabled 和真实上游配额仍有各自作用。`truck_live_calls_enabled` / `driving_live_calls_enabled` 只控制道路服务，不等于关闭全部地图/地理编码服务；后台自动关联由 `map_auto_geocode_enabled` 独立控制。不得把这些权限或可用性开关解释成内部总次数 cap。

## 取消、等待截止和资料保护边界

规划链使用同一 PlanningJob 的取消状态、计算预算及接受任务后总等待截止；Truck/Driving 在排队、GET 前后检查这些状态。

AMapSearchService 和 LocationRunner 当前没有接收 PlanningJob 的取消回调或绝对截止：fetch 调 limiter 时没有传该取消回调，HTTP 只有阶段超时；LocationRunner 只检查服务 stop_event 和自己的地址任务租约。它们是独立的共享资料维护任务，取消某个配送规划不会取消所有人的地址关联。因而本审计**不声称地理编码独立队列已经遵守某个配送 PlanningJob 的 300/2000 秒预算**。

如果以后把“缺坐标自动修复”同步加入规划流程，需要另接该 PlanningJob 的 deadline/cancel，并将建议位置隔离，不能直接复用会写资料的 LocationRunner，也不能悄悄修改冻结输入。目前缺失坐标仍会明确报告 COORDINATES_NOT_READY；已有坐标的参考转换不会调用地理编码，也不会把缓存缺边当成已测道路。

后台自动关联确实可能改变资料 revision/input fingerprint，从而让并行自动规划在发布前报 INPUT_CHANGED。自动规划的发布前校验会保留结果并阻止用变化前输入发布新方案；人工同快照重算仍按其冻结输入处理，不能称为已自动使用新资料回算。这个联系已纳入审计，未简单视为与规划无关。

本轮隔离环境保护措施：`prepare-test-runtime.py`、`start-test.py`、私有验收启动工具显式设 `map_auto_geocode_enabled=False`，`runtime_isolation.validate_test_runtime` 也拒绝隔离测试开启后台自动关联。旧资料、地址、坐标、历史方案与账号没有因本次审计被重写。本轮没有操作 locate、资料导入提交、保存位置等维护入口。

因此，这次拓展审计无需为地理服务删除另一个隐藏的 350/10 总 cap；同时也不扩大任务范围去改动人工地理资料或其后台维护规则。
