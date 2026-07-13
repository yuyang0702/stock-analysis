# 文档体系合并与归档设计

## 目标

保留 `docs/project_roadmap.md` 作为唯一主文档，把仍在使用的从文档控制在清晰、有限的集合中；已完成、已被覆盖或包含废弃流程的文档移入归档目录，不再作为日常必读资料。

## 决策

采用“保守合并 + 活跃索引 + 按月归档”：

- 不把所有内容塞进一个超长主文档。
- 合并已经被后续设计完整覆盖的同类文档。
- 已完成的实施计划保留历史原文，但移出活跃 `specs/` 和 `plans/`。
- 主文档列出所有当前有效从文档、职责、状态和读取条件。
- 归档文档不得定义当前状态、阶段或优先级；冲突时始终以主文档和活跃从文档为准。

## 活跃文档结构

### 核心文档

1. `docs/project_roadmap.md`：唯一主文档，保存项目状态、主流程、优先级和活跃从文档索引。
2. `docs/project_handoff.md`：新电脑、新对话和外部状态恢复快照。
3. `docs/live_trading_execution_plan.md`：模拟盘到真实资金前的阶段路线和验收门槛。
4. `docs/codex_simulation_observation_plan.md`：Codex 只读审核任务、证据和权限边界。
5. `docs/data_storage_policy.md`：所有持久化数据的长期强制约束。

### 当前专项

1. `docs/superpowers/specs/2026-07-11-simulation-stability-ledger-design.md`：账本 Batch 1 已知状态及后续账本目标。
2. `docs/superpowers/specs/2026-07-13-layered-exit-risk-management-design.md`：当前买入、卖出和风险控制权威设计。
3. `docs/superpowers/plans/2026-07-13-layered-exit-risk-management.md`：当前 Batch A-G 实施与部署观察计划。
4. `docs/superpowers/specs/2026-07-14-semi-automatic-parameter-review-design.md`：参数复核治理设计。
5. `docs/superpowers/plans/2026-07-14-semi-automatic-parameter-review.md`：参数复核未来实施任务。

## 合并与归档映射

| 原文档 | 处理 | 活跃替代文档 |
| --- | --- | --- |
| `2026-07-06-a-share-risk-engine-design.md` | 合并 short/mid、ATR、R、仓位和安全降级语义后归档 | 分层退出设计 |
| `2026-07-07-joinquant-integration-design.md` | 归档早期 dry-run/本地模拟并行方案 | 主路线图 + 实盘执行方案 |
| `2026-07-10-joinquant-filled-order-notification.md` | 归档已完成实施计划 | 主路线图 + 实盘执行方案 |
| `2026-07-11-simulation-ledger-batch1.md` | 归档已完成 Batch 1 实施步骤 | 稳定性账本设计 |
| `2026-07-13-full-holding-stop-loss-design.md` | 合并全持仓价格与止损优先级后归档 | 分层退出设计 |
| `2026-07-13-full-holding-stop-loss.md` | 归档被 Batch A-G 覆盖的实施计划 | 分层退出计划 |

## 分层退出计划清理

当前文件同时包含新 Batch A-G 和旧 Batch 0-3 两套相互冲突的实施顺序。活跃计划只保留：

- 当前一次补齐原则。
- Batch A-G 的目标、状态、部署门槛和观察口径。
- 唯一实施顺序。
- `planned / implemented / deployed / observed / validated` 边界。

旧 Batch 0-3 的独立部署顺序由新 Batch A-G 替代，不再留在活跃计划；仍有价值的策略公式、持仓周期和安全降级语义合并进活跃设计。

## 链接与读取规则

- `AGENTS.md` 先指向主文档，后续只读取主文档活跃索引中与任务相关的从文档。
- `project_handoff.md` 不再硬编码所有历史 spec/plan 为必读项。
- Codex 审核只把活跃索引作为当前文档证据；归档仅在追溯历史决策时读取。
- 归档目录增加 README，记录原路径、原因和替代入口。

## 不变边界

- 不修改代码、策略、配置、测试或运行数据。
- 不改变任何能力的事实状态。
- 不把本地 `implemented` 改写为服务器 `deployed`。
- 不提交、推送、部署或重启服务。

## 验收

- 活跃文档只有上述 10 份。
- 主文档存在完整的活跃从文档表。
- 活跃文档不引用旧路径或把归档文档作为当前依据。
- 所有相对文档链接可解析。
- 归档文件内容保留，归档 README 可追溯替代关系。
- 分层退出计划只存在一套 Batch A-G 实施顺序。
- `git diff --check` 通过。
