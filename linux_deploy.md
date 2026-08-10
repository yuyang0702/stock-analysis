# Linux 部署说明

> 当前项目规划以 `docs/project_roadmap.md` 为准。本文只保留服务器执行步骤。

> 2026-08-06 最新部署检查点：Batch A Tasks 1–8、Batch B Tasks 1–7 与 Batch C ML-7 Tasks 4–10 已合并、推送并随 `5d2c4a2018ca95b9febd6751b4964fec507fe1bc` 部署到 `/opt/stock-analysis`，正式交易库为 schema 12。服务器 Python 3.12.3 的既有虚拟环境已安装 `scikit-learn==1.9.0` 和 `joblib==1.5.3`；Linux 全量测试 1003/1003、`ledger-check` 健康/可写、迁移后在线备份完整性、环境文件哈希不变、三个核心服务与通知 timer active 均已核验。`stock-notify-retry.timer` 已路由到 SQLite `notification_worker.py --once`，部署后 outbox 为 sent=3、pending/dead/gap=0。
>
> 上述证据只支持 `implemented / committed / deployed`。新交易语义仍是 `not observed / not validated`。Batch C Task 11 已完成；Task 12 只完成代码与依赖部署，ML 保持 `enabled=0 / max_level=0 / dataset_configured=no`，标签、训练及 ML/history 备份 timer 未启用。当前没有真实一年/365 天 strict 数据、可信/可批准或活动模型、人工审批或服务器 L0 证据。JoinQuant 网站模板未在本次部署中修改。

> 2026-07-15 schema 7/模板 `2026-07-15.1-execution-state-recovery` 增量已随 `e2ce5b5` 推送并部署服务器：schema 6 备份完整性、Linux 324/324 测试、schema 7 `ledger-check`、配置哈希和三个服务状态均通过。用户报告已手动更新 JoinQuant 网站模板；新模板尚待交易日快照观察和验证。下列命令仍是未来部署操作说明，不代表可在没有当次授权时再次执行。

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
git status --short
git add <本次确认要提交的文件路径>
git commit -m "说明这次修改"
git push
```

如果是 Codex 帮忙修改代码，检查可在本地执行；提交和推送只有在用户当次明确授权后才能执行。

### 服务器更新到最新版

服务器固定目录：

```bash
cd /opt/stock-analysis
```

下面的快捷更新只适用于已经确认不含 SQLite schema、JoinQuant 模板、systemd 或配置变化的普通版本；任何这类变化都必须执行对应专项 runbook。2026-08-06 schema 12 首次部署已经完成，后续文档-only 或普通代码更新可在确认无迁移影响后使用本快捷路径。

经当次授权后更新普通版本：

```bash
git pull --ff-only origin main
chmod +x run_ubuntu.sh
```

随后只重启当次明确授权的服务；不得默认运行菜单中的“重启全部服务”或 `restart-all`。

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
bash run_ubuntu.sh install
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
stock-ml-labels.service/.timer
stock-ml-train.service/.timer
stock-ml-backup.service/.timer
stock-history-backup.service/.timer
stock-strategy-compare.service/.timer
stock-trading-backup.timer
stock-trading-backup-drill.timer
```

`stock-ml-report.timer` 是旧版复盘单元；新版本安装时会禁用并移除它，改由 `stock-strategy-compare.timer` 生成对照报告。ML 标签、训练、ML/历史库备份和对照报告 timer 只做有界离线维护，不批准、激活、发布或改变交易权限。2026-08-06 部署后，`stock-strategy-compare.timer` 延续部署前既有 active 状态；`stock-ml-labels.timer`、`stock-ml-train.timer`、`stock-ml-backup.timer` 和 `stock-history-backup.timer` 均未启用。后续启用任何 ML timer 必须另行确认目标单元、数据集和维护窗口。

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

经当次授权后，只重启读取该配置的策略扫描服务：

```bash
sudo systemctl restart stock-analysis.service
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

`unlock` 是交互式向导，不是强制解锁：它会执行一次完整对账，要求最近两个全量一致结果来自不同新鲜快照，先确认关闭 `KILL_SWITCH`，再二次确认恢复买入。`kill-switch-off` 不会自动把 `buy_enabled` 改回 1。2026-08-06 最后部署证据的服务器正式库为 schema 12；当前部署没有授权或执行新的人工解锁演练，交易控制实时值仍须单独只读核验。

前台调试：

```bash
bash run_ubuntu.sh run-strategy
bash run_ubuntu.sh run-web
bash run_ubuntu.sh run-joinquant-api
bash run_ubuntu.sh sync-joinquant
bash run_ubuntu.sh health
bash run_ubuntu.sh notify-retry
bash run_ubuntu.sh readiness
bash run_ubuntu.sh ml-labels
bash run_ubuntu.sh ml-train
bash run_ubuntu.sh ml-model-status
bash run_ubuntu.sh ml-backup --kind ml
bash run_ubuntu.sh ml-restore-check --kind ml
bash run_ubuntu.sh ml-retention-dry-run
bash run_ubuntu.sh ml-retention-apply
bash run_ubuntu.sh strategy-compare
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

Batch C 已部署的代码提供以下受控维护单元和命令：标签 `16:10`、对照报告 `16:25`、每周五训练 `18:00`、ML/strict 历史库备份 `19:00`。除延续既有 active 状态的对照报告 timer 外，其余 ML timer 当前均未启用。这些单元只能登记 challenger 和生成报告，不能批准、激活、发布或改变买卖权限；`ml-restore-check`、`ml-retention-dry-run` 和 `ml-retention-apply` 仍是人工命令，不能把它们当作自动恢复或自动清理已经上线。

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

## 历史 runbook：schema 10→11 小资金执行部署

> 本节记录 2026-08-06 schema 12 部署前所使用的分阶段迁移门槛，供审计和未来迁移设计参考。当前服务器已经是 schema 12，不得把本节命令重新用于当前正式库；新的 schema 迁移必须按目标版本另行编写并授权。

本节仅在用户当次明确授权目标提交、服务器、备份、测试、迁移和具体服务重启后执行。JoinQuant 网站编辑器更新需要另一项明确授权。部署不得显示私有配置内容、打印 Token/Webhook/私钥等变量值，或修改任何凭据。

部署前必须确认：目标提交已经进入 `origin/main`；服务器工作树干净；正式库仍为 schema 10；维护窗口内允许暂停全部项目服务、timer 及已经触发的 oneshot unit；迁移前备份目录位于项目外。先记录部署前 enabled/active 单元清单，完成后只恢复该清单中且本次获准恢复的单元。任一条件不满足即停止。

先设置当次明确授权的完整提交 SHA，并执行失败即退出的前置门；占位符未替换、远端不匹配或工作树不干净时不得继续：

```bash
set -euo pipefail
cd /opt/stock-analysis
TARGET_SHA='<当次授权的完整提交SHA>'
test "$TARGET_SHA" != '<当次授权的完整提交SHA>' || { echo 'STOP: TARGET_SHA 未设置'; exit 1; }
git fetch origin main
test "$(git rev-parse origin/main)" = "$TARGET_SHA" || { echo 'STOP: origin/main 不是授权提交'; exit 1; }
test "$(git branch --show-current)" = 'main' || { echo 'STOP: 当前分支不是 main'; exit 1; }
git cat-file -e "$TARGET_SHA^{commit}"
test -z "$(git status --porcelain --untracked-files=all)" || { echo 'STOP: 工作树不干净'; exit 1; }
printf '%s\n' "$TARGET_SHA" > /tmp/stock-target-sha-schema11.txt
```

### 1. 冻结私有配置并创建预备在线备份

先用服务器当前版本备份，尚未拉取新代码：

```bash
set -euo pipefail
cd /opt/stock-analysis
test -z "$(git status --porcelain --untracked-files=all)" || { echo 'STOP: 工作树不干净'; exit 1; }
git rev-parse HEAD
umask 077
sha256sum stock-analysis.env > /tmp/stock-env-before-schema11.sha256
bash run_ubuntu.sh backup
bash run_ubuntu.sh backup-status
```

这是第一个人工确认点。预备备份必须明确为 schema 10、`integrity_check=ok`，且包含完整核心表计数。它防止停机步骤本身失败，但不是迁移回滚的最终备份；最终备份必须在下一步确认所有写入者停止后重新创建。不得继续使用缺少清单、哈希不一致或位于项目目录内的备份。未逐项确认前不要复制执行下一块。

### 2. 记录单元状态、停止全部写入者、拉取指定提交并运行 Linux 测试

先记录状态，再停止 timer、三个常驻服务和 timer 可能已经触发的全部 oneshot service，避免旧进程在拉取或迁移时继续写正式库：

```bash
set -euo pipefail
systemctl list-unit-files 'stock-*.timer' --state=enabled --no-legend --plain | awk '{print $1}' | sort -u > /tmp/stock-enabled-timers-before-schema11.txt
systemctl list-units 'stock-*' --state=active,activating --no-legend --plain | awk '{print $1}' | sort -u > /tmp/stock-active-before-schema11.txt
cat /tmp/stock-enabled-timers-before-schema11.txt
cat /tmp/stock-active-before-schema11.txt
bash run_ubuntu.sh stop-all
sudo systemctl stop \
  stock-joinquant-sync.service stock-joinquant-health.service \
  stock-notify-retry.service stock-joinquant-readiness.service \
  stock-ml-report.service stock-global-context.service \
  stock-sector-context.service stock-strategy-compare.service \
  stock-strategy-compare-weekly.service stock-trading-backup.service \
  stock-trading-backup-drill.service
if systemctl list-units 'stock-*' --state=active,activating --no-legend --plain | grep -q .; then
  systemctl list-units 'stock-*' --state=active,activating
  echo 'STOP: 仍有项目 writer active/activating'
  exit 1
fi
```

只有上一块以 0 退出且两个部署前清单已人工保存后，才可创建最终静止备份：

```bash
set -euo pipefail
bash run_ubuntu.sh backup
bash run_ubuntu.sh backup-status
```

这是第二个人工确认点。必须确认这份停止全部 writer 后的备份仍为 schema 10、`integrity_check=ok`、SHA-256 与核心表计数完整，并把它标记为实际回滚源。确认后才可更新代码；不要把备份和拉取放在同一个可连续执行的命令块：

```bash
set -euo pipefail
TARGET_SHA="$(cat /tmp/stock-target-sha-schema11.txt)"
git cat-file -e "$TARGET_SHA^{commit}"
git merge --ff-only "$TARGET_SHA"
test "$(git rev-parse HEAD)" = "$TARGET_SHA" || { echo 'STOP: HEAD 不是授权提交'; exit 1; }
test -z "$(git status --porcelain --untracked-files=all)" || { echo 'STOP: 更新后工作树不干净'; exit 1; }
```

停止后状态检查不得仍显示任何项目单元处于 `active/activating`；否则不得创建最终备份、拉取或迁移。停止后的第二份备份才是 schema 10 迁移回滚依据，必须再次确认 `integrity_check=ok`、SHA-256 和完整表计数。`stop-all` 本身不覆盖已经启动的 oneshot，因此不能单独作为停机证据。

`HEAD` 必须等于当次授权的目标 SHA。随后只运行代码编译和使用隔离临时数据库的测试，不手工指定正式交易库：

```bash
set -euo pipefail
.venv/bin/python -m py_compile \
  config.py execution_contracts.py position_sizing.py execution_admission.py \
  pre_trade_check.py trading_store.py trading_backup.py joinquant_sync.py \
  order_ledger.py reconciliation.py trading_control.py joinquant_exporter.py \
  joinquant_signal_server.py a_share_strategy.py holdings_web.py \
  joinquant_strategy.py exit_policy.py gap_reentry.py historical_backtest.py
bash run_ubuntu.sh test
```

任一编译或测试失败都不得迁移正式库或启动服务。

### 3. 迁移、检查、迁移后备份和隔离恢复

`ledger-check` 会初始化正式库、执行幂等 migration 并做可写探针，因此它是 schema 10→11 的实际迁移步骤：

```bash
set -euo pipefail
bash run_ubuntu.sh ledger-check
bash run_ubuntu.sh backup
bash run_ubuntu.sh backup-status
bash run_ubuntu.sh backup-drill
sha256sum -c /tmp/stock-env-before-schema11.sha256
```

必须同时满足：`ledger-check` 报 schema 11、健康和可写探针成功；迁移后备份报 schema 11、`integrity_check=ok` 且包含全部新增表；隔离恢复演练成功；私有配置哈希未变。失败时保持服务停止，保留现场，不得只回滚 Git 后继续使用 schema 11 主库。恢复 schema 10 备份和回滚代码必须另行授权并在停机状态完成。

### 4. JoinQuant 单独授权和启动前核验

网站模板只能在单独授权后更新，并应在服务器交易进程仍停止时完成，避免旧模板先拉取新契约。更新时必须保留原 `SIGNAL_URL`、`SNAPSHOT_URL`、`SYNC_TOKEN` 和运行配置，不得显示或复制 Token 到日志、文档或聊天。编辑器代码应与仓库 `joinquant_strategy.py` 一致，并显示模板版本 `2026-08-11.1-runtime-isolation`；只有 `context.run_params.type == "sim_trade"` 才允许访问生产接口，服务器还会复核运行类型、模板和协议请求头。

没有网站更新授权、编辑器保存失败或版本无法确认时，不得启动交易相关服务；服务器迁移只能记为待完成部署，不能写成端到端 `deployed`。

### 5. 受控启动与端到端验收

只有前述证据和网站模板版本全部通过，才可按当次授权恢复部署前已启用且明确获准的单元。启动必须安排在 A 股非连续竞价时段，或已有明确人工停买状态。先运行 `bash run_ubuntu.sh trading-status` 保存控制值、原因、更新时间、自动恢复 owner、最近控制事件和最新对账基线。不要运行 `start-all`，因为它会无条件启动全部 timer，可能改变部署前运行配置。

先只恢复持仓 Web 和 JoinQuant 信号/快照接收服务，保持策略扫描器停止：

```bash
set -euo pipefail
for unit in stock-holdings-web.service stock-joinquant-signal.service; do
  grep -Fxq "$unit" /tmp/stock-active-before-schema11.txt || { echo "STOP: $unit 部署前未 active"; exit 1; }
done
sudo systemctl start \
  stock-holdings-web.service stock-joinquant-signal.service
systemctl is-active \
  stock-holdings-web.service stock-joinquant-signal.service
git status --short --branch
git rev-parse HEAD
```

等待一份新的完整账户快照，核对它回传模板版本 `2026-08-11.1-runtime-isolation`、`runtime_mode=sim_trade`、协议版本 1、账户 scope、snapshot 哈希和 current broker 子表均完整且一致。完整快照仍会执行既有对账控制：异常可自动停买或打开 kill switch，满足严格条件的 reconciliation-owned 停买也可能自动恢复。每收到一份验证快照都必须再次运行 `bash run_ubuntu.sh trading-status`，把控制值、reason/updated_at、自动恢复 owner、最近控制事件和最新对账与启动前基线逐项比较。版本不一致、快照不完整、仍在新鲜窗内的旧快照、控制状态意外恢复或 owner/事件无法解释时，均不得启动扫描器。不得靠启动扫描器来“试一下”模板。

完成上述核验后，才可启动获准的策略扫描服务：

```bash
set -euo pipefail
grep -Fxq stock-analysis.service /tmp/stock-active-before-schema11.txt || { echo 'STOP: stock-analysis.service 部署前未 active'; exit 1; }
sudo systemctl start stock-analysis.service
systemctl is-active stock-analysis.service
```

timer 只能逐个按部署前记录和当次授权恢复，并在恢复后逐个检查；不得借本次部署新启用此前 disabled 的 timer。

随后只检查授权范围内的服务状态、启动日志、schema 11 健康、新快照对账和最新 `trading-status`，并确认精确 `target_qty` 契约与服务器预期一致。任何异常都应停止尚未恢复的单元并保留证据；部署人员不得人工覆盖控制位或修改环境配置。既有对账代码产生的自动安全转换必须保留审计证据，且只有结果与启动前预期一致时才能继续恢复其他单元。

服务器代码、正式库和网站模板均完成上述核验后才可标记端到端 `deployed`；真实交易日证据出现前仍为 `not observed / not validated`。

`RISK_MODE=observe` 与 `enforce` 均受代码支持，但保留现有环境意味着服务器继续使用部署前的实际值；`observe` 只把本批新增经济性软门降为 warning，既有硬安全块仍强制。把它改为 `enforce` 是独立配置变更，必须明确授权、先记录旧值的非敏感状态并完成代表性模拟盘观察，不能由迁移脚本暗改。

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

`stock-joinquant-health.timer` 会每 5 分钟运行一次。它会检查信号文件、账户快照、今日 API 拉取/回传次数、失败订单原因、持仓一致性和稳定性评分；盘中发现信号/快照超时、文件异常、API 异常、持仓不一致或失败订单过多时，会通过企业微信发送去重后的异常报警。非交易时段如果只是信号/快照过期，只写健康报告，不反复推微信。当前 schema 12 服务器的 `stock-notify-retry.timer` 实际执行 `notification_worker.py --once` 消费 SQLite outbox；旧 JSON 队列只允许显式 legacy audit。

ML 基础复盘报告：
```bash
cat output/ml_signal_review.md
```

当前服务器已具备 strict 导入、标签、五头训练、治理、L0 旁路和维护命令，依赖也已安装；但 ML 明确保持关闭、数据集未配置，不训练模型，也不会影响 JoinQuant 买入、卖出或仓位。没有可信/活动模型、人工审批或服务器 L0 证据。未经 Task 12 后续单独授权，不得配置真实数据集、启用 L0、启用训练/标签 timer 或修改 ML 权限。

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

独立完整历史回测框架使用 `historical_backtest.py` 和 `cache/backtest/history.db`，通过 `bash run_ubuntu.sh historical-backtest-validate ...` 校验、`bash run_ubuntu.sh historical-backtest ...` 运行；两个入口均为手动命令，没有 systemd timer。框架已有服务器部署历史，但真实 6 个月/1 年 strict 数据尚未运行或验证。`price_core` 输出始终是代理证据，不能满足 Batch G。

盘后信号追踪：
```bash
cat cache/signal_watchlist.json
```

盘后复盘会基于已推送信号补充 D+N 快照、当日高低收、是否入场、是否触及止盈/止损、最大浮盈、最大回撤，并输出按模式/题材/市场状态的轻量策略质量统计。

## 修改配置

```bash
nano /opt/stock-analysis/stock-analysis.env
```

保存后先记录部署前单元状态，再只重启本次配置实际影响且已明确授权的服务；不得用 `restart-all` 代替影响分析，因为它会改变此前 inactive/disabled 的单元状态。
## 2026-07-09 阶段 1 补齐后的运维命令

健康检查现在会同时生成和读取这些文件：

- `cache/joinquant/api_events.jsonl`：JoinQuant 拉信号、访问 latest、回传快照和异常请求日志。
- `cache/joinquant/health_history.jsonl`：每次健康检查结果，用于连续交易日稳定性观察。
- `cache/notify_failed_queue.jsonl`：schema 10 历史基线的有界失败队列；当前 schema 12 使用 SQLite outbox，旧 JSON 只读且仅允许显式审计。
- `output/joinquant_health_YYYYMMDD.md`：手机可读的健康日报，包含 API 拉取/回传次数、失败原因拆分、持仓一致性和稳定性评分。

常用命令：

```bash
cd /opt/stock-analysis
bash run_ubuntu.sh health
bash run_ubuntu.sh notify-retry
bash run_ubuntu.sh status-all
```

`stock-joinquant-health.timer` 每 5 分钟运行健康检查；当前 `stock-notify-retry.timer` 保留兼容 unit 名，但执行 SQLite outbox worker。

本段是 2026-07-09 的历史 timer 说明。已配置服务器不得用 `bash run_ubuntu.sh install --skip-install` 刷新 systemd：当前脚本会重写环境文件、在未传参数时生成新 Token，并可能改变既有配置。未来 timer 更新必须使用经专项测试、明确保证原样保留私有环境的安全刷新入口，或由当次授权的部署步骤逐个写入并核验 unit；该入口实现前停止操作。
