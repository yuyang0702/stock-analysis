# 项目接管与新环境恢复说明

> 2026-07-14 增量状态：隔离工作树已实现分层退出、执行安全、组合风控、SQLite schema version 6完整账本、自动对账、停买/熔断控制和人工解锁入口；尚未提交、推送、部署、观察或验证。服务器最近确认仍为 `aa9acffaf62239e39c076408d83d113dce22b029` / schema version 1。接管时不得把本地实现状态当成服务器事实，详细边界以主文档 `docs/project_roadmap.md` 为准。

> 同日计划调整：用户要求一次补齐的买入、卖出和执行闭环现已在本地 `implemented`，仍未提交、推送、部署、观察或验证。

> 2026-07-14 计划补充：Batch G 已明确为半自动参数复核，即自动生成证据、人工按候选 ID/哈希批准、另行授权发布和版本化回滚。相关设计与实施计划已写入文档，但候选登记、评价准入、批准、激活和回滚代码均为 `planned`，不能从现有 ML 报表或 `parameter_version` 字段推断为已经实现。

> 2026-07-14 备份增量：`trading_backup.py`、项目外每日在线备份、SHA-256/完整性/schema/核心计数校验、7/4/12 轮转、隔离季度恢复演练、报告、告警复用和 systemd 模板已在本地 `implemented`。尚未提交、推送或安装到服务器，故仍为 `not deployed / not observed / not validated`；服务器最近确认状态不因本地代码变化而改变。

> 2026-07-14 历史回测增量：独立历史库、strict/price_core 双轨数据门、时点候选、A股逐日撮合、复权、绩效/walk-forward、运行持久化、CLI、原子报告和无 timer 的 Linux 手动入口已在本地 `implemented`。尚未提交、推送或部署，也没有真实 6 个月/1 年严格数据运行，因此仍为 `not deployed / not observed / not validated`；price_core 不能满足 Batch G。

> ML 状态边界：当前 `shadow_score.py` 是规则型影子评分，不是训练模型。ML-7 训练型模型和 ML-8/Batch G 参数复核是两条独立的 `planned` 路线；二者未来都必须自动分析止于候选、人工批准、显式发布和版本化回滚。

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

编写本快照时，GitHub `main`已包含：

- JoinQuant模拟盘信号、执行回报和持仓同步链路。
- 每5分钟健康检查、微信异常报警和失败通知重试。
- SQLite Batch 1策略运行、信号和观察型风控账本。
- JSON/SQLite信号一致性检查和readiness报告。
- 信号样本、影子评分、策略对照和信号级回测。
- Codex只读观察与阶段评估方案。
- 数据存储、文件增长与保留规范。
- 根目录 `AGENTS.md`仓库开发约束。

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
| SQLite schema 6完整执行账本 | implemented（仅本地） | 含持仓周期、订单、逐笔成交、账户/持仓检查点、日权益、对账、控制审计和冷却；服务器仍为schema 1。 |
| 自动对账与人工解锁 | implemented（仅本地） | ERROR停买、CRITICAL熔断、两次不同全量一致快照和二次确认；未部署、观察或验证。 |
| 完整历史回测 | implemented（仅本地框架） | 与信号级回测并存；真实 6 个月/1 年 strict 数据尚未导入、观察或人工验证，服务器未部署。 |
| 模拟盘买卖强制风控 | implemented（仅本地） | 尚未部署、观察或验证；真实资金级风控仍为planned。 |
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
→ 完整账本、存储治理和完整历史回测
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
