# 项目接管与新环境恢复说明

> 2026-07-16 统一有效止损与交易运行面板增量已随 `8db92bf6448466827a50560ae2fb8c7fde142c72` 推送并部署：schema 8 增加可空 `position_cycles.manual_stop_price`；成交成本保护校验后的 frozen initial、明确人工止损、首段止盈后移动止损共同解析唯一 effective stop；网页和策略不再各算一套。网页已移除 OCR/截图和直接清仓入口，增加认证、CSRF、运行/异常/风险/轨迹视图；JoinQuant 模板改用 bearer token 且 token 值未改变。部署前 schema 7 备份完整性为 `ok`，Linux全量414/414、编译、schema 8健康/可写、环境哈希不变、三个服务active、同步2个持仓、网页302登录保护、API未认证403及重启后ERROR日志为空均已核验。用户确认网站模板已手工更新为 `2026-07-16.1-unified-effective-stop`；新模板尚无交易日快照回传。当前严格为 `implemented（已推送） / deployed（服务器；网站由用户确认） / not observed / not validated`。

> 2026-07-19 网页可观测性增强已在隔离分支实现：今日运行区分别显示扫描、信号、快照
和对账年龄，并严格区分预期与实际回传模板；待执行区解释部分成交、未完成退出和异常
影响；持仓区增加 R 风险、入场来源及最多30条交易链路；研究区只读显示严格阶段。
不新增 schema、持久化、直接交易、自动解锁或模型/参数入口。网页专项7/7通过，Windows
可运行全量416项通过，3项 Linux 脚本测试因本机无 `bash.exe` 留待服务器补跑。当前为
`implemented / not deployed / not observed / not validated`。

> 2026-07-18 新增“跳空越价后二次确认入场”专项设计：旧计划只作为价格和风险锚点，当前策略必须重新选中；封死涨停不排队，开板后两次独立有效扫描确认，回封重置，价格上限为原入场加 `0.5R`；不足 100 股仅在完整风险预算允许时使用最小一手。确认后必须产生全新信号，不能恢复旧信号。当前严格为 `planned / not implemented / not deployed / not observed / not validated`，没有改变服务器或 JoinQuant 当前行为。权威边界见 `docs/superpowers/specs/2026-07-18-gap-reentry-confirmation-design.md`。

> 2026-07-15 最新部署增量：成交全量对账已改为仅比较快照交易日的 SQLite 成交与 JoinQuant 当日 `get_trades()`；跨日历史成交不再产生假 `FILL_MISSING_PLATFORM`，同日缺失与平台侧未落账的严重度保持不变。代码提交 `cd83f26` 已推送并部署；SQLite 备份完整性、Linux全量326/326、Python编译、schema 7健康/可写、配置未变、三个服务active及重启后ERROR日志为空均已核验。当前为 `implemented（已推送） / deployed（服务器） / not observed / not validated`。

> 2026-07-15 当前部署检查点：schema 7 执行链增量已随实现提交 `e2ce5b50590edc28cb748bee1fa985f43c9a0366` 进入 `origin/main` 并部署到服务器 `/opt/stock-analysis`。部署前 schema 6 备份完整性为 `ok`；Python 编译、Linux 隔离测试账本全量测试 324/324、正式账本 `ledger-check` 和 schema 7 迁移通过；`stock-analysis.env` 哈希未变化，三个核心服务 active，重启后五分钟 ERROR 日志为0，服务器工作树干净并与 `origin/main` 一致。状态为 `implemented（已推送） / deployed（服务器） / not observed / not validated`。

> 用户报告已在 JoinQuant 网站手动更新模板 `2026-07-15.1-execution-state-recovery`；更新后尚无新快照证据，故网站侧记录为 `deployed（用户确认） / not observed / not validated`。服务器 `buy_enabled=0`、`kill_switch=0`；盘后立即恢复因 `ACCOUNT_SNAPSHOT_STALE` 被拒绝。一次性 timer 已排程在 2026-07-16 09:32、09:35 完整对账并于09:37在全部安全门满足时 CAS 恢复买入，成功前不得写成已经解除。

> 2026-07-14 的 `aabe1e6` / `52b3653` / schema 6 / 模板 `2026-07-14.2-p0-execution-contract` 是上一部署基线，已被上述 `e2ce5b5` / schema 7 检查点取代；其中分层退出、完整账本、备份恢复、历史回测框架、通知复盘和五项 P0 仍包含在当前版本中。真实交易日行为仍为 `not observed / not validated`，真实 6 个月/1 年 strict 数据也尚未导入和重复运行。

> 2026-07-14 `7c31684` 通知复盘增量已包含在服务器当前 `52b3653` 中：SQLite 新 fill/legacy 累计成交增量驱动执行回报、统一企业微信服务器时间，以及 D+0/D+1/D+3/D+5/D+10 全量买点复盘。当前为 `implemented（已推送） / deployed / not observed / not validated`。

> 2026-07-14 五项执行正确性 P0 已随 `52b3653` 进入 `origin/main` 并部署到服务器 `/opt/stock-analysis`，包括强制风险准入、版本化唯一买入计划、退出意图续执行及优先级保护、JoinQuant 5只/80%双层边界与买卖开关、已有持仓及未完成买单分类暴露。服务器专项测试 123/123、Python 编译和 `ledger-check` 通过，环境文件校验未变，三个核心服务 active；JoinQuant “AI” 策略已持久化模板版本 `2026-07-14.2-p0-execution-contract` 并保留原运行配置。当前严格为 `implemented（已推送） / deployed（服务器与 JoinQuant 模板） / not observed / not validated`。

> 上一服务器外部检查点（用户提供）：2026-07-14 20:06，服务器 HEAD 为 `131118213f22bbdaecd5cd8ab89a87db9aaf7f85`，分支与 `origin/main` 一致且干净；SQLite schema 6 完整性/可写检查通过，环境文件哈希未变，三个核心服务均 active。该历史检查点已被本次 `52b3653` 服务器与 JoinQuant 部署证据取代，但仍可用于追溯部署前基线。

> 2026-07-16 ML-7 基础增量：实施计划 Task 1–3 已实现并推送，包括共享候选评分/严格样本契约、独立有界 `cache/ml/ml.db` schema v1、以及完整五分钟实时候选采集与发布来源审计；Windows 可运行回归 402 项和 Linux 静态测试 2 项通过，最终独立审查无 Critical/Important/Minor。严格状态为 `partially implemented（基础能力已推送） / not deployed / not observed / not validated`。服务器尚无该 ML 库或运行证据，`ML_TRAINED_SHADOW_ENABLE` 默认关闭；尚未训练模型。Task 4–12 以及 ML-8/Batch G 的候选治理仍为 `planned`。

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
- 信号样本、影子评分、策略对照和信号级回测。
- ML-7 共享候选评分/严格样本契约、独立 ML SQLite schema v1 和完整五分钟实时候选采集代码；默认关闭且尚未部署。
- 分层退出、组合风险和可交易性保护，以及自动备份恢复基础。
- Codex只读观察与阶段评估方案。
- 数据存储、文件增长与保留规范。
- 根目录 `AGENTS.md`仓库开发约束。

`9f4c12d` 在此基础上增加 schema 6 完整执行账本、自动对账与人工解锁，以及独立逐日历史回测框架；该提交已进入 `origin/main` 并包含在服务器当前 `52b3653` 中。Git 本身只能证明代码已推送；本次服务器和 JoinQuant 的 `deployed` 结论来自独立部署证据。

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
| SQLite Batch 1 | deployed | 服务器已运行 schema version 1；待部署后首个有效交易日确认双写和交易日一致性。 |
| SQLite schema 7完整执行账本 | deployed | schema 6完整账本由 `e2ce5b5` 幂等迁移到7，新增生命周期和当前执行问题状态；服务器健康/可写检查通过，尚未观察或验证。 |
| 自动对账、人工解锁与受限自动恢复 | deployed | ERROR停买、CRITICAL熔断、两次不同新鲜快照、CAS 与所有权边界已部署；尚未观察或验证。 |
| 成交回报幂等与 D+N 全量复盘 | deployed | 新 fill/legacy 增量触发、统一服务器时间、完整行情和分片复盘已随 `52b3653` 部署；尚未观察或验证。 |
| 完整历史回测 | deployed（框架） | 与信号级回测并存且代码已在服务器；真实 6 个月/1 年 strict 数据尚未导入、运行或人工验证。 |
| 模拟盘买卖强制风控 | deployed（服务器与 JoinQuant 模板） | 已同步 `52b3653` 与模板 `2026-07-14.2-p0-execution-contract`，尚未观察或验证；真实资金级风控仍为planned。 |
| ML-7 训练型影子模型 | partially implemented（Task 1–3 已推送） | 共享评分/契约、独立 ML SQLite schema 和实时完整候选采集已实现；服务器未部署且默认关闭。strict 历史导入、标签、训练、治理与 L0 推理仍待实现；没有模型，不得写成 deployed/observed/validated。 |
| 统一有效止损与交易运行面板 | deployed | schema 8、成交后只收紧校验、manual/trailing/effective stop、认证面板和 OCR 删除已部署；网站模板由用户确认更新，但新快照和真实卖出仍未观察或验证。 |
| 半自动参数复核与版本化发布 | planned | 当前只有样本、部分标签、策略对照、信号级回测和参数版本字段；无候选登记、统一准入、人工决定、激活或回滚机制。 |

阶段1仍需连续10个有效交易日验收。SQLite Batch 1部署后的完整交易日双写观察尚需以服务器实际数据确认。专项设计中的20个有效交易日是完整账本加固与策略验证门槛，不得与阶段1基础10日门槛混为同一结论。非交易日 readiness 只构成静态检查证据，不计为有效观察日。

## 5. SQLite实际范围

默认服务器路径：

```text
/opt/stock-analysis/cache/trading/trading.db
```

Batch 1当前主要保存：

- schema migration。
- 策略运行记录。
- 不可变信号记录和原始JSON。
- 观察型风控判断。
- 少量系统状态。

当前不应假设已经保存：

- 完整委托。
- 成交明细。
- 全量账户快照。
- 真实持仓历史。
- 权益曲线。
- 手续费和滑点账本。
- 信号到订单、成交和收益的完整关联。
- 参数候选、评价、人工决定、激活和回滚记录；这些表属于 2026-07-14 Batch G 计划，不属于服务器 schema 1 或本地 schema 6 的当前实际范围。
- 服务器当前仍不应假设存在五分钟 ML 候选、训练标签、模型预测、模型登记或人工模型事件。代码已具备独立 `cache/ml/ml.db` schema v1 和实时候选写入能力，但尚未部署、启用或产生服务器实际库；标签、预测和训练能力仍未实现。

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
6. Batch A-E 部署并完成执行安全观察后，再部署并观察已本地实现的自动备份恢复，随后检查完整账本和 Batch G 数据就绪门。

当前推荐顺序仍是：

```text
只读审核员接入
→ 连续交易日基线
→ 阶段1验收
→ Batch A-E模拟盘部署与Batch F执行安全观察
→ 部署并观察完整账本与存储治理、运行并验证 strict 历史回测
→ ML-7 五分钟数据契约与 L0 训练型影子观察
→ Batch G半自动参数复核（自动分析、人工批准、另行授权发布）
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
