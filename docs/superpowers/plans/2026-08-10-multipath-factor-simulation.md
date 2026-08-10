# 多路径因子模拟策略实施记录

本记录对应[多路径因子模拟策略设计](../specs/2026-08-10-multipath-factor-simulation-design.md)。状态均指本地工作区，不代表提交、服务器部署、聚宽网站更新、实盘观察或验证。

| 批次 | 实现证据 | 本地状态 |
| --- | --- | --- |
| B0 | `factor_contracts.py` 的时区/未来 bar 契约，候选通道对该错误重新抛出；生成包 Python 3.6 AST 测试 | implemented |
| B1 | `FactorDecision`、稳定路径与 setup ID、注册表、稳定拒绝阶段 | implemented |
| B2 | `candidate_channels.py` 的 30+10+5/总 45、有界筛选、按股去重和多通道归因 | implemented |
| B3 | `factor_wave3.py` 的确认枢轴、结构评分、5 分钟突破和价格计划 | implemented |
| B4 | `factor_limitdown.py` 的连续跌停、首次开板质量和次日 09:50 确认 | implemented |
| B5 | `strategy_economics.py`、实时/便携组合容量、100 股和真实费用门槛 | implemented |
| B6 | `strategy_exit_runtime.py`、实时持仓路径恢复、PIT 原生回测、strict 导出、ML 特征与有界归因 | implemented |
| B7 | `factor_research.py` 的样本、期望、利润因子、Top3、walk-forward 和回撤准入；无自动发布入口 | implemented |

发布边界：当前代码必须先完成专项、集成和全量回归；随后是否 commit、push、部署服务器、受控重启和重新生成聚宽脚本，均按用户当次授权分别执行。部署成功后仍只能标记 `deployed / not observed / not validated`，直到出现足量真实模拟样本并通过 B7 人工复核。

本地回归证据（2026-08-10）：Windows 全量发现共 1060 项，1057 项通过；仅 `tests.test_joinquant_linux_script` 的 3 项因本机没有 `bash` 而无法启动，需在 Linux/服务器环境补验。没有因子、策略、导出器、快照、严格历史或训练数据测试失败。此证据不改变提交、部署、观察和验证状态。
