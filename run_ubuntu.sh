#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${SCRIPT_DIR}"
VENV_DIR="${APP_DIR}/.venv"
ENV_FILE="${APP_DIR}/stock-analysis.env"
SYSTEMD_DIR="/etc/systemd/system"

STRATEGY_SERVICE="stock-analysis.service"
WEB_SERVICE="stock-holdings-web.service"
JQ_SIGNAL_SERVICE="stock-joinquant-signal.service"
JQ_SYNC_SERVICE="stock-joinquant-sync.service"
JQ_SYNC_TIMER="stock-joinquant-sync.timer"
JQ_READINESS_SERVICE="stock-joinquant-readiness.service"
JQ_READINESS_TIMER="stock-joinquant-readiness.timer"
JQ_HEALTH_SERVICE="stock-joinquant-health.service"
JQ_HEALTH_TIMER="stock-joinquant-health.timer"
NOTIFY_RETRY_SERVICE="stock-notify-retry.service"
NOTIFY_RETRY_TIMER="stock-notify-retry.timer"
ML_REPORT_SERVICE="stock-ml-report.service"
ML_REPORT_TIMER="stock-ml-report.timer"
GLOBAL_CONTEXT_SERVICE="stock-global-context.service"
GLOBAL_CONTEXT_TIMER="stock-global-context.timer"
SECTOR_CONTEXT_SERVICE="stock-sector-context.service"
SECTOR_CONTEXT_TIMER="stock-sector-context.timer"
STRATEGY_COMPARE_SERVICE="stock-strategy-compare.service"
STRATEGY_COMPARE_TIMER="stock-strategy-compare.timer"
STRATEGY_COMPARE_WEEKLY_SERVICE="stock-strategy-compare-weekly.service"
STRATEGY_COMPARE_WEEKLY_TIMER="stock-strategy-compare-weekly.timer"
TRADING_BACKUP_SERVICE="stock-trading-backup.service"
TRADING_BACKUP_TIMER="stock-trading-backup.timer"
TRADING_BACKUP_DRILL_SERVICE="stock-trading-backup-drill.service"
TRADING_BACKUP_DRILL_TIMER="stock-trading-backup-drill.timer"
ALL_SERVICES=("${STRATEGY_SERVICE}" "${WEB_SERVICE}" "${JQ_SIGNAL_SERVICE}")
ALL_TIMERS=("${JQ_SYNC_TIMER}" "${JQ_HEALTH_TIMER}" "${NOTIFY_RETRY_TIMER}" "${JQ_READINESS_TIMER}" "${ML_REPORT_TIMER}" "${GLOBAL_CONTEXT_TIMER}" "${SECTOR_CONTEXT_TIMER}" "${STRATEGY_COMPARE_TIMER}" "${STRATEGY_COMPARE_WEEKLY_TIMER}" "${TRADING_BACKUP_TIMER}" "${TRADING_BACKUP_DRILL_TIMER}")

log() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  bash run_ubuntu.sh
  bash run_ubuntu.sh install [--webhook URL] [--token TOKEN] [--cash NUM] [--web-port NUM] [--signal-port NUM] [--skip-install] [--skip-ocr] [--no-start]
  bash run_ubuntu.sh start-all|stop-all|restart-all|status-all
  bash run_ubuntu.sh logs-strategy|logs-web|logs-joinquant
  bash run_ubuntu.sh run-strategy|run-web|run-joinquant-api|sync-joinquant|ledger-check|health|notify-retry|readiness|ml-report|global-context|sector-context|strategy-compare|strategy-compare-weekly|backtest|backup|backup-drill|backup-status|test|show-env

First deploy:
  bash run_ubuntu.sh install --webhook 'YOUR_WECOM_WEBHOOK' --token 'YOUR_LONG_RANDOM_TOKEN'
EOF
}

show_menu() {
  cat <<EOF

========== A股策略服务器菜单 ==========
目录：${APP_DIR}

  1) 查看服务状态
  2) 重启全部服务
  3) 启动全部服务
  4) 停止全部服务
  5) 查看策略日志
  6) 查看 JoinQuant API 日志
  7) 前台运行一次策略
  8) 前台启动 JoinQuant API
  9) 同步 JoinQuant 持仓
 10) 生成 JoinQuant 健康检查
 11) 重试失败微信推送
 12) 生成 readiness 报告
 13) 生成 ML 复盘报告
 14) 更新美日韩市场上下文
 15) 生成策略对照复盘
 16) 推送策略对照周报
 17) 运行本地信号回测
 18) 查看当前配置
 19) 运行测试
 20) 首次安装/重写配置
 21) 立即执行SQLite备份
 22) 执行SQLite恢复演练
 23) 查看SQLite备份状态
  h) 帮助
  q) 退出

EOF
}

menu_install() {
  local webhook=""
  local token=""
  local cash="100000"
  local web_port="8000"
  local signal_port="8010"

  read -r -p "企业微信机器人 URL（可留空）： " webhook
  read -r -p "JoinQuant token（留空自动生成）： " token
  read -r -p "初始资金，默认 100000： " cash
  read -r -p "持仓网页端口，默认 8000： " web_port
  read -r -p "JoinQuant API 端口，默认 8010： " signal_port
  read -r -p "是否跳过依赖安装？代码更新选 y，首次部署选 n。[y/N] " skip_install
  cash="${cash:-100000}"
  web_port="${web_port:-8000}"
  signal_port="${signal_port:-8010}"

  printf '\n将执行 install 并写入 systemd 配置。确认继续？[y/N] '
  local answer
  read -r answer
  if [[ "${answer}" =~ ^[Yy]$ ]]; then
    if [[ "${skip_install}" =~ ^[Yy]$ ]]; then
      install_all --webhook "${webhook}" --token "${token}" --cash "${cash}" --web-port "${web_port}" --signal-port "${signal_port}" --skip-install
    else
      install_all --webhook "${webhook}" --token "${token}" --cash "${cash}" --web-port "${web_port}" --signal-port "${signal_port}"
    fi
  else
    warn "已取消安装/重写配置"
  fi
}

menu_loop() {
  local choice
  while true; do
    show_menu
    read -r -p "请输入序号： " choice
    case "${choice}" in
      1) handle_command status-all ;;
      2) handle_command restart-all ;;
      3) handle_command start-all ;;
      4) handle_command stop-all ;;
      5) handle_command logs-strategy ;;
      6) handle_command logs-joinquant ;;
      7) handle_command run-strategy ;;
      8) handle_command run-joinquant-api ;;
      9) handle_command sync-joinquant ;;
      10) handle_command health ;;
      11) handle_command notify-retry ;;
      12) handle_command readiness ;;
      13) handle_command ml-report ;;
      14) handle_command global-context ;;
      15) handle_command strategy-compare ;;
      16) handle_command strategy-compare-weekly ;;
      17) handle_command backtest ;;
      18) handle_command show-env ;;
      19) handle_command test ;;
      20) menu_install ;;
      21) handle_command backup ;;
      22) handle_command backup-drill ;;
      23) handle_command backup-status ;;
      h|H) usage ;;
      q|Q|"") break ;;
      *) warn "未知选项：${choice}" ;;
    esac
    printf '\n'
    read -r -p "按回车返回菜单..." _
  done
}

python_bin() {
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    printf '%s\n' "${VENV_DIR}/bin/python"
  else
    command -v python3
  fi
}

load_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
  fi
}

generate_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    python3 -c 'import secrets; print(secrets.token_hex(24))'
  fi
}

escape_sed_value() {
  printf '%s' "$1" | sed -e 's/[\/&|]/\\&/g'
}

set_env() {
  local key="$1"
  local value="$2"
  local escaped
  escaped="$(escape_sed_value "${value}")"
  touch "${ENV_FILE}"
  if grep -q "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${escaped}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
}

env_value() {
  local key="$1"
  local fallback="$2"
  if [[ -f "${ENV_FILE}" ]]; then
    local value
    value="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- || true)"
    [[ -n "${value}" ]] && printf '%s\n' "${value}" && return
  fi
  printf '%s\n' "${fallback}"
}

require_project_files() {
  for file in \
    a_share_strategy.py holdings_web.py joinquant_signal_server.py joinquant_sync.py \
    joinquant_health.py notify_retry.py backtest_engine.py trading_backup.py \
    joinquant_readiness_report.py ml_dataset.py global_market_context.py strategy_compare_report.py requirements.txt; do
    [[ -f "${APP_DIR}/${file}" ]] || die "${file} not found in ${APP_DIR}"
  done
}

install_system_packages() {
  local install_ocr="$1"
  log "Installing system packages"
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip git curl openssl
  if [[ "${install_ocr}" == "yes" ]]; then
    sudo apt-get install -y tesseract-ocr tesseract-ocr-chi-sim
  else
    warn "Skipped OCR packages"
  fi
}

install_python_deps() {
  log "Creating Python venv: ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  python -m pip install --upgrade pip
  pip install -r "${APP_DIR}/requirements.txt"
}

write_env_file() {
  local webhook="$1"
  local token="$2"
  local cash="$3"
  local web_port="$4"

  log "Writing environment file: ${ENV_FILE}"
  set_env "WECOM_WEBHOOK_URL" "${webhook}"
  set_env "SCAN_MODE" "auto"
  set_env "SCAN_TOP" "10"
  set_env "SCAN_INTERVAL" "300"
  set_env "SCAN_JITTER_SEC" "30"
  set_env "MIN_PRICE" "1.5"
  set_env "MIN_AMOUNT" "50000000"
  set_env "SKIP_PRESSURE" "0"
  set_env "SKIP_LHB" "0"
  set_env "SKIP_NEWS" "0"
  set_env "STOCK_NEWS_LIMIT" "5"
  set_env "NOTICE_DAYS_BACK" "2"
  set_env "MAX_CANDIDATES_FOR_NEWS" "8"
  set_env "STOCK_SCAN_TIMEOUT" "12"
  set_env "SIGNAL_WATCHLIST_DAYS" "10"
  set_env "NOTIFY_ENABLE" "1"
  set_env "NOTIFY_ONLY_SIGNAL" "0"
  set_env "NOTIFY_TOP_N" "8"
  set_env "NOTIFY_COOLDOWN_SEC" "1800"
  set_env "NOTIFY_MIN_SCORE" "75"
  set_env "NOTIFY_NON_TRADING_DAY" "0"
  set_env "A_SHARE_HOLIDAYS" ""
  set_env "WECOM_TIMEOUT_SEC" "10"
  set_env "PAPER_TRADE_ENABLE" "0"
  set_env "PAPER_TRADE_CASH" "${cash}"
  set_env "PAPER_TRADE_COMMISSION_RATE" "0.0003"
  set_env "PAPER_TRADE_STAMP_TAX_RATE" "0.001"
  set_env "PAPER_TRADE_SLIPPAGE_PCT" "0.001"
  set_env "PAPER_TRADE_COOLDOWN_DAYS" "3"
  set_env "PAPER_TRADE_MAX_POSITIONS" "5"
  set_env "PAPER_TRADE_MAX_POSITION_PCT" "20"
  set_env "PAPER_TRADE_MAX_TOTAL_POSITION_PCT" "80"
  set_env "JOINQUANT_ENABLE" "1"
  set_env "JOINQUANT_SYNC_TOKEN" "${token}"
  set_env "JOINQUANT_DRY_RUN" "false"
  set_env "JOINQUANT_ENFORCE_HEALTH_GATE" "1"
  set_env "JOINQUANT_PORTFOLIO_RISK_ENABLE" "1"
  set_env "JOINQUANT_TRADABILITY_FILTER_ENABLE" "1"
  set_env "JOINQUANT_REGIME_CONFIRM_ENABLE" "1"
  set_env "JOINQUANT_EXIT_COOLDOWN_ENABLE" "1"
  set_env "JOINQUANT_LAYERED_EXIT_ENABLE" "1"
  set_env "JOINQUANT_MIN_SCORE" "75"
  set_env "JOINQUANT_MAX_SIGNAL_AGE_MIN" "20"
  set_env "JOINQUANT_MAX_POSITIONS" "5"
  set_env "JOINQUANT_MAX_TOTAL_POSITION_PCT" "80"
  set_env "JOINQUANT_REQUEST_TIMEOUT" "8"
  set_env "JOINQUANT_HEALTH_SIGNAL_MAX_AGE_MIN" "30"
  set_env "JOINQUANT_HEALTH_SNAPSHOT_MAX_AGE_MIN" "15"
  set_env "JOINQUANT_HEALTH_FAILED_ORDER_LIMIT" "3"
  set_env "RISK_MODE" "observe"
  set_env "MAX_SINGLE_POSITION_PCT" "30"
  set_env "MAX_TOTAL_POSITION_PCT" "95"
  set_env "MIN_CASH_RESERVE_PCT" "5"
  set_env "MAX_SECTOR_EXPOSURE_PCT" "60"
  set_env "MAX_NEW_POSITIONS_PER_DAY" "10"
  set_env "MAX_ORDERS_PER_DAY" "50"
  set_env "MAX_DAILY_TURNOVER_PCT" "200"
  set_env "DAILY_LOSS_WARN_PCT" "5"
  set_env "ACCOUNT_DRAWDOWN_WARN_PCT" "15"
  set_env "MAX_CONSECUTIVE_ORDER_FAILURES" "5"
  set_env "MAX_CONSECUTIVE_LOSSES" "3"
  set_env "MAX_OPEN_RISK_NORMAL_PCT" "4"
  set_env "MAX_OPEN_RISK_CAUTION_PCT" "2"
  set_env "MAX_INDUSTRY_POSITION_PCT" "25"
  set_env "MAX_THEME_POSITION_PCT" "20"
  set_env "MAX_UNCATEGORIZED_POSITION_PCT" "10"
  set_env "ACCOUNT_SNAPSHOT_MAX_AGE_SEC" "300"
  set_env "SIGNAL_MAX_AGE_SEC" "1200"
  set_env "RECONCILIATION_POSITION_TOLERANCE" "0"
  set_env "TRADING_DB_FILE" "${APP_DIR}/cache/trading/trading.db"
  set_env "TRADING_BACKUP_DIR" "/opt/stock-analysis-backups"
  set_env "TRADING_BACKUP_DAILY_KEEP" "7"
  set_env "TRADING_BACKUP_WEEKLY_KEEP" "4"
  set_env "TRADING_BACKUP_MONTHLY_KEEP" "12"
  set_env "ML_SIGNAL_SAMPLE_FILE" "${APP_DIR}/cache/ml/signal_samples.jsonl"
  set_env "ML_REVIEW_REPORT_FILE" "${APP_DIR}/output/ml_signal_review.md"
  set_env "GLOBAL_MARKET_CONTEXT_FILE" "${APP_DIR}/cache/market/global_context.json"
  set_env "STRATEGY_COMPARE_REPORT_FILE" "${APP_DIR}/output/strategy_compare_report.md"
  set_env "ENABLE_AI" "0"
  set_env "ARK_API_KEY" ""
  set_env "ARK_MODEL" ""
  set_env "ARK_BASE_URL" "https://ark.cn-beijing.volces.com/api/v3"
  set_env "PORTFOLIO_WEB_HOST" "0.0.0.0"
  set_env "PORTFOLIO_WEB_PORT" "${web_port}"
}

write_systemd_units() {
  local signal_port="$1"
  local py
  py="$(python_bin)"

  log "Writing systemd units"
  sudo tee "${SYSTEMD_DIR}/${STRATEGY_SERVICE}" >/dev/null <<EOF
[Unit]
Description=A Share Strategy Scanner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/a_share_strategy.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

  sudo tee "${SYSTEMD_DIR}/${WEB_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Stock Holdings Web
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/holdings_web.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

  sudo tee "${SYSTEMD_DIR}/${JQ_SIGNAL_SERVICE}" >/dev/null <<EOF
[Unit]
Description=JoinQuant Signal API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/joinquant_signal_server.py --host 0.0.0.0 --port ${signal_port}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

  sudo tee "${SYSTEMD_DIR}/${JQ_SYNC_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Sync JoinQuant snapshot to local holdings

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/joinquant_sync.py
EOF

  sudo tee "${SYSTEMD_DIR}/${JQ_SYNC_TIMER}" >/dev/null <<EOF
[Unit]
Description=Run JoinQuant snapshot sync every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
Unit=${JQ_SYNC_SERVICE}

[Install]
WantedBy=timers.target
EOF

  sudo tee "${SYSTEMD_DIR}/${JQ_READINESS_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Build JoinQuant readiness report

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/joinquant_readiness_report.py
EOF

  sudo tee "${SYSTEMD_DIR}/${JQ_HEALTH_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Build JoinQuant health report and alert

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/joinquant_health.py --notify
EOF

  sudo tee "${SYSTEMD_DIR}/${JQ_HEALTH_TIMER}" >/dev/null <<EOF
[Unit]
Description=Run JoinQuant health check every five minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true
Unit=${JQ_HEALTH_SERVICE}

[Install]
WantedBy=timers.target
EOF

  sudo tee "${SYSTEMD_DIR}/${NOTIFY_RETRY_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Retry failed WeCom notifications

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/notify_retry.py
EOF

  sudo tee "${SYSTEMD_DIR}/${NOTIFY_RETRY_TIMER}" >/dev/null <<EOF
[Unit]
Description=Retry failed WeCom notifications every five minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
Persistent=true
Unit=${NOTIFY_RETRY_SERVICE}

[Install]
WantedBy=timers.target
EOF

  sudo tee "${SYSTEMD_DIR}/${JQ_READINESS_TIMER}" >/dev/null <<EOF
[Unit]
Description=Build JoinQuant readiness report after market close

[Timer]
OnCalendar=Mon..Fri *-*-* 15:20:00
Persistent=true
Unit=${JQ_READINESS_SERVICE}

[Install]
WantedBy=timers.target
EOF

  sudo tee "${SYSTEMD_DIR}/${ML_REPORT_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Build ML signal sample review report

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/ml_dataset.py
EOF

  sudo tee "${SYSTEMD_DIR}/${ML_REPORT_TIMER}" >/dev/null <<EOF
[Unit]
Description=Build ML signal sample review report after market close

[Timer]
OnCalendar=Mon..Fri *-*-* 15:25:00
Persistent=true
Unit=${ML_REPORT_SERVICE}

[Install]
WantedBy=timers.target
EOF

  sudo tee "${SYSTEMD_DIR}/${GLOBAL_CONTEXT_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Fetch US Japan Korea market context
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/global_market_context.py
EOF

  sudo tee "${SYSTEMD_DIR}/${GLOBAL_CONTEXT_TIMER}" >/dev/null <<EOF
[Unit]
Description=Fetch US Japan Korea market context before A-share open

[Timer]
OnCalendar=Mon..Fri *-*-* 08:55:00
Persistent=true
Unit=${GLOBAL_CONTEXT_SERVICE}

[Install]
WantedBy=timers.target
EOF

  sudo tee "${SYSTEMD_DIR}/${SECTOR_CONTEXT_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Refresh A-share sector market context cache
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/a_share_strategy.py --sector-context-only
EOF

  sudo tee "${SYSTEMD_DIR}/${SECTOR_CONTEXT_TIMER}" >/dev/null <<EOF
[Unit]
Description=Refresh A-share sector market context cache at low frequency

[Timer]
OnCalendar=Mon..Fri *-*-* 09:20:00
OnCalendar=Mon..Fri *-*-* 10:30:00
OnCalendar=Mon..Fri *-*-* 13:00:00
OnCalendar=Mon..Fri *-*-* 14:30:00
Persistent=true
Unit=${SECTOR_CONTEXT_SERVICE}

[Install]
WantedBy=timers.target
EOF

  sudo tee "${SYSTEMD_DIR}/${STRATEGY_COMPARE_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Build original vs shadow strategy compare report

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/strategy_compare_report.py
EOF

  sudo tee "${SYSTEMD_DIR}/${STRATEGY_COMPARE_TIMER}" >/dev/null <<EOF
[Unit]
Description=Build strategy compare report after market close

[Timer]
OnCalendar=Mon..Fri *-*-* 15:35:00
Persistent=true
Unit=${STRATEGY_COMPARE_SERVICE}

[Install]
WantedBy=timers.target
EOF

  sudo tee "${SYSTEMD_DIR}/${STRATEGY_COMPARE_WEEKLY_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Send weekly original vs shadow strategy compare report

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/strategy_compare_report.py --notify --weekly
EOF

  sudo tee "${SYSTEMD_DIR}/${STRATEGY_COMPARE_WEEKLY_TIMER}" >/dev/null <<EOF
[Unit]
Description=Send weekly strategy compare report after Friday close

[Timer]
OnCalendar=Fri *-*-* 15:45:00
Persistent=true
Unit=${STRATEGY_COMPARE_WEEKLY_SERVICE}

[Install]
WantedBy=timers.target
EOF

  sudo tee "${SYSTEMD_DIR}/${TRADING_BACKUP_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Create verified SQLite trading ledger backup

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/trading_backup.py backup
EOF

  sudo tee "${SYSTEMD_DIR}/${TRADING_BACKUP_TIMER}" >/dev/null <<EOF
[Unit]
Description=Create verified SQLite trading ledger backup daily

[Timer]
OnCalendar=*-*-* 16:30:00 Asia/Shanghai
Persistent=true
Unit=${TRADING_BACKUP_SERVICE}

[Install]
WantedBy=timers.target
EOF

  sudo tee "${SYSTEMD_DIR}/${TRADING_BACKUP_DRILL_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Run isolated SQLite trading ledger restore drill

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${py} ${APP_DIR}/trading_backup.py drill
EOF

  sudo tee "${SYSTEMD_DIR}/${TRADING_BACKUP_DRILL_TIMER}" >/dev/null <<EOF
[Unit]
Description=Run quarterly SQLite trading ledger restore drill

[Timer]
OnCalendar=Sun *-01,04,07,10-01..07 03:30:00 Asia/Shanghai
Persistent=true
Unit=${TRADING_BACKUP_DRILL_SERVICE}

[Install]
WantedBy=timers.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable "${ALL_SERVICES[@]}" "${ALL_TIMERS[@]}"
}

install_all() {
  local webhook=""
  local token=""
  local cash="100000"
  local web_port="8000"
  local signal_port="8010"
  local skip_install="no"
  local install_ocr="yes"
  local start_services="yes"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --webhook) webhook="${2:-}"; shift 2 ;;
      --token) token="${2:-}"; shift 2 ;;
      --cash) cash="${2:-}"; shift 2 ;;
      --web-port|--port) web_port="${2:-}"; shift 2 ;;
      --signal-port) signal_port="${2:-}"; shift 2 ;;
      --skip-install) skip_install="yes"; shift ;;
      --skip-ocr) install_ocr="no"; shift ;;
      --no-start) start_services="no"; shift ;;
      *) die "Unknown install option: $1" ;;
    esac
  done

  require_project_files
  mkdir -p "${APP_DIR}/cache/trading"
  sudo install -d -m 700 -o "$(id -u)" -g "$(id -g)" "/opt/stock-analysis-backups"
  [[ -n "${token}" ]] || token="$(generate_token)"
  if [[ "${skip_install}" == "no" ]]; then
    install_system_packages "${install_ocr}"
    install_python_deps
  fi
  write_env_file "${webhook}" "${token}" "${cash}" "${web_port}"
  write_systemd_units "${signal_port}"
  if [[ "${start_services}" == "yes" ]]; then
    sudo systemctl restart "${ALL_SERVICES[@]}" "${ALL_TIMERS[@]}"
  fi

  cat <<EOF

Install complete.

Env file:
  ${ENV_FILE}

JoinQuant strategy values:
  SIGNAL_URL   = http://SERVER_IP:${signal_port}/joinquant/signals
  SNAPSHOT_URL = http://SERVER_IP:${signal_port}/joinquant/account_snapshot
  SYNC_TOKEN   = ${token}
  DRY_RUN      = False

Holdings web:
  http://SERVER_IP:${web_port}
EOF
}

service_cmd() {
  local action="$1"
  local service="$2"
  [[ -f "${SYSTEMD_DIR}/${service}" ]] || die "Service ${service} not found. Run: bash run_ubuntu.sh install"
  sudo systemctl "${action}" "${service}"
}

timer_cmd() {
  local action="$1"
  local timer="$2"
  [[ -f "${SYSTEMD_DIR}/${timer}" ]] || die "Timer ${timer} not found. Run: bash run_ubuntu.sh install"
  sudo systemctl "${action}" "${timer}"
}

show_env_summary() {
  cat <<EOF
Project: ${APP_DIR}
Env file: ${ENV_FILE}
Webhook configured: $(if [[ -f "${ENV_FILE}" ]] && grep -Eq '^WECOM_WEBHOOK_URL=.+$' "${ENV_FILE}"; then echo yes; else echo no; fi)
Scanner mode: $(env_value SCAN_MODE auto)
Paper trading: $(env_value PAPER_TRADE_ENABLE 0), cash=$(env_value PAPER_TRADE_CASH 100000)
JoinQuant: $(env_value JOINQUANT_ENABLE 0), dry_run=$(env_value JOINQUANT_DRY_RUN true), enforce_health_gate=$(env_value JOINQUANT_ENFORCE_HEALTH_GATE 0)
JoinQuant health: signal_max_age=$(env_value JOINQUANT_HEALTH_SIGNAL_MAX_AGE_MIN 30)m, snapshot_max_age=$(env_value JOINQUANT_HEALTH_SNAPSHOT_MAX_AGE_MIN 15)m
ML samples: $(env_value ML_SIGNAL_SAMPLE_FILE "${APP_DIR}/cache/ml/signal_samples.jsonl")
ML report: $(env_value ML_REVIEW_REPORT_FILE "${APP_DIR}/output/ml_signal_review.md")
Global context: $(env_value GLOBAL_MARKET_CONTEXT_FILE "${APP_DIR}/cache/market/global_context.json")
Sector context: ${APP_DIR}/cache/market/sector_context.json
Strategy compare: $(env_value STRATEGY_COMPARE_REPORT_FILE "${APP_DIR}/output/strategy_compare_report.md")
Backtest report: ${APP_DIR}/output/backtest_report.md
SQLite backup: $(env_value TRADING_BACKUP_DIR /opt/stock-analysis-backups), keep=$(env_value TRADING_BACKUP_DAILY_KEEP 7)/$(env_value TRADING_BACKUP_WEEKLY_KEEP 4)/$(env_value TRADING_BACKUP_MONTHLY_KEEP 12)
Notify retry queue: ${APP_DIR}/cache/notify_failed_queue.jsonl
Holdings web port: $(env_value PORTFOLIO_WEB_PORT 8000)
EOF
}

run_foreground() {
  local file="$1"
  shift
  load_env
  cd "${APP_DIR}"
  "$(python_bin)" "${APP_DIR}/${file}" "$@"
}

ledger_check() {
  load_env
  cd "${APP_DIR}"
  "$(python_bin)" -c 'import config, uuid; from trading_store import SCHEMA_VERSION, TradingStore; store = TradingStore(config.TRADING_DB_FILE); store.initialize(); health = store.health(); assert health.ok and health.schema_version == SCHEMA_VERSION, health; probe = f"ledger_check_probe_{uuid.uuid4().hex}"; exec("with store.transaction() as conn:\n store.set_system_state(conn, probe, \"ok\", \"deployment writable probe\")\n conn.execute(\"DELETE FROM system_state WHERE key = ?\", (probe,))"); print(f"schema_version={health.schema_version} health=ok writable_probe=ok")'
}

handle_command() {
  case "${1:-help}" in
    install)
      shift
      install_all "$@"
      ;;
    start-all)
      for service in "${ALL_SERVICES[@]}"; do service_cmd start "${service}"; done
      for timer in "${ALL_TIMERS[@]}"; do timer_cmd start "${timer}"; done
      ;;
    stop-all)
      for timer in "${ALL_TIMERS[@]}"; do timer_cmd stop "${timer}" || true; done
      for service in "${ALL_SERVICES[@]}"; do service_cmd stop "${service}" || true; done
      ;;
    restart-all)
      for service in "${ALL_SERVICES[@]}"; do service_cmd restart "${service}"; done
      for timer in "${ALL_TIMERS[@]}"; do timer_cmd restart "${timer}"; done
      ;;
    status-all)
      for service in "${ALL_SERVICES[@]}"; do service_cmd status "${service}" || true; done
      for timer in "${ALL_TIMERS[@]}"; do timer_cmd status "${timer}" || true; done
      ;;
    logs-strategy) sudo journalctl -u "${STRATEGY_SERVICE}" -f ;;
    logs-web) sudo journalctl -u "${WEB_SERVICE}" -f ;;
    logs-joinquant) sudo journalctl -u "${JQ_SIGNAL_SERVICE}" -f ;;
    run-strategy) run_foreground a_share_strategy.py ;;
    run-web) run_foreground holdings_web.py ;;
    run-joinquant-api) run_foreground joinquant_signal_server.py --host 0.0.0.0 --port "$(env_value JOINQUANT_SIGNAL_PORT 8010)" ;;
    sync-joinquant) run_foreground joinquant_sync.py ;;
    ledger-check) ledger_check ;;
    health) run_foreground joinquant_health.py ;;
    notify-retry) run_foreground notify_retry.py ;;
    readiness) run_foreground joinquant_readiness_report.py ;;
    ml-report) run_foreground ml_dataset.py ;;
    global-context) run_foreground global_market_context.py ;;
    sector-context) run_foreground a_share_strategy.py --sector-context-only ;;
    strategy-compare) run_foreground strategy_compare_report.py ;;
    strategy-compare-weekly) run_foreground strategy_compare_report.py --notify --weekly ;;
    backtest) run_foreground backtest_engine.py ;;
    backup) run_foreground trading_backup.py backup ;;
    backup-drill) run_foreground trading_backup.py drill ;;
    backup-status) run_foreground trading_backup.py status ;;
    test) cd "${APP_DIR}"; "$(python_bin)" -m unittest discover -s tests -v ;;
    show-env) show_env_summary ;;
    help|-h|--help) usage ;;
    *) die "Unknown command: $1" ;;
  esac
}

if [[ $# -eq 0 && -t 0 ]]; then
  menu_loop
else
  handle_command "$@"
fi
