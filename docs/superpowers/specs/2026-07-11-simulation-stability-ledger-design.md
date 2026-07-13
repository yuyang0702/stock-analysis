# 模拟盘稳定性账本与策略验证设计

更新日期：2026-07-11

> 文档层级：本文件是模拟盘稳定性专项设计从文档。
>
> 主文档：`docs/project_roadmap.md`。
>
> 执行从文档：`docs/live_trading_execution_plan.md`。
>
> 如果项目状态、已实现能力、部署方式或优先级与本文件冲突，以 `docs/project_roadmap.md` 为准。本文件描述已批准的五批次设计目标；其中 Batch 1 已实现并部署，待部署后首个有效交易日双写观察，Batch 2-5 仍为待实现目标。只有代码、测试、部署和对应线上验收完成后，相关能力才能提升到 implemented、deployed、observed 或 validated。

## 0. 2026-07-13 后续增量说明

原 Batch 1 的服务器已部署事实仍指 schema version 1 的策略运行、信号、观察型风险和系统状态账本。当前本地未提交实现已把总 schema 升至 version 5：version 2 新增 `position_cycles`，后续 migration 再加入幂等委托事件、退出意图和再买冷却；这些都属于分层退出专项的后续扩展，尚未部署、观察或验证，不得倒写成原 Batch 1 已部署范围。

## 1. 目标与范围

本阶段保留现有“A 股扫描与信号生成 -> JoinQuant 模拟盘执行 -> 账户与订单回传 -> 企业微信通知和复盘”主链路，用 20 个有效交易日验证策略表现和执行稳定性。

本阶段采用渐进式加固，不重写策略，不接入真实资金，不让新增风控改变原始策略的买卖和仓位。新增能力包括：

- SQLite 正式运行账本；
- 持久化信号和订单幂等；
- 订单状态机；
- 账户、持仓、订单和成交自动对账；
- 硬安全检查和影子风控；
- HTTPS、请求签名和防重放；
- 停买与紧急停止开关；
- 每日稳定性与策略验证报告。

本阶段不包括：

- 真实券商或实盘下单；
- 完整逐日历史回测；
- 修改现有选股评分、买点、卖点或原始仓位算法；
- 机器学习参与排序、过滤、仓位或下单；
- PostgreSQL 或多节点部署。

## 2. 设计原则

1. 模拟盘的首要目标是验证策略，亏损是有效实验结果，不因亏损而判定系统不稳定。
2. 硬安全规则只阻止会污染实验数据或产生重复、非法订单的行为。
3. 仓位、集中度、换手和回撤规则在 `RISK_MODE=OBSERVE` 下只记录，不阻止模拟交易。
4. SQLite 是正式账本，JSON/JSONL 是过渡期兼容输出。
5. 任意订单必须能追溯到原始信号、策略运行、风控结果和成交。
6. 程序重启、网络超时和重复回调不得导致重复订单或重复成交记录。
7. 账本不可写或账户无法对账时，停止新买入，但继续允许合法减仓和止损。

## 3. 总体架构

```text
A 股扫描策略
    |
    v
不可变信号 + 策略运行元数据
    |
    v
SQLite 正式账本 ----> 影子风控评估
    |
    +----> signals.json 兼容输出
              |
              v
       HTTPS/HMAC 信号接口
              |
              v
       JoinQuant 模拟执行适配器
              |
              v
       订单、成交、账户快照回传
              |
              v
SQLite 订单/成交/持仓账本
    |
    +----> 自动对账与运行控制
    |
    +----> 健康报告、策略验证报告、企业微信告警
```

## 4. 数据存储

数据库固定为 `cache/trading/trading.db`。SQLite 连接必须启用：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

迁移按顺序执行并记录 schema 版本。进程不得在运行期间执行破坏性迁移。

### 4.1 strategy_runs

记录每次策略扫描：

- `run_id` 主键；
- 交易日期；
- 开始和结束时间；
- Git commit；
- 策略版本和参数版本；
- 数据状态；
- 扫描结果；
- 错误信息；
- 创建和更新时间。

### 4.2 signals

保存不可变原始信号：

- `signal_id` 唯一键；
- `run_id` 外键；
- 股票代码和 JoinQuant 代码；
- `buy` 或 `sell`；
- 目标仓位、信号价格、止损和止盈；
- 最终评分和策略模式；
- 生成时间和过期时间；
- 原始 JSON；
- 创建时间。

重复写入同一 `signal_id` 必须返回已有记录，不得生成第二条信号。

### 4.3 risk_decisions

每次执行前检查都保存：

- `signal_id`；
- `risk_mode`；
- `allowed`；
- 硬阻断代码；
- 影子风控代码列表；
- 检查时现金、总资产和持仓；
- 当前及预计单票、组合和行业暴露；
- 当日盈亏、账户回撤和换手率；
- 账户快照时间；
- 完整检查 JSON；
- 检查时间。

### 4.4 orders

订单状态机字段包括：

- `client_order_id` 唯一键；
- `signal_id`；
- JoinQuant 订单号；
- 股票、方向、目标数量、目标价格和目标仓位；
- 已成交数量和平均成交价；
- 当前状态；
- 提交次数；
- 首次提交、最后更新时间和完成时间；
- 失败或拒绝原因；
- 原始订单 JSON。

### 4.5 fills

每笔实际成交单独保存：

- 成交唯一键；
- 订单外键；
- 股票、方向、数量和成交价格；
- 手续费；
- 成交时间；
- 原始成交 JSON。

重复回调不得生成重复成交记录。

### 4.6 account_snapshots 与 positions

保存每次账户快照及其持仓明细，包括现金、总资产、股票数量、可卖数量、成本、现价和市值。原始快照必须保留以支持复查。

### 4.7 reconciliation_runs 与 reconciliation_items

保存每次对账的开始、结束、结果、严重程度，以及现金、持仓、订单和成交的逐项差异。

### 4.8 system_state

保存：

- `kill_switch`；
- `buy_enabled`；
- 最后成功信号时间；
- 最后成功快照时间；
- 连续订单失败数；
- 当前部署版本；
- 最后一次对账状态。

## 5. 模块边界

### trading_store.py

负责数据库连接、初始化、迁移和事务化读写。不得包含策略评分或交易决策。

### pre_trade_check.py

输入信号、账户快照、持仓、待完成订单和系统状态，输出结构化 `RiskCheckResult`。输出同时区分硬阻断和影子告警。

### order_ledger.py

负责客户端订单号、订单状态转换、成交去重和订单查询。非法状态转换必须拒绝并写审计事件。

### reconciliation.py

比较本地账本与 JoinQuant 账户快照，输出差异、严重程度和建议控制动作。

### trading_control.py

负责 `KILL_SWITCH`、停买、恢复条件和异常触发。默认不得自动解除 `KILL_SWITCH`。

### stability_report.py

生成每日稳定性与策略验证报告，判断当天是否为有效观察日。

### 现有模块

- `a_share_strategy.py` 继续生成候选和原始信号；
- `joinquant_exporter.py` 继续生成兼容 `signals.json`，同时写入正式账本；
- `joinquant_signal_server.py` 负责认证、信号传输和回调接收；
- `joinquant_strategy.py` 保持为 JoinQuant 内的薄执行适配器；
- `joinquant_health.py` 保留现有检查并增加数据库和对账指标；
- `strategy_compare_report.py` 保留现有影子评分对照，并读取新的账本数据。

## 6. 风控模式

模拟验证期固定使用：

```text
RISK_MODE=OBSERVE
MAX_SINGLE_POSITION_PCT=30
MAX_TOTAL_POSITION_PCT=95
MIN_CASH_RESERVE_PCT=5
MAX_SECTOR_EXPOSURE_PCT=60
MAX_NEW_POSITIONS_PER_DAY=10
MAX_ORDERS_PER_DAY=50
MAX_DAILY_TURNOVER_PCT=200
DAILY_LOSS_WARN_PCT=5
ACCOUNT_DRAWDOWN_WARN_PCT=15
MAX_CONSECUTIVE_ORDER_FAILURES=5
ACCOUNT_SNAPSHOT_MAX_AGE_SEC=300
SIGNAL_MAX_AGE_SEC=1200
RECONCILIATION_POSITION_TOLERANCE=0
```

仓位、行业暴露、新开仓数量、订单数、换手率、当日亏损和账户回撤只生成影子告警，不阻止模拟盘策略执行。

## 7. 硬安全检查

以下情况实际阻止新订单：

- `KILL_SWITCH` 已开启；
- 账本不可写；
- 信号重复或订单幂等键重复；
- 信号超过 1200 秒；
- 交易时段内账户快照超过 300 秒；
- 股票代码、价格、方向、数量或交易单位非法；
- 不足一个有效交易单位；
- 无持仓却发送卖出；
- 相同股票存在同方向未完成订单；
- 下单结果未知且尚未对账；
- 持仓数量对账不一致；
- 连续 5 笔订单执行失败；
- 系统时间漂移超过 30 秒。

停止买入不得阻止合法卖出、止损和减仓。

稳定的硬阻断代码包括：

```text
KILL_SWITCH_ACTIVE
LEDGER_UNAVAILABLE
DUPLICATE_SIGNAL
DUPLICATE_ORDER
STALE_SIGNAL
STALE_ACCOUNT_SNAPSHOT
INVALID_ORDER_INPUT
PENDING_ORDER_EXISTS
SUBMIT_RESULT_UNKNOWN
POSITION_MISMATCH
CONSECUTIVE_ORDER_FAILURES
CLOCK_DRIFT
```

## 8. 订单状态机与幂等

合法状态流转：

```text
CREATED
  -> RISK_REJECTED
  -> READY
      -> SUBMITTING
          -> SUBMITTED
              -> PARTIALLY_FILLED
                  -> FILLED
                  -> CANCELLED
              -> FILLED
              -> CANCELLED
              -> REJECTED
          -> SUBMIT_UNKNOWN
```

终态为 `RISK_REJECTED`、`FILLED`、`CANCELLED` 和 `REJECTED`，终态不得回退。

客户端订单号使用：

```text
SHA256(strategy_version + trade_date + signal_id + action + jq_code)[:32]
```

当下单请求超时或返回不明确时进入 `SUBMIT_UNKNOWN`：

1. 禁止自动重新提交；
2. 查询 JoinQuant 订单和账户快照；
3. 找到订单后恢复为 `SUBMITTED` 或对应成交状态；
4. 明确不存在后才允许受控重试；
5. 超过 5 分钟仍无法确认时触发 critical 告警并停止新买入。

聚宽策略重启后必须从服务端同步已处理订单摘要，不能依赖内存中的 `g.executed_signal_ids`。

## 9. 自动对账

账户快照每分钟回传后执行增量对账；盘前、午间和收盘后执行完整对账。

对账范围：

- 现金与总资产；
- 股票持仓总数量和可卖数量；
- 未完成订单；
- 累计成交；
- 本地未知订单；
- JoinQuant 未知持仓和手工交易。

严重程度和动作：

```text
INFO     记录正常价格和市值波动
WARNING  记录订单或快照短暂延迟
ERROR    停止新买入，允许卖出
CRITICAL 开启 KILL_SWITCH 并立即告警
```

恢复买入要求：连续两次对账一致、账户快照恢复、没有 `SUBMIT_UNKNOWN`、连续失败已清零，并由人工确认或明确配置恢复。`KILL_SWITCH` 默认只能人工解除。

## 10. 接口安全

生产部署必须使用 HTTPS。认证从 URL 查询参数迁移到请求头，并增加：

- HMAC-SHA256 请求签名；
- 请求时间戳和随机数；
- 请求体 SHA-256 摘要；
- 2 分钟有效窗口；
- 随机数防重放存储；
- IP 白名单；
- 请求体大小限制；
- 拉取和回调接口速率限制；
- 信号拉取与账户回调使用不同密钥；
- 日志中隐藏所有凭据。

旧查询参数令牌只允许在迁移窗口内兼容，并配置明确的停用日期。

## 11. 双写与故障处理

迁移期间先提交 SQLite 事务，再更新 JSON/JSONL 兼容文件。

- SQLite 失败：停止新买入、允许卖出并发送 critical 告警；
- JSON 兼容输出失败：记录 error，停止发布该批新信号，避免数据库与拉取结果分叉；
- 信号服务不可用：聚宽不执行旧信号；
- 行情源失败：不生成新买单，卖出使用账户和 JoinQuant 行情；
- 企业微信失败：写入现有重试队列，不改变交易状态；
- 对账失败：停止新买入；
- 聚宽或服务器重启：先恢复账本和对账，再允许新买入；
- 非交易时段快照不更新不得误报交易时段故障。

## 12. 上线批次

### 批次 1：账本与观测模式

加入 SQLite、双写、策略运行、信号、风险决策和系统状态。上线观察 1 个交易日，确认信号和委托结果不变。

### 批次 2：幂等和订单状态机

加入客户端订单号、合法状态转换、`SUBMIT_UNKNOWN`、服务端已处理摘要和重启恢复。

### 批次 3：账户快照和自动对账

加入快照、持仓、订单与成交对账，异常分级和自动停买。

### 批次 4：接口安全与运行控制

加入 HTTPS、请求头认证、HMAC、防重放、IP 白名单、速率限制和运维控制命令。

### 批次 5：策略验证报表

加入每日稳定性报告、原始策略组合和影子风控组合对照报告。

每一批必须独立测试、部署和观察，不在同一交易日同时上线多个批次。

## 13. 测试策略

### 单元测试

覆盖数据库迁移、幂等插入、订单状态转换、观察/阻断模式、持仓与现金对账、HMAC、防重放、时效规则和指标计算。

### 集成测试

覆盖完整流程：

```text
生成信号 -> 写入 SQLite -> API 拉取 -> 模拟 JoinQuant 回调
-> 更新订单 -> 写入成交 -> 更新持仓 -> 对账 -> 生成日报
```

### 故障注入

覆盖 SQLite 锁定、JSON 写入失败、API 超时、重复回调、回调乱序、服务器重启、聚宽重启、损坏快照、系统时间偏移和企业微信不可用。

必须验证同一信号连续拉取 10 次、下单前后重启、请求超时、回调失败和部分成交都不会产生重复有效订单。

## 14. 20 个交易日观察

### 第 1-3 日：数据正确性

逐笔核对 SQLite、JSON、JoinQuant 委托和成交，确认信号、订单、成交能够关联且日报可人工复算。

### 第 4-10 日：运行稳定性

要求交易时段服务无中断，信号拉取和快照回传成功率均不低于 99%，重启能够恢复且没有无法解释的订单。

### 第 11-20 日：策略有效性

冻结核心策略参数，比较原始策略、影子风控、`short/mid`、分数区间、市场状态、行业、题材、持仓时间和成本前后收益。

20 日观察完成也不自动授权改参。后续参数复核从属于 `docs/superpowers/specs/2026-07-14-semi-automatic-parameter-review-design.md`：自动任务只能生成候选和评价，人工批准与另行授权发布缺一不可；完整历史回测不足时还需累计至少 60 个有效模拟盘交易日才能把候选标为可批准。该能力当前为 `planned`，不属于本账本设计已经实现或部署的范围。

一个交易日计入有效样本必须满足：

- 服务覆盖完整交易时段；
- 信号、订单和成交数据完整；
- 没有重复订单；
- 收盘对账一致；
- 没有未解决的 `SUBMIT_UNKNOWN`；
- 无数据库损坏和数据缺口；
- 所有订单可追溯；
- 日报正常生成。

策略当日亏损不影响有效日判定。

## 15. 报告与评价指标

每日生成：

```text
output/stability/YYYYMMDD.md
output/strategy_validation/YYYYMMDD.md
```

报告至少包括：

- 信号、实际执行、拒单、未成交和部分成交数；
- 原始策略组合与影子风控组合的理论结果；
- 当日与累计净值、基准和超额收益；
- 最大回撤、胜率、盈亏比和 Profit Factor；
- 手续费、成交额和换手率；
- `short/mid`、分数、市场状态、行业和题材分组表现；
- 最大有利波动和最大不利波动；
- 被影子风控标记交易的后续表现；
- 数据缺失、对账差异和运行异常。

20 日结束时，工程门槛为：

- 至少 20 个有效交易日；
- 无重复有效订单；
- 无未解释持仓差异；
- 信号和快照成功率均不低于 99%；
- 所有订单可追溯；
- 无遗留 critical 事件。

策略评价不预设必须盈利，而是判断收益能否覆盖成本、是否产生超额收益、回撤来源、有效分组，以及影子风控是否改善收益回撤比。

## 16. 运维接口

统一通过 `run_ubuntu.sh` 提供：

```bash
bash run_ubuntu.sh trading-status
bash run_ubuntu.sh stop-buy
bash run_ubuntu.sh resume-buy
bash run_ubuntu.sh kill-switch-on
bash run_ubuntu.sh kill-switch-off
bash run_ubuntu.sh reconcile
```

所有控制动作必须写入审计记录，包含操作者、时间、旧值、新值和原因。

## 17. 后续阶段

完成 20 日观察后，根据数据决定：

1. 是否调整现有策略评分、买点、卖点或仓位；
2. 哪些影子风控规则值得转为正式阻断；
3. 是否进入完整逐日历史回测；
4. 是否延长模拟验证；
5. 是否开始单独设计小资金实盘适配层。

未完成完整历史回测、订单对账和实盘级风控前，不接入真实资金。
