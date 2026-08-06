# 小资金实盘准备、事件通知与训练模型整合设计

日期：2026-07-28
状态：`Batch A Tasks 1-8 implemented（本地功能分支，未合并/未推送） / not deployed / not observed / not validated`；Batch B Tasks 1–7 schema 12 通知 outbox、生产者、容量控制、CRITICAL 复报、规则影子退役、systemd 路由和审计 CLI 为 `implemented（仅本地，未提交） / not deployed / not observed / not validated`；Batch C ML-7 Tasks 4–10 为 `implemented（本地未提交）`、Task 11 本地总验收进行中、Task 12 未授权/未运行，整体 `not committed / not deployed / not observed / not validated`；Batch D planned。

> 文档层级：本文件是 `docs/project_roadmap.md` 的专项设计从文档。主文档仍是唯一项目状态依据。
>
> 本文件整合小资金实盘前置、企业微信事件化通知、规则型影子评分退役、ML-7 五头表格模型和 QMT 执行节点边界。它引用现有专项文档，不重复定义已经部署的分层退出、五项执行正确性 P0、完整账本、历史回测和 ML-7 Task 1-3。
>
> 本设计获用户确认。2026-08-06 时 Batch A Tasks 1-8、Batch B Tasks 1–7 和 Batch C ML-7 Tasks 4–10 已在本地工作树实现；Batch C Task 11 本地总验收、文档真值和安全复审进行中，Task 12 未授权/未运行。Batch B 包含 schema 12 通知基础、来源事务生产者、稳定计划/TTL、租约 worker、有限重试、容量控制、CRITICAL 交易分钟复报、规则影子退役、systemd 路由、详细 `ledger-check` 和带事件键校验的人工解除 CLI。Task 7 已覆盖一手/奇数手离散目标、独立盈利保护、真实回调推进 stage、因果成交时间窗、所有有效止损消费者和 strict 日K反前视回归，执行计划版本为 `2026-08-01.1-small-capital-live-risk`。这些 synthetic 测试不等于真实 strict 数据验证；当前没有真实一年 strict 数据、可信/可批准或活动模型、人工审批或服务器 L0 证据。任何本地实现都不代表服务器已部署、JoinQuant 网站模板已更新、真实交易日已观察或真实资金已验证。任何配置修改、Git 提交/推送、服务器部署、服务重启、JoinQuant 更新或 QMT 实盘启用仍需当次单独授权。

## 1. 背景

当前主流程由 Linux 服务器生成策略信号，JoinQuant 模拟盘执行并回传账户、订单和成交。现有系统已经部署分层退出、五项执行正确性 P0、schema 10 交易账本、自动对账和运行证据修复，但这些能力大多仍处于 `deployed / not observed / not validated`。

面向后续小资金真实交易，当前还有五类相互关联的缺口：

1. 服务器当前运行入口尚未部署本批统一 `pre_trade_check(..., mode="enforce")`；本地功能分支已经完成纯函数检查、原子准入、容量预留和 JoinQuant 精确数量接线。
2. 服务器当前运行模板尚未获得本批数量契约；本地模板已移除普通买单金额回退，并按 100 股整数手、最低佣金、双边费用、人民币风险上限和组合容量生成唯一精确数量。
3. 企业微信通用去重依赖正文哈希和共享 JSON 文件。多进程覆盖、每轮变化的 `run_id` 和实时行情字段会造成重复消息或丢失去重状态。
4. 当前 `shadow_score.py` 是规则加权，不是训练模型。它增加了盘中展示和周报噪声，却不能证明统计泛化能力。
5. ML-7 Tasks 4–10 已在本地实现 strict 五分钟历史、成本标签、五头训练、模型治理和 L0 推理代码；Task 11 本地总验收进行中，Task 12 未授权/未运行。QMT 仍只有路线规划，没有适配器或执行节点代码。

本设计把这些缺口放在同一条执行链上解决，避免模型、资金分配、风控、通知和券商节点各自维护一套订单语义。

## 2. 当前状态基线

| 能力 | 当前严格状态 | 本设计中的处理 |
| --- | --- | --- |
| 2026-07-14 五项执行正确性 P0 | `implemented / deployed / not observed / not validated` | 保留并增加运行不变量测试，不重复改写既有业务语义。 |
| 分层退出和统一有效止损 | `implemented / deployed / not observed / not validated` | 保留硬止损、T+1、退出意图和卖出优先；只修正整数手分段语义。 |
| 通用 `pre_trade_check`、原子准入与 JoinQuant 精确数量 | `implemented（本地功能分支） / not deployed / not observed / not validated` | 双模式检查、不可变意图、容量预留、精确数量导出、服务器二次绑定和模板数量执行已完成本地测试；尚未改变当前服务器交易。QMT 必须使用 `enforce`。 |
| 通知去重和失败重试 | `implemented / deployed`（历史文件级基线） | Batch B 已在本地迁移到 SQLite 业务事件 outbox；提交、迁移和部署前仍不能把新语义写成服务器现状。 |
| 规则型影子评分 | `implemented（本地已退役） / not deployed / not observed / not validated` | 从活动运行链路、微信和周报退役；历史字段只读兼容。服务器在部署前仍可能保留旧版本行为。 |
| ML-7 Task 1-3 | `implemented / not deployed / not observed / not validated` | 保留共享契约、独立 `ml.db` 和候选采集。 |
| ML-7 Task 4-10 | `implemented（本地未提交） / not deployed / not observed / not validated` | strict 导入、标签、训练、五头模型包、治理、L0 旁路和维护代码已实现；没有真实一年 strict 数据或可信/活动模型。 |
| ML-7 Task 11 | `in progress（本地总验收）` | Batch E 文档、全量验证和安全复审正在进行；完成前不能声称本地总验收通过。 |
| ML-7 Task 12 | `not authorized / not started` | 服务器部署、L0 启用和交易日观察仍需另行授权；当前没有服务器 L0 证据。 |
| QMT 券商接入 | `planned` | 新增平台无关协议、故障模拟器和默认禁单的 Windows 节点；真实账户仍需外部条件。 |

旧 P0 的 `deployed` 不能用来证明新的真钱前置 P0 已完成。新能力必须独立经历 `implemented / deployed / observed / validated` 状态推进。

## 3. 目标与非目标

### 3.1 目标

- Linux 成为策略、模型、资金分配、强制风控、订单状态和 SQLite 账本的唯一权威。
- JoinQuant 继续承担主模拟盘执行；Windows QMT 节点作为未来真实券商的薄执行端。
- 所有平台使用同一份版本化费用、证券交易单位、精确数量和下单前风险契约。
- 买入遇到未知状态时 fail closed；合法卖出、止损和减仓不被买入开关阻断。
- 企业微信按业务事件发送，静默无变化的五分钟扫描、空计划和正常对账。
- 退役规则型影子评分，保留原规则策略作为唯一确定性基线。
- 完成五个小型表格预测头的训练、治理和 L0 旁路推理代码，但不自动放权。
- 所有新增持久化数据有容量、保留、备份、恢复、隐私和测试边界。

### 3.2 非目标

- 本设计不授权真实资金下单、修改服务器私有配置或开启 QMT 交易。
- 不用 QMT 节点计算策略、模型、仓位或放宽 Linux 风控结果。
- 不引入神经网络、强化学习、在线学习或自动参数批准。
- 模型不得控制卖出、硬止损、T+1、停牌、涨跌停、绝对仓位、`buy_enabled` 或 `kill_switch`。
- 不移除 JoinQuant 模拟盘，不恢复旧本地模拟盘。
- 不把框架测试、模拟故障测试或非交易日检查写成 `observed` 或 `validated`。

## 4. 方案选择

### 4.1 执行架构

采用“Linux 主系统 + 一台长期在线的 Windows QMT 薄执行节点”。

```text
Linux
行情/规则策略/五头模型/资金分配/强制风控/SQLite账本
  -> 已签名、可过期、精确数量的执行意图
Windows
QMT节点：账户与行情复核/下单/撤单/查询/回传
  -> 券商
```

Windows 节点主动向 Linux 拉取命令并回传事件，不要求 Windows 暴露公网入站端口。首选私有 VPN；即使使用私网，也保留应用层签名、时效和防重放。

不采用以下方案：

- Windows 策略一体机：会形成第二套决策、风控和状态事实源，重启或版本漂移时难以审计。
- Linux 直接运行 QMT：QMT/miniQMT 通常依赖本地 Windows 券商终端，不符合运行环境。
- 立即用 vn.py 替换现有项目：会扩大迁移范围。未来只有在券商提供稳定 Linux gateway 时，才把 vn.py 作为第二个 `BrokerAdapter`。

### 4.2 通知

采用 SQLite transactional outbox，不继续扩大共享 JSON 冷却文件。成交、退出意图、订单终态、对账转换和控制事件已经写入 `trading.db` 时，对应通知必须在写入业务事实的同一事务入队。盘前、盘后和周报等派生事件使用独立的幂等事务。这样才能关闭“事实已提交但通知未创建”的漏报窗口。

### 4.3 模型

采用五个独立表格模型：D+3、D+5、D+10 扣费收益、下行损失和下一合理成交窗口成交概率。Ridge/Logistic 作为透明基线，scikit-learn `HistGradientBoosting` 作为第一代 challenger。

不采用首版神经网络。现有数据以结构化股票日为主，五分钟重复行高度相关，不足以支撑高容量序列模型。

## 5. 目标组件与职责

### 5.1 Linux 决策主链

Linux 依次执行：

```text
候选与规则计划
-> 可选 L0 模型预测
-> 小资金经济性与数量分配
-> 强制 pre_trade_check
-> SQLite 原子容量预留和订单就绪
-> BrokerAdapter 传输
-> 订单/成交回传与对账
-> 通知 outbox
```

模型先回答市场机会，账户经济层再回答这笔交易对当前小资金账户是否值得做。账户余额、持仓槽位和最低佣金不进入市场预测特征。

### 5.2 Windows QMT 节点

Windows 节点只允许：

- 回传心跳、能力、交易日、券商时间和节点版本；
- 查询账户、持仓、订单、成交、行情和证券规则；
- 提交 Linux 已批准的精确数量订单；
- 撤销明确指定的订单；
- 按稳定 ID 重放订单和成交回报；
- 在断线重连后先完成全量快照与对账。

Windows 节点禁止：

- 自行生成买卖信号；
- 调整数量、止损、价格上限或风险阈值；
- 因超时自动补单；
- 在 Linux 意图过期后继续执行；
- 把本地缓存当成新的策略事实源。

### 5.3 JoinQuant 模拟盘

JoinQuant 保持当前模拟执行职责。新增执行契约应逐步由 JoinQuant 和 QMT 共用，但本轮不得为了适配 QMT 重写已部署的 JoinQuant 主链。两端差异只存在于平台映射和回调采集。

## 6. 统一执行契约

### 6.1 `FeeSchedule`

费用模型必须版本化，并被实时数量分配、历史回测、ML 标签和复盘共同引用：

- 买入佣金率和最低佣金；
- 卖出佣金率和最低佣金；
- 印花税；
- 过户费、经手费和其他可配置费用；
- 买卖侧滑点；
- 生效日期和 `fee_schedule_version`；
- 契约版本及 `execution_scope=simulation/live/both`。

禁止在 `backtest_engine.py`、`historical_backtest.py`、标签代码和实盘分配器中保留互相矛盾的默认税费。真实券商成交费用以 `reported` 回报为最终事实；预测和下单前使用冻结版本的估算费用。QMT 新买入只接受 `live/both` 费用契约；`simulation` 契约不能通过改名获得实盘权限。旧费用载荷按 v1 原哈希只读兼容，不得重新解释为实盘费用。

### 6.2 `InstrumentRules`

每只证券至少冻结：

- 交易所、板块和证券类型；
- 买入最小申报量、数量步长和零股卖出规则；
- 最小价格变动单位；
- 当日涨跌停价、停牌和特殊交易状态；
- 规则来源、时间和哈希。

QMT 实盘缺少证券规则或规则陈旧时禁止新买入。不得只用股票代码前缀长期推断交易单位和涨跌幅。

### 6.3 `BrokerSnapshot`

下单前快照包含：

- 稳定 `account_scope_id`、交易日、券商时间和快照 ID/hash；
- 总资产、现金、可用与冻结资金；
- 全量持仓的总量、可卖、冻结和当日买入量；
- 全体未完成订单、当日成交和已报告费用；
- 日内盈亏、账户回撤和数据新鲜度；
- 适配器、节点、会话和能力版本。

快照只保存必要账户事实，不在日志、通知或模型库中暴露完整账号和凭据。

完整快照必须显式包含持仓、订单和成交集合，缺失集合不得解释为空集合。`daily_risk_evidence_status=reported` 时必须显式提供日内盈亏和账户回撤；缺失任一字段只能标记为 `unknown`，并禁止新买入。旧 v1 快照保持原规范化载荷和哈希，不得重新解释账户适配器或风险证据状态。

### 6.4 `StrategyOrderCandidate` 与 `ExecutionIntent`

策略先生成 `StrategyOrderCandidate`。它包含候选 ID、稳定 `logical_signal_id`、仅供审计的来源 `signal_id/run_id`、策略/参数/模型版本、账户作用域、股票、方向、建议入场/止损/目标、信号时间和冻结有效期；不包含最终数量、`pre_trade_result_id` 或 `client_order_id`。

`pre_trade_check` 允许后，Linux 才根据候选和风控结果生成不可变 `ExecutionIntent`：

- `client_order_id`、`logical_signal_id`、来源 `signal_id`、策略/参数/模型/费用版本；
- 账户与适配器作用域；
- 股票、方向、精确 `order_qty`、预期当前持仓和目标持仓；
- 限价或价格上限、止损、信号时间、过期时间；
- 使用的账户/行情/证券规则快照 ID 与哈希；
- `pre_trade_result_id` 和规范化意图哈希。

`client_order_id` 在同一账户范围内全局稳定。相同 ID 和相同内容幂等返回；相同 ID 和不同内容属于数据冲突，必须拒绝并升级告警。

### 6.5 `PreTradeResult`

统一检查为纯函数，不在函数内部生成最终执行意图或下单：

```text
pre_trade_check(candidate, broker_snapshot, quote, instrument_rules,
                system_state, risk_policy, reservations)
```

输出至少包含：

- `allowed`；
- 稳定 hard block 和 warning 代码；
- 规范化候选、批准的精确数量和目标持仓；
- 预计现金、单票/总仓位、行业/题材和开放风险；
- 单笔人民币风险、百分比风险、往返费用和费用侵蚀；
- 所用快照和策略版本；
- `checked_at`、`valid_until` 和结果哈希；
- 风险策略、容量视图和系统控制状态的独立哈希。

`RISK_MODE` 扩展为 `observe` 和 `enforce`，但只控制本设计新增的资金经济性和迁移期策略软门。既有风险拒绝、`buy_enabled/kill_switch`、陈旧信号/快照、重复订单、唯一执行计划、T+1、可交易性、5 只/80% 和分类暴露等不可变安全块在两种模式下都必须硬阻断。JoinQuant 只可在明确迁移阶段观察新增软门；任何 QMT 真实账户适配器必须对全部门使用 `enforce`，否则拒绝启动交易。

买入检查适用全部资金、容量、经济性和系统控制门。卖出检查只验证意图所有权、当前可卖量、T+1、停牌/跌停、价格保护和快照新鲜度；`buy_enabled=0`、买入容量不足或买入经济性不成立不得阻断合法止损、止盈和减仓。

容量视图逐仓绑定证据。新执行链持仓引用完整签名买入意图，并持续使用签名止损/跳空场景中更严格的开放风险；迁移前已有持仓使用独立 `adopted_legacy` 证据，绑定账户、适配器、持仓周期、初始数量、收养时间、来源哈希、有效止损和冻结跳空价，不伪造历史 `ExecutionIntent`。其开放风险按券商平均成本和当前剩余数量重新应用完整往返费用。两类证据都必须与同一账户、适配器和完整券商快照一致，否则新买入失败关闭。

### 6.6 原子容量预留与提交权

多个候选可能由同一轮或不同进程同时处理。强制流程必须在一个 SQLite 事务内：

```text
重读系统控制和有效账户快照
-> 汇总已有持仓、未完成订单和活动预留
-> 对 StrategyOrderCandidate 执行 pre_trade_check
-> 写入完整风险决定
-> 根据允许结果生成 ExecutionIntent
-> 写入引用该 ExecutionIntent 的现金/仓位/行业/题材/开放风险预留
-> 以 client_order_id 创建唯一 READY 订单
```

禁止使用 `PortfolioState.empty()` 或不含真实账户/挂单的空状态生成实盘审计证据。两个并发买单不能分别看到同一份剩余容量并同时通过。风险拒绝只写候选和 `PreTradeResult`，不创建订单、意图或预留。QMT 节点领取 READY 订单使用带租约的 CAS。从未进入 `SUBMITTING` 的 READY 意图可以在无有效租约（包括租约安全回收后）且到期时转为 `EXPIRED` 并释放预留；已经提交的订单只有在 `NOT_SUBMITTED/REJECTED/CANCELLED/FILLED` 终态及对应对账完成后，才能释放适用的剩余预留。终态但尚待对账的预留继续占用剩余容量；`SUBMITTING/SUBMIT_UNKNOWN/SUBMITTED/PARTIALLY_FILLED` 不得因意图时间到期释放。部分成交按实际成交和未完成数量、与账本一致的向上分币规则调整预留。终态买入预留不得阻断已有持仓的保护性卖出；READY、提交中、未知、部分成交或待撤的同股买单必须先完成撤单确认，未确认前不得并发卖出。卖出按目标持仓和活动退出意图占用唯一执行权，避免同一股票出现重复减仓。

同一候选只有在意图仍为 `READY`、预留仍活动、订单从未提交、未过期且当前券商订单/成交/事件不存在任何提交证据时才可幂等返回原准入结果。`SUBMIT_UNKNOWN` 不得由增量快照关闭；明确未受理也必须等同账户全量订单/成交对账一致后才能转 `NOT_SUBMITTED` 并释放预留。自动恢复买入沿用两份不同、停单后、同账户 `matched` 快照的既有规则，增量或全量均可计数；但任何活动的未知或终态待对账预留都会继续阻断恢复，因此未知提交至少先经过一次 `full + matched + 0 difference` 才可能解除。

新买单在 `NOT_SUBMITTED` 后不得按同一 `logical_signal_id` 自动重发，未来只能由带审计控制事件的人工重发入口创建新尝试。保护性卖出为避免退出死锁，可在原尝试已成为 `EXPIRED/CANCELLED/REJECTED`、预留经合法证据释放、同一退出 owner/target 仍活动且没有任何活动/未知券商订单时，用新的 candidate/client order ID 重试；`NOT_SUBMITTED` 仍不自动重发。

## 7. 小资金数量与风险

### 7.1 数量分配

所有买单统一生成 `target_qty`，不让平台按目标金额自行舍入。最低佣金是订单级非线性成本，数量分配必须按证券数量步长离散求解，不能把它摊成固定每股成本后直接使用闭式公式：

```text
按权益比例计算的风险金额 = 账户权益 × 板块单笔风险比例
最终单笔风险金额 = min(按权益比例计算的风险金额, 配置的人民币风险上限)
初始数量上界 = 单票、总仓位、现金、行业、题材、开放风险和槽位共同上限
for qty in 从初始数量上界按证券步长向下枚举:
    计算 qty 对应的完整买入费、保守卖出费、双侧滑点和现金占用
    计算止损损失、跳空损失、组合开放风险和费用侵蚀
    选择第一个同时通过风险、现金、容量和经济性约束的 qty
target_qty = 合格 qty；不存在合格整数手时拒绝买入
```

计划止损场景按有效止损价计算卖出费用和损失，跳空场景按冻结跳空价另算一遍；两种场景都完整应用卖出最低佣金、税费和滑点，并使用更严格的结果。入场价与有效止损价的风险距离必须为正且所有输入有限；止损缺失、止损不低于入场价、费用版本缺失或计算结果非有限时禁止买入。QMT 真实账户必须配置正数人民币单笔风险上限；未配置时禁止买入。JoinQuant 现有模拟流程在迁移阶段可继续使用当前按权益比例计算的风险金额，但必须在报告中明确未应用人民币上限。

### 7.2 最低经济订单

分配器计算：

- 往返费用和滑点金额；
- 盈亏平衡涨幅；
- 规则目标或模型预测下的预期净收益；
- 止损和跳空情景下的预期人民币损失；
- `cost_to_expected_edge_ratio`；
- `economic_trade_allowed` 和稳定拒绝原因。

不足最小申报量、预计优势被最低佣金和滑点吞噬、T+1 隔夜/跳空风险超过预算时拒绝。经济层只能减量或否决，不能扩大规则给出的最大仓位。

模型权限必须贯穿经济层。模型关闭、L0 和 L1 时，`economic_trade_allowed` 只使用冻结的规则目标和费用假设；L0 预测只记账，L1 只改变规则合格候选的排序。只有人工批准的 L2/L3 才能把模型预期收益用于减少或否决买入，且 L3 的仓位调整仍受 `0.8-1.1` 和全部硬风控约束。

### 7.3 一手和奇数手的 `+2R`

当前按向下取整计算“剩余半仓”，100 股会在 `+2R` 变成目标 0，300 股会卖出 200 股。新语义冻结为：

- 目标剩余数量按证券步长向上取整到“不少于初始半仓”；
- 100 股在 `+2R` 不卖出，但写入独立的盈利保护激活状态并启用移动止盈；
- 300 股在 `+2R` 目标剩余 200 股，只卖 100 股；
- 硬止损、有效移动止损、时间止损和市场风险退出仍可清仓；
- 报告记录实际卖出比例，不再笼统显示“卖出一半”。

新增 `profit_protection_activated_at` 和 `trailing_stop_active_from` 两个独立状态字段。达到 `+2R` 时在同一事务写入盈利保护激活时间、最高价和下一决策批次生效时间，避免每五分钟重复触发；同一扫描批次不得先激活再立即按新移动止损清仓。`position_cycles.take_profit_stage` 只在真实部分减仓成交确认后推进，不能用它表示“未成交但保护已激活”。后续批次仍按既有退出优先级和唯一退出意图处理，禁止盈利保护另建重复卖单。

该变更属于策略行为变化，必须独立回测并在 JoinQuant 模拟盘观察后才能用于真实资金。

### 7.4 跳空一手例外

跳空一手必须同时通过：

- 板块对应的单笔风险预算；
- 组合剩余开放风险；
- 单票、总仓位、行业、题材、现金和持仓数量上限；
- 真实往返费用和滑点；
- 绝对价格上限和当前批次重新确认。

禁止把“组合剩余开放风险”当成“单笔风险预算”传入一手判断。两者必须分别计算并分别记录拒绝原因。

## 8. QMT 安全协议与状态机

### 8.1 连接方式

- Windows 节点主动连接 Linux；首选 Tailscale、WireGuard 或等价私有 VPN。
- 应用层请求包含协议版本、节点 ID、账户作用域、时间戳、nonce、正文 SHA-256 和 HMAC。
- 默认允许 30 秒时钟偏差；命令本身另有更短业务过期时间。
- Linux 保存 nonce 的有界防重放记录和单账户节点租约；同一账户只能有一个活动执行节点。
- QMT 账号、客户端路径、会话参数和密钥只存在于 Windows 私有配置，不进入 Git。

### 8.2 最小接口

协议至少支持：

```text
health/capabilities
lease next ready order
post final preflight snapshot
begin submit with lease token
acknowledge submit result
post account/position snapshot
post order events
post fill events
query order by client_order_id
request explicit cancellation
```

Windows 节点的本地 `QmtBrokerAdapter` 实现：

```text
fetch_account()
fetch_positions()
fetch_orders(since)
fetch_trades(since)
place_order(order_intent)
cancel_order(order_id)
query_by_client_order_id(client_order_id)
```

Linux 与 Windows 之间传输规范化对象，不暴露 XtQuant 原生对象。

### 8.3 订单状态

沿用现有统一状态：

```text
CANDIDATE -> RISK_REJECTED
CANDIDATE -> CREATED -> READY -> EXPIRED
CANDIDATE -> CREATED -> READY -> SUBMITTING -> SUBMITTED
SUBMITTING -> REJECTED/NOT_SUBMITTED/SUBMIT_UNKNOWN
SUBMITTED -> PARTIALLY_FILLED -> FILLED/CANCELLED
SUBMITTED -> FILLED/CANCELLED/REJECTED
```

`RISK_REJECTED` 是候选终态，不生成订单；`EXPIRED/NOT_SUBMITTED/REJECTED/CANCELLED/FILLED` 是订单终态，均不得回退。重复和乱序回调只允许数量与状态向前推进。逐笔成交按券商成交 ID 幂等；缺少稳定 ID 时使用确定性摘要并执行内容冲突检查。`NOT_SUBMITTED` 只表示已经获得券商权威未受理证据并完成全量对账，不得由本地超时推断。

`SUBMIT_UNKNOWN` 的处理顺序固定为：

1. 禁止重新提交；
2. 按 `client_order_id`、券商备注和订单号查询；
3. 找到订单或成交后恢复统一状态；
4. 只有券商给出权威的未受理/不存在结果，并完成账户、订单和成交全量对账后，Linux 才把本次提交关闭为 `NOT_SUBMITTED`；
5. 原意图不得自动重试。再次下单必须由人工控制事件批准，重新取得新鲜快照、重新执行风控，并创建新的 `client_order_id`；
6. 超过时限仍未知时停止新买入并进入 CRITICAL。

### 8.4 断线与重启

- Windows 断线时 Linux 不产生可被其他节点接管的重复提交；租约到期后仍先查询未知状态。
- 节点重启先回传账户、持仓、未完成订单和当日成交全量快照，再允许领取新买单。
- 每笔订单领取后、调用 `place_order` 前，Windows 必须重新查询账户、持仓、未完成订单、行情和证券规则，并把最终快照 ID/hash 回传 Linux。Linux 校验账户作用域、`valid_until`、价格保护和预留仍成立后，才用同一租约 CAS 把 `READY` 改为 `SUBMITTING` 并签发一次性提交许可。
- 最终快照与 Linux 前置条件不一致时，Windows 不得自行调整数量或价格，也不得调用券商；它返回稳定拒绝原因，由 Linux 使用新快照重新执行完整风控。签发提交许可后无明确结果的订单一律按 `SUBMIT_UNKNOWN` 处理。
- 过期意图不得重放；未过期卖出意图仍需通过当前可卖量和行情检查。
- 对账差异、未知提交或节点版本不匹配时进入 sell-only 或全自动停单，具体动作按严重度和所有权规则执行。
- QMT 节点不得在 Linux 不可达时自行执行缓存中的新订单。

首版节点必须默认 `QMT_ORDER_ENABLE=0`。真实下单采用双重门：Linux 对目标账户明确启用 QMT 适配器且处于 `enforce`，Windows 对同一账户明确启用下单；任一侧关闭、账户摘要不一致或版本不匹配都 fail closed。只有协议测试、Windows 故障模拟、账户只读查询和人工核对通过后，用户才能在独立任务中授权启用这两个门。未配置或未启用的 QMT 节点状态为 `not applicable`，不得影响当前 JoinQuant 模拟交易的健康状态。

## 9. 企业微信事件 outbox

### 9.1 数据模型

正式交易 SQLite 增加 `notification_outbox`，至少包含：

- 稳定的 `account_scope_id` 和全局唯一 `event_key`；
- 事件类型、业务对象、优先级和正文版本；
- `pending / leased / sent / dead / cancelled`；
- `lease_owner`、`lease_until`、尝试次数和下次尝试时间；
- 业务发生时间、创建时间、`expires_at`、发送时间、取消请求/原因和最后错误代码；
- 规范化事件载荷 SHA-256、当前有界正文、正文 SHA-256 和元数据 JSON。

同库增加有界 `notification_enqueue_gaps`，以 `event_key` 唯一记录因容量或写入路径异常未能创建 outbox 行的载荷哈希、来源事实 ID、原因、发生时间和修复状态。它只证明通知缺口，不参与发送；修复工具必须在核对来源事实和 tombstone 后把事件补入 outbox 或记录不可重放结论。

每个逻辑事件独立入队。账本内业务事实和 outbox 行必须共用一个 SQLite 事务；生产者不得先提交事实再调用通用发送函数。相同 `event_key` 和相同规范化事件载荷幂等；相同键与不同载荷属于数据冲突，禁止覆盖。发送时增加的当前时间不属于事件语义。领取使用 SQLite CAS；只有当前租约持有者可以写发送结果。工作器崩溃后，租约到期的同一行可重新领取。

Webhook 无服务端幂等键。HTTP 已送达但响应丢失时仍可能重复，因此系统只能承诺应用层“至少一次、通常一次”，不得写成严格 exactly-once。

### 9.2 稳定业务键

```text
{adapter}:{account_scope}:buy-plan:{trade_date}:{logical_signal_id}:{plan_version}
{adapter}:{account_scope}:exit:{position_cycle_id}:{exit_intent_id}:{stage}
{adapter}:{account_scope}:fill:{fill_id}
{adapter}:{account_scope}:order-terminal:{client_order_id}:{status}:{reason_code}
{adapter}:{account_scope}:issue:{issue_key}:{incident_id}:{transition_seq}:{transition}:{severity}
{adapter}:{account_scope}:issue:{issue_key}:{incident_id}:reminder:{reminder_seq}
{adapter}:{account_scope}:control:{control_event_id}
{adapter}:{account_scope}:pre:{trade_date}
{adapter}:{account_scope}:close:{trade_date}
{adapter}:{account_scope}:weekly:{iso_week}
```

`account_scope_id` 是账户首次登记时生成并永久绑定的随机 UUID；私有配置把真实账号映射到该 UUID，事件键不从账号、Token、Webhook、盐值或其他可轮换秘密派生。`adapter` 使用冻结枚举（首版为 `joinquant` 或 `qmt`）。

`logical_signal_id` 是 `account_scope_id + trade_date + strategy_id + strategy_version + code + side + setup_type` 规范化 JSON 的 SHA-256 前 20 位，同一股票同一交易日同一策略形态只生成一个。当前随 `run_id` 变化的来源 `signal_id` 只保留审计，必须在 Batch B 重构前从通知身份中移除。

`plan_version` 是以下物质字段规范化 JSON 的 SHA-256 前 16 位：股票、方向、目标数量/持仓、按最小价位取整的入场边界与止损、冻结有效期、策略和参数版本。冻结有效期在 logical signal 首次建立时按策略配置和当日收盘时间确定并持久化，后续五分钟扫描不得向后滑动；只有带稳定更新原因的物质变更才能生成新 `plan_version`。名称、展示文字、来源 `signal_id/run_id`、当前发送时间、未跨价位的行情微变、正文截断和旧规则影子分不得参与版本或事件身份。

`transition_seq` 在写入 issue 状态转换和 outbox 的同一 SQLite 事务内按 incident 原子递增，并由 `(issue_key, incident_id, transition_seq)` 唯一约束。`reminder_seq` 按第 9.4 节的交易分钟边界确定，不由进程内计数器生成。

### 9.3 发送规则

- 五分钟正常扫描、无变化 JoinQuant 计划、空计划、正常对账和无变化健康状态静默。
- 盘前摘要每个交易日一次，盘后复盘每个交易日一次，周报每周一次。
- 新买入计划、退出意图、逐笔成交、订单终态、控制变化和异常状态转换按业务事件发送一次。
- 多个事件可以合并展示，但发送成功后只能确认正文实际包含的每个事件，不能用第一项的键把其余项标成已通知。
- 网络、408、429 和 5xx 按原行退避重试；永久 4xx 或第五次失败进入 `dead`。
- 买点复盘资格以 outbox 的 `sent_at` 为事实，不依赖同步 `send_markdown()` 返回值；重试后成功仍进入观察池。
- 微信正文同时显示业务事件时间和本次实际发送时间。重试时只更新发送时间，不改变事件键或规范化事件载荷。
- L0 模型预测不产生五分钟盘中微信；模型状态和样本外结果只进入盘后模型摘要及周报。

时效事件使用 `Asia/Shanghai` 和项目交易日历冻结 TTL：买入计划在 `min(plan.frozen_valid_until, 当日15:00)` 到期，盘前摘要在当日 09:30 到期，盘后摘要在下一交易日 09:15 到期，周报在下一交易日 09:15 到期。成交、退出、订单终态、控制和 issue 转换没有业务 TTL，按重试上限进入 `sent/dead`。新计划会在同一事务对同一 `trade_date + logical_signal_id` 的旧 pending/leased 版本设置取消请求。

工作器领取后、实际 HTTP 请求前必须重读 `expires_at`、取消请求和业务对象当前状态；已过期或已被替代的行转 `cancelled`。issue 恢复会取消该 incident 尚未发送的旧转换和 reminder，并用一条恢复消息概括 incident。已经进入 HTTP 调用的请求无法撤回，该残余竞态必须记录为 ambiguous delivery。

### 9.4 CRITICAL 复报

- `OPENED / CHANGED / ESCALATED / RECOVERED` 立即发送。
- 普通 ERROR 只在进入、变化、升级和恢复时发送，不周期复报。
- 仅持续未恢复的 CRITICAL 在累计第 180、360、540... 个 A 股交易分钟生成 reminder。进程在线时逐边界生成；停机或长阻塞跨过多个边界后恢复时只补当前最高 `reminder_seq`，并取消同 incident 更低序号的未发送 reminder，避免恢复瞬间连续推送。既有 `sent/dead` 历史保持不变。
- 午休、盘后、周末和配置节假日不计时，reminder 只能在交易时段发送。
- 恢复时取消未发送 reminder，并立即发送恢复事件。
- reminder 序号等于 `floor(critical_trading_minutes / 180)`；一次提醒失败只重试原行，不能每五分钟创建新 reminder。发送成功、重试失败或进入 `dead` 都不重置交易分钟，下一条仍在下一个 180 分钟边界生成。
- 问题从已恢复状态再次出现时创建新的 `incident_id`。升级、降级或内容变化都作为立即发送的转换并递增原 incident 的 `transition_seq`；从 CRITICAL 降为 ERROR 时暂停交易分钟，从 ERROR 再升级 CRITICAL 时在同一 incident 继续累计。恢复时终止累计并取消全部尚未发送的 incident 行。
- `critical_trading_minutes`、当前严重度、最近计入的交易分钟和下一个 `reminder_seq` 必须持久化，不能在进程重启、午休或隔夜后归零。

### 9.5 旧文件迁移

现有 `wecom_notify_state.json` 和 `notify_failed_queue.jsonl` 在一个兼容窗口内只读核对：

- `wecom_notify_state.json` 只用于识别可确定已经成功的旧键，不从中创建待发送事件；
- `notify_failed_queue.jsonl` 只有在能生成确定业务键、能证明尚未成功且正文仍有效时才导入原业务事件；其余项只生成不可发送的 legacy 审计摘要；
- 不重放已成功、无法证明未发送或已经失效的历史正文；
- `errcode=40058` 和已达上限项只进入 `dead` 审计摘要；
- 新事件只写 SQLite outbox；
- 兼容窗口结束后旧文件不再参与发送和去重，但不自动删除。

## 10. 规则型影子评分退役

退役范围：

- 移除日常扫描对 `apply_shadow_scores` 的活动调用；
- 不再在买点、扫描汇总、JoinQuant 计划和网页中展示 `enhanced_score`、`shadow_rank`、`shadow_reason`；
- 停止规则影子周五对照微信；
- 原策略 `final_score`、实际执行和历史样本保持不变；
- 历史字段和报告解析保持只读兼容，不迁移、不回填、不重放旧消息。

当运行调用和测试依赖全部移除后，`shadow_score.py` 可以从活动代码删除。ML 报表改为“原规则策略 vs 训练模型”，不再要求三方比较。训练模型 L0 在用户可见文案中称为“模型观察”，避免与退役的规则影子混淆。

退役规则影子不表示训练模型已经存在。完成退役后，ML 状态仍按 Task 1-12 的真实证据单独报告。

## 11. ML-7 五头表格模型

### 11.1 数据与标签

Task 4-6 实现：

- strict 五分钟候选和下一合理成交窗口价格导入；
- 精确 `available_at <= decision_at`，禁止用当前缓存回填历史；
- 保存毛收益、滑点、佣金、税费、其他费用和标准化扣费收益；
- D+3、D+5、D+10 标签按交易日成熟；
- `downside_loss` 定义为入场后至 D+10 收盘的扣费最大不利盯市损失，数值越大越坏；
- fill 标签表示下一合理窗口是否可按计划价和市场规则成交；
- 同一股票日多批样本使用倒数加权，不能把五分钟重复行当成独立交易日；
- 至少三段扩展式 walk-forward、D+10 对应的 10 个交易日隔离带和最后 40 个交易日一次性封存集。

标准化扣费收益使用冻结的参考名义金额和费用版本，只用于跨样本比较；标签同时保留毛收益与各项成本，不能把某个账户余额写进市场标签。运行时再按实际 `target_qty`、真实最低佣金和当前账户重算人民币净收益与风险。

`downside_loss` 使用与收益头相同的下一合理成交窗口基准价 `entry_ref`。对入场后至 D+10 收盘之间每个严格可用的复权五分钟最低价 `low_t` 计算：

```text
net_mark_return_t = low_t / entry_ref - 1
                    - reference_buy_cost_rate
                    - reference_sell_cost_rate(low_t)
downside_loss = max(0, -min(net_mark_return_t))
```

停牌区间沿用停牌前最后一个官方收盘价作为盯市值并记录 `paused_path=1`；跌停价仍计入盯市路径，同时记录 `exit_blocked=1`，不得假装可以成交退出。除权除息无法可靠复权、入场后没有任何有效官方价格或 D+10 到期仍无法形成可审计路径时，标签保持空值并计入数据质量失败。`quantile=0.8` 是下行模型的训练损失参数，不是标签分位或截断规则。

收益与下行头只在标签完整且符合成交条件的样本上训练；fill 头使用全部合格候选。规则影子字段、股票代码、名称和未来执行结果不得作为首版特征。

### 11.2 模型

五个头分别为：

1. D+3 扣费收益回归；
2. D+5 扣费收益主回归；
3. D+10 扣费收益回归；
4. 下行损失分位数回归；
5. 下一合理窗口成交概率分类。

透明基线：Ridge 和 LogisticRegression。第一代 challenger：`HistGradientBoostingRegressor/Classifier`；下行损失头固定使用 `loss="quantile"` 和 `quantile=0.8`。首版不增加 LightGBM、CatBoost、神经网络或 GPU 依赖。

诊断训练门：

- 线性基线至少 5,000 个合格股票日和 120 个交易日；
- 梯度提升 challenger 至少 15,000 个合格股票日和 180 个交易日；
- 门槛按股票日和时间跨度计算，不按五分钟行数计算。

达到诊断门只允许生成数据质量、训练稳定性和离线指标报告，不产生可批准模型。进入 L0 的硬门仍是 ML-7 已确认的一年 strict 数据：数据首尾至少跨 365 个自然日且包含至少 240 个有效交易日。`fill_label` 在全部已成熟 strict 候选中的覆盖率至少 99%；D+3/D+5/D+10 和 `downside_loss` 在已成交且对应期限成熟的有效样本中分别至少覆盖 90%。质量失败样本必须保留分母和稳定原因，不能先删除再计算覆盖率。

市场状态使用决策时已存在的版本化 `market_regime`，只允许 `NORMAL / CAUTION / RISK_OFF`。一年数据中每种状态至少覆盖 10 个交易日和 500 个合格股票日；未达到时仍可训练诊断模型，但不得批准进入 L0。

候选模型进入 L0 前还必须同时通过样本外性能门：

- D+5 排序相关性在三段 walk-forward 中至少两段为正，整体为正；
- 候选池前 20% 的扣费 D+5 收益优于完整候选池和对应线性基线；
- 一次性封存集保持正向排序能力；
- 预测下行风险分组的真实 MAE/止损率方向单调恶化；
- 成交概率分组的真实成交率方向一致；
- 反事实过滤/仓位组合的扣费收益改善，最大回撤不劣于规则基线。
- D+3 和 D+10 的整体排序相关性均不得为负，且每个头最多一段 walk-forward 为负；首版不使用这两个头直接否决单只股票。
- 三个收益头的 MAE 不劣于对应 Ridge 基线；下行头的 pinball loss 不劣于训练折常数 0.8 分位数基线。
- 每个回归头的冻结 80% 残差区间在各 walk-forward 和封存集中的经验覆盖率为 75%-85%。
- fill 头的 Brier score 不劣于 Logistic 基线，按预测概率十分位计算的最大校准偏差不超过 0.10。

任一性能门、泄漏检查、标签覆盖或版本一致性失败时，本轮只登记为 rejected challenger，不得批准、激活或进入 L0。通过这些门只证明模型具备 L0 观察资格，不等于 `validated`。

### 11.3 输出与账户经济层

模型输出：

- 三期扣费收益预测；
- 下行损失；
- 成交概率；
- 置信度、覆盖/漂移/分歧状态和模型版本；
- 确定性 `ml_score`，用于离线排序和 L0 对照。
- 确定性 `ml_filter`、`ml_position_multiplier` 和每项原因码。

首版 `ml_score` 使用模型包内冻结的样本外参考分布，不使用当前候选批次排名。对冻结数组 `S` 和有限预测 `x`：

```text
frozen_midrank_pct(x, S) = 100 * (count(S < x) + 0.5 * count(S == x)) / len(S)
return_component = clip(frozen_midrank_pct(predicted_D5, D5_oof_predictions), 0, 100)
risk_component = 100 - clip(frozen_midrank_pct(predicted_downside,
                                                downside_oof_predictions), 0, 100)
fill_component = 100 * clip(calibrated_fill_probability, 0, 1)
ml_score = 0.60 * return_component + 0.30 * risk_component + 0.10 * fill_component
```

并列值使用中秩；超出训练范围自然落到 0 或 100；空参考分布、缺失或非有限预测使该候选中性化。即使当前批次只有一只候选，结果也只由冻结参考分布决定。D+3 和 D+10 只用于短中期一致性、模型包验收和报告，不直接否决单只股票。权重和参考分布哈希属于模型输出契约，修改时必须形成新模型策略版本。

首版 `confidence` 冻结为以下三个分量的最小值，并裁剪到 `[0, 1]`：

```text
coverage_component = 已提供的必需特征数 / 必需特征总数
disagreement_component = max(0, 1 - abs(challenger_D5 - ridge_D5)
                                / max(训练集样本外差值P90, 1e-6))
drift_component = max(0, 1 - max_feature_PSI / 0.25)
confidence = min(coverage_component, disagreement_component, drift_component)
```

漂移使用最近 20 个有效候选批次且至少 200 行，与模型清单中的最终训练分布比较。数值特征使用训练分布十分位边界，重复边界合并，常数特征在训练时移出必需特征；类别特征使用训练编码器的冻结类别并增加 `OTHER` 和 `MISSING` 桶，数值缺失也使用独立 `MISSING` 桶。训练和当前窗口的每个桶先增加 0.5 伪计数再归一化，随后计算 `PSI = sum((current_i - train_i) * ln(current_i / train_i))`；`max_feature_PSI` 是所有必需特征的最大值。分箱、类别、伪计数和训练频率全部写入模型清单并参与哈希。

样本不足时 `drift_status=insufficient`。`confidence < 0.60`、必需特征覆盖率低于 95%、漂移样本不足、任一输出非有限、schema/hash/依赖不一致时，保留原始预测供审计，但 `ml_filter=0`、`ml_position_multiplier=1.0`，禁止任何 L1-L3 影响。

每个模型包保存各头样本外绝对残差的 80 分位数。首版策略契约冻结为：

- L0：只写预测和反事实结果，候选顺序、过滤、数量、信号 JSON 逐字段不变；
- L1：仅在同一批规则合格候选内按 `ml_score` 降序排列，同分保持原规则顺序；不新增或删除候选，不改变数量；
- L2：仅当置信门通过，且 D+5 预测上界 `prediction + residual_q80 <= 0`、下行预测下界 `max(0, prediction - residual_q80)` 不低于训练样本外预测的 80 分位数，或校准后的 fill 概率上界 `min(1, probability + residual_q80)` 低于 0.60 时，`ml_filter=1`；它只能删除买入；
- L3：先执行 L2。未被过滤的候选按 `ml_score < 40 / [40, 60) / [60, 80) / >= 80` 映射为 `0.8 / 0.9 / 1.0 / 1.1`，再由规则仓位和全部硬风控裁剪。

任何阈值、分位数、区间或映射变化都必须形成新的模型策略版本、重新走历史门和人工批准，周训练不得自动改写。

账户经济层在模型之后计算：

- `proposed_lots`、`order_notional` 和往返成本；
- `conditional_net_pnl_yuan`、`unconditional_expected_net_pnl_yuan`、`conservative_edge_yuan` 和 `filled_downside_yuan`；
- `cost_to_expected_edge_ratio`；
- T+1 隔夜风险和跳空风险；
- `economic_trade_allowed` 与原因。

收益头预测的是“入场已经成交”条件下、按参考名义金额扣费后的收益。运行时先用冻结参考成本还原预测毛收益，再按实际 `target_qty`、最低佣金、税费和滑点重算：

```text
conditional_net_pnl_yuan = order_notional * predicted_gross_return
                           - actual_round_trip_cost_yuan
unconditional_expected_net_pnl_yuan = fill_probability * conditional_net_pnl_yuan
                                      - (1 - fill_probability) * no_fill_cost_yuan
conservative_edge_yuan = confidence * max(unconditional_expected_net_pnl_yuan, 0)
                        + min(unconditional_expected_net_pnl_yuan, 0)
conservative_price_downside_rate = max(0, predicted_downside_loss
                                          + downside_residual_q80
                                          - reference_round_trip_cost_rate)
filled_downside_yuan = order_notional * conservative_price_downside_rate
                       + actual_round_trip_cost_yuan
```

首版 A 股 `no_fill_cost_yuan=0`，但保留券商费用字段。硬风险预算使用 `max(规则止损人民币损失, filled_downside_yuan)`，不得乘成交概率或置信度；模型只能让数量更小。模型关闭、L0 和 L1 时，上述模型金额只用于报告，`economic_trade_allowed` 仍由规则经济层决定。市场模型不读取账户余额；账户经济层每次减量或否决后重新执行全部硬风控。

### 11.4 治理与权限

- 模型包不可变，记录训练代码、特征、标签、数据范围、切分、参数、依赖版本和 SHA-256。运行时只加载本项目本机生成、已登记且哈希获批的模型包，不接受外部上传的 pickle/joblib。
- 训练成功不等于批准；批准不等于激活；激活不等于部署。
- 只有已登记、哈希一致且具备人工批准事件的模型才能被激活；CAS 不能绕过批准状态直接提升权限。
- L0 只记录预测，失败自动回退纯规则，不改变订单。
- L1 至少 20 个有效交易日后才可经人工批准参与排序。
- L2 至少 40 个有效交易日后才可经人工批准只减买入或否决，不增加仓位。
- L3 至少 60 个有效交易日和 30 个闭合持仓周期后，才可经人工批准在 `0.8-1.1` 内微调规则仓位。
- L1/L2/L3 的任何推进、降级和回滚都需要明确人工动作和单独部署授权。
- 模型永久不控制卖出、硬止损和绝对风险上限。

权限验证是合取条件，不是只等待天数。“有效交易日”必须同时满足交易日健康有效、五分钟候选采集完整、同一模型/策略版本持续生效且没有未恢复的数据缺口。每层统计窗口截至所需最长标签已经成熟的最后一个决策日，不使用尚未成熟样本。

运行质量固定计算为：`prediction_availability = 有限且哈希/schema正确的预测数 / 应预测候选数`，`runtime_fault_rate = 发生加载/超时/非有限输出的候选批次数 / 应推理批次数`。晋级要求 availability 至少 99%、fault rate 不超过 1%、hash/schema/权限越权为 0、窗口最大 PSI 不超过 0.10，并且对应层的行为不变量 100% 成立。

效果比较按交易日做 2,000 次 block bootstrap，固定随机种子 7，使用单侧 90% 置信区间。L1 比较实际槽位数下“ML 排序前 N”与“原规则排序前 N”；L2 比较“过滤后未投资现金收益为 0”与原规则组合；L3 比较应用仓位系数与同一批 L2 基准。所有收益均扣除冻结费用和滑点，改善下限固定为平均收益 0.10 个百分点，且置信区间下界不低于 0；样本不足以形成该区间时不得晋级。

| 层级 | 最低运行证据 | 必须同时满足 |
| --- | --- | --- |
| L0 | 一年 strict 数据、全部历史性能门、部署后至少 5 个有效交易日和 500 个真实 L0 预测 | 模型/代码/特征/依赖哈希一致；运行质量门通过；候选顺序、过滤、数量、买卖和信号 JSON 逐字段等价率 100%；人工批准并激活。 |
| L1 | 至少 20 个有效交易日、200 个成熟 D+5 预测 | D+5 标签覆盖和历史性能门仍通过；L1 候选集合/数量不变率 100%；排序效果达到上述 0.10 个百分点和置信区间门；运行质量门通过；人工批准。 |
| L2 | 至少 40 个有效交易日、300 个成熟 D+10 预测、30 个成熟反事实过滤样本 | D+10 标签覆盖达标；新增候选数和放大数量次数均为 0；过滤组合达到效果/置信区间门且最大回撤不劣于规则基线；运行质量门通过；人工批准。 |
| L3 | 至少 60 个有效交易日、30 个闭合持仓周期 | 所有系数位于 `0.8-1.1`、所有原硬拒绝仍拒绝且卖出逐字段不变；仓位组合达到效果/置信区间门且最大回撤不劣于 L2 基准；运行质量门通过；人工批准。 |

任何自动降级、模型/策略版本变化、标签覆盖跌破门槛或性能门失败都会中止当前验证窗口；恢复后必须重新人工批准，不能沿用已经失效的等待天数。

## 12. 持久化与增长边界

### 12.1 正式交易库

`cache/trading/trading.db` 保存：

- 统一 pre-trade 结果和容量预留；
- 规范化订单意图、租约和平台状态；
- QMT/JoinQuant 回传的既有订单、成交、账户与对账事实；
- 通知 outbox；
- 通知 enqueue gap、终态 tombstone 和容量控制所有权；
- 节点会话和心跳的低频当前状态。

不得复制 XtQuant 原始全量历史到新数据库。原始平台字段只在订单、成交和异常证据中有界保留。

数量目标：订单与容量预留沿用每年低于 5 万行；节点心跳按当前状态覆盖，不逐秒永久追加；outbox 正常每天为个位数至十余条。新增正式库和索引仍受年度低于 200 MB、超过 300 MB/年或单日 2 MB 告警的既有门槛约束。outbox、订单和容量表纳入交易库现有的每日 7 份、每周 4 份、每月 12 份在线备份与恢复演练。

### 12.2 通知保留

- 高优先级事件固定为成交、退出意图、订单终态、控制事件和 ERROR/CRITICAL issue；盘前/盘后/周报和买入计划为普通优先级；
- 普通优先级 `pending + leased` 软上限为 1,000 行/4 MiB；高优先级专用预留为 5,000 行/20 MiB；`dead` 明细上限为 1,000 行/4 MiB；
- `sent` 完整元数据热保留 366 天，正文 30 天后删除；`dead/cancelled` 明细保留 30 天；随后终态行原位压缩为只含 `event_key`、规范化载荷哈希、终态和最终时间的 tombstone；
- tombstone 与来源账本同寿命，不计入 active/dead 行数上限；新入队必须同时检查 active 行和 tombstone 的唯一键，防止旧 fill/control 重放；
- 不得静默丢弃高优先级事件；
- 清理顺序固定为：取消已过期/被替代普通事件、删除 30 天以上正文、压缩到期终态明细；不得清除 active 高优先级行；
- 高优先级 active 使用达到专用预留的 80%、出现高优先级 enqueue gap 或 outbox 无法写入时，`NOTIFICATION_CAPACITY` 控制所有者通过 CAS 禁止新买入并写 `control_events`/本地健康证据；合法卖出继续执行，不能因为通知故障停掉风险退出；
- 到达 5,000 行/20 MiB 硬上限后，账本业务事实仍优先提交；无法创建的 outbox 行必须在同一事务写 `notification_enqueue_gap(event_key, payload_sha256, source_fact_id)`，禁止伪装成已通知；
- 只有高优先级 active 低于 20%、高优先级 `dead/enqueue_gap` 为 0、数据库健康可写且连续两个相隔至少 5 分钟的工作器周期一致时，`NOTIFICATION_CAPACITY` 才能通过 CAS 恢复自己关闭的买入。它不得解除人工、账户风控、对账、`kill_switch` 或其他所有者的限制；
- 正文继续受 4,000 字节上限，按事件边界分片；
- webhook、token、账户号和环境变量不得入库。

### 12.3 ML 和历史库

- `cache/ml/ml.db` 保存候选、标签、预测、模型登记和权限事件；
- `cache/backtest/history.db` 保存 strict 历史数据和五分钟 cohort，不复制到 ML 库；
- 模型包位于 `cache/ml/models/`，文件哈希与数据库登记双向校验；
- 继续执行现有 ML 每年 1 GB 目标、2 GB 停止新增明细和历史库 3 GB 导入拒绝门；
- 三个数据库分别在线备份和完整性检查，不进行跨库事务伪装。

### 12.4 Windows 节点

- 仅保留当前配置、最近同步游标和有界运行日志；
- 保留有界的本地执行日志，记录当前及最近 10 个交易日的 `client_order_id`、规范化意图哈希、最终快照哈希、提交许可 ID、`permit_received/place_called/result_received` 边界、券商订单 ID 和终态，用于进程重启后的幂等查询；它不是第二套订单事实源；
- 日志按日/大小轮转，热保留 30 天，异常摘要可保留 180 天；
- 不保存 Linux 策略历史、模型包或永久订单副本；
- QMT 私有配置和凭据不进入日志、Git 或 Linux 报告。

## 13. 故障处理

| 故障 | 行为 |
| --- | --- |
| ML 导入、训练或推理失败 | 关闭该批 ML 数据或模型影响，规则交易继续；不得触发 `kill_switch`。 |
| 费用或证券规则缺失 | 禁止新买入；合法卖出按平台可卖规则处理并告警。 |
| pre-trade 或容量预留失败 | 不创建 READY 订单，不发送平台。 |
| Linux 账本不可写 | 禁止新买入；已提交订单只通过平台快照恢复，未提交意图不由节点执行。 |
| 已启用且持有账户执行租约的 Windows QMT 在交易时段离线 | 不转移给第二节点，不重放过期命令；进入 CRITICAL 并要求恢复后全量对账。未启用节点为 `not applicable`。 |
| 提交返回未知 | `SUBMIT_UNKNOWN`，先查询，禁止盲目重发。 |
| 重复/乱序订单或成交 | 按稳定 ID 幂等，状态只前进；内容冲突升级 CRITICAL。 |
| 企业微信不可用 | outbox 原行退避重试，不改变交易状态。 |
| outbox 达容量上限 | 按第 12.2 节清理和分级；高优先级达到 80% 水位即由专属所有者禁止新买入，硬上限后的 enqueue gap 必须入账，合法卖出继续执行。 |
| JoinQuant 或 QMT 快照陈旧 | 交易时段禁止新买入；盘外按 not applicable，不污染有效观察日。 |

## 14. 测试设计

### 14.1 小资金与强制风控

- `observe/enforce` 模式和 QMT 非 enforce 拒绝启动；
- 不同资金、费用、证券单位、价格和止损距离矩阵；
- 普通买入精确 `target_qty`；
- 百分比与人民币风险同时约束；
- 单笔风险和组合开放风险分别拒绝；
- 100/300/500 股 `+2R` 离散语义；
- 两个并发买单的 SQLite 容量预留；
- 当前旧 P0 的风险拒绝、唯一执行计划、退出续执行、5只/80%和分类暴露不变量。

### 14.2 通知

- 双进程同键入队、同键异正文冲突和并发领取；
- `account_scope_id`、`plan_version` 和转换序号在重启、Token/URL 轮换、`run_id` 变化及非物质行情微变下保持稳定；
- 当前来源 `signal_id` 随 `run_id` 改变时，`logical_signal_id` 和冻结有效期仍保持稳定；物质计划变化才产生新版本并取消旧 pending/leased 行；
- 租约工作器崩溃与到期恢复；
- 临时失败、永久 4xx、第五次失败和 dead 保留；
- 多个对账转换逐项确认，正文截断不误确认；
- ERROR 不周期复报；午休、隔夜、周末和节假日下 180 交易分钟 CRITICAL；
- 180/360 分钟边界、CRITICAL 降级暂停/再升级继续、reminder dead 后下一周期和恢复取消；
- 买入计划、盘前/盘后/周报 TTL，替代版本取消和发送前二次时效检查；
- 重试成功后买点进入复盘；
- 旧文件不重放；
- 普通/高优先级容量水位、终态 tombstone、防历史重放、enqueue gap 和 `NOTIFICATION_CAPACITY` 所有权恢复；
- 所有规则影子微信出口和周报退役。

### 14.3 ML

- 五分钟内未来特征、跨日泄漏和版本混用拒绝；
- 标签成熟、费用分解、D+10、成交条件和 downside 正负号；
- 股票日权重、10 日隔离、三段 walk-forward 和 40 日封存集；
- 模型包哈希、登记、批准、激活、降级和回滚；
- L0 超时/异常回退纯规则且交易输出逐字段不变；
- 账户经济层不扩大规则上限。

### 14.4 QMT 与故障模拟

- 协议签名、nonce、时效、账户作用域和单节点租约；
- 精确数量、价格上限和快照前置条件；
- 部分成交、撤单、拒单、重复回调和乱序回调；
- 提交超时后查询恢复；
- Linux/Windows 重启、网络分区、日切和过期命令；
- 重连后全量对账前不得领取新买单；
- QMT 依赖不存在时节点只读自检可解释失败，Linux 全量测试不依赖 XtQuant。

## 15. 实施批次

本文件是跨子系统的整合设计，不作为一份单体实施计划执行。Batch A-D 必须分别形成可独立测试、复审和回滚的实施计划；Batch E 只做跨批次总验收和主从文档收敛。

### Batch A：真钱前置 P0/P1

实施计划：`docs/superpowers/plans/2026-07-28-small-capital-live-risk-execution.md`

- 统一费用和证券规则契约；
- 精确整数手资金分配与小资金经济层；
- 强制 `pre_trade_check`、真实账户快照和原子容量预留；
- 跳空单笔风险修复和一手/奇数手 `+2R` 修复；
- 旧五项 P0 运行不变量测试。

### Batch B：事件通知与规则影子退役

实施计划：`docs/superpowers/plans/2026-07-28-transactional-notification-outbox.md`

- SQLite outbox、租约工作器和业务事件键；
- 静默规则、CRITICAL 180 交易分钟和买点复盘衔接；
- 兼容旧失败队列但不重放；
- 移除规则影子活动计算、展示和周报。

### Batch C：ML-7 Task 4-10

实施计划：`docs/superpowers/plans/2026-07-28-five-head-ml-training-runtime.md`

- strict 五分钟导入和成本标签；
- 时间切分训练集、五头基线/challenger；
- 不可变模型包、审批/回滚和 L0 推理；
- 模型数据就绪、训练和对照报告。

当前状态：上述代码已在本地实现但未提交、未部署、未观察或验证；Task 11 本地总验收仍在进行。没有真实一年 strict 数据、可信模型、人工审批、活动模型或服务器 L0 证据。

### Batch D：BrokerAdapter 与 QMT 节点

实施计划：`docs/superpowers/plans/2026-07-28-broker-adapter-qmt-node.md`

- 平台无关契约和 Linux 订单租约接口；
- 内存/SQLite 故障模拟适配器；
- 默认禁单的 Windows QMT 节点和可选 XtQuant 绑定；
- 只读账户/持仓/订单/成交同步与重连对账。
- schema 13 必须先把当前仍按股票全局唯一的 `position_cycles/exit_intents` 迁移为账户作用域，并让 READY 到期扫描只处理调用账户；完成前 Task 5 只证明单一 JoinQuant 账户，不能宣称 JoinQuant/QMT 双账户隔离。

### Batch E：总验收与文档同步

- 专项和全量 Windows/Linux 测试；
- schema migration、备份、恢复和容量验证；
- 主文档、实盘执行方案、ML-7、通知和存储文档同步；
- 输出 implemented 范围与仍需外部验证的事实。

Batch E 对应 ML-7 Task 11，当前正在本地执行；ML-7 Task 12 的服务器部署、L0 启用和交易日观察未获授权，不属于本设计自动授权的实施范围。

批次可以在代码层并行准备，但发布顺序固定为 A -> B -> C/D 的只读能力 -> 单独授权部署。QMT 下单启用永远是独立发布任务。

## 16. 状态推进与验收

### 16.1 `implemented`

只有以下条件全部满足才能标记对应批次 `implemented`：

- 代码接入正式入口；默认关闭的 ML L0 和 QMT 节点必须具有可测试入口，但保持关闭不妨碍标记代码 `implemented`；
- migration 幂等；
- 专项、故障和全量测试通过；
- 数据增长、备份、恢复和秘密处理符合规范；
- 主从文档准确区分完成和未完成范围。

### 16.2 `deployed`

- Linux 服务器运行包含目标代码的 SHA；
- schema、备份、恢复、配置哈希和服务状态核验；
- Windows 节点只读部署需记录节点版本，但不得因此声称 QMT 下单已部署；
- JoinQuant 模板只有实际更新并回传版本后才是网站侧 deployed。

### 16.3 `observed`

- JoinQuant：真实模拟盘交易日产生精确数量、费用、订单、成交、通知和对账证据；
- 通知分项记状态：核心 enqueue/claim/sent 需连续 5 个有效交易日完成来源事件对账后才能标记 `observed`；失败重试、TTL 取消和 CRITICAL 180 分钟 reminder 只有各自出现真实事件证据后才能分别标记 `observed`，不能由其他通知样本代替；
- ML：服务器真实五分钟候选和 L0 预测入账，且交易输出不变；
- QMT：只读账户/订单回传可先 observed；真实下单必须另行授权后有券商订单证据。

### 16.4 `validated`

- 旧阶段 1 和账本 10/20 日门槛继续适用；
- 新资金分配和退出语义需完成 strict 回测与代表性模拟盘样本；
- 通知核心需在连续 20 个有效交易日内逐日满足 `来源业务事件数 = sent + pending/leased + dead + cancelled + enqueue_gap`（按唯一 event key 计数），且高优先级 `dead/enqueue_gap=0`、pending 不超过重试 SLA、无并发重复领取或载荷冲突；HTTP 成功响应丢失造成的残余重复必须单独记录，不能据此宣称严格 exactly-once。失败重试、TTL 和 CRITICAL reminder 子能力还需各自具有真实样本，否则保持 `not observed / not validated`；
- ML 必须同时满足第 11.4 节对应层级的时间、成熟样本、标签覆盖、冻结性能、运行健康、哈希绑定和人工批准条件；只达到天数或闭合周期不得标记 `validated`；
- QMT 需完成断线、重启、未知提交、部分成交和全量对账演练后，才可能进入小资金灰度。

## 17. 外部前置与诚实边界

当前代码工作可以完成协议、模型工程、故障模拟和默认禁单节点，但以下事实不能从 Git 或本地测试生成：

- 真实 6 个月 strict 五分钟数据只能支持诊断训练；进入 L0 需要满足第 11.2 节的一年 strict 数据硬门；
- 可信模型及真实 walk-forward/封存指标；
- 券商实际佣金、QMT 权限、账户、客户端路径和可用 XtQuant 版本；
- Windows 长期在线、QMT 登录、网络和券商交易时段稳定性；
- 20/40/60 个有效交易日模型观察；
- 真实模拟盘或小资金订单的 `observed / validated` 证据。

缺少这些条件时，相关能力最高只能标记为 `implemented`，不得用模拟器测试替代外部事实。

## 18. 文档取代关系

本节只定义目标设计的取代关系。Batch B 部署前，服务器仍按旧 JSON 冷却/失败队列和持续 ERROR 提醒运行；`docs/project_roadmap.md` 对当前运行状态的描述仍是真实基线，不得提前改写为新行为。

- `docs/superpowers/specs/2026-07-14-notification-review-idempotency-design.md` 的成交事件和 D+N 复盘历史基线继续有效；其“共享 JSON 冷却/失败队列作为长期通知传输方案”由本设计取代。
- `docs/superpowers/specs/2026-07-26-runtime-evidence-integrity-repair-design.md` 的运行证据、健康口径和当前已部署失败终态继续有效；其中 JSON 失败队列只作为迁移期基线，Batch B 部署后不再是活动通知传输方案。
- `docs/project_roadmap.md` 中“持续 ERROR 每 30 分钟提醒”和规则影子周五摘要是当前已部署事实；Batch B 完成、部署并核验后，主文档才分别更新为“仅未恢复 CRITICAL 每 180 个 A 股交易分钟复报”和“规则影子 retired”，并分别记录 outbox 核心、失败重试、TTL、CRITICAL reminder 的 `implemented/deployed/observed/validated` 状态。
- `docs/superpowers/specs/2026-07-15-trained-shadow-model-design.md` 的 Task 1-3 和模型治理基础继续有效；三方对照改为“原规则策略 vs 训练模型”，规则影子不再作为活动 comparator。
- `docs/superpowers/specs/2026-07-14-execution-contract-p0-fixes-design.md` 的五项已部署 P0 继续有效；本设计新增真钱/QMT 前置 P0，不把旧 P0 改写为未实现。
- `docs/superpowers/specs/2026-07-13-layered-exit-risk-management-design.md` 的硬止损、退出意图、T+1 和卖出优先继续有效；本设计只覆盖一手/奇数手首段止盈的离散语义。
- `docs/live_trading_execution_plan.md` 中的交易适配层方向继续有效；实现后应把首选路线更新为 Linux 主系统加 Windows QMT 节点，并把 vn.py 保留为未来第二适配器。
