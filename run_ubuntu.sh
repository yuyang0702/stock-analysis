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
ML_REPORT_SERVICE="stock-ml-report.service"
ML_REPORT_TIMER="stock-ml-report.timer"
ALL_SERVICES=("${STRATEGY_SERVICE}" "${WEB_SERVICE}" "${JQ_SIGNAL_SERVICE}")
ALL_TIMERS=("${JQ_SYNC_TIMER}" "${JQ_READINESS_TIMER}" "${ML_REPORT_TIMER}")

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
  bash run_ubuntu.sh run-strategy|run-web|run-joinquant-api|sync-joinquant|readiness|ml-report|backtest|test|show-env

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
 10) 生成 readiness 报告
 11) 生成 ML 复盘报告
 12) 运行本地信号回测
 13) 查看当前配置
 14) 运行测试
 15) 首次安装/重写配置
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
      10) handle_command readiness ;;
      11) handle_command ml-report ;;
      12) handle_command backtest ;;
      13) handle_command show-env ;;
      14) handle_command test ;;
      15) menu_install ;;
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
    backtest_engine.py \
    joinquant_readiness_report.py ml_dataset.py requirements.txt; do
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
  set_env "JOINQUANT_MIN_SCORE" "75"
  set_env "JOINQUANT_MAX_SIGNAL_AGE_MIN" "20"
  set_env "JOINQUANT_MAX_POSITIONS" "5"
  set_env "JOINQUANT_MAX_TOTAL_POSITION_PCT" "80"
  set_env "JOINQUANT_REQUEST_TIMEOUT" "8"
  set_env "ML_SIGNAL_SAMPLE_FILE" "${APP_DIR}/cache/ml/signal_samples.jsonl"
  set_env "ML_REVIEW_REPORT_FILE" "${APP_DIR}/output/ml_signal_review.md"
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
JoinQuant: $(env_value JOINQUANT_ENABLE 0), dry_run=$(env_value JOINQUANT_DRY_RUN true)
ML samples: $(env_value ML_SIGNAL_SAMPLE_FILE "${APP_DIR}/cache/ml/signal_samples.jsonl")
ML report: $(env_value ML_REVIEW_REPORT_FILE "${APP_DIR}/output/ml_signal_review.md")
Backtest report: ${APP_DIR}/output/backtest_report.md
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
    readiness) run_foreground joinquant_readiness_report.py ;;
    ml-report) run_foreground ml_dataset.py ;;
    backtest) run_foreground backtest_engine.py ;;
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
