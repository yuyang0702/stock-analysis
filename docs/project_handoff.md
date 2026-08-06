# 项目接管与新环境恢复说明

> 2026-08-05 本地功能分支检查点：`feature/live-readiness-integration` 已完成 Batch A Tasks 1–8 与 Batch B Tasks 1–7 的本地实现和 Windows 专项验证；Batch B 新增 schema 12 通知 outbox/gap、来源事务生产者、稳定计划/TTL、租约 worker、有限重试、容量控制、CRITICAL 复报、规则影子退役、systemd 路由、详细 `ledger-check` 和带事件键校验的人工解除 CLI。Tasks 尚未合并、推送或部署；当前 Windows 缺少 `bash.exe`，3 个 Linux 脚本测试仍需 Linux 复验，故整体仍为 `implemented（本地） / not deployed / not observed / not validated`。本检查点没有修改配置、Token、服务器、JoinQuant 或 QMT。最后可引用的外部检查点仍是下段 2026-07-26 记录；服务器实时状态必须重新核验。

> 2026-08-06 Batch C ML-7 检查点：Tasks 4–10 已在当前工作树本地实现（strict 五分钟历史导入、成本标签、训练帧/切分、五头模型包、人工治理、校验推理/L0 旁路和维护报告），状态为 `implemented（本地未提交）`；Task 11 本地总验收、文档真值和安全复审正在进行；Task 12 的服务器部署、L0 启用和交易日观察未获授权且未运行。Batch C 整体严格为 `not committed / not deployed / not observed / not validated`。没有真实一年/365 天 strict 数据证据、可信/可批准训练模型、人工审批、活动模型或服务器 L0 证据；本地/synthetic 测试不等于这些外部事实。

> 2026-07-26 运行证据完整性修复部署检查点：本地、`origin/main` 和服务器均已快进到
`68d7283da758e7afe8107278a33e245005eee585`，三项核心服务 active，实际/期望
JoinQuant 模板均为 `2026-07-18.1-gap-reentry`，服务器
`GAP_REENTRY_ENABLE=1`。2026-07-12 至 2026-07-26 有 306 次成功扫描和 150 次失败，
其中 146 次为 7 月 23 日已修复的 Pandas 持仓 Series 问题；修复后仅有 7 月 24 日
一个完整交易日，尚不足以 validated。正式订单 7 笔均 filled，7 月 20 至 24 日对账
均 matched；但扫描失败未写入 `strategy_runs`、信号结构化列为空、费用和已实现盈亏
缺少可信度标识、盘外陈旧污染健康评分、6 条 `errcode=40058` 通知被反复重试，且
`/login` 存在非 ASCII 错误令牌异常。修复专项设计为
`docs/superpowers/specs/2026-07-26-runtime-evidence-integrity-repair-design.md`。代码已实现 schema 10、扫描终态、信号结构化列、来源可信度、盘内/盘外健康、通知 dead 状态
和 Unicode 登录修复。部署前 schema 9 在线备份完整性通过；Linux全量457/457、目标模块编译、
schema 10 `ledger-check`、迁移后在线备份及隔离恢复演练均通过，配置哈希未变化，重启后 ERROR
日志计数为0。当前严格为
`implemented（已推送） / deployed（服务器） / not observed / not validated`；没有
修改交易策略、交易控制或任何 Token。

> 2026-07-23 盘中扫描健康事故已修复、推送、部署并完成首轮真实观察：当当前持仓股票进入候选池时，
`build_risk_bundle` 把整行 `pd.Series` 传给风险引擎，后者的 `if holding` 触发 Pandas
布尔歧义并终止整轮扫描，导致信号文件停止刷新。共享判断已改为
`holding is not None`，不改变任何交易或风控规则。回归测试完成 RED/GREEN，风险引擎
专项 7/7、Windows 可运行测试 436 项和服务器 Linux 全量 441/441 通过。提交
`2cb90485290e75883379dada2b934637d87ffa37` 已进入 `origin/main` 与服务器；部署前备份
完整性、schema 9 健康/可写、环境哈希不变、三服务 active 且无 warning 均已核验。
13:05:58 首轮真实扫描成功处理南山铝业“持仓风控”并刷新扫描文件和 `signals.json`，
随后增量对账 matched、0 差异，`buy_enabled=1 / kill_switch=0`。当前为
`implemented（已推送） / deployed（服务器） / observed / not validated`；连续稳定性
仍需后续交易时段观察。

> 2026-07-16 统一有效止损与交易运行面板增量已随 `8db92bf6448466827a50560ae2fb8c7fde142c72` 推送并部署：schema 8 增加可空 `position_cycles.manual_stop_price`；成交成本保护校验后的 frozen initial、明确人工止损、首段止盈后移动止损共同解析唯一 effective stop；网页和策略不再各算一套。网页已移除 OCR/截图和直接清仓入口，增加认证、CSRF、运行/异常/风险/轨迹视图；JoinQuant 模板改用 bearer token 且 token 值未改变。部署前 schema 7 备份完整性为 `ok`，Linux全量414/414、编译、schema 8健康/可写、环境哈希不变、三个服务active、同步2个持仓、网页302登录保护、API未认证403及重启后ERROR日志为空均已核验。用户确认网站模板已手工更新为 `2026-07-16.1-unified-effective-stop`；新模板尚无交易日快照回传。当前严格为 `implemented（已推送） / deployed（服务器；网站由用户确认） / not observed / not validated`。

> 2026-07-19 网页可观测性增强已随 `5ad0ad539ef66aa7cf1073ad7142fde116d74ea5` 推送并部署服务器：今日运行区分别显示扫描、信号、快照
和对账年龄，并严格区分预期与实际回传模板；待执行区解释部分成交、未完成退出和异常
影响；持仓区增加 R 风险、入场来源及最多30条交易链路；研究区只读显示严格阶段。
不新增 schema、持久化、直接交易、自动解锁或模型/参数入口。网页专项7/7通过，Windows
服务器虚拟环境全量440/440、编译、schema 9健康/可写、环境哈希、三个服务和重启后
warning 及以上日志均已核验。当前为
`implemented（已推送） / deployed（服务器） / not observed / not validated`。

> 2026-07-19 “跳空越价后二次确认入场”已实现、完成复查、推送并部署服务器：旧计划只作
锚点，当前候选重新验证；封板不排队，开板两次独立扫描确认，回封重置，上限为原入场
加 `0.5R`；不足100股只在完整风险预算允许时提升为一手。schema 9 新增每股/每日/机会
一行的 `gap_reentry_opportunities`，备份和健康报告已覆盖；JoinQuant 模板版本为
`2026-07-18.1-gap-reentry`，100股例外按精确数量下单并按当前价加费用缓冲复核现金，
部分成交立即撤余单，交易控制拦截不会把机会误标为已发布。部署前 schema 8 在线备份
完整性为 `ok`；服务器 Linux 全量440/440、编译、schema 9健康/可写、环境哈希不变、
三个服务active且重启后无 warning 及以上日志。当前严格为
`implemented（已推送） / deployed（服务器与网站模板，功能已开启） / not observed / not validated`。
2026-07-26 只读核验确认 `GAP_REENTRY_ENABLE=1`，实际/期望模板均为
`2026-07-18.1-gap-reentry`；机会账本仍为空，不能把启用状态写成已观察或已验证。

> 2026-07-15 最新部署增量：成交全量对账已改为仅比较快照交易日的 SQLite 成交与 JoinQuant 当日 `get_trades()`；跨日历史成交不再产生假 `FILL_MISSING_PLATFORM`，同日缺失与平台侧未落账的严重度保持不变。代码提交 `cd83f26` 已推送并部署；SQLite 备份完整性、Linux全量326/326、Python编译、schema 7健康/可写、配置未变、三个服务active及重启后ERROR日志为空均已核验。当前为 `implemented（已推送） / deployed（服务器） / not observed / not validated`。

> 2026-07-15 当前部署检查点：schema 7 执行链增量已随实现提交 `e2ce5b50590edc28cb748bee1fa985f43c9a0366` 进入 `origin/main` 并部署到服务器 `/opt/stock-analysis`。部署前 schema 6 备份完整性为 `ok`；Python 编译、Linux 隔离测试账本全量测试 324/324、正式账本 `ledger-check` 和 schema 7 迁移通过；`stock-analysis.env` 哈希未变化，三个核心服务 active，重启后五分钟 ERROR 日志为0，服务器工作树干净并与 `origin/main` 一致。状态为 `implemented（已推送） / deployed（服务器） / not observed / not validated`。

> 用户报告已在 JoinQuant 网站手动更新模板 `2026-07-15.1-execution-state-recovery`；更新后尚无新快照证据，故网站侧记录为 `deployed（用户确认） / not observed / not validated`。服务器 `buy_enabled=0`、`kill_switch=0`；盘后立即恢复因 `ACCOUNT_SNAPSHOT_STALE` 被拒绝。一次性 timer 已排程在 2026-07-16 09:32、09:35 完整对账并于09:37在全部安全门满足时 CAS 恢复买入，成功前不得写成已经解除。

> 2026-07-14 的 `aabe1e6` / `52b3653` / schema 6 / 模板 `2026-07-14.2-p0-execution-contract` 是上一部署基线，已被上述 `e2ce5b5` / schema 7 检查点取代；其中分层退出、完整账本、备份恢复、历史回测框架、通知复盘和五项 P0 仍包含在当前版本中。真实交易日行为仍为 `not observed / not validated`，真实 6 个月/1 年 strict 数据也尚未导入和重复运行。

> 2026-07-14 `7c31684` 通知复盘增量已包含在当时服务器检查点 `52b3653` 中：SQLite 新 fill/legacy 累计成交增量驱动执行回报、统一企业微信服务器时间，以及 D+0/D+1/D+3/D+5/D+10 全量买点复盘。该历史增量为 `implemented（已推送） / deployed / not observed / not validated`。

> 2026-07-14 五项执行正确性 P0 已随 `52b3653` 进入 `origin/main` 并部署到服务器 `/opt/stock-analysis`，包括强制风险准入、版本化唯一买入计划、退出意图续执行及优先级保护、JoinQuant 5只/80%双层边界与买卖开关、已有持仓及未完成买单分类暴露。服务器专项测试 123/123、Python 编译和 `ledger-check` 通过，环境文件校验未变，三个核心服务 active；JoinQuant “AI” 策略已持久化模板版本 `2026-07-14.2-p0-execution-contract` 并保留原运行配置。当前严格为 `implemented（已推送） / deployed（服务器与 JoinQuant 模板） / not observed / not validated`。

> 上一服务器外部检查点（用户提供）：2026-07-14 20:06，服务器 HEAD 为 `131118213f22bbdaecd5cd8ab89a87db9aaf7f85`，分支与 `origin/main` 一致且干净；SQLite schema 6 完整性/可写检查通过，环境文件哈希未变，三个核心服务均 active。该历史检查点已被本次 `52b3653` 服务器与 JoinQuant 部署证据取代，但仍可用于追溯部署前基线。

> 2026-07-16 ML-7 基础增量（历史）：实施计划 Task 1–3 已实现并推送，包括共享候选评分/严格样本契约、独立有界 `cache/ml/ml.db` schema v1、以及完整五分钟实时候选采集与发布来源审计；Windows 可运行回归 402 项和 Linux 静态测试 2 项通过，最终独立审查无 Critical/Important/Minor。该历史状态随后由 2026-08-06 本地检查点取代；服务器尚无 ML-7 部署或运行证据，`ML_TRAINED_SHADOW_ENABLE` 默认关闭；尚未形成可信/可批准或活动模型。Task 12 以及外部观察仍需单独授权。

> 本文件用于新电脑、新Codex对话和跨环境交接，是一个可提交到Git的时间点快照。`docs/project_roadmap.md`仍是唯一主文档；如两者冲突，以主文档为准。服务器、JoinQuant和运行数据状态必须重新验证，不能仅凭本文件认定为当前事实。

## 1. 接管目标

新环境应尽可能恢复：

- 代码、文档和Git历史。
- 主从文档关系和当前实施阶段。
- 已实现、已部署、待观察和待实现能力的边界。
- 策略、JoinQuant、SQLite和Codex审核员的职责边界。
- 数据存储和文件增长开发约束。
- 提交、推送、部署和服务器访问的权限边界。

Git不能恢复：

- 历史聊天记忆。
- 服务器实时状态。
- `stock-analysis.env`及其他私密配置。
- `cache/`、`output/`和SQLite运行数据。
- Python虚拟环境。
- SSH密钥。
- Codex本机计划任务、授权和应用设置。

## 2. 当前可验证代码基线

本次审核可由本地 Git 确认：提交 `8e35d03`、`9f4c12d` 和通知复盘实现 `7c31684` 都已包含在 `origin/main` 历史中。`origin/main` 已包含：

- JoinQuant模拟盘信号、执行回报和持仓同步链路。
- 每5分钟健康检查、微信异常报警和失败通知重试。
- SQLite Batch 1策略运行、信号和观察型风控账本。
- JSON/SQLite信号一致性检查和readiness报告。
- 信号样本、历史规则影子兼容字段、策略对照和信号级回测；当前本地 Batch B 已退役规则影子活动计算，训练模型尚不存在。
- ML-7 Tasks 1–3 是既有已推送基础；Tasks 4–10（strict 导入、标签、训练帧、五头模型、治理、L0 旁路和维护）已在本地实现且未提交；默认关闭且尚未部署、观察或验证。
- 分层退出、组合风险和可交易性保护，以及自动备份恢复基础。
- Codex只读观察与阶段评估方案。
- 数据存储、文件增长与保留规范。
- 根目录 `AGENTS.md`仓库开发约束。

`9f4c12d` 在此基础上增加 schema 6 完整执行账本、自动对账与人工解锁，以及独立逐日历史回测框架；该提交已进入 `origin/main` 并有服务器部署历史。`52b3653` 是 2026-07-14 检查点，不是当前版本断言；Git 本身只能证明代码已推送，`deployed` 结论来自独立部署证据。

接管时不要把本文记录的SHA永久写死为“最新版本”。应执行：

```bash
git status --short --branch
git log -1 --oneline
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

只有本地与远端SHA一致、工作区干净时，才可认为代码同步完成。

## 3. 当前项目主流程

```text
服务器A股扫描和策略评分
→ 生成JoinQuant买卖信号
→ JSON兼容发布 + SQLite Batch 1账本
→ JoinQuant模拟盘拉取并执行
→ 回传账户、持仓和订单结果
→ 服务器持仓同步、健康检查、微信通知和策略复盘
```

职责边界：

- 服务器负责扫描、评分、生成信号、账本、API、同步、健康检查和报告。
- JoinQuant模拟盘负责实际模拟下单和撮合。
- 企业微信负责交易计划、实际成交和健康异常通知。
- 本地模拟盘已废弃，不是当前模拟交易依据。

## 4. 当前阶段快照

统一状态模型：

| 状态 | 含义 |
| --- | --- |
| `planned` | 文档规划，代码尚未完成。 |
| `implemented` | 代码和测试完成，部署尚未确认。 |
| `deployed` | 服务器已运行包含该能力的版本。 |
| `observed` | 已在真实模拟盘交易日产生证据。 |
| `validated` | 达到连续天数、成功率、一致性和样本标准。 |

编写本快照时的已知状态：

| 能力 | 已知状态 | 接管后必须验证 |
| --- | --- | --- |
| 策略扫描 | deployed | 服务状态、交易日扫描日志和输出。 |
| JoinQuant信号与模拟下单 | deployed | 信号拉取、委托和网站策略状态。 |
| 订单回报与持仓同步 | deployed | 快照回传、实际成交和持仓一致性。 |
| 健康检查和微信异常报警 | deployed | timer、报告、告警和失败重试。 |
| SQLite Batch 1 | deployed（历史基础） | schema 1 双写能力仍包含在后续版本中；服务器最后记录的实际基线已推进到 schema 10。 |
| SQLite schema 7完整执行账本 | deployed（历史中间版本） | schema 6完整账本曾由 `e2ce5b5` 幂等迁移到7；当前外部基线以 2026-07-26 的 schema 10 记录为准。 |
| 自动对账、人工解锁与受限自动恢复 | deployed | ERROR停买、CRITICAL熔断、两次不同新鲜快照、CAS 与所有权边界已部署；尚未观察或验证。 |
| 成交回报幂等与 D+N 全量复盘 | deployed | 新 fill/legacy 增量触发、统一服务器时间、完整行情和分片复盘已随 `52b3653` 部署；尚未观察或验证。 |
| 完整历史回测 | deployed（框架） | 与信号级回测并存且代码已在服务器；真实 6 个月/1 年 strict 数据尚未导入、运行或人工验证。 |
| 模拟盘买卖强制风控 | deployed（服务器与 JoinQuant 模板的旧基线） | 旧五项 P0 已部署但未观察/验证；Batch A 的统一强制准入和精确数量只在本地实现，尚未部署。 |
| ML-7 训练型影子模型 | implemented（Tasks 1–3 已推送；Tasks 4–10 本地未提交） | strict 导入、标签、训练帧、五头模型包、治理、L0 旁路和维护代码已在本地实现；Task 11 本地总验收进行中，Task 12 未授权/未运行。服务器未部署且默认关闭；没有可信/可批准或活动模型，不得写成 deployed/observed/validated。 |
| 统一有效止损与交易运行面板 | deployed | schema 8、成交后只收紧校验、manual/trailing/effective stop、认证面板和 OCR 删除已部署；网站模板由用户确认更新，但新快照和真实卖出仍未观察或验证。 |
| 跳空越价后二次确认入场 | deployed（服务器与网站模板，功能已开启） | schema 9、二次确认、精确一手、部分成交撤余单和机会账本已部署；开关已开启且模板一致，但机会账本为空，未观察、未验证。 |
| 运行证据完整性修复 | deployed（服务器） | `68d7283`、Linux 457/457、schema 10、迁移后备份及隔离恢复演练、配置哈希和三个服务均已核验；尚未经历部署后的真实交易日观察或验证。 |
| 小资金真钱前置 Batch A | implemented（本地功能分支，未合并/未推送） | schema 11、统一 `pre_trade_check`、不可变 candidate/result/intent、原子容量预留、普通 BUY 精确 `target_qty` 和离散盈利保护已通过 Windows 验证；未部署、未观察、未验证，服务器仍以最后记录的 schema 10 检查点为准。 |
| Batch B 通知 outbox | implemented（仅本地，未提交） | Tasks 1–7 已实现：schema 12 合同、来源事务生产者、稳定计划/TTL、租约 worker、有限重试、歧义证据、容量控制、CRITICAL 复报、规则影子退役、systemd 路由、详细 `ledger-check` 和人工解除 CLI；Linux 脚本复验、提交/推送、迁移、部署、观察和验证仍未完成。 |
| 半自动参数复核与版本化发布 | planned | 当前只有样本、部分标签、策略对照、信号级回测和参数版本字段；无候选登记、统一准入、人工决定、激活或回滚机制。 |

阶段1仍需连续10个有效交易日验收。SQLite Batch 1部署后的完整交易日双写观察尚需以服务器实际数据确认。专项设计中的20个有效交易日是完整账本加固与策略验证门槛，不得与阶段1基础10日门槛混为同一结论。非交易日 readiness 只构成静态检查证据，不计为有效观察日。

## 5. SQLite实际范围

默认服务器路径：

```text
/opt/stock-analysis/cache/trading/trading.db
```

最后有外部证据的服务器交易库为 schema 10，已覆盖策略运行、不可变信号、风险决策、订单与事件、逐笔成交、账户/持仓快照、日权益、持仓周期、退出意图、冷却、自动对账、控制审计、执行问题、跳空机会，以及费用/已实现盈亏来源状态。不能再把完整订单、成交、账户、持仓或权益写成“尚未保存”。该外部范围仍需在下一次服务器访问时重新核验。

本地 Batch A 把目标升级为 schema 11，并新增：

- `account_scopes` 及按账户原子覆盖的 `broker_snapshot_current`、`broker_position_current`、`broker_order_current`。
- 长期审计的 `strategy_order_candidates`、`pre_trade_results`、`execution_intents`、`capacity_reservations` 和 `position_capacity_adoptions`。
- 持仓周期的一手盈利保护时间字段，以及对账所需的账户作用域/快照证据字段。

这些 schema 11 内容只存在于本地功能分支，尚未进入服务器。当前 broker 表是有界当前状态，不是高频追加历史；candidate/result/intent/reservation/adoption 属于订单级长期审计。训练样本、标签、预测和模型登记仍属于独立的 `cache/ml/ml.db`，不得混入交易库；服务器也不得假设已经部署或启用 ML 数据库。任何库都不得保存 Token、Webhook、SSH 私钥或完整券商账号。

## 6. 关键文档读取顺序

新 Codex 对话在行动前应完整读取：

1. `AGENTS.md`
2. `docs/project_roadmap.md`
3. `docs/project_handoff.md`
4. 主文档“当前有效从文档索引”中与任务直接相关的从文档
5. 与当前任务直接相关的代码、测试和最近 20 条 Git 提交

专项读取规则：修改交易、策略或风控时读取实盘执行方案和当前分层风险 spec/plan；修改账本或对账时读取稳定性账本设计；修改持久化数据时读取数据存储规范；执行 Codex 自动审核或服务器只读诊断时读取 Codex 观察方案；参数复核任务读取对应 spec/plan。`docs/archive/` 只在追溯历史决策时读取，不属于默认必读资料。

读取后必须先区分：

```text
planned / implemented / deployed / observed / validated
```

不得把文档计划误认为代码实现，不得把代码实现误认为服务器部署，也不得把服务器部署误认为连续交易日验收通过。

## 7. 新电脑安装与克隆

建议准备：

- Git。
- Python 3.12附近的兼容版本。
- Codex桌面应用、CLI或IDE扩展。
- OpenSSH客户端（需要服务器只读审核时）。

克隆：

```powershell
git clone https://github.com/yuyang0702/stock-analysis.git
cd stock-analysis
git status --short --branch
git log -1 --oneline
```

如果目录已存在：

```powershell
git status --short --branch
git pull --ff-only origin main
```

如果工作区有未提交修改，不得直接覆盖、reset或checkout；先判断修改归属并保留用户工作。

## 8. 新对话接管提示词

建议将下面内容作为新项目任务的第一条消息：

```text
这是一个长期维护的A股量化交易项目。开始任何修改前，请先完整读取：

1. AGENTS.md
2. docs/project_roadmap.md
3. docs/project_handoff.md
4. 主文档“当前有效从文档索引”中与当前任务相关的从文档
5. 与当前任务相关的代码、测试和最近20条Git提交

归档目录只用于追溯历史，不作为默认必读或当前状态依据。

请先不要修改文件，不要提交、推送、部署或重启服务。

读取后输出：
- 主从文档关系
- 当前主流程和实施阶段
- 已实现、已部署、待真实验证和待实现能力
- SQLite当前实际范围
- 服务器与JoinQuant职责边界
- Codex只读审核员权限边界
- 数据存储和文件增长约束
- 当前P0/P1/P2事项
- 文档矛盾、过期状态和无法从Git确认的外部信息

必须区分planned / implemented / deployed / observed / validated。
```

## 9. 外部状态补充模板

在新对话输出项目理解后，可以补充以下非秘密状态；内容必须按实际情况更新：

```text
当前外部状态快照：
- 服务器项目路径：/opt/stock-analysis
- 当前主模拟盘：JoinQuant模拟盘
- SQLite默认路径：/opt/stock-analysis/cache/trading/trading.db
- 行业运行数据：/opt/stock-analysis/cache/industry/
- 阶段1目标：连续10个有效交易日
- Codex权限：只读观察、阶段评审和优化建议
- 禁止：自动修复、Git写操作、部署、重启和交易操作
- 服务器状态、Git SHA、服务、SQLite和交易日证据仍需重新验证
```

不得在聊天中发送：

- SSH私钥。
- 服务器密码。
- 企业微信Webhook。
- JoinQuant Token。
- SMTP授权码。
- 完整 `stock-analysis.env`。

## 10. 运行数据与私密配置

以下内容不会由Git同步：

```text
stock-analysis.env
cache/
output/
.venv/
SSH keys
Codex scheduled tasks
local application settings and approvals
```

只开发代码时无需复制服务器运行数据。

需要离线分析时，只复制必要的只读、脱敏副本，例如：

- 健康报告和健康历史。
- 脱敏日志。
- SQLite一致性备份。
- 策略样本。
- 账户快照的脱敏副本。

运行数据迁移必须遵守 `docs/data_storage_policy.md`，尤其是备份一致性、敏感信息和恢复验证要求。

## 11. 服务器只读接入

不建议为Codex保留root免密SSH。

建议创建专用用户：

```text
stockmonitor
```

允许读取：

- 健康报告和健康历史。
- API事件和必要日志摘要。
- 当前信号和账户快照。
- SQLite只读查询结果。
- 服务状态和服务器Git SHA。

禁止：

- sudo和root权限。
- systemctl写操作和服务重启。
- 修改项目文件或运行数据。
- Git add、commit、push、merge、pull。
- 读取 `stock-analysis.env`秘密值。

新电脑需要重新生成专用密钥并安装公钥。密钥和授权不通过Git同步。

## 12. Codex权限边界

默认允许：

- 读取代码和文档。
- 只读诊断。
- 读取经过授权的服务器证据。
- 判断阶段和生成优化建议。
- 检查数据增长和规范符合性。

默认禁止：

- 因诊断请求自动修复代码。
- 未经明确授权修改文件。
- 自动提交、推送或部署。
- 自动重启服务或修改配置。
- 自动清理、归档或移动服务器数据。
- 自动改变买卖、仓位和风控逻辑。
- 自动操作订单和持仓。
- 为参数候选写入批准/拒绝决定，或激活、回滚任何参数版本。

即使用户授权实施某项修改，提交、推送和部署仍按用户当次明确授权范围执行，不从历史聊天推断长期授权。

## 13. 每次任务的开始检查

```text
1. 读取AGENTS.md和相关文档。
2. 检查Git工作区和当前分支。
3. 判断请求是解释、诊断、设计还是实施。
4. 识别主文档状态和相关未完成阶段。
5. 识别是否会改变业务逻辑或持久化数据。
6. 对照data_storage_policy检查增长治理。
7. 明确需要的测试和权限。
```

诊断请求不自动实施修复；实施请求不得扩大到未授权的业务变化。

## 14. 每次任务的交付检查

如发生代码或文档修改，至少确认：

- 改动与主文档和专项设计一致。
- 未覆盖用户已有或未完成的业务逻辑。
- 相关测试和格式检查通过。
- 新持久化数据符合存储规范。
- Git状态和未提交内容准确报告。
- 未经授权不提交、不推送、不部署。
- 如已执行Git或部署，报告提交SHA、服务器SHA和验证结果。

## 15. 当前建议优先级

接管后首先重新验证，而不是直接继续开发：

1. GitHub、本地和服务器SHA。
2. SQLite Batch 1健康及JSON双写。
3. JoinQuant信号拉取、账户快照、委托、成交和持仓同步。
4. 阶段1有效观察日和连续稳定日。
5. 健康历史、API事件、快照历史、扫描输出和缓存增长基线。
6. 当前仅本地代码授权可继续实现 Batch B、C、D；不得据此提交、推送或发布。
7. 外部发布仍从 Batch A 开始：经单独授权提交/合并/推送，再按 schema 10→11 部署门完成 Linux 全量测试、备份迁移、隔离恢复、代表性 JoinQuant 交易日观察和 strict walk-forward；后续批次分别走独立发布门。

当前推荐顺序仍是：

```text
完成 Batch A 本地检查点
→ 本地代码依序准备 Batch B、C、D（无外部动作）
→ 另行授权发布 Batch A
→ schema 10→11 Linux 验证、备份迁移、模拟盘观察与 strict 验证
→ Batch B、C、D 分别独立发布和观察
→ 半自动参数复核（自动分析、人工批准、另行授权发布）
```

## 16. 快照维护规则

每次发生以下变化时更新本文件：

- 主阶段发生变化。
- 关键能力从implemented变为deployed或validated。
- SQLite范围发生变化。
- 服务器目录、主流程或职责边界改变。
- Codex权限边界改变。
- 新增新电脑必须知道的安全或迁移约束。

不要把高频运行状态、每日统计和秘密值写入本文件。每日证据属于服务器运行数据和Codex审核报告，本文只保留低频交接信息。
