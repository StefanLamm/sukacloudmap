# 本地备份、恢复与部署交接

2026-11-11 / v0.8.0。当前已验证SQLite，目标PostgreSQL与生产反向代理尚未提供。

## 启动和发布

Python3.12、Node22/24和pnpm，后端依赖固定在requirements.lock，前端锁定在frontend/pnpm-lock.yaml。新环境安装与管理员初始化见README。已有管理员时不要重新初始化或重置密码。

1. 保留当前代码、frontend/dist与数据库备份，记录版本和摘要。
2. 停止旧应用。安装锁定依赖，运行测试、前端构建。
3. `.venv/bin/python -m app.cli migrate`，当前目标0005_location_tasks，增加地址定位队列，旧未提交导入预览需重新上传。该升级保留已有账号及业务资料。
4. `./scripts/start.sh`，浏览器访问本机8000端口，检查healthz、登录、资料、快照、方案与导出。

第四阶段到本阶段的迁移只增加road_cache、planning_jobs、plan_versions，不修改或清空原有账号和业务资料。应用启动检查schema，不会隐式建表或创建演示账号。真实工作区迁移前自动保存的备份位置与完整性证据在stage5-8-workspace-evidence.json。

## SQLite在线备份

可在项目根目录执行，目标路径必须是新文件：

```bash
.venv/bin/python scripts/backup_sqlite.py data/suka-backup-UNIQUE.db
```

工具使用SQLite backup API取得一致快照，备份文件创建时权限0600，并验证integrity_check、foreign_key_check及schema，输出SHA256。拒绝覆盖既有备份。账号哈希、会话和业务数据都在备份中，应存放于受控目录，不能放进代码仓库或公开分享。

## SQLite恢复

1. 停止所有连接同一数据库的应用进程，保存当前库的独立备份。
2. 在临时目录先用SQLite打开待恢复备份，检查integrity_check和schema。
3. 用SQLite backup API将备份恢复到一个新文件，核对账号/资料数量、快照与方案content_hash。测试中已验证完整恢复后方案内容和金额相同。
4. 将服务的SUKA_DATABASE_URL指向恢复文件，配置权限0600；执行与该版本匹配的迁移后启动。
5. 检查既有账号登录及选定历史方案导出。恢复时间点之后的写入不会在旧备份中出现，应由业务人员核对。

不要在运行中的数据库上直接覆盖单个db文件，尤其不能遗漏可能存在的WAL文件；在线导出应通过backup API。

## 回退

保留v0.4.0代码与构建产物，服务停止后用迁移前备份恢复到新文件，再以旧代码和对应0003_daily_inputs schema启动。不要直接对已产生方案的业务库执行降级删表。只读核对恢复结果后再切换服务路径。回退前的新业务库也需保留，后续可核对和补录备份之后的操作。

自动化已在临时库验证0004→0003→0004的结构往返及元数据一致性；此测试不表示允许在实际库删除历史方案。

## PostgreSQL与正式环境

正式部署需要专用PostgreSQL账号、HTTPS域名、ALLOWED_HOSTS、PUBLIC_ORIGIN、Secure Cookie，参考.env.example。迁移、bootstrap和服务应使用同一个数据库配置。多进程共享数据库，后台工作使用短事务认领；不要把SQLite本地性能等同于生产并发性能。

由部署负责人使用pg_dump/pg_restore完成备份、恢复、权限和回退演练，然后复跑全部测试与真实HTTP重启测试。当前没有目标环境，因此PostgreSQL、TLS、反向代理、正式货车道路和真实API成本验收标为待执行。

## 运行与故障

任务状态QUEUED/RUNNING/COMPLETED/FAILED/CANCELLED保存在数据库。任务请求保存快照、预算、种子、调用上限、手动选择和expected_head；不会保存地图密钥。长搜索与外部道路请求在写事务外运行，取消后保留完整基线或已找到结果。异常日志仅记录异常类型和请求/任务ID，不记录输入、URL查询串或密钥。

服务突然终止时，运行任务的租约最长为本次预算加30秒；到期后显示中断失败，用户可重新开始。确认方案会重新检查输入与独立证明；存在冲突、未知道路、过期输入或版本并发时返回明确错误。共享SQLite数据库出现忙等待时，先检查是否存在长事务或重复服务，不清空数据或重置账号。
