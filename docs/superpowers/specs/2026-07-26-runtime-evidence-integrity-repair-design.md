# 运行证据完整性修复设计

日期：2026-07-26；状态复核：2026-08-05
历史专项状态：`implemented（已推送） / deployed（服务器） / not observed / not validated`

当前工作树增量状态：Batch B 对通知 outbox、容量/失败证据、影子评分退役及运维入口的后续修改为
`implemented（本地功能分支，未提交） / not deployed / not observed / not validated`，不能回写成
服务器已运行状态。

部署证据：实现提交 `0731ac5` 与文档提交已进入 `origin/main`，服务器于 2026-07-26
快进到 `68d7283`。部署前 schema 9 在线备份完整性通过；Linux 全量 457/457、目标模块
编译、schema 10 `ledger-check`、迁移后在线备份及隔离恢复演练均通过，配置文件哈希未变化，
三个核心服务 active，重启后 ERROR 日志计数为 0。以上仅证明部署完成，不构成真实交易日
`observed` 或 `validated`。

## 1. 背景与目标

> 迁移注记：本专项正文中的“100 项/30 天、最多 5 次、`errcode=40058` 转 dead 的文件队列”仅描述已部署 schema 10 历史基线。Batch B 本地目标已改为 schema 12 SQLite outbox 的普通/高优先级/死信容量、tombstone、enqueue gap、有限重试和显式 legacy audit；这些后续语义尚未部署服务器。

服务器在 2026-07-12 至 2026-07-26 已产生两周运行证据，但现有账本和健康报告不能完整回答“每一轮是否成功、当天是否有效、收益是否包含费用、通知失败是否已经终止重试”。

已确认的主要缺口：

- journal 中有 150 次扫描失败，但 `strategy_runs` 没有对应失败记录，已有运行的 `result` 也为空。
- 盘后、午休和周末仍因信号或快照陈旧被计入 `critical`，污染稳定性评分。
- 企业微信有 6 条超过合理长度的消息返回 `errcode=40058`，失败队列每五分钟重复尝试且没有永久失败终态。
- 成交费用和日已实现盈亏缺失时被保存为数值 0，无法区分“真实为零”和“来源没有提供”。
- `signals` 已有结构化列，但价格、止损、止盈、评分和策略模式仅保存在 `raw_json`。
- `/login` 对非 ASCII 错误令牌使用字符串 `compare_digest`，会抛出 `TypeError`。

本次目标是修复运行证据和运维可观测性，使后续 10/20 个有效交易日观察可被可靠计数。不得改变候选筛选、评分、买入、卖出、止盈止损、仓位、风险门、自动对账控制或 JoinQuant 下单语义。

## 2. 方案选择

### 方案 A：增量扩展现有 SQLite 与有界文件（采用）

- 复用 `strategy_runs` 和 `signals` 现有列。
- schema 10 只增加费用/盈亏来源状态列，不重建成交表。
- 复用当前通知失败队列，增加状态、次数和下次重试时间，并按数量和时间界限重写。
- 健康报告增加会话新鲜度和观察状态，不新建第二套健康数据库。

优点是改动小、可幂等迁移、不会改变交易路径；缺点是历史费用和盈亏只能诚实标记为 `unknown`，不能凭空补算。

### 方案 B：重建成交/权益表并把金额列改为 NULL

语义最纯粹，但 SQLite 表重建、外键迁移和线上回滚风险较高，不适合作为本轮证据修复。

### 方案 C：继续只依赖 `raw_json` 和 journal

无需迁移，但无法形成可靠查询、有效交易日门槛或长期审核证据，不满足目标。

## 3. 设计

### 3.1 策略运行账本

主扫描进程在调用行情和候选构建前生成一次 `run_id`，写入：

- `result=running`
- `data_status=pending`
- `started_at`
- 当前策略和参数版本

同一 `run_id` 传给 JoinQuant 信号导出，避免扫描与导出形成两条不关联记录。

扫描正常返回时更新：

- `result=success`
- `data_status=complete`；空候选使用 `empty`，仍属于成功完成
- `finished_at`
- `error_message=NULL`

扫描抛出异常时，在保持原有守护进程继续运行的前提下更新：

- `result=failed`
- `data_status=failed`
- `finished_at`
- 有界、去换行、不得包含凭据的异常类型与消息

账本写入本身失败时只写 stderr/journal，不得阻止原有扫描或交易保护逻辑。

### 3.2 信号结构化字段

`SignalRecord` 和 `record_signal` 补齐现有列：

- `signal_price`：买入优先 `entry_price`，卖出使用 `price`
- `stop_loss`
- `take_profit`
- `final_score`
- `strategy_mode`：信号自身模式，缺失时使用扫描模式

`raw_json` 继续保留为不可变原始证据。重复信号仍按原有不可变冲突规则处理，不改变发布和执行幂等性。

### 3.3 费用与已实现盈亏可信度

schema 10 采用增加状态列而不是重建金额列：

- `fills.fee_data_status`：`reported` 或 `unknown`
- `daily_equity.fee_data_status`
- `daily_equity.realized_pnl_status`

判断规则：

- 逐笔成交只有在来源明确声明 `fee_data_status=reported`，且全部费用字段存在并可解析时才为 `reported`；旧 JoinQuant 模板会为缺失属性合成0，因此仅有三个数值字段仍按 `unknown`，不得把兼容默认值视为真实零。
- 当日只要存在一笔费用未知的成交，日费用状态就是 `unknown`；金额列可保存已知部分之和，但报告不得把它展示为完整成本。
- `realized_pnl` 只有来源明确提供或能够由完整、可追溯且费用可信的闭环成交计算时才标记 `reported/calculated`。本轮不使用账户总资产差额猜测已实现盈亏；无法可靠计算时保持数值兼容并标记 `unknown`。
- schema 10 迁移把历史成交和历史日权益状态统一回填为 `unknown`，不伪造历史真实性。

### 3.4 健康与有效交易日口径

健康报告拆分三个概念：

- `system_status`：账本、控制、模板、API、文件结构、持仓和对账是否正常。
- `freshness_status`：仅在 A 股连续交易时段要求信号和账户快照新鲜；盘前、午休、盘后、周末为 `not_applicable`。
- `observation_status`：当前检查点为 `valid`、`degraded`、`invalid` 或 `not_applicable`。

盘外陈旧年龄仍可展示，但不得：

- 添加 `signal_stale` / `snapshot_stale` issue；
- 降低稳定性评分；
- 把日观察计为失败；
- 触发微信报警。

交易时段内陈旧、账本不可用、模板不一致、对账 ERROR/CRITICAL 或交易服务关键错误仍按现有安全语义报告。日级有效性由当日健康历史中的交易时段检查聚合：存在 `invalid` 即当日无效；只有 `valid` 为有效；仅有轻微非执行性问题时为 `degraded`；无交易时段样本为 `not_observed`。

### 3.5 企业微信失败队列

公共发送出口在提交请求前按 UTF-8 字节计算完整 markdown 内容，保证不超过企业微信单条限制。超长内容按字符边界截断并添加“内容已截断”尾注；不拆成多个独立执行回报，避免破坏幂等语义。

失败项增加：

- `state=pending|dead`
- `attempt_count`
- `next_retry_at`
- `last_attempt_at`
- `error_kind`
- `error`

处理规则：

- `errcode=40058` 直接进入 `dead`，不再重试。
- 网络错误、HTTP 5xx 和未知临时错误有限退避重试。
- 达到 5 次仍失败进入 `dead`。
- 已因相同 `dedupe_key` 成功发送的队列项视为完成并移除，不再永久滞留。
- 队列最多保留 100 项、最长 30 天；重写采用临时文件原子替换。永久失败保留在同一有界队列中供审核，不新增无限 JSONL。

### 3.6 网页登录

比较前把提交令牌和配置令牌都编码为 UTF-8 bytes，再调用 `secrets.compare_digest`。任何 Unicode 错误令牌都只得到正常登录失败页面，不产生 500；令牌内容不得写日志或响应。

### 3.7 存储、备份与恢复

- schema 10 只增加三个短文本状态列，增长与现有成交/日权益行数一致。
- `strategy_runs` 每轮一行；超过366天且没有信号引用的空/失败运行随每日清理删除，有信号引用的运行作为长期审计证据保留并进入现有备份。
- `signals` 不增加新行或文件，只填充已有列。
- 通知队列有 100 项/30 天硬上限。
- 不新增逐扫描文件，不扩大 `raw_json`，不读取或输出密钥。
- 部署前必须完成正式 SQLite 在线备份和完整性校验；迁移后运行 `ledger-check`。
- 恢复演练只能从新备份恢复到隔离临时目录/临时数据库，执行 `integrity_check`、schema 和表计数核验；禁止覆盖正式 `trading.db`。

## 4. 测试与验收

测试必须先失败后实现，至少覆盖：

1. 成功、空结果和异常扫描均写入同一个运行账本，失败不会消失。
2. 信号结构化列与 `raw_json` 一致，重复发布保持幂等。
3. 费用字段完整、缺失和部分缺失的状态正确；历史迁移为 `unknown`。
4. 交易时段陈旧为异常，盘外陈旧为 `not_applicable` 且不扣分。
5. 当日 `valid/degraded/invalid/not_observed` 聚合正确。
6. 4096 字节边界、中文截断、40058 永久失败、临时失败退避、五次转 dead、去重成功清理和 100 项/30 天界限。
7. 非 ASCII 登录令牌返回认证失败而不是 500。
8. Windows 可运行全量测试、Linux 全量测试、目标模块编译和 schema 10 新库/旧库迁移测试。

部署后的严格状态只能标为 `implemented / deployed`。至少一个完整交易日无同类问题才能标记 `observed`；连续门槛和足够交易闭环满足前仍为 `not validated`。本轮修复完成后，稳定观察时钟从首个完整、有效的新交易日重新累计。

## 5. 明确不做

- 不修改买卖信号内容、候选池、评分阈值或排序。
- 不修改止盈止损、移动止盈、时间止损、T+1 和跳空二次入场规则。
- 不自动恢复或关闭 `buy_enabled` / `kill_switch`。
- 不启用训练模型、影子模型下单或自动调参。
- 不修改 JoinQuant URL、Token、网页认证 Token 或服务器其他密钥。
- 不用推算数据覆盖真实来源缺失。

## 6. 实现证据

2026-07-26 已在本地功能分支完成代码和测试：

- schema 10 增加三个来源状态列，不重建成交或权益表。
- 扫描在行情请求前创建运行记录，成功、空结果和异常均写终态；同一 `run_id` 贯穿信号导出。
- 超过366天且无信号引用的运行记录由既有每日清理删除，引用运行不破坏外键和审计链。
- 信号价格、止损、止盈、评分和模式写入既有结构化列。
- 盘外信号/快照陈旧为 `not_applicable`；健康报告增加系统、会话、检查点和日级观察状态。
- 企业微信完整 markdown 限制在 4000 UTF-8 字节内；旧/新 `40058` 进入 dead，临时失败最多五次，队列限制为100项/30天。
- 非 ASCII 错误登录令牌返回普通认证失败，不再产生 500。

目标专项 142 项通过；Windows 全量发现 457 项，其中 454 项完成通过。另有 3 项
`test_joinquant_linux_script` 因当前 Windows 环境没有 Bash 而无法启动，不属于业务断言失败，
需在 Linux 部署前运行全量测试补齐。目标模块 `py_compile` 和 `git diff --check` 通过。
上述 2026-07-26 历史专项的服务器部署证据仍有效；但当前工作树的后续增量尚未推送、部署、迁移正式库或重启服务，
因此这些增量不得标记 `deployed/observed/validated`。
