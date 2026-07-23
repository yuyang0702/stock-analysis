# 持仓候选触发 Pandas 布尔歧义修复设计

日期：2026-07-23

## 状态

`implemented（已推送） / deployed（服务器） / observed / not validated`

## 问题

盘中扫描在当前持仓股票进入候选池时失败，服务日志只记录：

```text
The truth value of a Series is ambiguous.
```

只读内存诊断取得的完整调用链为：

```text
run_once
-> build_risk_bundle
-> build_risk_decision
-> if holding
```

`build_risk_bundle` 会在候选已经持仓时把该候选的整行 `pd.Series` 作为
`holding` 传入。`build_risk_decision` 使用隐式布尔判断检查它是否存在，导致
Pandas 抛出布尔歧义异常并终止整轮扫描。

## 修复边界

在共享的 `build_risk_decision` 中把持仓存在性判断改为显式的
`holding is not None`。保留传入的持仓字段、策略判断、止盈止损、仓位、评分和
执行契约的原有含义。

不采用以下处理：

- 不捕获并忽略异常；
- 不把该候选降级为未持仓；
- 不调整买卖规则、风险阈值或通知规则；
- 不新增持久化数据、配置或依赖。

## 测试

先增加一个回归测试，使用非空 `pd.Series` 作为 `holding` 调用
`build_risk_decision`，确认当前代码以相同异常失败。完成最小修复后，验证该调用
正常返回且持仓逻辑仍被执行，再运行风险引擎专项测试和完整测试套件。

## 发布与验证

本地测试通过只能标记为 `implemented`。提交、推送、服务器部署和服务重启需要
另行授权。部署后必须观察真实扫描成功刷新信号文件，才能标记为 `observed`；连续
交易时段运行和持仓候选场景验证通过后，才能标记为 `validated`。

本地实现证据：回归测试先以生产相同的 Pandas 布尔歧义异常失败，修复后风险引擎
专项 7/7 通过；目标模块编译通过，不依赖 Linux Bash 的 Windows 测试 436 项通过。
Windows 缺少 Bash，3 个 `run_ubuntu.sh ledger-check` 用例留待获准后的 Linux 验证。

部署证据：提交 `2cb90485290e75883379dada2b934637d87ffa37` 已进入
`origin/main` 和服务器。部署前正式 SQLite 备份完整性为 `ok`；服务器 Linux 全量
441/441、目标模块编译、schema 9 `ledger-check` 健康/可写、环境文件哈希不变，三个
服务重启后 active 且无 warning。13:00 后首轮真实扫描成功处理包含持仓的南山铝业，
13:05:58 写出新扫描文件并刷新 `signals.json`；13:06:10 增量对账 matched、0 差异。
单轮真实证据足以标记 `observed`，连续交易时段稳定性尚未达到 `validated`。
