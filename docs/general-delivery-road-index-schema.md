# 道路索引提示哈希口径修正

本次修正仅处理索引元数据字段命名，不改变样本选择、输入、测量、道路别名、历史提示、采集回执或任何费用结果。没有执行求解器，也没有发出网络请求。

旧 `freeze_unseen_roads.py` 在 `cases[].hint_hash` 中保存了提示表按 `evidence_hash` 排序后的 SQL 行列表哈希。正式验收的 `case_identity` 则对 `FrozenEvidenceRoad.route_seed_samples` 字典计算哈希；这两种序列化结构不同，即使提示完全相同，哈希也不同。直接把旧字段交给 `validate_general_acceptance.make_lock` 会因字段口径不一致而拒绝。

新脚本分别记录：

- `hint_rows_hash`：原 SQL 行列表哈希，证明提示行逐字保留。
- `hint_hash`：`content_hash(FrozenEvidenceRoad.route_seed_samples)`，与正式验收 `historical_hints_hash` 保持一致。
- `hint_count`：冻结提示字典条目数。

旧 preparation manifest 仍可读取：当缺少 `hint_rows_hash` 时，只在旧 manifest 校验路径把其 `hint_hash` 解释为 SQL 行哈希。新的 prepare/execute 输出均显式保存两个字段。

既有文件 `acceptance/roads-collected-v2/frozen-cases.json` 保持原样，另建：

`acceptance/roads-collected-v2/frozen-cases-hash-schema-v2.json`

| 索引 | SHA256 |
| --- | --- |
| 原索引 | `9abaeb2c1da01aa0b93f98a51290c356ce0e34b2bcd8783fa19068e8376ade1e` |
| 元数据修正副本 | `a01a027b859b2373ff79e57996c104d849c76b0757fc10c34964ad393e81a795` |

对应 `frozen-cases-hash-schema-v2.audit.json` 记录 8 个 case 的原 case 文件哈希、两个道路图文件哈希、共享测量图哈希、两种提示哈希与输入哈希。原索引写后再次验 SHA256 未变。该命令未打开 baseline 文件或算法结果。

正式验收锁使用修正副本。若此前屏选使用原索引，须保留该记录并披露此次字段兼容修正；两者所指的 case/input/graph/hints 内容相同，不能将其称为新增样本或新的道路采集批次。
