# 聚宽严格历史数据导出完整手册

日常操作先看[三步说明](strict_history_three_step_guide.md)。本文解释两份脚本、反前视规则、产物和排错边界。

## 当前可交付能力

一键生成包内含两份完整脚本：

| 文件 | 能力 | 已验证 |
| --- | --- | --- |
| `聚宽原生回测策略.py` | 在聚宽分钟回测中复现同一五分钟候选和组合规则，并把下一决策点订单镜像到聚宽撮合。 | Python 3.6 实际运行；09:35 决策、09:40 下单、T+1 同日卖单不提交。 |
| `聚宽严格历史导出.py` | 按月生成服务器训练所需 strict ZIP。 | 聚宽真实 2026-06 整月完成：30,240 候选、1,351,104 条五分钟价格路径；ZIP 与服务器数据库均通过完整性校验。 |

当前本地严格导出器版本为 `2026-08-10.28-multipath`，时点内核版本为 `2026-08-10.1-multipath`；快照身份以 `SNAPSHOT_MANIFEST["snapshot_id"]` 为准。最终脚本通过桌面“一键生成聚宽策略快照”生成，不要从旧 Notebook 反向复制覆盖本地文件。该多路径增量在完成提交、服务器部署和重新生成前仍只属于本地实现。

## 两条路径为什么分开

原生回测交给聚宽撮合，适合看收益、回撤、订单和日志。strict 导出保留每个决策时点“为什么选中或拒绝”、特征可用时间和 D+10 价格路径，适合服务器训练。二者共享策略快照，但用途不同：不能拿回测收益文件训练，也不能拿训练 ZIP冒充聚宽收益曲线。

## 反前视契约

每行特征都是：

```python
{"value": 12.3, "available_at": "2025-07-01T10:00:00+08:00"}
```

运行时强制 `available_at <= decision_at`。数据来源规则：

- 当前行情：截止决策时点已闭合的 5 分钟 K 线和累计成交额。
- 技术指标：严格早于交易日的完整日线；没有 30 根有效日线时跳过并补位。
- 市值：上一完整交易日估值。
- 前收盘价：优先使用聚宽日线的 `pre_close`；若该字段缺失，只允许回看更早且有限、为正的真实收盘价。无法取得真实证据的股票日会被排除并写入 `daily_core_audit`，不会用当日价格或未来数据补造。
- 行业：指定历史日期的聚宽行业分类。
- 个股新闻/LHB：线上盘中观察池本来不展开，因此历史回放也固定为 0，并记录 `intraday_watch_unexpanded`。
- 同日市场新闻：聚宽日表没有可靠盘中发布时间，状态为 `neutral_no_intraday_timestamp`，不进入当日盘中决策。
- 成交：原生回测在下一决策点镜像，卖单还受聚宽实际持仓与 `closeable_amount` 限制。

## 月包内容

成功后生成：

```text
jq_strict_exports/
└── jq-pit-202507-<快照前12位>/
    ├── 202507/
    │   ├── bars.csv
    │   ├── status.csv
    │   ├── universe.csv
    │   ├── features.csv
    │   ├── decision_candidates.jsonl
    │   ├── candidate_prices.jsonl
    │   ├── strict_manifest.json
    │   └── metadata.json
    └── jq-pit-202507-<快照前12位>-2025-07.zip
```

`decision_candidates.jsonl` 是训练核心，保存候选特征、选择结果、拒绝阶段、拒绝代码和规则顺序。`candidate_prices.jsonl` 保存候选的五分钟路径和 D+10 标签依据。`strict_manifest.json` 与 `metadata.json` 保存表哈希、文件 SHA-256、版本和行数。

当前 `features.csv` 允许只有表头，因为本阶段目标是五分钟候选训练；`metadata.json` 必须显式记录 `daily_features_required=false` 与 `daily_features_complete=false`，服务器才会接受。它不等于另一条“完整逐日特征回测”已经完成；若以后启用该门，需要另行提供 `daily_feature_builder` 并把 `require_daily_features=True`。

## 聚宽原生回测操作

1. “我的策略”→“新建策略”→“空白模板”。
2. 完整替换为 `聚宽原生回测策略.py`，不要追加在模板后面。
3. 选择 Python 3、分钟频率、日期和初始资金。
4. 保存后先跑 1 个交易日，日志应出现：

   ```text
   PIT_NATIVE_READY ... no_future=1
   PIT_DECISION at=... cohort=30 selected=... intents=... submitted=...
   ```

5. 短区间无异常后再扩展日期。

## 正式月度导出操作

1. 新建干净 Python 3 Notebook；内存高时先重启研究环境。
2. 完整粘贴 `聚宽严格历史导出.py` 到一个单元格。
3. 只改 `STRICT_EXPORT_MONTH`，例如 `"2025-07"`。
4. 运行并等待 `STRICT_EXPORT_OK`。脚本会自动使用 48 个决策时点、D+10、严格候选生成器、稳定版本和哈希。
5. 下载显示的 ZIP，不要修改，拖到桌面上传图标。

建议每次只导出一个月。月份必须已经拥有至少 10 个后续交易日，否则正确行为是报 `D10_PRICE_HORIZON_NOT_MATURE`。

## 常见错误

| 错误 | 处理 |
| --- | --- |
| `from __future__ imports must occur...` | 单元格中残留旧代码；新建空单元格，完整覆盖。 |
| `ModuleNotFoundError: dataclasses` | 使用了旧文件；重新一键生成并复制最终脚本。 |
| `INVALID_NUMBER: prev_close` | 使用了旧脚本；不要续跑旧单元格。重新一键生成，完整替换为 `2026-08-10.27` 或更新版本后从头运行该月。 |
| `history database and WAL reserve exceed...` | 旧服务器把每条索引记录误按一个 4 KB 页估算。同步当前存储代码后重试，不要关闭 3 GB 硬上限。 |
| `FEATURE_FROM_FUTURE` | 修正来源或排除样本，绝不能改时间戳绕过。 |
| `JOINQUANT_*_REQUIRED` | 聚宽 API 或证据字段缺失；停止导出并核对平台环境。 |
| `D10_PRICE_HORIZON_NOT_MATURE` | 等月份成熟，不缩短 D+10。 |
| `TABLE_HASH_MISMATCH` / `FILE_HASH_MISMATCH` | 丢弃损坏下载，重新从聚宽下载。 |
| 研究环境内存不足 | 保存 Notebook、重启研究环境，只跑一个月。 |

## 安全与验收

- 生成包不含 SSH 私钥、Token、Webhook、账户或数据库。
- 同一快照的原生回测和 strict 导出必须显示同一 `snapshot_id`。
- 正式导出必须保留默认 48 个决策时点和 D+10。
- 上传端必须同时通过 ZIP SHA-256、表哈希、版本身份和数据库原子导入校验。
- 失败包不覆盖上一份有效归档或服务器正式历史库。

2026-06 正式包的服务器验收基线：`daily_bars=109324`、`daily_status=109324`、`daily_universe=109327`、`decision_candidates=30240`、`candidate_prices=1351104`、`point_in_time_features=0`。最后一项为 0 是本阶段显式采用五分钟候选训练契约的结果，不代表数据丢失。正式库架构版本为 2，导入后的 `PRAGMA integrity_check` 必须为 `ok`。
