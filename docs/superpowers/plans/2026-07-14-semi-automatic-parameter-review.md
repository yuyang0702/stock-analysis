# Semi-Automatic Parameter Review Implementation Plan

> **状态：`planned`。** 本计划未实施、未部署、未观察、未验证。执行时必须逐批测试和复核，不能因为文档已存在而改变状态。

**Goal:** 在现有模拟盘账本、策略对照和信号级回测基础上，实现自动生成参数候选与证据、人工按哈希批准、显式发布和可审计回滚；任何定时任务和 Codex 审核任务均不得自动改变活动参数。

**Architecture:** SQLite 保存不可变参数版本、评价、人工决定和激活事件；纯函数评价器只消费显式时间窗口的数据并返回结构化结果；报告层原子覆盖 latest 文件；发布命令使用候选 ID、哈希和预期活动版本做比较并交换。参数白名单与硬风险边界留在受测试保护的代码中，JoinQuant 信号和账本分别记录代码、模型和活动 `parameter_version`。首版候选生成是确定性有界搜索，不依赖或冒充机器学习模型。

**Dependencies:** Python 标准库、现有 `TradingStore`、`backtest_engine.py`、`strategy_compare_report.py` 和 unittest；不新增第三方依赖。完整设计见 `docs/superpowers/specs/2026-07-14-semi-automatic-parameter-review-design.md`。

---

## 实施前置与顺序

本计划是 `docs/superpowers/plans/2026-07-13-layered-exit-risk-management.md` 的 Batch G。开始编码前必须确认：

1. Batch A-E 已部署到 JoinQuant 模拟盘，并完成 Batch F 首日逐笔核对和连续 3 个有效交易日执行安全观察。
2. SQLite 完整订单、成交、费用、持仓周期和收益关联足以复算评价指标；缺口未补齐时先完成账本工作。
3. 自动 SQLite 备份、保留轮转和恢复演练已实现，不能只依赖本地 `backup_to` 单元测试。
4. 逐日历史回测框架已本地实现，但在真实 strict 数据、3 个 walk-forward 窗口和人工复算形成可用证据前，候选只能保持 `research_only`；否则需满足 60 个有效模拟盘交易日的替代门槛。
5. 基线参数版本已经冻结，服务器、JoinQuant 和账本中的版本一致。

实施顺序固定为：数据契约 → 只读评价 → 候选与报告 → 人工决定 → 显式激活/回滚 → 调度与健康检查 → 模拟盘观察。不得先实现自动发布再补权限保护。

## Task 1: 固化参数白名单和不可变版本格式

**Files:**

- Create: `strategy_parameters.py`
- Create: `tests/test_strategy_parameters.py`
- Modify: `config.py`
- Modify: `tests/test_config_env.py`

**Step 1: Write failing tests**

覆盖：规范化 JSON 与 SHA-256 稳定；参数顺序不影响哈希；未知字段、越界值、非有限数字、试图关闭硬止损或放宽绝对风险上限均拒绝；每个候选最多修改一个参数族；默认版本能复现当前冻结参数。

**Step 2: Run the focused tests**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_strategy_parameters tests.test_config_env -v
```

Expected: 新测试因模块不存在或校验未实现而失败，且失败原因与目标一致。

**Step 3: Implement the minimum contract**

新增不可变 `ParameterSet`、规范化序列化、哈希、版本格式和参数族白名单。`config.py` 只负责读取明确选择的已批准版本；现有环境变量兼容路径保留，但检测到其覆盖受管参数时必须在健康结果中标记 `unmanaged_parameter_override`，不得静默覆盖。

**Step 4: Re-run tests**

Expected: focused tests pass。

## Task 2: 增加 SQLite 参数审计 schema

**Files:**

- Modify: `trading_store.py`
- Modify: `tests/test_trading_store.py`

**Step 1: Write failing migration and constraint tests**

覆盖从当前 schema version 6 原位迁移且旧数据不丢失；创建 `parameter_sets`、`parameter_evaluations`、`parameter_decisions`、`parameter_activations`；版本、哈希、评价窗口和决定幂等；候选内容不可更新；外键和唯一约束生效；重复迁移安全；在线备份和恢复副本可读取新表。

**Step 2: Run the focused test**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_trading_store -v
```

Expected: schema 版本和新 API 断言失败。

**Step 3: Implement migration and typed records**

使用显式 migration、事务和索引。提供最小 API：登记候选、写入不可变评价、按 ID/状态读取、记录人工决定、比较并交换活动版本、记录回滚/退役事件。禁止通用 SQL 字符串接口和无条件状态更新。

**Step 4: Re-run tests and ledger check tests**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_trading_store tests.test_joinquant_linux_script -v
```

Expected: tests pass；随后更新 `run_ubuntu.sh ledger-check` 的目标 schema 和对应断言。

## Task 3: 实现无未来数据的时间切分评价器

**Files:**

- Create: `parameter_evaluator.py`
- Create: `tests/test_parameter_evaluator.py`
- Modify: `backtest_engine.py`
- Modify: `tests/test_backtest_engine.py`

**Step 1: Write failing pure-function tests**

使用固定交易样本覆盖：20/30/60 日和闭合周期门槛；按交易日而非自然日切分；至少 3 个不重叠 walk-forward 窗口；候选确定前保留集不可参与排序；费用和滑点计入；最大回撤、Profit Factor、尾部损失和分层指标可人工复算；去掉最高 3 笔交易后结论；输入顺序变化不影响结果；数据缺口、版本混用和重复样本硬拒绝。

**Step 2: Run focused tests and confirm failure**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_parameter_evaluator tests.test_backtest_engine -v
```

**Step 3: Implement evaluator**

评价器只接受显式基线、候选、交易记录和时间窗口，不自行读取最新文件或当前日期。复用 `backtest_engine.py` 可共享的成本与回撤计算；若现有接口混合文件 I/O，先抽出纯函数并保持原 CLI 输出兼容。

**Step 4: Re-run tests**

Expected: deterministic tests pass，且现有 backtest tests 无回归。

## Task 4: 生成有界候选并应用准入门

**Files:**

- Create: `parameter_review.py`
- Create: `tests/test_parameter_review.py`
- Modify: `strategy_compare_report.py`
- Modify: `tests/test_strategy_compare_report.py`

**Step 1: Write failing candidate tests**

覆盖：每次最多 5 个候选、每族最多 2 个、单候选单参数族、有限步长、同输入同 ID/哈希；样本不足只返回 `insufficient_data`；缺少可用 strict 历史回测证据且不足 60 日时只能 `research_only`；2/3 窗口、PF +5%、净收益、回撤、离群交易、分层灾难性退化和执行失败率门槛逐项可解释。

**Step 2: Run focused tests**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_parameter_review tests.test_strategy_compare_report -v
```

**Step 3: Implement bounded generation and gate reasons**

候选搜索只遍历白名单相邻步长，不反复使用保留集。每个门输出稳定 issue code、观测值、门槛和通过状态。将现有策略对照聚合抽成可复用读取层，但不改变原策略 vs 影子评分报表口径。

**Step 4: Re-run tests**

Expected: all focused tests pass。

## Task 5: 实现 latest 报告和低频归档

**Files:**

- Modify: `parameter_review.py`
- Modify: `tests/test_parameter_review.py`
- Modify: `config.py`
- Modify: `tests/test_config_env.py`

**Step 1: Write failing report/storage tests**

覆盖：`output/parameter_review_latest.md` 原子覆盖；报告明确显示 `planned / implemented / deployed / observed / validated` 边界、活动版本、候选哈希、样本门、各评价门和“批准不等于部署”；数据不足时不制造候选；只有候选状态变化或阶段验收才写低频归档；不得新增 JSONL 或逐扫描文件；单次评价明细超过 5 MB 时拒绝持久化并报警。

**Step 2: Implement report writer and retention metadata**

SQLite 保存长期事实；latest 报告只做展示。归档文件按批准/回滚/阶段事件命名并使用稳定事件 ID，重复运行不得增加文件。配置只新增输出路径、每次最大候选数和容量硬上限，不把安全门槛暴露为可随意修改的环境变量。

**Step 3: Run tests**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_parameter_review tests.test_config_env -v
```

Expected: tests pass。

## Task 6: 实现人工决定命令

**Files:**

- Create: `parameter_admin.py`
- Create: `tests/test_parameter_admin.py`
- Modify: `trading_store.py`
- Modify: `tests/test_trading_store.py`

**Step 1: Write failing authorization/state tests**

覆盖：`approve` 和 `reject` 必须提供候选 ID、完整哈希和理由；只有 `approvable` 可批准；内容、父版本、代码版本或评价窗口变化后批准失效；重复同决定幂等，冲突决定拒绝；定时任务默认入口没有决定写权限；操作输出不包含秘密值或账户明细。

**Step 2: Implement explicit CLI**

建议接口：

```text
python parameter_admin.py approve --candidate ID --sha256 HASH --reason TEXT
python parameter_admin.py reject --candidate ID --sha256 HASH --reason TEXT
```

命令只记录人工决定，不激活参数、不修改环境变量、不部署或重启。operator 使用本地受控标识，不保存个人敏感信息。

**Step 3: Run tests**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_parameter_admin tests.test_trading_store -v
```

Expected: tests pass。

## Task 7: 实现显式激活和回滚

**Files:**

- Modify: `parameter_admin.py`
- Modify: `tests/test_parameter_admin.py`
- Modify: `joinquant_exporter.py`
- Modify: `tests/test_joinquant_exporter.py`
- Modify: `joinquant_strategy.py`
- Modify: `tests/test_joinquant_strategy_template.py`

**Step 1: Write failing safety tests**

覆盖：只有已批准候选可激活；必须提供目标哈希和预期当前版本；比较并交换失败时不改变任何状态；活动版本唯一；回滚目标必须是历史已激活版本；未批准、过期、硬约束越界、环境变量覆盖或数据库健康异常时失败；信号 JSON、SQLite 策略运行和 JoinQuant 快照携带相同参数版本；旧 JSON schema 1 消费方保持兼容。

**Step 2: Implement prepare/promote/rollback separation**

建议接口：

```text
python parameter_admin.py prepare --candidate ID --sha256 HASH --expect-active VERSION
python parameter_admin.py promote --candidate ID --sha256 HASH --expect-active VERSION
python parameter_admin.py rollback --to VERSION --sha256 HASH --expect-active VERSION --reason TEXT
```

`prepare` 只做 dry-run 和输出变更摘要；`promote/rollback` 只切换本机账本中的活动版本，不执行 Git、SSH、服务重启或 JoinQuant 网站操作。部署步骤仍由独立用户授权的运维任务完成。

**Step 3: Run tests**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_parameter_admin tests.test_joinquant_exporter tests.test_joinquant_strategy_template -v
```

Expected: tests pass，兼容 JSON schema 保持 version 1。

## Task 8: 增加只读健康检查与自动分析 timer

**Files:**

- Modify: `joinquant_health.py`
- Modify: `tests/test_joinquant_health.py`
- Modify: `joinquant_readiness_report.py`
- Modify: `tests/test_joinquant_readiness_report.py`
- Modify: `run_ubuntu.sh`
- Modify: `tests/test_joinquant_linux_script.py`

**Step 1: Write failing integration tests**

覆盖：健康报告比较活动参数、信号、策略运行和 JoinQuant 模板回传版本；`parameter_version_mismatch` 为 critical 并阻止新买但不阻断安全卖出；自动分析 service 只运行 `parameter_review.py`，其 systemd 用户无管理命令入口；timer 每周最多一次，重复执行幂等；安装、状态和卸载路径完整；schema check 使用新版本。

**Step 2: Implement integration**

增加 `parameter-review` 运维命令和每周盘后 timer。自动任务只生成候选、评价和 latest 报告，绝不调用 `parameter_admin.py`。readiness 报告显示数据就绪、候选状态和活动版本一致性，但非交易日仍不能计为有效观察。

**Step 3: Run integration tests**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_joinquant_health tests.test_joinquant_readiness_report tests.test_joinquant_linux_script -v
```

Expected: tests pass。

## Task 9: 更新文档、样例配置与全量验证

**Files:**

- Modify: `stock-analysis.env.example`
- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/codex_simulation_observation_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/superpowers/plans/2026-07-13-layered-exit-risk-management.md`
- Modify: `docs/superpowers/specs/2026-07-11-simulation-stability-ledger-design.md`

**Step 1: Run full local verification**

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.venv\Scripts\python.exe -m py_compile strategy_parameters.py parameter_evaluator.py parameter_review.py parameter_admin.py trading_store.py joinquant_health.py joinquant_exporter.py
git diff --check
```

Expected: all tests pass, compilation succeeds, no whitespace errors。

**Step 2: Synchronize status labels**

代码和本地测试通过后只能把新能力标为 `implemented`。只有服务器实际运行对应版本才改为 `deployed`；只有有效交易日证据才改为 `observed`；达到本文门槛并经人工确认才改为 `validated`。记录准确 schema、参数版本和 JoinQuant 模板版本，不把本地事实写成服务器事实。

**Step 3: Perform a review checkpoint**

人工检查硬约束白名单、时间切分、保留集隔离、批准哈希、比较并交换、回滚路径、存储增长和 Codex 只读权限。发现任何 P0/P1 时停止部署准备。

## Task 10: 单独授权后的模拟盘部署与观察

本任务不是代码实施的自动后续。只有用户在当次对话明确授权提交、推送、服务器操作、参数激活和必要重启后才能执行相应动作。

部署前后按以下证据记录：

1. 本地、GitHub 和服务器 Git SHA。
2. SQLite 备份、恢复副本、`PRAGMA integrity_check`、schema version 和候选/活动版本计数。
3. 参数候选 ID、哈希、人工决定、父版本和回滚版本。
4. 服务器与 JoinQuant 模板中的活动参数版本。
5. 首个有效交易日逐笔核对；连续 3 日执行安全；至少 20 日策略表现。

状态推进规则：

```text
本地代码和测试完成 = implemented
服务器和JoinQuant同步完成 = deployed
有效交易日产生一致证据 = observed
达到门槛并人工验收 = validated
```

任何部署错误立即使用已验证父版本回滚。短期收益不佳本身不触发自动回滚或再次搜索参数。
