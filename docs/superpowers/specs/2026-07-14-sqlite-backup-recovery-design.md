# SQLite 自动备份与恢复演练设计

> 状态：已随本地提交 `9f4c12d` 完成 `implemented`，服务器 `not deployed / not observed / not validated`。`trading_backup.py`、7/4/12 轮转、隔离恢复演练、状态报告、告警复用和 systemd 模板已通过本地测试；核心计数现已覆盖 schema 6 完整账本表。服务器安装、自动运行证据和季度演练验收仍需单独授权与真实观察。

## 1. 目标

为 `cache/trading/trading.db` 建立独立、可验证且不影响交易路径的运行保护：

- 每日使用 SQLite 在线备份 API 生成一致性副本；
- 保存 SHA-256、schema version、完整性检查和核心表计数；
- 保留最近 7 份每日、4 份每周和 12 份每月备份；
- 每季度自动恢复最新有效备份并复核内容；
- 失败时生成状态和企业微信告警，但不修改买卖、风控或服务状态。

## 2. 非目标

本批不实现：

- JSONL、扫描文件和缓存归档；
- 完整订单、成交、账户快照和对账账本；
- 参数复核、机器学习或历史回测；
- 远程对象存储、跨地域复制或备份加密；
- 自动部署、重启、数据库回滚或主库替换。

季度恢复演练只验证副本，不得覆盖正在使用的主库。

## 3. 方案选择

采用独立 Python CLI，复用 `TradingStore.backup_to`、`TradingStore.integrity_check`、现有配置和通知组件，只使用 Python 标准库。CLI 提供：

```text
python trading_backup.py backup
python trading_backup.py drill
python trading_backup.py status
```

不采用 Bash 直接调用 `sqlite3`，避免新增服务器 CLI 依赖和跨平台测试差异；不并入 `joinquant_health.py`，避免备份故障与交易健康检查耦合。

## 4. 文件与目录

默认备份根目录位于项目外：

```text
/opt/stock-analysis-backups/
```

通过 `TRADING_BACKUP_DIR` 显式覆盖。目录结构固定为：

```text
/opt/stock-analysis-backups/
  daily/
  weekly/
  monthly/
  manifests/
    daily/
    weekly/
    monthly/
  drill/
  status.json
```

备份文件名包含每日、ISO周或自然月槽位和数据库 SHA-256 短摘要：

```text
trading-<slot>-<sha12>.db
```

manifest 保存在 `manifests/<tier>/` 下并使用同名 `.json`，保存完整 SHA-256、源库路径、源库大小、备份时间、schema version、`PRAGMA integrity_check`、核心表计数和所属保留层。`status.json` 原子覆盖最近一次备份和演练状态并保留最近成功时间。不得保存 webhook、token、私有配置内容或完整账户明细。

latest 状态报告原子覆盖：

```text
output/trading_backup_latest.md
```

只有季度演练和状态变化才允许产生低频归档报告；重复失败只更新 latest 并使用同一告警去重键，禁止每次检查新增无界报告。

季度报告使用固定槽位 `output/trading_backup_drill_YYYY-QN.md`，同一季度重跑时原子覆盖。

## 5. 每日备份流程

`backup` 命令按以下顺序执行：

1. 解析主库和备份根目录，拒绝主库不存在、备份根目录等于或位于项目目录内、解析路径逃逸以及备份目标覆盖主库。
2. 在备份根目录内创建唯一临时文件。
3. 调用 `TradingStore.backup_to` 完成在线备份。
4. 对临时副本执行 `PRAGMA integrity_check`，读取 schema version 和核心表计数。
5. 计算临时副本 SHA-256，写入临时 manifest。
6. 数据库与 manifest 均成功后原子改名；任一步失败都删除未完成临时文件，不删除已有有效备份。
7. 按本地日期归入每日层；每个自然日只有一个槽位。当日重复运行时先发布新的有效副本，再替换当日旧槽位，因此不会累积重复文件，也不会在失败时丢失已有有效副本。
8. 每次成功运行都将当前有效副本登记为本 ISO 周和本月的最新槽位，因此周期结束后自然保留该周、该月的最后一个成功备份。登记优先使用硬链接；文件系统不支持时使用普通复制，并重新核对 SHA-256。
9. 轮转只删除超出最近 7 个自然日、4 个 ISO 周、12 个自然月集合的备份及其 manifest；任何无法配对或校验失败的文件不自动删除，只报告异常。
10. 原子更新 latest 报告；失败时发送去重企业微信告警并返回非零退出码。

每日任务安排在 `16:30 Asia/Shanghai`，交易日对应收盘后，非交易日同样执行以验证主库持续可恢复。systemd timer 使用 `Persistent=true`，服务器错过运行时在恢复后补跑；每日槽位规则防止同日重复副本。

## 6. 季度恢复演练

`drill` 命令每季度第一个周日凌晨执行：

1. 从 monthly、weekly、daily 的最新有效 manifest 中选择最新备份。
2. 先核对备份文件 SHA-256 与 manifest。
3. 将备份复制到 `drill/` 下的唯一临时目录，不直接打开或修改正式备份。
4. 对恢复副本执行 `PRAGMA integrity_check`，读取 schema version 和核心表计数。
5. 将结果与 manifest 对比；任一不一致均判定失败。
6. 原子写入季度演练报告，包含演练时间、备份标识、恢复路径摘要、SHA-256、schema、核心计数和结果。
7. 无论成功失败都尝试删除演练临时副本；清理失败只追加告警，不掩盖主要结果。

演练不得初始化、迁移、写入或替换主库。没有有效备份时必须失败并告警，不能生成伪成功报告。

## 7. 核心计数与兼容性

当前核心计数覆盖 schema version 6 的基础账本、持仓周期和完整执行账本表：

```text
schema_migrations
strategy_runs
signals
risk_decisions
system_state
position_cycles
order_events
exit_intents
trade_cooldowns
orders
fills
account_snapshots
position_snapshots
daily_equity
reconciliation_runs
reconciliation_items
control_events
```

读取时先检查 `sqlite_master`；未来新增表不要求旧备份包含。manifest 记录备份自身 schema version，演练按该版本的实际表集合核对，不对历史备份自动迁移。

## 8. 配置与运行入口

新增配置：

```text
TRADING_BACKUP_DIR=/opt/stock-analysis-backups
TRADING_BACKUP_DAILY_KEEP=7
TRADING_BACKUP_WEEKLY_KEEP=4
TRADING_BACKUP_MONTHLY_KEEP=12
```

保留数必须是正整数，并设有合理上限，防止错误配置导致无界增长。默认值是项目存储政策的正式值；不提供关闭完整性检查或路径保护的开关。

`run_ubuntu.sh` 增加：

```text
bash run_ubuntu.sh backup
bash run_ubuntu.sh backup-drill
bash run_ubuntu.sh backup-status
```

安装逻辑生成独立 oneshot service 和 timer。新增 timer 不加入交易服务重启链；执行失败由 systemd 记录并通过应用通知告警。

## 9. 错误处理与通知

失败必须满足：

- 返回非零退出码；
- latest 报告明确记录阶段、错误摘要和最近一次成功时间；
- 使用现有 `WeComNotifier` 和失败重试队列发送去重告警；
- 不删除已有有效备份；
- 不改变 `KILL_SWITCH`、买入许可、卖出许可、健康门或任何策略状态。

manifest、报告和通知只保存截断后的错误摘要，不包含环境变量值或敏感请求信息。

## 10. 测试与验收

自动测试至少覆盖：

- 在线备份可恢复 schema version 6 和上述核心计数；
- 临时文件与 manifest 只有全部校验成功后才原子发布；
- 损坏副本和 SHA-256 不一致被拒绝；
- 同日重复执行只保留最后一个成功槽位，失败不替换已有有效副本；
- 跨日、跨周、跨月后只保留 7/4/12 份；
- 无法配对或损坏的备份不被自动删除；
- 备份目录位于项目内、目标覆盖主库或路径逃逸时失败；
- 季度演练不修改主库和正式备份；
- 演练成功和失败均清理临时副本并生成正确状态；
- 通知失败进入现有重试队列；
- Linux 安装脚本包含 service、timer、环境变量和 CLI 入口。

状态升级标准：

| 状态 | 标准 |
| --- | --- |
| `planned` | 仅有本文和实施计划。 |
| `implemented` | 本地代码、单元测试和 Linux 静态检查通过。 |
| `deployed` | 服务器配置、service 和 timer 已安装，手工运行一次成功。 |
| `observed` | 至少 7 个自然日自动备份成功，7/4/12 轮转证据可读且无连续失败。 |
| `validated` | 至少一次自动季度恢复演练成功，SHA-256、完整性、schema 和核心计数全部一致，并经人工复核报告。 |

## 11. 权限和部署边界

本地实现不授权 Git 提交、推送、服务器写操作、systemd 安装、服务重启或数据库替换。部署必须在用户当次明确授权后单独执行；Codex 自动审核员仍只允许读取状态、报告和备份元数据，不得运行备份、恢复、清理或回滚命令。
