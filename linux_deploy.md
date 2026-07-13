# Linux 部署说明

> 当前项目规划以 `docs/project_roadmap.md` 为准。本文只保留服务器执行步骤。

项目上传到服务器后只用一个入口脚本：

```bash
bash run_ubuntu.sh
```

不带参数执行会进入交互菜单，适合日常使用；带参数执行仍然兼容原来的命令方式。

建议目录：

```bash
/opt/stock-analysis
```

## GitHub 更新流程

项目代码已经托管在：

```text
https://github.com/yuyang0702/stock-analysis.git
```

后续修改代码时，标准流程是：本地改代码并推送到 GitHub，服务器只用 `git pull` 增量更新，不再删除目录重新上传。

### 本地修改后上传 GitHub

在本地 Windows 项目目录执行：

```bash
git status
git add .
git commit -m "说明这次修改"
git push
```

如果是 Codex 帮忙修改代码，Codex 会在本地完成检查、提交并推送到 GitHub。

### 服务器更新到最新版

服务器固定目录：

```bash
cd /opt/stock-analysis
```

更新代码并重启服务：

```bash
git pull origin main
chmod +x run_ubuntu.sh
bash run_ubuntu.sh
```

进入菜单后选择“重启服务”。如果只想用命令方式：

```bash
bash run_ubuntu.sh restart-all
```

### 首次从 GitHub 部署

如果服务器上还没有 GitHub 版本，先备份旧目录，再 clone：

```bash
cd /opt
mv stock-analysis stock-analysis.bak.$(date +%Y%m%d-%H%M%S)
git clone --depth 1 https://github.com/yuyang0702/stock-analysis.git stock-analysis
cd stock-analysis
```

再从备份目录复制服务器私有配置和运行缓存，例如备份目录是 `/opt/stock-analysis.bak.20260708-203220`：

```bash
cp /opt/stock-analysis.bak.20260708-203220/stock-analysis.env /opt/stock-analysis/ 2>/dev/null || true
cp -r /opt/stock-analysis.bak.20260708-203220/cache /opt/stock-analysis/ 2>/dev/null || true
```

然后启动：

```bash
cd /opt/stock-analysis
chmod +x run_ubuntu.sh
bash run_ubuntu.sh
```

注意：`stock-analysis.env` 和 `cache/` 不上传 GitHub。前者保存企业微信 webhook、token、端口等私有配置；后者保存运行缓存、JoinQuant 同步状态、推送记录和复盘数据。日常 `git pull` 不会覆盖它们。

## 首次安装和启动

```bash
cd /opt/stock-analysis
bash run_ubuntu.sh install \
  --webhook '你的企业微信机器人URL' \
  --token '你自己设置的长随机token'
```

可选参数：

```bash
bash run_ubuntu.sh install --cash 200000
bash run_ubuntu.sh install --web-port 8080
bash run_ubuntu.sh install --signal-port 8010
bash run_ubuntu.sh install --skip-ocr
bash run_ubuntu.sh install --skip-install
bash run_ubuntu.sh install --no-start
```

`--webhook` 和 `--token` 会写入：

```text
/opt/stock-analysis/stock-analysis.env
```

对应字段：

```bash
WECOM_WEBHOOK_URL=你的企业微信机器人URL
JOINQUANT_SYNC_TOKEN=你自己设置的长随机token
NOTIFY_NON_TRADING_DAY=0
A_SHARE_HOLIDAYS=
```

## 服务

`install` 会注册并启动：

```text
stock-analysis.service
stock-holdings-web.service
stock-joinquant-signal.service
stock-joinquant-sync.timer
stock-joinquant-health.timer
stock-notify-retry.timer
stock-joinquant-readiness.timer
stock-ml-report.timer
stock-trading-backup.timer
stock-trading-backup-drill.timer
```

默认安全模式：

```bash
JOINQUANT_ENABLE=1
JOINQUANT_DRY_RUN=false
PAPER_TRADE_ENABLE=0
```

`PAPER_TRADE_ENABLE=0` 表示本地模拟盘已废弃并默认停用；当前模拟交易主账户以 JoinQuant 模拟盘为准。

## 微信推送与节假日

默认推送规则：
- 非 A 股交易日：不推普通扫描、买点提醒、JoinQuant 空计划，避免节假日刷屏。
- 交易日盘前：可以推观察摘要，但不会导出 JoinQuant 买入计划；`09:15-09:29` 集合竞价也按盘前观察处理，不算可下单盘中。
- 交易日盘中：允许买点提醒、JoinQuant 买入/卖出计划、执行回报。
- 交易日午休：常驻模式默认跳过。
- 交易日盘后：推盘后复盘和信号追踪复盘，不推买点下单计划。

联调时如果想在周末或节假日仍然推送，把环境变量改成：

```bash
NOTIFY_NON_TRADING_DAY=1
```

法定节假日可手动配置，逗号分隔：

```bash
A_SHARE_HOLIDAYS=2026-10-01,2026-10-02,2026-10-05
```

修改后重启：

```bash
bash /opt/stock-analysis/run_ubuntu.sh restart-all
```

## 日常命令

菜单方式：
```bash
cd /opt/stock-analysis
bash run_ubuntu.sh
```

常用菜单项包括查看状态、重启服务、查看日志、前台跑策略、同步 JoinQuant、生成健康检查、重试失败微信推送、生成 readiness、生成 ML 复盘、运行本地信号回测、执行或检查 SQLite 备份和运行测试。

命令方式：
```bash
bash run_ubuntu.sh status-all
bash run_ubuntu.sh restart-all
bash run_ubuntu.sh logs-strategy
bash run_ubuntu.sh logs-web
bash run_ubuntu.sh logs-joinquant
bash run_ubuntu.sh show-env
bash run_ubuntu.sh health
bash run_ubuntu.sh notify-retry
bash run_ubuntu.sh backtest
bash run_ubuntu.sh backup
bash run_ubuntu.sh backup-drill
bash run_ubuntu.sh backup-status
bash run_ubuntu.sh trading-status
bash run_ubuntu.sh reconcile
bash run_ubuntu.sh unlock
bash run_ubuntu.sh stop-buy --reason "人工停止原因"
bash run_ubuntu.sh kill-switch-on --reason "人工熔断原因"
bash run_ubuntu.sh test
```

`unlock` 是交互式向导，不是强制解锁：它会执行一次完整对账，要求最近两个全量一致结果来自不同新鲜快照，先确认关闭 `KILL_SWITCH`，再二次确认恢复买入。`kill-switch-off` 不会自动把 `buy_enabled` 改回 1。schema 6、上述命令和新菜单当前仅本地 `implemented`；服务器部署、真实运行和解锁演练仍需单独授权与证据。

前台调试：

```bash
bash run_ubuntu.sh run-strategy
bash run_ubuntu.sh run-web
bash run_ubuntu.sh run-joinquant-api
bash run_ubuntu.sh sync-joinquant
bash run_ubuntu.sh health
bash run_ubuntu.sh notify-retry
bash run_ubuntu.sh readiness
bash run_ubuntu.sh ml-report
bash run_ubuntu.sh backtest
bash run_ubuntu.sh backup
bash run_ubuntu.sh backup-drill
bash run_ubuntu.sh backup-status
```

## SQLite 自动备份与恢复演练

默认备份目录位于项目外：

```text
/opt/stock-analysis-backups
```

`stock-trading-backup.timer` 每天 `16:30 Asia/Shanghai` 使用 SQLite 在线备份 API 生成一致性副本，校验 SHA-256、`PRAGMA integrity_check`、schema 和核心表计数，并按 7 份每日、4 份每周、12 份每月轮转。`stock-trading-backup-drill.timer` 在每季度第一个周日凌晨复制最新有效备份到隔离临时目录进行恢复校验；它不会替换或写入正在使用的主库。

部署或数据库迁移前先手工执行：

```bash
cd /opt/stock-analysis
bash run_ubuntu.sh backup
bash run_ubuntu.sh backup-status
```

检查 timer 和报告：

```bash
systemctl status stock-trading-backup.timer stock-trading-backup-drill.timer
cat output/trading_backup_latest.md
ls -la /opt/stock-analysis-backups/daily
```

手工恢复演练只验证副本，不执行主库恢复：

```bash
bash run_ubuntu.sh backup-drill
cat output/trading_backup_drill_$(date +%Y)-Q$((($(date +%-m)-1)/3+1)).md
```

如需改变目录或保留数量，只允许修改 `stock-analysis.env` 中的 `TRADING_BACKUP_DIR`、`TRADING_BACKUP_DAILY_KEEP`、`TRADING_BACKUP_WEEKLY_KEEP` 和 `TRADING_BACKUP_MONTHLY_KEEP`；备份目录必须位于项目外并保证运行 systemd service 的用户可写。任何主库替换仍需停机、人工确认和单独恢复流程，本命令不会自动执行。

## JoinQuant 平台配置

把 `joinquant_strategy.py` 复制到聚宽策略编辑器，然后填：

```python
SIGNAL_URL = "http://你的服务器IP:8010/joinquant/signals"
SNAPSHOT_URL = "http://你的服务器IP:8010/joinquant/account_snapshot"
SYNC_TOKEN = "你 install 时传入的 token"
DRY_RUN = False
```

当前 `joinquant_strategy.py` 使用 `handle_data` 每根 bar 拉取并执行信号：服务器触发买入信号，JoinQuant 模拟盘就尝试买到目标仓位；服务器触发卖出信号，JoinQuant 模拟盘就尝试清仓卖出。执行后会立即回传快照，收盘后还会再回传一次账户状态。

可执行信号规则：
- 买入只在 A 股连续竞价时间导出给 JoinQuant，当前口径是 `09:30-11:30`、`13:00-15:00`；`09:29` 不会再被当成盘中下单时间。
- 买入必须当前价达到或高于建议入场价，且涨幅低于 9.8%。
- 如果止盈价不高于建议入场价，微信会显示“无有效空间”，并且不会导出 JoinQuant 买入信号。
- 卖出必须先确认 JoinQuant 同步持仓里已有该股票；未持仓股票不会导出卖出计划。
- 卖出每轮由服务器风控重新确认；最新信号里没有卖出，JoinQuant 就不会按历史计划卖出。
- T+1、停牌、涨跌停、休市、撮合失败由 JoinQuant 模拟盘处理，并通过执行回报回传。

如果服务器前面有 HTTPS 反向代理，就把 URL 换成 HTTPS 域名。

## 页面和报告

持仓网页：

```text
http://你的服务器IP:8000
```

JoinQuant readiness 报告：

```bash
ls output/joinquant_readiness_*.md
cat output/joinquant_readiness_$(date +%Y%m%d).md
```

JoinQuant 健康检查：

```bash
bash run_ubuntu.sh health
cat output/joinquant_health_$(date +%Y%m%d).md
```

`stock-joinquant-health.timer` 会每 5 分钟运行一次。它会检查信号文件、账户快照、今日 API 拉取/回传次数、失败订单原因、持仓一致性和稳定性评分；盘中发现信号/快照超时、文件异常、API 异常、持仓不一致或失败订单过多时，会通过企业微信发送去重后的异常报警。非交易时段如果只是信号/快照过期，只写健康报告，不反复推微信。`stock-notify-retry.timer` 会每 5 分钟重试失败的企业微信推送。

ML 基础复盘报告：
```bash
cat output/ml_signal_review.md
```

当前 ML 只用于样本采集、基础复盘和信号级回测，不训练模型，也不会影响 JoinQuant 买入、卖出或仓位。

ML 样本日志：
```bash
tail -n 5 cache/ml/signal_samples.jsonl
```

本地信号回测：
```bash
bash run_ubuntu.sh backtest
cat output/backtest_report.md
head output/backtest_trades.csv
```

第一版回测默认读取 `cache/ml/signal_samples.jsonl`，如果没有该文件则读取 `cache/joinquant/signals.json`。它是信号级轻量回测：基于已生成的 JoinQuant/ML 信号模拟买卖、手续费、印花税、T+1、止盈止损和仓位限制，输出总收益、最大回撤、交易次数、胜率、未平仓数量和交易明细。

支持天数取决于输入文件里已经积累的信号天数：如果只有今天的 `signals.json`，就只能回测今天这一批；如果 `signal_samples.jsonl` 积累了 30/180 个交易日，就能覆盖对应区间。该信号级入口不自动下载历史行情，也不会按过去 6 个月逐日重跑全市场策略。

独立完整历史回测框架使用 `historical_backtest.py` 和 `cache/backtest/history.db`，通过 `bash run_ubuntu.sh historical-backtest-validate ...` 校验、`bash run_ubuntu.sh historical-backtest ...` 运行；两个入口均为手动命令，没有 systemd timer。框架当前仅本地 `implemented`，服务器 `not deployed`，真实 6 个月/1 年 strict 数据 `not observed / not validated`。`price_core` 输出始终是代理证据，不能满足 Batch G。

盘后信号追踪：
```bash
cat cache/signal_watchlist.json
```

盘后复盘会基于已推送信号补充 D+N 快照、当日高低收、是否入场、是否触及止盈/止损、最大浮盈、最大回撤，并输出按模式/题材/市场状态的轻量策略质量统计。

## 修改配置

```bash
nano /opt/stock-analysis/stock-analysis.env
bash /opt/stock-analysis/run_ubuntu.sh restart-all
```
## 2026-07-09 阶段 1 补齐后的运维命令

健康检查现在会同时生成和读取这些文件：

- `cache/joinquant/api_events.jsonl`：JoinQuant 拉信号、访问 latest、回传快照和异常请求日志。
- `cache/joinquant/health_history.jsonl`：每次健康检查结果，用于连续交易日稳定性观察。
- `cache/notify_failed_queue.jsonl`：企业微信发送失败后的重试队列。
- `output/joinquant_health_YYYYMMDD.md`：手机可读的健康日报，包含 API 拉取/回传次数、失败原因拆分、持仓一致性和稳定性评分。

常用命令：

```bash
cd /opt/stock-analysis
bash run_ubuntu.sh health
bash run_ubuntu.sh notify-retry
bash run_ubuntu.sh status-all
```

`stock-joinquant-health.timer` 每 5 分钟运行健康检查；`stock-notify-retry.timer` 每 5 分钟重试失败的企业微信推送。

更新到包含新 timer 的版本后，需要刷新 systemd：

```bash
cd /opt/stock-analysis
git pull origin main
bash run_ubuntu.sh install --skip-install --skip-ocr
bash run_ubuntu.sh status-all
```
