# 统一执行契约与五项 P0 修复设计

更新日期：2026-07-14

> 主文档：`docs/project_roadmap.md`。本文是风险准入、最终买入计划、退出意图续执行、JoinQuant 组合上限和持仓分类暴露的专项设计。
>
> 当前状态：`implemented（已推送） / deployed（服务器与 JoinQuant 模板） / not observed / not validated`。代码已随 `52b3653` 推送并部署到服务器 `/opt/stock-analysis`；专项测试 123/123、Python 编译和 `ledger-check` 通过，SQLite 仍为 schema version 6，三个核心服务 active。JoinQuant “AI” 策略已持久化模板版本 `2026-07-14.2-p0-execution-contract`，并保留原 URL、token 和运行配置。真实模拟盘交易日尚无本版本证据。

## 1. 目标

修复当前模拟盘链路中的五项 P0：

1. 风险引擎判定 `allowed=False` 时，导出器仍可能生成买入信号。
2. SQLite 中未完成的退出意图不会主动重新进入下一轮卖出发布。
3. JoinQuant 的 5 只持仓、80% 总仓位和买卖开关已配置但未形成完整强制链路。
4. 扫描通知、信号导出和持仓周期冻结使用不同的止损、止盈和仓位计算结果。
5. 已有持仓缺少行业和题材分类，导致集中度计算把其暴露当成零。

本设计保持当前 JoinQuant 模拟盘为唯一模拟执行端，不接入真实资金，不改变 SQLite schema version 6，不引入第三方依赖。

## 2. 总体方案

采用单一、版本化的执行契约：

```text
风险引擎当前轮准入
→ 生成唯一最终执行计划
→ 信号锚点冻结价格与仓位
→ 当前轮再次确认 execution_allowed
→ 组合容量与分类暴露检查
→ JSON + SQLite 使用同一计划
→ JoinQuant 做平台侧二次上限检查
```

最终执行计划是通知、信号、SQLite 原始信号和持仓周期的共同事实源。导出器不得再用另一套公式静默替换扫描结果。

## 3. 状态和字段契约

买入候选至少携带：

```text
execution_plan_version
execution_allowed
execution_reject_reason
entry_price
stop_loss
take_profit
risk_per_share
risk_reward
position_pct
board_type
market_regime
industry
theme
```

第一版执行计划版本使用明确字符串，例如：

```text
2026-07-14.2-p0-execution-contract
```

`execution_allowed` 是当前扫描轮次的准入结果，不进入长期冻结。入场价、冻结止损、首段止盈和目标仓位可以按现有信号锚点规则冻结。

卖出信号继续使用 schema version 1 的兼容扩展：

```text
id
action=sell
target_qty
reason
```

卖出不要求买入执行计划字段，也不受普通买入准入失败影响。

## 4. P0-1：风险准入必须强制执行

`build_risk_decision()` 继续负责当前轮风险判断。`build_risk_bundle()` 必须把 `decision.allowed` 映射为 `execution_allowed`，并保存稳定拒绝原因。

导出器处理买入时：

- `execution_allowed=False`：拒绝并记录 `buy_risk_disallowed`。
- 主流程缺少执行契约或执行计划版本无效：拒绝并记录 `buy_execution_plan_missing`。
- 卖出行不执行上述买入检查。

信号锚点不得恢复旧轮次的允许状态。即使同一信号先前允许买入，只要当前轮趋势、流动性、市场状态或盈亏比使风险引擎拒绝，本轮就不能发布买入。

## 5. P0-2：退出意图续执行

每轮扫描在计算新退出动作时同时读取 `TradingStore.get_open_exit_intents()`。对于仍有持仓且 `current_qty > target_qty` 的活动退出意图，必须重新合并为卖出行，即使当前价格已经离开最初触发位。

续执行规则：

- 保持原 `signal_id`、`target_qty` 和原因，保证账本关联稳定。
- T+1、停牌、跌停、部分成交、撤单或临时拒单不会完成退出意图。
- 当前持仓达到或低于目标数量后，由快照对账把意图标记为完成。
- 同证券存在平台未完成委托时，JoinQuant 继续拒绝重复委托；委托终止后，同一活动退出意图可以再次尝试。

退出优先级保持：

```text
硬止损
> 市场风险退出
> 首段止盈后的移动止盈
> +2R 首段止盈
> 时间止损
```

新的更高优先级退出可以覆盖旧意图；相同或更低优先级动作不能把正在执行的高优先级退出降级。更严格的目标数量可以覆盖更宽松目标，反向覆盖不允许。

## 6. P0-3：JoinQuant 强制组合边界

实际模拟盘强制参数为：

```text
JOINQUANT_MAX_POSITIONS=5
JOINQUANT_MAX_TOTAL_POSITION_PCT=80
JOINQUANT_ALLOW_BUY=1
JOINQUANT_ALLOW_SELL=1
```

职责划分：

- `JOINQUANT_MAX_POSITIONS` 和 `JOINQUANT_MAX_TOTAL_POSITION_PCT` 是实际买入强制参数。
- `MAX_TOTAL_POSITION_PCT=95` 继续作为观察账本的软阈值，不再决定实际 JoinQuant 买入容量。
- `JOINQUANT_ALLOW_BUY` 与交易时段、健康门和 SQLite 控制状态共同决定是否允许发布买入。
- `JOINQUANT_ALLOW_SELL` 是明确的人工配置开关；默认开启。普通买入禁用不得影响卖出。

服务器导出器按以下数据累计检查：

- 当前实际持仓数量和市值。
- 未完成买单的仓位与风险占用。
- 本轮已接受的更高分候选。
- 80% 总仓位和 5 只持仓上限。

JoinQuant 模板再检查当前平台持仓数量和总仓位，形成第二道保护。模板版本同步升级，旧网站模板必须由健康检查识别为不一致。

稳定拒绝原因至少包括：

```text
buy_disabled
sell_disabled
buy_max_positions
buy_total_position_limit
```

## 7. P0-4：唯一最终执行计划

最终买入计划由一个纯函数生成，复用现有 `exit_policy` 的板块、ATR、最大止损距离和账户风险预算规则：

```text
entry = 风险引擎确认的计划入场价
stop = 板块 + 支撑 + ATR + 最大止损距离共同确定
R = entry - stop
take_profit = entry + 2R
position_pct = 单笔风险预算反推仓位，并受原始仓位上限与市场状态约束
```

风险引擎仍可用 short/mid、压力位、趋势和最低盈亏比决定是否允许介入；一旦允许，实际通知与执行字段必须使用上述最终计划。

信号锚点规则：

- 首次有效候选冻结最终 `entry/stop/take/position`。
- 后续扫描不得下移冻结止损或用另一套公式改写计划。
- 执行计划版本变化时，旧版本缓存不复用，下一轮重新建立新版本锚点。
- 当前轮 `execution_allowed`、拒绝原因和买点状态始终重新计算，不从缓存恢复。
- `risk_plan` 文案在锚点处理完成后从最终字段生成，确保企业微信、控制台、CSV、JSON 和 SQLite 一致。

导出器只验证并复制最终字段，不自行生成不同的止损、止盈或仓位。

## 8. P0-5：已有持仓分类暴露

新买入信号的 `raw_json` 保存规范化 `industry` 和 `theme`。活动持仓通过：

```text
position_cycles.entry_signal_id
→ signals.raw_json
→ industry/theme
```

恢复原始买入分类。查询只覆盖活动持仓周期，不扫描多年历史。

旧持仓回退顺序：

1. 活动周期关联的原始买入信号。
2. 当前行业映射缓存。
3. 可稳定推导的题材标签；没有独立题材时使用当前规范化行业标签作为保守分组。
4. 仍无法分类时进入统一的 `__UNCATEGORIZED__` 暴露桶。

组合检查必须把当前持仓、未完成买单和本轮已接受买入累计到同一暴露表：

```text
单行业目标市值 <= 25%
同题材目标市值 <= 20%
未分类新增仓位 <= 10%，且已有未分类暴露不再按零处理
```

## 9. 异常与安全降级

- SQLite 无法读取活动周期或退出意图：停止新买入，仍保留可由可信持仓固定止损生成的合法卖出。
- 执行计划字段无效：拒绝买入，不以默认值猜测。
- 持仓分类查询失败：该持仓计入未分类暴露，不按零处理。
- JoinQuant 网站模板版本不一致：健康检查阻止新买入，卖出继续按现有安全边界处理。
- 退出意图重放不改变 `signal_id`，不创建逐轮新意图或无界事件文件。

## 10. 存储约束

本修复不升级 SQLite schema version 6。

新增分类和执行计划字段只进入已有不可变 `signals.raw_json`；活动分类通过索引关联查询。退出意图继续使用现有长期审计表，不为每轮重试新增一条意图。

因此：

- 不新增 JSONL。
- 不新增逐轮快照文件。
- 不复制持仓历史。
- 信号行年度增长口径不变。
- 备份、366 天热数据和长期账本保留策略不变。

## 11. 测试要求

至少补充以下回归测试：

### 风险准入

- `decision.allowed=False` 的高分候选不能导出买入。
- 旧锚点曾允许、当前轮拒绝时不能买入。
- 卖出不受买入执行契约缺失影响。

### 最终计划一致性

- 企业微信/扫描行、JSON 信号和 SQLite `raw_json` 的入场、止损、止盈和仓位逐字段一致。
- 执行计划版本变化后旧缓存不会覆盖新计划。

### 退出意图

- 价格反弹后活动硬止损意图仍重新发布。
- T+1、部分成交和撤单后继续发布剩余目标。
- 达到目标后停止发布。
- 新硬止损覆盖旧首段止盈，低优先级动作不能反向覆盖。

### 组合上限

- 已持有 5 只时拒绝第 6 只。
- 当前持仓、未完成买单和本轮候选累计后不得超过 80%。
- `JOINQUANT_ALLOW_BUY=0` 阻止买入但保留卖出。
- `JOINQUANT_ALLOW_SELL=0` 产生明确拒绝诊断。
- JoinQuant 模板包含平台侧 5 只/80% 二次检查。

### 分类暴露

- 原始买入信号分类可由活动周期恢复。
- 已有行业/题材持仓计入 25%/20% 上限。
- 本轮多个候选依次累计暴露。
- 旧持仓分类缺失时计入未分类桶，不按零处理。

## 12. 文档与状态同步

代码和测试完成后同步：

- `docs/project_roadmap.md`
- `docs/project_handoff.md`
- `docs/live_trading_execution_plan.md`
- `docs/codex_simulation_observation_plan.md`
- `docs/data_storage_policy.md`
- `docs/superpowers/specs/2026-07-13-layered-exit-risk-management-design.md`
- `docs/superpowers/plans/2026-07-13-layered-exit-risk-management.md`
- `linux_deploy.md`

状态必须分别记录：

```text
planned
implemented
deployed
observed
validated
```

本地代码和测试完成后最多标记 `implemented（未提交/未推送）`。Git 提交、推送、服务器部署、SQLite 备份、服务重启和 JoinQuant 网站模板更新均需要用户后续分别授权。2026-07-14 用户已完成这些后续授权，本次部署证据支持提升为 `implemented（已推送） / deployed`，但不支持提升为 `observed / validated`。

## 13. 实施边界

本次不包含：

- 真实券商或 vn.py 接入。
- 自动调参或机器学习训练。
- 新的第二止盈目标。
- 市场风险清仓规则扩展。
- SQLite schema 7。
- 自动提交、推送、部署或服务重启。
