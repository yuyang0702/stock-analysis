# 文档体系合并与归档执行计划

> 本计划本身位于归档区，不属于项目业务实施计划。

**Goal:** 把 16 份活跃文档整理为 10 份当前有效文档和可追溯的月度归档，并在主文档建立唯一从文档索引。

**Architecture:** 主文档负责状态与索引；活跃从文档按交接、阶段执行、只读审核、存储治理和专项设计分工；历史原文移动到 `docs/archive/2026-07/`，不再参与当前状态判断。

**Tech Stack:** Markdown、PowerShell、Git 只读检查。

## Global Constraints

- 只修改或移动文档和 `AGENTS.md`。
- 不修改业务代码、测试、配置和运行数据。
- 保留 `planned / implemented / deployed / observed / validated` 状态语义。
- 不提交、推送、部署或重启。

### Task 1: 合并当前分层风险设计

- [ ] 将早期风控设计中的 short/mid、ATR、R、仓位和缺数降级合并到当前分层退出设计。
- [ ] 将全持仓硬止损设计中的候选池外覆盖、价格回退和卖出优先级合并到当前设计。
- [ ] 确保设计只描述当前权威业务语义，不复制实施步骤。

验证：当前设计可独立说明买点、止损、止盈、仓位、持仓周期、JoinQuant 契约和安全降级。

### Task 2: 清理分层退出实施计划

- [ ] 保留新 Batch A-G。
- [ ] 删除已被替代的旧 Batch 0-3 和第二套推荐顺序。
- [ ] 保留状态边界、统一部署门槛和参数复核链接。

验证：`rg -n "Batch 0|## Batch 1|## Batch 2|## Batch 3" docs/superpowers/plans/2026-07-13-layered-exit-risk-management.md` 无匹配。

### Task 3: 归档被覆盖和已完成文档

- [ ] 创建 `docs/archive/2026-07/specs/` 和 `docs/archive/2026-07/plans/`。
- [ ] 移动设计中列出的 6 份文档，保持文件名不变。
- [ ] 创建归档 README，记录原路径、归档原因和活跃替代文档。

验证：原路径不存在，归档目标存在且内容非空。

### Task 4: 建立主文档活跃索引

- [ ] 在路线图加入 10 份活跃文档表、权威范围和读取条件。
- [ ] 明确归档不参与当前状态判断。
- [ ] 更新 `AGENTS.md` 和 handoff 的读取顺序。
- [ ] 更新 Codex 观察计划的文档证据规则。

验证：所有活跃文档都能从路线图索引到达；日常读取规则不要求遍历归档。

### Task 5: 修复引用并验证

- [ ] 搜索所有归档前旧路径引用。
- [ ] 活跃文档改为指向活跃替代文档；归档内部历史引用可以保留，但不得作为当前依据。
- [ ] 检查 Markdown 文件存在性、隐藏 Unicode、冲突状态词和 whitespace。
- [ ] 输出最终活跃/归档清单和未授权动作确认。

验证命令：

```powershell
rg -n "2026-07-06-a-share-risk-engine-design|2026-07-07-joinquant-integration-design|2026-07-10-joinquant-filled-order-notification|2026-07-11-simulation-ledger-batch1|2026-07-13-full-holding-stop-loss" AGENTS.md docs --glob '!archive/**'
git diff --check
```

预期：第一条无活跃引用；第二条退出码 0 且无 whitespace 错误。

## 执行结果（2026-07-14）

- Task 1-5 已执行：活跃文档为 10 份，6 份旧文档已归档，主文档已建立从文档索引。
- 分层风险设计已吸收早期买卖点与全持仓硬止损语义；实施计划只保留 Batch A-G。
- 活跃文档不存在旧路径引用，活跃文档中的 `docs/*.md` 引用均可解析。
- 187 项非 Linux 测试通过；完整 192 项测试中 3 项因当前 Windows 没有 Bash 而报 `WinError 2`，不是业务断言失败，需在 Linux 或安装 Bash 后补跑。
- 本次未提交、推送、部署或重启服务。
