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
