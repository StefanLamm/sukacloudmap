# 苏咔云图

2026-11-14 测试分支：应用界面迁移至 Astryx，采用暖中性色与橙色品牌，统一登录、导航、表格、表单、弹窗、规划和地图。开发代理仅连接独立测试版 8002。详见 [界面改造与验收](docs/astryx-ui.md)。

0.16.0（2026-11-14）：门店营业开始/结束时间编辑白屏修复及运行隔离已通过用户验收，合并至`main`并以`v0.16.0`标记发布。稳定版与测试版使用独立源码、Python依赖、前端构建、配置和运行目录；稳定入口使用8000及原业务库，测试入口使用8002及独立副本，原账号密码可登录。测试页面显示“测试版 · 独立数据”，测试修改不会同步到稳定版。完整启动、更新和隔离边界见 [稳定版与测试版运行隔离](docs/runtime-isolation.md)。GitHub为主仓库；Git发布不会自动切换本机运行快照，既有`v0.15.0`标签保留。

0.15.0（2026-11-13）：完整深度选优已接入日常「开始规划」。新建需求分组、录入需求并冻结快照后，默认以300秒计算时限自动完成多轮多种子搜索、跨轮候选汇总、CP-SAT联合选优、高德道路复核及必要的线路修复，保存本次找到的最低费用可行草稿。进度显示当前阶段、轮次和候选费用；真实道路等待另计并有总等待上限，可取消。结果通过完整配送、车辆、时间、报价和道路回执校验后才保存，有限候选池最优不等于所有可能线路的全局最优。

本轮在主库及道路图的私有副本中新建需求组，通过真实HTTP验证225店、4378件、5个种子、10000条保留候选，实际180次高德调用、最终155段选中道路，费用23882.15元；未继承其他分组方案，未修改主库需求或方案，既有test2 V5保留。后端2321项、前端115项、TypeScript及生产构建通过，另有26项发布保护专项通过。新组验收金额是该次输入、时限和道路批次的结果，不保证新建分组复现历史长测金额。详见 [日常深度规划与验收](docs/latest-improvements.md)。外网地址以 `data/external-test/current.json` 为准，发布重启后可能变化。

0.14.0（2026-11-13）：完成103轮算法对照，默认使用以总费用为目标的混合搜索。2026-11-12的test2已追加V5草稿：225店、4378件全部分配，报价23595.33元，比原方案减少6980.04元（22.83%，包含归仓修正和最新道路重算）。原账号、资料、需求、快照及历史方案保留。后端2086项、前端107项及发布专项校验通过；这是普通驾车参考方案，未证明225店全局最优。详见 [算法与道路参考说明](docs/latest-improvements.md)。

2026-11-12功能增补：分页显示页码、支持20/50/100条；人工调整后点击「开始核算」生成新版本并比较费用；高德算路总次数改为无限制，仍遵守各服务每秒3次。当天最晚到店默认跟随门店营业结束时间，支持次日闭店。本轮后端1414项、前端45项及生产构建通过，说明见 [分页、核算与外网测试](docs/latest-improvements.md)。

内部人员共用的配送工作平台。v0.11.1完成失败位置批量重关联、真实配送全量回放及专车轨迹/车辆导入验收，修复地址检索错误分类、受限检索降级、坐标兼容和日期输入问题，并按控制台每秒上限增加共享限流和重试。包含账号与共享资料、四类基础管理、每日需求和输入快照、费用时序、联合排线、人工调整、只读历史与Excel导出。所有业务统一位于「物流线路规划」。本机使用普通驾车参考规划，结果未核验货车限行及计划发车时刻，不能确认执行；正式部署验收仍待目标环境提供。

2026-11-12位置编辑修复：门店、仓库地图搜索只回填地址、坐标、坐标系和位置来源，POI名称不再覆盖业务名称；新增、编辑、单独选位置及主动人工改名均验证通过。本轮前端40项、类型检查和生产构建通过，按用户确认恢复「徐州矿大文昌店」名称并保留新地址与坐标。见 [位置名称修复记录](docs/retest-20261112/location-name-fix.json)。

完整配送验收结果见 [真实配送全量回放与重关联验收](docs/retest-20261112/guide.md)：该轮后端1397项、前端32项通过，150日全部241806件回放通过（含用户确认归合肥仓测试的264行），实际50辆车辆文件导入及真实三店轨迹/改序/历史恢复通过。此前高德接入见 [地图和模板说明](docs/amap-update/guide.md)，四类资料删除规则见 [删除说明](docs/master-update/guide.md)。

已准备 [隔离试用入口](http://127.0.0.1:8001)，使用现有账号和密码；也可双击「启动隔离测试.command」。含两仓、365家门店、150日真实件数，默认日期2026-08-06。试用库及会话与原业务8000端口独立，页面明确标注模拟道路，不能确认执行。使用方法见 [试用指南](docs/hardening/trial-guide.md)，本轮结果和未验事项见 [高强度测试报告](docs/hardening/release-readiness.md)。

2026-11-12增补历史费用核对：按用户确认的“配送日期＋车牌”只取来源首行，金额不同也取首行，195,5等已确认文本按小数点解析。已通过19项专项测试及五份原表独立复核，见 [历史支出规则与结果](docs/hardening/historical-cost-review.md)。整车历史支出不自动变成门店未来拼车单价。

2026-11-12本轮新增需求分组及级联删除、按人工顺序或最低费用优化核算、地区附加费报价、门店省市区筛选/批改/导出、账号删除、地图缩放/全屏、按日期的团队任务及完成/超时分类。后端1451项、前端65项通过，现有历史已按日期迁入「历史需求」组。使用说明与验证见 [最新功能说明](docs/latest-improvements.md)。

0.13.0新增按门店地区附加费、共配无到店时间限制和多起点车辆组合排线；test2刷新真实驾车道路后由31720.97元降至30575.37元（4→5辆专车，未证明全局最优）。详见 [功能与验收说明](docs/latest-improvements.md)。

0.12.0新增多选地区及按区县收费、跨页全选所有、24000秒规划时限、默认10条分页和专车自动分车排线。后端1514项、前端79项通过；支持的分页条数为10、20、50、100。

## 本地启动

当前工作区已安装依赖并构建前端、已有管理员账号。可双击 **启动苏咔云图.command** 在独立后台进程中运行；关闭启动终端或聊天窗口后服务继续运行。也可在项目根目录以前台方式执行：

```bash
./scripts/start.sh
```

全新安装请先使用上述前台命令，首次启动会提示设置管理员密码，账号为 `admin`；密码需12至128位且不回显。应用没有默认密码，也不会自动植入演示账号。访问 [本地工作区](http://127.0.0.1:8000)，前台服务使用 Ctrl+C 停止，后台服务使用 **停止全部测试入口.command** 停止。业务数据库保存在 `data/suka.db`，重启不会清空。

电脑重启、注销或测试进程退出后，双击 **恢复全部测试入口.command** 只恢复本机稳定版（8000）与独立测试版（8002）。按当前选择不启动外网隧道；历史验收快照仍保留。电脑需保持开机；未设置开机自启。

如果自定义账号，先执行以下命令，再启动服务：

```bash
.venv/bin/python -m app.cli init-admin --username your_admin --display-name 系统管理员
```

初始化命令只创建首位管理员；已有管理员时拒绝覆盖密码。后续账号在管理界面创建。停用后可重新启用；停用、重置密码或改变管理员权限均会撤销该账号所有旧会话。新密码不能与原密码相同。

## 新环境安装

使用 Python 3.12 以上、Node.js 22/24 与 pnpm。以下命令均在项目根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
./scripts/build-frontend.sh
./scripts/start.sh
```

前端依赖由 `frontend/pnpm-lock.yaml` 固定，后端和测试依赖由 `requirements.lock` 固定。依赖仅安装到本项目。`.env` 可从 `.env.example` 复制，真实凭证、数据库及缓存不进入版本管理。

开发界面可在 `frontend` 运行 `pnpm dev`，由 Vite 把 `/api` 转发至8000端口。后端运行：

```bash
.venv/bin/python -m app.cli migrate
.venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

## 使用

1. 管理员登录后，在「账号管理」创建业务人员或其他管理员。
2. 在「共享工作区」新增、搜索或编辑记录。所有登录人员都读取同一套资料，业务人员不会看到账号管理及操作记录入口。
3. 管理员可以停用、重新启用账号和重置密码。「操作记录」显示操作人、动作和北京时间。
4. 两人同时修改同一记录，旧修订提交返回409并保留当前表单输入，避免静默覆盖。关闭窗口查看最新记录后再编辑。
5. 所有登录成员可在「仓库管理」「门店管理」「车辆管理」「报价方案」新增、搜索、编辑和删除共用资料。勾选支持跨页批量删除；有关联资料时须先解除关联，整批成功或全部保留。先维护仓库和报价，再关联门店与车辆。
6. 在「物流线路规划」选择配送日期，查看该日有效报价，设置仓库最早发车时间、车辆当天归仓和可用性。四个基础管理页面不再提供业务日期和当日操作，只维护默认资料。
7. 下载页面中的xlsx模板，填写后上传预览。错误定位到来源行和字段；全部通过后才能确认整批导入。按稳定编号或车牌整行更新，可选字段空白会清空原值，未出现在文件内的资料保留。仓库/门店坐标留空时，导入成功后按地址后台自动关联地图；无法精确关联时显示「需手动关联」，可筛选后手动选点或批量重试。
8. 在「物流线路规划」的门店列表勾选两家以上门店，点击「判断报价兼容」。同仓且完整计费参数一致才兼容；不同城市和方案名称不影响判断。道路、容量和时间条件另行验证。

9. 仓库和门店可在「选择位置」中输入地点文字搜索、点击地图或拖拽橙色标记，保存后切换「地图」查看已关联位置。拖拽后可核对和修改文字地址。

10. 在「物流线路规划 → 每日需求与快照」上传每日或月度件数文件，先读取并选择工作表、表头行，再确认列匹配、日期列、行错误、重复汇总及整日替换前后差异。月表按当前配送日期过滤，不把其他日期混入当天；无目标日数据不能清空当天。确认后自动保存规划输入；调整当天资源或资料后可重新冻结，历史内容保留。

当天最晚到店默认跟随门店营业结束时间，次日闭店自动使用次日日期；无需逐店填写。有特殊收货要求时可在当前配送日单独设置，留空或选择「按营业结束时间」恢复默认。营业时间或收货限制变更后，重新冻结输入再规划。

第四阶段操作和接口见 [每日需求与规划输入说明](docs/stage4-guide.md)。地图调整见 [地图与业务板块说明](docs/ui-revision-guide.md)。字段、模板、零价与空值等规则见 [第三阶段使用与接口说明](docs/stage3-guide.md)。

11. 在「方案规划与调整」点击「开始规划」，默认300秒，自动执行多轮多种子搜索、跨轮候选汇总、联合选优和道路复核。查看阶段进度、线路、共配、费用与地图；按门店改方式、车辆及顺序后，点击「开始核算」保存人工调整的新草稿。计算时限可调整到24000秒，网络排队另计且有总等待上限。普通驾车结果保留人工复核提示，不能确认货车执行；历史可恢复和导出。右侧固定道路样例可直接演练，不写入实际业务。

第五至八阶段使用、计算边界及接口见 [规划与人工调整说明](docs/stage5-8-guide.md)，部署和恢复见 [运维交接](docs/deploy-backup-rollback.md)，实际验收结果见 [本轮验证报告](docs/stage5-8-validation.json)。

## 数据库与迁移

使用 SQLAlchemy，Alembic 当前版本为 `0006_group_workflows`，包含持久化定位队列、独立导入修订标记及需求分组工作流，保留账号、共享记录、基础资料、需求与历史方案。建表、索引、外键及修订号均由迁移管理；启动脚本明确执行迁移，应用检查版本，不隐式建表或自动创建账号。

```bash
.venv/bin/python -m app.cli migrate
```

可重复执行。SQLite 每个连接启用外键、忙等待和WAL，新建数据库文件权限为0600；写操作通过SQLite写事务或PostgreSQL固定系统行锁串行化，保证账号生命周期和共享资料更新在同一事务内检查。文件上传先接收受限大小的请求体并解析，再获取写锁并重新认证，避免上传和解析长期占用共享写入锁。

共享数据不按账号过滤，账号保留稳定ID；停用不删除其已创建的资料。账号、会话、基础资料、报价版本、导入批次和操作记录存储于同一数据库。审计与会话时间保存Unix秒，API转为带 `+08:00` 的北京时间；配送日期使用ISO日期，最晚到店保存明确时区的日期时间。金额用规范化十进制文本保存，避免浮点误差。

## PostgreSQL 部署配置

在已有 PostgreSQL 服务上创建专用数据库及账号，将连接配置放入服务器环境或未提交的 `.env`：

```dotenv
SUKA_DATABASE_URL=postgresql+psycopg://suka:YOUR_PASSWORD@127.0.0.1:5432/suka
SUKA_ENVIRONMENT=production
SUKA_PUBLIC_ORIGIN=https://YOUR_INTERNAL_DOMAIN
SUKA_ALLOWED_HOSTS=["YOUR_INTERNAL_DOMAIN"]
SUKA_SECURE_COOKIE=true
```

先执行迁移、初始化管理员，再由进程管理器启动后端；HTTPS反向代理负责TLS，并转发正确的Host。服务只信任配置中的来源，正式环境不允许通配Host。前端构建由同一后端托管，不需要跨域开放API。多工作进程连接同一 PostgreSQL 数据库。`SUKA_BOOTSTRAP_PASSWORD` 仅供受控自动初始化使用，从秘密管理器注入，使用后从环境移除，不经命令行参数传密码。

当前已实测 SQLite；本环境未提供 PostgreSQL 服务，因此未执行 PostgreSQL 真库联调。迁移与驱动适配已提供，上线前需要在目标 PostgreSQL 上复跑第二、三阶段用例。使用SQLite的本地程序不是正式上线验收结论。

默认使用高德JS API 2.0地图和服务端地址解析，新选点保存为GCJ02；已有WGS84/BD09在展示和普通驾车参考请求中显式转换，原坐标不改写，货车接口仍要求核实的GCJ02。在本机`.env`配置`SUKA_AMAP_JS_KEY`、`SUKA_AMAP_SECURITY_JS_CODE`和`SUKA_AMAP_WEB_SERVICE_KEY`。只有JS公开Key通过登录后的配置接口提供给浏览器，安全密钥经同源限定路径代理使用，Web服务Key仅服务端读取，二者不进入前端或日志。`SUKA_MAP_PROVIDER=OPEN`可选用旧版开放地图。

高德请求按当前控制台每秒上限调度：地理编码、关键词搜索、驾车规划各3次/秒，地图认证代理10次/秒，平滑间隔另留20毫秒。主服务、试用和验收服务共用`data/amap/rate-limit.db`，覆盖多页面、后台任务及同机多进程；可以用`SUKA_AMAP_RATE_LIMIT_PATH`指定共用路径。QPS超限会短暂退避重试，地址最多3次、算路最多2次，算路重试计入原任务预算。持续超限只标记当前地址，其他排队记录继续；不维护或限制月调用量。跨主机部署时还需共用限流服务，本轮验证范围为本机。

本机按用户要求设置`SUKA_PLANNING_ROUTE_MODE=DRIVING`及`SUKA_DRIVING_LIVE_CALLS_ENABLED=true`，`SUKA_DRIVING_JOB_MAX_CALLS=unlimited`取消总次数上限。每个服务仍按每秒最多3次排队，任务可取消并受所选优化时限控制。API的`call_limit:null`表示不设次数上限，`0`只使用缓存，正整数可指定本次预算。普通驾车接口返回的里程、耗时及折线用于参考草稿，仍计算容量、同仓、报价和时间条件，不能作为货车限行证明。高德货车接口此前返回10012，已停止复测付费接口。未来切回TRUCK模式仍需独立完成货车能力验收。

## 备份和恢复

本地SQLite使用 `.venv/bin/python scripts/backup_sqlite.py data/suka-backup-UNIQUE.db` 创建一致、私有且经过完整性验证的在线备份；恢复和版本回退按 [运维交接](docs/deploy-backup-rollback.md) 执行。会话数据库也在文件中，备份按业务数据保护。PostgreSQL使用目标环境的 `pg_dump`/恢复流程；本轮尚未执行 PostgreSQL 恢复演练。

## 验证

```bash
.venv/bin/python -m pytest -q
./scripts/build-frontend.sh
# 前端组件测试，需pnpm和Node可用
pnpm --dir frontend test
.venv/bin/python scripts/verify_live_restart.py
.venv/bin/python scripts/benchmark_planning.py --runs 20 --budget 15
```

前三项分别为后端回归、前端类型检查/构建、前端组件测试。重启脚本仅使用临时数据库，启动两个独立Uvicorn进程，通过真实HTTP验证共享资料、账号会话、基础资料、每日需求、快照及方案版本/导出在重启后保留；需要本机端口监听权限，结束时自动停止服务并清理临时数据。最后一项是固定道路求解性能测试，不调用真实货车API。

第四阶段历史结果为163项自动化用例通过，包含46项第四阶段专项与117项回归，另完成浏览器和真实HTTP重启验证，详见 [第四阶段验证记录](docs/stage4-validation.json)。地图调整的117项历史结果及地图交互见 [界面调整验证记录](docs/ui-revision-validation.json)。原第三阶段结果见 [第三阶段验证记录](docs/stage3-validation.json)，第二阶段历史证据见 [第二阶段验证记录](docs/stage2-validation.json)。第三阶段共105项自动化用例通过，包含72项第三阶段用例及33项账号回归用例，另完成浏览器操作和真实HTTP重启验证。测试凭证只用于临时数据库，不会被应用初始化到真实业务库。第一阶段交付文档及原始未执行状态保留，实际进展另行记录。

完整配送验收的 **1397项后端测试、32项前端组件测试通过**；随后位置名称修复的前端测试增至 **40项通过**，类型检查与生产构建通过，后端代码未变更。150日全部真实件数回放、三店真实道路、浏览器人工改序及数据保留见 [本轮交付验证](docs/retest-20261112/guide.md)。可双击「启动本轮完整验收.command」或「查看真实轨迹验收.command」复查归档。最终业务人工复核和正式部署验收仍待完成。

第五至八阶段此前的229项测试与4.15318秒固定数据P95作为历史结果保留，见 [阶段验收记录](docs/stage5-8-acceptance.md)。前一轮高强度测试的1250项后端、5项前端、150日234690件模拟道路回放、12账号2400次并发操作及备份恢复见 [高强度测试报告](docs/hardening/release-readiness.md)。其固定道路20轮P95为4.6574秒；本轮未重复测量该性能数据。

## API 与安全约定

| 路由 | 权限及行为 |
| --- | --- |
| `POST /api/auth/login` | 匿名；JSON账号密码登录，失败统一提示，持久化限速 |
| `GET /api/auth/me` | 已登录；当前账号及CSRF令牌 |
| `POST /api/auth/logout` | 已登录及CSRF；撤销当前会话 |
| `GET/POST /api/accounts` | 管理员；查询或创建账号 |
| `PATCH /api/accounts/{id}` | 管理员及CSRF；更新状态、角色、姓名，要求修订号 |
| `POST /api/accounts/{id}/reset-password` | 管理员及CSRF；重置并撤销所有旧会话，要求修订号 |
| `GET/POST /api/workspace/records` | 已登录；共用资料空间，写操作要求CSRF |
| `PATCH /api/workspace/records/{id}` | 已登录及CSRF；修订冲突返回409 |
| `GET /api/audit-events` | 管理员；最近操作记录 |
| `GET/POST /api/{warehouses,stores,vehicles,quotes}` | 已登录；查询或新增基础资料 |
| `GET/PATCH /api/{kind}/{id}` | 已登录；读取或按修订号更新，写操作要求CSRF |
| `DELETE /api/{kind}/{id}?expected_revision=...` | 已登录及CSRF；校验修订和引用后删除单条资料 |
| `POST /api/master-data/{kind}/bulk-delete` | 已登录及CSRF；所选ID及修订号，1至1000条，整批原子删除 |
| `POST /api/master-data/{warehouses,stores}/locate` | 已登录及CSRF；将所选未定位资料重新加入自动关联队列 |
| `GET /api/master-data/management` | 已登录；读取默认资料，与业务日期和当日覆盖分开 |
| `GET /api/master-data/catalog` | 已登录；供物流线路规划按配送日读取有效资料 |
| `GET /api/maps/config` | 已登录；地图提供方/就绪状态/署名，仅AMAP模式含公开JS Key |
| `GET /api/maps/search?q=...` | 已登录；主动文字搜索，限频、缓存及失败提示 |
| `PUT /api/{warehouses,vehicles}/{id}/days/{date}` | 已登录及CSRF；当日安排，要求修订号 |
| `GET /api/quotes/{id}/versions` | 已登录；只读报价历史 |
| `POST /api/master-data/compatibility` | 已登录及CSRF；同仓与完整报价比较 |
| `GET /api/master-data/templates/{kind}.xlsx` | 已登录；下载模板 |
| `POST /api/master-data/imports/{kind}/preview` | 已登录及CSRF；受限xlsx原始请求体，逐行预览 |
| `POST /api/master-data/imports/{id}/commit` | 已登录及CSRF；基础资料修订校验，事务提交并保留回执 |
| `GET /api/planning/days/{date}` | 已登录；每日需求、统计、输入变化及快照状态 |
| `POST /api/planning/demand-imports/inspect` | 已登录及CSRF；只读识别xlsx工作表及建议表头行 |
| `POST /api/planning/demand-imports/upload` | 已登录及CSRF；受限xlsx上传，显式选择工作表与表头行 |
| `POST /api/planning/demand-imports/{id}/preview` | 已登录及CSRF；列匹配、逐行校验及替换对照 |
| `POST /api/planning/demand-imports/{id}/commit` | 已登录及CSRF；版本检查、整日替换和自动快照 |
| `POST /api/planning/days/{date}/snapshots` | 已登录及CSRF；按输入指纹保存只读版本 |
| `GET /api/planning/snapshots/{id}` | 已登录；查看并校验历史快照 |
| `GET /healthz` | 仅基础健康状态，无业务资料 |
| `GET /api/runtime` | 匿名；仅环境标记、试用默认日期，无凭证或数据库路径 |

密码使用Argon2id，数据库会话只存令牌和CSRF摘要，浏览器使用HttpOnly、SameSite Cookie。正式环境启用Secure及`__Host-` Cookie。写操作校验CSRF与来源；停用、改角色、重置后旧会话失效。错误统一返回 `code/message/details/request_id`，字段错误不返回提交值，日志不打印请求体、密码或地图凭证。

参考实现依据：[FastAPI Cookie](https://fastapi.tiangolo.com/advanced/response-cookies/)、[FastAPI密码散列](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)、[SQLAlchemy SQLite约束](https://docs.sqlalchemy.org/en/21/dialects/sqlite.html)、[Alembic连接与迁移](https://alembic.sqlalchemy.org/en/latest/cookbook.html)。

