# 模拟盘稳定性账本与策略验证设计

更新日期：2026-07-14

> 文档层级：本文件是模拟盘稳定性专项设计从文档。
>
> 主文档：`docs/project_roadmap.md`。

> 2026-07-14 执行正确性增量以 `docs/superpowers/specs/2026-07-14-execution-contract-p0-fixes-design.md` 为准：它复用 schema 6 的 `signals`、`position_cycles`、`orders` 和 `exit_intents`，不改变本账本设计的存储、对账或保留口径。该增量已随 `52b3653` 推送并部署到服务器和 JoinQuant 模板，当前为 `implemented（已推送） / deployed / not observed / not validated`。

> 外部部署状态更新：2026-07-14 后续部署确认服务器已到 `52b3653`，schema version 6、完整性/可写检查通过，环境校验未变且三个核心服务 active；JoinQuant 模板也已同步。服务器与模板可标记 `deployed`，自动 timer 连续运行和真实交易日行为仍未成为 `observed / validated`。
>
> 执行从文档：`docs/live_trading_execution_plan.md`。
>
> 如果项目状态、已实现能力、部署方式或优先级与本文件冲突，以 `docs/project_roadmap.md` 为准。原 Batch 1 schema 1 已部署事实保持不变；完整账本与自动对账已随提交 `9f4c12d` 进入 `origin/main`，并包含在服务器当前 `52b3653` 中。当前为 `implemented（已推送） / deployed / not observed / not validated`。

## 0. 当前增量状态

原 Batch 1 的 schema version 1 部署事实是历史基线。`origin/main` 已包含 schema version 5 基础提交 `8e35d03c90af2592921c81347bddf8b5af41ba94` 和在其上实现 schema version 6 的 `9f4c12d`；服务器当前 `52b3653` 已通过 schema version 6 的 `ledger-check`。`aa9acffaf62239e39c076408d83d113dce22b029` / schema version 1 仅保留为历史检查点。

下述“完整账本与自动对账增量”已获用户设计批准并在 `origin/main` 中 `implemented（已推送）`：schema version 6、订单/逐笔成交、账户与持仓检查点、日权益、自动对账、控制审计、企业微信摘要和人工解锁入口均有自动化证据。该增量始于 `9f4c12d` 并包含在服务器当前 `52b3653` 中，状态是 `deployed / not observed / not validated`。

### 0.1 完整账本与自动对账增量

本增量先完成模拟盘证据闭环，不改变选股、评分、买卖、仓位、止盈止损或 JoinQuant 撮合语义。完整历史回测已作为独立模块实现，仍与本次 schema migration、正式交易账本和部署状态分离。

采用标准库和现有 SQLite 单库扩展方案：在一个向前兼容 migration 中增加正式订单、逐笔成交、账户摘要、持仓检查点、权益日线、对账批次、差异项和人工控制审计表；保留现有 `order_events` 作为原始状态事件，不复制成第二套无关事件流。JoinQuant 每分钟回传当前账户、平台订单和逐笔成交，服务器在同一事务内幂等入账并完成增量对账。

不采用每分钟永久复制全部未变化原始快照，也不引入 PostgreSQL、消息队列、事件溯源框架或第三方依赖。高频账户摘要可以逐次保存；完整持仓只在内容变化、整点和收盘检查点保存；订单按稳定 ID 更新，成交按稳定 ID 不可变插入；对账批次保存摘要，只有不一致项保存明细。

### 0.2 新增表

现有 schema version 5 之后新增 schema version 6 migration，至少包含：

- `orders`：以 `client_order_id` 为主键，保存可空的信号关联、JoinQuant 订单号、方向、目标、请求、成交、均价、状态、提交次数、失败原因和原始订单；JoinQuant 订单号建立唯一索引。平台手工订单使用确定性平台订单 ID，不能伪造信号关联。
- `fills`：以平台成交 ID 为主键；平台缺少成交 ID 时，使用订单号、股票、方向、成交时间、数量和价格的确定性 SHA-256 摘要。保存订单关联、数量、价格、佣金、印花税、其他费用和原始成交；重复回传不得重复入账。
- `account_snapshots`：保存每次回调的现金、可用现金、总资产、持仓市值、当日盈亏、回撤、换手、模板版本、状态哈希和时间。完整原始 JSON 仅在状态变化、整点或收盘检查点保存，其余记录只保存摘要和哈希。
- `position_snapshots`：按被保留的账户检查点保存股票、总数量、可卖、冻结、当日数量、成本、现价、市值和盈亏；同一检查点和股票唯一。
- `daily_equity`：每个交易日一行，保存期初/期末权益、现金、持仓市值、已实现/未实现盈亏、费用、净入金和最大回撤，用于长期策略复算。
- `reconciliation_runs`：每次增量或完整对账一行，保存范围、开始/结束、结果、最高严重度、差异数量、来源快照、控制动作和摘要。
- `reconciliation_items`：只保存不一致或需要人工解释的现金、资产、持仓、订单和成交差异，包含本地值、平台值、容差、严重度和原因代码。
- `control_events`：不可变保存停买、恢复买入、开启/解除 `KILL_SWITCH` 的操作者、旧值、新值、原因、关联对账编号和时间。

`order_events` 继续保存平台状态事件；`orders` 保存每笔订单的最新正式状态。两者通过 `signal_id`、`client_order_id` 和 `order_id` 关联。旧 schema version 5 数据只迁移结构，不猜测或伪造历史成交、费用和账户快照。

### 0.3 回传与入账顺序

JoinQuant 模板从 `get_trades()` 构造 `trades`，字段至少包括成交 ID、订单 ID、信号 ID、股票、方向、数量、价格、佣金、税费、成交时间。平台字段缺失时保留空值，不猜测费用；缺少稳定成交 ID 时按确定性字段生成回退 ID。

服务器接收快照后按以下顺序执行：

```text
验证 schema 和字段
→ SQLite 单事务写账户摘要
→ 幂等 upsert 订单最新状态
→ 不可变插入逐笔成交
→ 按变化/整点/收盘规则保存持仓检查点
→ 更新持仓周期和退出意图
→ 执行增量对账并保存结果
→ 提交事务
→ 原子覆盖最新 JSON 兼容文件
→ 更新 ML 标签、通知和 API 指标
```

SQLite 事务失败时，回调返回非成功状态并记录告警；不得先发布新的兼容 JSON 再丢失正式账本。相同快照重试必须幂等。合法卖出、止损和减仓不因买入控制状态被禁止。

### 0.4 对账规则

每分钟账户回传后执行增量对账；盘前、午间和收盘后可通过 CLI 执行完整对账。首版使用 JoinQuant 快照作为平台事实源，用本地正式订单、成交、持仓周期和前后账户摘要作为独立账本侧。

必须检查：

- `cash + position_market_value` 与平台 `total_value` 的内部平衡，使用显式绝对/比例容差；
- 活动 `position_cycles.current_qty` 与平台总持仓数量；
- 本地最新持仓检查点与平台总数量、可卖数量和冻结数量；
- `orders.filled_qty` 与同订单 `fills.qty` 汇总；
- 本地未完成订单与平台未完成订单集合；
- 平台未知订单、平台未知成交、本地未知成交和无信号人工交易；
- 退出意图目标、已成交数量和剩余持仓是否一致；
- 每日权益变化与成交现金流、费用和持仓估值变化是否可解释。

正常价格和市值波动为 `INFO`；短暂回调时序差异为 `WARNING`；持仓、订单或成交无法解释为 `ERROR`；重复有效成交、账本损坏或严重持仓差异为 `CRITICAL`。每个差异使用稳定原因代码，报告不得只保存自由文本。

### 0.5 自动控制与人工解除

- `ERROR`：自动设置 `buy_enabled=0`，禁止新买入但继续允许合法卖出、止损和减仓。
- `CRITICAL`：自动设置 `kill_switch=1`，停止自动下单并保持 `buy_enabled=0`。
- 状态不得自动恢复。恢复买入要求最近连续两次完整对账一致、账户快照新鲜、没有 `SUBMIT_UNKNOWN`、没有未解释差异。
- 解除 `KILL_SWITCH` 不自动恢复买入；必须再单独恢复 `buy_enabled`。
- 所有控制变化写入 `control_events`，`reason` 必填并关联最近对账编号。

`run_ubuntu.sh` 增加“交易控制与自动对账”子菜单：

```text
1. 查看交易控制状态
2. 执行完整对账
3. 交易解锁向导
4. 手动停止新买入
5. 手动开启 KILL_SWITCH
0. 返回
```

非交互入口为：

```bash
bash run_ubuntu.sh trading-status
bash run_ubuntu.sh reconcile
bash run_ubuntu.sh unlock
bash run_ubuntu.sh stop-buy --reason "..."
bash run_ubuntu.sh kill-switch-on --reason "..."
bash run_ubuntu.sh kill-switch-off --reason "..."
bash run_ubuntu.sh resume-buy --reason "..."
```

`unlock` 只启动交互向导，不是无条件一键解锁。向导先显示最近差异和控制状态，执行或确认连续两次完整对账一致，要求输入原因并二次确认，然后先解除 `KILL_SWITCH`、再单独恢复买入。不满足门槛时只显示稳定原因代码，不提供绕过参数。非交互解除命令必须使用显式 `--reason`，并接受预期旧状态以避免并发覆盖。

### 0.6 通知

进入 `ERROR`、进入 `CRITICAL`、严重度上升、解除失败和人工状态变更时立即发送企业微信。连续两次完整对账恢复一致后发送“可以人工解除”通知，但不得自动解除。通知包含对账编号、时间、差异类别、受影响股票/订单摘要、当前控制状态和服务器检查命令，不包含 token、webhook、完整账户明细或环境变量。

同一稳定问题使用对账原因代码与对象 ID 组成去重键；重复状态只按冷却期摘要，避免每分钟刷屏。发送失败复用现有通知重试队列，不改变交易状态。

### 0.7 增长、保留与备份

按每年 250 个交易日、每天 240 次回调、平均 10 个持仓、50 笔订单和 50 笔成交估算：账户摘要约 6 万行/年；持仓检查点只在变化、整点和收盘保存，目标低于 5 万行/年；订单、成交和差异明细低于 5 万行/年。新增表连同索引年度增长目标低于 200 MB；超过 300 MB/年或单日增长超过 2 MB 时由健康报告告警并暂停新增高频明细。

订单、成交、日权益、控制事件和异常对账明细长期保留，不按日志清理。高频账户摘要保留至少 1 年；更长期只保留每日权益和变化检查点，未来压缩或清理必须另行设计、dry-run 和人工授权。现有在线备份、7/4/12 轮转、SHA-256、完整性检查和季度恢复演练必须把新表纳入按实际 schema 读取的核心计数；旧备份不迁移。

### 0.8 测试与验收

测试必须覆盖 migration 幂等、订单状态合法性、成交重复/乱序回调、缺失成交 ID 回退、部分成交、撤单后再成交、账户与持仓检查点压缩、现金/资产容差、持仓差异、未知订单/成交、人工交易、退出意图、控制状态、连续两次一致门槛、菜单向导、通知重试、SQLite 锁定、事务回滚、备份核心计数和 Linux CLI。

集成测试至少覆盖：

```text
信号入账
→ JoinQuant 拉取
→ 订单回传
→ 部分/完整成交回传
→ 账户和持仓入账
→ 自动对账
→ ERROR停买/CRITICAL熔断
→ 连续两次一致
→ 人工向导解除
```

本地代码和测试通过只代表 `implemented`；服务器 schema、服务和 JoinQuant 模板同步后是 `deployed`；真实模拟盘交易日出现订单、成交、对账和控制证据后是 `observed`；连续 3 个有效交易日无重复成交、无不可解释差异且人工解除演练通过，才可把执行与对账标为 `validated`。策略有效性仍按 20 个有效交易日评价。

## 1. 原始基线设计（保留追溯）

本节至第 17 节保留 2026-07-11 的 Batch 1 原始设计和观察口径，用于解释 schema 1 的服务器历史检查点；当前本地 schema 6、强制模拟盘风险规则、部署顺序和历史回测状态分别以第 0 节、分层退出专项 spec/plan、完整账本 plan 和逐日历史回测 spec/plan 为准。旧基线中的“后续、未实现或观察模式”不得覆盖这些当前文档。

### 1.1 目标与范围

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
3. 是否导入 strict 时点数据并完成完整逐日历史回测运行与验证；
4. 是否延长模拟验证；
5. 是否开始单独设计小资金实盘适配层。

未完成 strict 历史回测验证、订单对账和实盘级风控前，不接入真实资金。
