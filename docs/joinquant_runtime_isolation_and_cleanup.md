# 聚宽回测与模拟盘隔离、污染识别和清理手册

本文是 `joinquant_strategy.py`、生产信号接口、账户快照同步以及回测污染清理的开发与运维契约。它不替代 strict 历史导出手册；strict 导出和聚宽原生回测都应保持本地、无生产网络访问。

## 事故结论：2026-08-08 至 2026-08-10

用户确认这三天都运行过聚宽回测。生产服务器证据必须按实际写入区分：

| 日期 | 生产接口/账本证据 | 结论 |
| --- | --- | --- |
| 2026-08-08 | 03:19:56–03:25:42 约每秒一组请求；241 次信号 GET、241 次账户 POST；落库 211 个账户快照、217 次对账、1 条日权益、1 份当前券商快照 | 旧在线策略模板被聚宽加速回测调用，属于生产账本污染 |
| 2026-08-09 | 生产 JoinQuant API 无同类请求和落库 | 回测没有写入生产账本，不做数据删除 |
| 2026-08-10 | 生产 JoinQuant API 无账户回测写入；交易服务有 27 次真实盘中扫描 | 回测没有写入生产账本；27 次真实扫描必须保留 |

8 月 8 日污染快照没有持仓、订单、成交、对账差异、控制事件或异常状态引用。只有在再次检查仍满足这些条件时，工具才允许自动清理；一旦出现任何实质交易或控制引用，必须拒绝自动删除并转人工复核。

## 三层隔离

### 第一层：聚宽代码不联网

`joinquant_strategy.py` 只把 `context.run_params.type == "sim_trade"` 视为在线模拟盘。`simple_backtest`、`full_backtest`、缺失或未知类型均 fail closed：

- 不注册 15:05 账户回传；
- 不执行启动自检；
- 不拉取服务器信号；
- 不回传账户；
- 不调用下单 API。

不要为了在回测中复用服务器信号而放宽此判断。聚宽原生回测应使用策略快照生成的 `聚宽原生回测策略.py`，strict 数据应使用 `聚宽严格历史导出.py`；这两个成员不得访问生产服务器。

### 第二层：服务器验证运行身份

生产 `/joinquant/signals`、`/joinquant/latest`、`/joinquant/account_snapshot` 在 Token 验证后，还要求三项完全匹配：

```text
X-JoinQuant-Run-Type: sim_trade
X-JoinQuant-Template-Version: 当前部署版本
X-JoinQuant-Protocol-Version: 1
```

账户 POST 的 JSON 还必须包含匹配的 `runtime_mode`、`strategy_template_version` 和 `runtime_protocol_version`。缺失、回测类型、旧模板或头部/正文不一致统一在写文件、写 SQLite、下发信号之前返回 409。Token 本身不会因版本升级而变化。

### 第三层：同步器复核已落盘快照

`joinquant_sync.py` 在生产定时任务中再次验证快照是当前 `sim_trade` 模板。没有已接受快照时，命令输出 `JOINQUANT_SYNC_SKIPPED no_accepted_snapshot` 并以成功状态退出，不生成持仓文件、不追加派生事件；旧文件或人工放入的回测快照不能继续传播到网页持仓。

## 清理工具

只读检查：

```bash
cd /opt/stock-analysis
set -a
. ./stock-analysis.env
set +a
bash run_ubuntu.sh joinquant-cleanup-inspect
```

正式隔离：

```bash
bash run_ubuntu.sh joinquant-cleanup-apply
```

正式操作前必须停止 `stock-joinquant-sync.timer` 与 `stock-joinquant-signal.service`，避免文件或 SQLite 在清理中被并发写入。工具仍会独立执行以下防线：

1. 检查 SQLite `integrity_check` 和外键；
2. 精确识别 8 月 8 日 03:19:56（含）至 03:25:43（不含）的账户快照链；
3. 检查持仓、订单、成交、对账差异、控制事件、异常状态和其他券商快照引用；
4. 在项目目录外创建经 SHA-256 和完整性验证的交易库备份；
5. 把原始数据库行、JSONL 命中行和清理前完整文件写入隔离目录；
6. 事务删除污染数据库行，原子重写 JSONL，移走污染的当前账户/持仓文件；
7. 再次执行完整性、外键和剩余污染计数验证；
8. 写入只读隔离清单。重复执行返回 `already_completed`，不会重复删除。

默认隔离目录：

```text
/opt/stock-analysis-backups/quarantine/joinquant-backtest-20260808-20260810/
```

该目录是事故审计证据，不纳入项目 Git，不自动删除。包含清理前完整文件，权限必须是目录 700、文件 600。清单和日志不得记录 Token、SSH 私钥、Webhook 或环境文件内容。

## 部署与重启不变量

- 部署前后只比较 `stock-analysis.env` SHA-256，不打印内容；哈希必须完全一致。
- 不修改、重置或轮换 JoinQuant Token、SSH 密钥、Webhook、网站登录信息。
- 清理前必须有新的 verified backup；隔离目录不能位于项目目录内。
- 只删除工具列出的污染链。`strategy_runs` 中 8 月 10 日真实扫描、通知、系统状态和健康历史不能按日期批量删除。
- 受控重启后核对核心服务、同步 timer、SQLite 完整性、外键、污染剩余计数和 409 防线。
- 新在线模板部署服务器后，还必须把聚宽模拟盘策略更新到相同模板版本；旧在线模板会被服务器安全拒绝，但不会导致 Token 失效。

## 回归清单

至少运行：

```bash
python -m py_compile joinquant_runtime_isolation.py joinquant_runtime_cleanup.py joinquant_signal_server.py joinquant_sync.py joinquant_strategy.py
python -m unittest tests.test_joinquant_runtime_cleanup tests.test_joinquant_strategy_template tests.test_joinquant_signal_server tests.test_joinquant_sync -v
python -m unittest discover -s tests -v
```

关键测试必须覆盖：回测零网络/零下单、未知模式 fail closed、请求头与正文双验证、拒绝时零数据库/文件副作用、同步器拒绝旧快照、清理 dry inspection、引用保护、备份/隔离、真实扫描保留和重复执行幂等。
