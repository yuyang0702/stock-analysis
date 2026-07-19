# 统一有效止损与交易运行面板实施计划

日期：2026-07-16  
设计：`docs/superpowers/specs/2026-07-16-unified-effective-stop-trading-dashboard-design.md`

1. 先补纯函数测试：成交后初始止损校验、manual stop、移动止损激活和 effective stop 优先级。
2. 增加 schema v8 与幂等迁移，为活动周期增加可空 `manual_stop_price`，实现只上调/清除和 `control_events` 审计接口。
3. 新持仓按真实成本校验信号止损；活动旧周期在新鲜快照时只上调修复，并记录一次迁移控制事件。
4. 卖出合并逻辑改用统一解析器；输出初始、手工、移动和有效止损字段，保持退出优先级与退出意图续驱不变。
5. JoinQuant 同步不再生成成本价 3.5% 页面止损；同步后从活动周期回填统一风险字段。API 支持 Authorization bearer 并保持临时 query 兼容。
6. 重写持仓网页为认证后的交易运行面板；增加 CSRF、状态/异常/风险/轨迹视图和折叠手工止损维护。
7. 删除截图/OCR 页面、函数、路由和新上传入口；保留服务器历史上传目录。
8. 运行专项、全量测试、Python 编译、文档一致性和 secret 扫描；修复后复跑。
9. 更新主文档索引、交接、执行计划和存储规范，严格标记 implemented/deployed/observed/validated。
10. 提交并推送 main；服务器先备份 SQLite 和环境文件哈希，再拉取、测试、ledger-check/migration、重启三个服务、完整对账并核验状态。不得修改或输出现有 token。

## 实施结果

Tasks 1–10 已完成。实现提交 `8db92bf6448466827a50560ae2fb8c7fde142c72` 已推送并通过验证后的增量 bundle fast-forward 部署；服务器 Linux 全量414/414、schema 8健康/可写、环境文件哈希不变、三个服务active，部署后同步2个持仓。用户确认 JoinQuant 网站模板 `2026-07-16.1-unified-effective-stop` 已手工更新。严格状态为 `implemented / deployed / not observed / not validated`；下一步只做交易日快照版本回传、止损执行闭环和连续观察验收。

## 第二阶段：网页可观测性增强

状态：implemented / deployed（服务器） / not observed / not validated。

11. 先以失败测试固定独立数据时效、预期/实际 JoinQuant 版本和缺失数据降级语义。
12. 增加待执行订单、未完成退出意图、等待时间、阻塞原因和影响范围的有界只读视图。
13. 扩展单只持仓风险与交易链路，关联信号、订单、成交、退出意图和对账差异。
14. 增加只读研究与验证区，严格显示 planned / implemented / deployed / observed /
    validated，不提供模型、参数或部署写入口。
15. 保留现有登录、CSRF、人工止损审计和 OCR 路由移除行为；不增加直接交易或自动解锁。
16. 更新主文档、交接、执行计划和存储规范，运行专项、全量测试、编译、diff 和秘密扫描。
17. 实现完成后仅标记 implemented；推送、服务器部署、真实交易日观察和连续验收分别
    需要独立证据，不得随代码完成自动升级。

### 执行文件与接口

- 修改 `tests/test_holdings_web.py`：先增加数据时效/版本、待执行原因、持仓风险/链路、
  能力状态和降级语义测试，并逐项确认在实现前失败。
- 修改 `holdings_web.py`：增加纯时间格式化、异常解释、能力状态和页面只读数据组装；
  `_dashboard_data()` 继续作为唯一页面查询入口，所有列表保留 `LIMIT`。
- 修改 `docs/project_roadmap.md`、`docs/project_handoff.md`、
  `docs/live_trading_execution_plan.md`、`docs/data_storage_policy.md` 和本专项文档：
  只在测试与实现完成后把增强阶段更新为
  `implemented / not deployed / not observed / not validated`。

### 测试驱动顺序

1. 增加 `test_dashboard_shows_independent_freshness_and_template_confirmation`，构造扫描、
   信号和 JoinQuant 快照，断言预期版本、实际回传版本和独立时间状态；先运行并确认
   因页面缺少字段而失败，再实现时间/版本卡并复跑通过。
2. 增加 `test_dashboard_explains_pending_execution_and_issue_impact`，构造部分成交订单、
   活动退出意图和执行问题，断言等待状态、原因及“停买不影响卖出”；先失败，再实现
   最多30条的待执行与异常解释查询并复跑。
3. 增加 `test_dashboard_traces_position_risk_and_signal_provenance`，构造入场信号和持仓周期，
   断言持仓天数、1R、收益倍数、入场路径以及信号/订单/成交/对账轨迹；先失败，再实现
   每只持仓最多30条轨迹和有限快照派生。
4. 增加 `test_dashboard_shows_strict_capability_states_without_controls`，断言研究区逐项显示
   planned / implemented / deployed / observed / validated，且不存在模型启用、自动解锁
   或直接交易按钮；先失败，再实现固定只读能力清单。
5. 每个红绿循环运行对应单测和 `python -m unittest tests.test_holdings_web -v`；最后运行
   `python -m unittest discover -s tests -v`、`python -m py_compile holdings_web.py` 和
   `git diff --check`。本机缺少 `bash.exe` 导致的三个 Linux 脚本测试作为已批准环境限制，
   必须在服务器部署前补跑，不得记为本地通过。

### 第二阶段实施结果

Tasks 11–16 已完成：网页采用三个锚点分区，增加独立数据时效和预期/实际模板版本、
待执行与未完成退出解释、异常影响提示、持仓 R 风险与信号来源、30条有界交易链路以及
只读研究状态和数据库异常安全降级。未新增 schema、运行文件、第三方依赖或交易/解锁
入口。新增网页测试5项，专项7/7通过；Windows 全量419项中416项通过，另外3项因本机不存在 `bash.exe`
无法启动 Linux `run_ubuntu.sh ledger-check`，该环境限制已由用户确认可留待服务器部署
前补跑。当前未提交推送、未部署、未观察、未验证。
