# Linux 部署检查清单

> 当前项目规划以 `docs/project_roadmap.md` 为准。本文只保留部署核对项；本地模拟盘已废弃并默认停用。

## 1. 上传文件

服务器目录建议：

```bash
/opt/stock-analysis
```

必须包含：

```text
run_ubuntu.sh
a_share_strategy.py
paper_trading.py
risk_engine.py
strategy_profile.py
notifier.py
config.py
holdings_web.py
joinquant_exporter.py
joinquant_signal_server.py
joinquant_sync.py
joinquant_health.py
joinquant_readiness_report.py
joinquant_strategy.py
notify_retry.py
ml_dataset.py
backtest_engine.py
requirements.txt
tests/
```

`paper_trading.py` 目前仅作为历史兼容文件保留，主流程不会启用本地模拟盘。

## 2. 首次安装

```bash
cd /opt/stock-analysis
bash run_ubuntu.sh install \
  --webhook '你的企业微信机器人URL' \
  --token '你自己设置的长随机token'
```

## 3. 检查状态

```bash
bash run_ubuntu.sh status-all
bash run_ubuntu.sh show-env
```

## 4. 查看日志

```bash
bash run_ubuntu.sh logs-strategy
bash run_ubuntu.sh logs-web
bash run_ubuntu.sh logs-joinquant
```

## 5. 聚宽配置

复制 `joinquant_strategy.py` 到聚宽，并填：

```python
SIGNAL_URL = "http://你的服务器IP:8010/joinquant/signals"
SNAPSHOT_URL = "http://你的服务器IP:8010/joinquant/account_snapshot"
SYNC_TOKEN = "run_ubuntu.sh install 时传入的 token"
DRY_RUN = False
```

## 6. 验证

```bash
bash run_ubuntu.sh test
bash run_ubuntu.sh health
bash run_ubuntu.sh notify-retry
bash run_ubuntu.sh readiness
```

健康检查报告：

```bash
cat output/joinquant_health_$(date +%Y%m%d).md
```

持仓网页：

```text
http://你的服务器IP:8000
```
