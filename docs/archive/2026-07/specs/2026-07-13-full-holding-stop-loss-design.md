# 全持仓硬止损信号设计

更新日期：2026-07-13

> 主文档：`docs/project_roadmap.md`。
>
> 本文件只定义 JoinQuant 同步持仓的硬止损信号补全；如状态、阶段或优先级与主文档冲突，以主文档为准。

## 问题

当前策略只把 JoinQuant 持仓信息挂到已经进入扫描候选池的股票行上。卖出导出器虽然支持持仓卖出，但持仓股票如果没有进入候选池，就没有对应数据行，因此即使现价已经跌破同步持仓的止损价，也不会生成卖出信号。

## 目标

每轮策略扫描都独立检查全部 JoinQuant 同步持仓。持仓股票无需进入候选池；只要有效现价小于或等于既有 `stop_price`，就生成 `stop_loss` 卖出行，并复用现有 JoinQuant 信号导出、SQLite Batch 1 双写和微信计划通知路径。

## 数据与价格

- 持仓来源仍为 `cache/portfolio_web/positions.json`，该文件由 JoinQuant 账户快照同步生成。
- 优先使用本轮全市场实时行情中的现价。
- 实时行情缺失或价格无效时，回退使用同步持仓的 `current_price`。
- 止损阈值只使用持仓中已经存在且大于零的 `stop_price`；本次不重新计算或改变止损比例。
- 只有 `status` 为 `holding` 或 `partial_sell`、数量大于零、代码和价格有效的持仓才参与判断。

## 数据流

1. `run_once()` 获取全市场实时行情并读取 JoinQuant 同步持仓。
2. 独立函数根据全市场价格和全部持仓构造触发硬止损的最小 DataFrame。
3. 在调用 `run_joinquant_export()` 前，将止损行追加到本轮候选导出源。
4. 同一股票若已经存在于候选导出源，则以硬止损行替换该股票的候选行，避免同轮同时出现买入和卖出意图。
5. `joinquant_exporter.py` 继续负责验证已有持仓并生成 `sell` 契约，不新增第二套导出逻辑。

## 卖出行契约

止损行至少包含：

- `code`、`name`、`price`
- `signal_action=stop_loss`
- `signal_state=stop_hit`
- `has_holding=True`
- `hold_status`、`hold_qty`、`hold_cost_price`
- `hold_stop_price`、`hold_take_price`
- `signal_note`，明确记录现价和止损价

JoinQuant 最终仍接收现有 schema version `1` 的 `action=sell` 信号；不改变 JoinQuant 网站策略模板。

## 安全边界

- 本次只补硬止损，不实现止盈、移动止盈、时间止损、趋势卖出或分批卖出。
- 不改变止损比例、候选池、评分、买点、仓位和买入过滤规则。
- 不绕过 JoinQuant 的交易日、T+1、停牌、跌停和实际撮合限制。
- 若无法取得有效价格或止损价，不猜测、不生成卖出信号。
- 不新增持久化文件、数据库表、第三方依赖或无限增长数据。

## 测试与验收

- 单元测试证明：持仓不在候选池且实时价格跌破止损时，仍会构造 `stop_loss` 行。
- 单元测试覆盖实时价格优先、快照价格回退、未触发止损、无效持仓和重复股票覆盖。
- 集成级测试证明：止损行经现有导出器生成 `action=sell`，且不会同时生成同股票买入。
- 运行相关测试和完整单元测试集。
- 本地测试通过只代表 `implemented`；服务器拉取并重启后才是 `deployed`；真实交易日产生卖出计划才是 `observed`；连续观察达到项目门槛后才是 `validated`。
