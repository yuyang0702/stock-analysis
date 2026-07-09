from __future__ import annotations

import os
from pathlib import Path


def _env_text(name: str, default: str = "", *aliases: str) -> str:
    for key in (name, *aliases):
        value = os.getenv(key)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _env_int(name: str, default: int) -> int:
    value = _env_text(name)
    if not value:
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = _env_text(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = _env_text(name).lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on", "y"}:
        return True
    if value in {"0", "false", "no", "off", "n"}:
        return False
    return default


# =============================
# 基础路径配置
# =============================
# 项目根目录，所有相对路径都以它为基准。
BASE_DIR = Path(__file__).resolve().parent

# 扫描结果输出目录，保存 CSV 和 Markdown 报告。
OUTPUT_DIR = BASE_DIR / "output"

# 运行缓存目录，保存新闻、龙虎榜、通知去重状态等缓存。
CACHE_DIR = BASE_DIR / "cache"

# 行业映射缓存文件，首次同步后会生成，后续直接复用。
INDUSTRY_CACHE = BASE_DIR / "stock_industry_db.json"


# =============================
# 网络与请求超时
# =============================
# A 股行情接口超时时间，防止某个接口卡住整轮扫描。
STOCK_SCAN_TIMEOUT = _env_int("STOCK_SCAN_TIMEOUT", 12)

# 企业微信机器人请求超时，避免通知接口阻塞主流程。
WECOM_TIMEOUT_SEC = _env_int("WECOM_TIMEOUT_SEC", 10)


# =============================
# 扫描策略默认值
# =============================
# 默认扫描模式：pre=盘前，intraday=盘中，after=盘后。
SCAN_MODE_DEFAULT = _env_text("SCAN_MODE", "after")

# 每轮默认输出的候选股数量。
SCAN_TOP_DEFAULT = _env_int("SCAN_TOP", 10)

# 轮询间隔，单位秒。300 表示 5 分钟。
SCAN_INTERVAL_DEFAULT = _env_int("SCAN_INTERVAL", 300)

# 轮询随机抖动，避免固定频率过密打接口。
SCAN_JITTER_DEFAULT = _env_int("SCAN_JITTER_SEC", 30)

# 最低价格过滤，避免过低价格的噪声标的。
MIN_PRICE_DEFAULT = _env_float("MIN_PRICE", 1.5)

# 最低成交额过滤，默认过滤低流动性标的。
MIN_AMOUNT_DEFAULT = _env_float("MIN_AMOUNT", 50_000_000)

# 是否默认跳过压力位计算。盘中快扫可开，盘后可关。
SKIP_PRESSURE_DEFAULT = _env_bool("SKIP_PRESSURE", False)

# 是否默认跳过龙虎榜检查。盘中快扫可开，盘后可关。
SKIP_LHB_DEFAULT = _env_bool("SKIP_LHB", False)

# 是否默认跳过新闻分析。若接口不稳，可临时开启。
SKIP_NEWS_DEFAULT = _env_bool("SKIP_NEWS", False)

# 单只股票最多抓取的新闻条数，控制接口压力。
STOCK_NEWS_LIMIT_DEFAULT = _env_int("STOCK_NEWS_LIMIT", 5)

# 公告回看天数，越大越慢，但可能抓到更多事件。
NOTICE_DAYS_BACK_DEFAULT = _env_int("NOTICE_DAYS_BACK", 2)

# 只对前多少只候选股做新闻和 AI 分析，避免全量请求太重。
MAX_CANDIDATES_FOR_NEWS_DEFAULT = _env_int("MAX_CANDIDATES_FOR_NEWS", 8)

# 压力位、龙虎榜、新闻的本地缓存 TTL，单位秒。
PRESSURE_TTL_SEC = 1800
LHB_TTL_SEC = 1800
MARKET_NEWS_TTL_SEC = 900
STOCK_NEWS_TTL_SEC = 1800


# =============================
# 企业微信通知配置
# =============================
# 企业微信机器人 Webhook，用于向群里推送扫描和告警消息。
# 你可以直接把机器人地址写这里，也可以改成环境变量读取。
WECOM_WEBHOOK_URL = _env_text("WECOM_WEBHOOK_URL", "", "WECom_WEBHOOK_URL")

# 是否默认启用通知。1 表示启用，0 表示关闭。
NOTIFY_ENABLE_DEFAULT = _env_bool("NOTIFY_ENABLE", True)

# 是否只推送强信号和风险信号，不发整轮汇总。
NOTIFY_ONLY_SIGNAL_DEFAULT = _env_bool("NOTIFY_ONLY_SIGNAL", False)

# 默认通知时选取的前 N 只候选股。
NOTIFY_TOP_N_DEFAULT = _env_int("NOTIFY_TOP_N", 8)

# 同一条信号的通知冷却时间，单位秒，避免重复刷屏。
NOTIFY_COOLDOWN_SEC_DEFAULT = _env_int("NOTIFY_COOLDOWN_SEC", 1800)

# 触发通知的最低总分阈值，低于这个值通常不推送。
NOTIFY_MIN_SCORE_DEFAULT = _env_float("NOTIFY_MIN_SCORE", 75.0)

# 非交易日是否仍然推送，用于服务器联调。默认关闭，避免节假日刷屏。
NOTIFY_NON_TRADING_DAY_DEFAULT = _env_bool("NOTIFY_NON_TRADING_DAY", False)

# A 股节假日日期，逗号分隔，格式 YYYY-MM-DD。周末默认按非交易日处理。
A_SHARE_HOLIDAYS_DEFAULT = {
    item.strip()
    for item in _env_text("A_SHARE_HOLIDAYS", "").split(",")
    if item.strip()
}

# 盘中买点预警阈值，距离压力位小于这个值时视为“临近买点”。
INTRADAY_NEAR_PRESSURE_PCT_DEFAULT = _env_float("INTRADAY_NEAR_PRESSURE_PCT", 3.0)

# 盘中买点触发阈值，低于这个值时视为“已到买点”。
INTRADAY_TRIGGER_PRESSURE_PCT_DEFAULT = _env_float("INTRADAY_TRIGGER_PRESSURE_PCT", 1.2)

# 盘中额外观察池的放大倍数，避免只盯前几名。
INTRADAY_WATCH_MULTIPLIER_DEFAULT = _env_int("INTRADAY_WATCH_MULTIPLIER", 3)

# 每轮最多推送多少只盘中买点，避免消息过长。
INTRADAY_MAX_ALERTS_DEFAULT = _env_int("INTRADAY_MAX_ALERTS", 3)


# =============================
# AI 配置
# =============================
# 是否默认启用 AI 复盘。生产上建议按需打开。
ENABLE_AI_DEFAULT = _env_bool("ENABLE_AI", False)

# AI 模型名，留空表示由环境变量覆盖。
ARK_MODEL_DEFAULT = _env_text("ARK_MODEL", "")

# AI 服务地址，兼容 OpenAI 协议的接口地址。
ARK_BASE_URL_DEFAULT = _env_text("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")


# =============================
# 环境变量键名
# =============================
# 这些键名统一放这里，方便 Linux 上用 EnvironmentFile 管理。
ENV_WECOM_WEBHOOK_URL = "WECOM_WEBHOOK_URL"
ENV_NOTIFY_ENABLE = "NOTIFY_ENABLE"
ENV_NOTIFY_ONLY_SIGNAL = "NOTIFY_ONLY_SIGNAL"
ENV_NOTIFY_TOP_N = "NOTIFY_TOP_N"
ENV_NOTIFY_COOLDOWN_SEC = "NOTIFY_COOLDOWN_SEC"
ENV_NOTIFY_MIN_SCORE = "NOTIFY_MIN_SCORE"
ENV_NOTIFY_NON_TRADING_DAY = "NOTIFY_NON_TRADING_DAY"
ENV_A_SHARE_HOLIDAYS = "A_SHARE_HOLIDAYS"
ENV_ENABLE_AI = "ENABLE_AI"
ENV_ARK_API_KEY = "ARK_API_KEY"
ENV_ARK_MODEL = "ARK_MODEL"
ENV_ARK_BASE_URL = "ARK_BASE_URL"
ENV_SCAN_MODE = "SCAN_MODE"
ENV_SCAN_TOP = "SCAN_TOP"
ENV_SCAN_INTERVAL = "SCAN_INTERVAL"
ENV_SCAN_JITTER_SEC = "SCAN_JITTER_SEC"
ENV_MIN_PRICE = "MIN_PRICE"
ENV_MIN_AMOUNT = "MIN_AMOUNT"
ENV_SKIP_PRESSURE = "SKIP_PRESSURE"
ENV_SKIP_LHB = "SKIP_LHB"
ENV_SKIP_NEWS = "SKIP_NEWS"
ENV_STOCK_NEWS_LIMIT = "STOCK_NEWS_LIMIT"
ENV_NOTICE_DAYS_BACK = "NOTICE_DAYS_BACK"
ENV_MAX_CANDIDATES_FOR_NEWS = "MAX_CANDIDATES_FOR_NEWS"
ENV_STOCK_SCAN_TIMEOUT = "STOCK_SCAN_TIMEOUT"
ENV_SIGNAL_WATCHLIST_DAYS = "SIGNAL_WATCHLIST_DAYS"
ENV_PAPER_TRADE_ENABLE = "PAPER_TRADE_ENABLE"
ENV_PAPER_TRADE_CASH = "PAPER_TRADE_CASH"
ENV_PAPER_TRADE_COMMISSION_RATE = "PAPER_TRADE_COMMISSION_RATE"
ENV_PAPER_TRADE_STAMP_TAX_RATE = "PAPER_TRADE_STAMP_TAX_RATE"
ENV_PAPER_TRADE_SLIPPAGE_PCT = "PAPER_TRADE_SLIPPAGE_PCT"
ENV_PAPER_TRADE_COOLDOWN_DAYS = "PAPER_TRADE_COOLDOWN_DAYS"
ENV_PAPER_TRADE_MAX_POSITIONS = "PAPER_TRADE_MAX_POSITIONS"
ENV_PAPER_TRADE_MAX_POSITION_PCT = "PAPER_TRADE_MAX_POSITION_PCT"
ENV_PAPER_TRADE_MAX_TOTAL_POSITION_PCT = "PAPER_TRADE_MAX_TOTAL_POSITION_PCT"
ENV_JOINQUANT_ENABLE = "JOINQUANT_ENABLE"
ENV_JOINQUANT_SYNC_TOKEN = "JOINQUANT_SYNC_TOKEN"
ENV_JOINQUANT_MAX_SIGNAL_AGE_MIN = "JOINQUANT_MAX_SIGNAL_AGE_MIN"
ENV_JOINQUANT_ALLOW_BUY = "JOINQUANT_ALLOW_BUY"
ENV_JOINQUANT_ALLOW_SELL = "JOINQUANT_ALLOW_SELL"
ENV_JOINQUANT_DRY_RUN = "JOINQUANT_DRY_RUN"
ENV_JOINQUANT_MIN_SCORE = "JOINQUANT_MIN_SCORE"
ENV_JOINQUANT_MAX_POSITIONS = "JOINQUANT_MAX_POSITIONS"
ENV_JOINQUANT_MAX_TOTAL_POSITION_PCT = "JOINQUANT_MAX_TOTAL_POSITION_PCT"
ENV_JOINQUANT_REQUEST_TIMEOUT = "JOINQUANT_REQUEST_TIMEOUT"
ENV_JOINQUANT_HEALTH_SIGNAL_MAX_AGE_MIN = "JOINQUANT_HEALTH_SIGNAL_MAX_AGE_MIN"
ENV_JOINQUANT_HEALTH_SNAPSHOT_MAX_AGE_MIN = "JOINQUANT_HEALTH_SNAPSHOT_MAX_AGE_MIN"
ENV_JOINQUANT_HEALTH_FAILED_ORDER_LIMIT = "JOINQUANT_HEALTH_FAILED_ORDER_LIMIT"
ENV_ML_SIGNAL_SAMPLE_FILE = "ML_SIGNAL_SAMPLE_FILE"
ENV_ML_REVIEW_REPORT_FILE = "ML_REVIEW_REPORT_FILE"

# 推送信号观察池最多保留多少天，用于盘后追踪之前推送过的股票。
SIGNAL_WATCHLIST_DAYS_DEFAULT = _env_int("SIGNAL_WATCHLIST_DAYS", 10)
PAPER_TRADE_ENABLE_DEFAULT = _env_bool("PAPER_TRADE_ENABLE", False)
PAPER_TRADE_CASH_DEFAULT = _env_float("PAPER_TRADE_CASH", 100_000)
PAPER_TRADE_COMMISSION_RATE_DEFAULT = _env_float("PAPER_TRADE_COMMISSION_RATE", 0.0003)
PAPER_TRADE_STAMP_TAX_RATE_DEFAULT = _env_float("PAPER_TRADE_STAMP_TAX_RATE", 0.001)
PAPER_TRADE_SLIPPAGE_PCT_DEFAULT = _env_float("PAPER_TRADE_SLIPPAGE_PCT", 0.001)
PAPER_TRADE_COOLDOWN_DAYS_DEFAULT = _env_int("PAPER_TRADE_COOLDOWN_DAYS", 3)
PAPER_TRADE_MAX_POSITIONS_DEFAULT = _env_int("PAPER_TRADE_MAX_POSITIONS", 5)
PAPER_TRADE_MAX_POSITION_PCT_DEFAULT = _env_float("PAPER_TRADE_MAX_POSITION_PCT", 20.0)
PAPER_TRADE_MAX_TOTAL_POSITION_PCT_DEFAULT = _env_float("PAPER_TRADE_MAX_TOTAL_POSITION_PCT", 80.0)
PAPER_TRADE_FILE = CACHE_DIR / "paper_trading.json"
JOINQUANT_ENABLE_DEFAULT = _env_bool("JOINQUANT_ENABLE", False)
JOINQUANT_SYNC_TOKEN = _env_text("JOINQUANT_SYNC_TOKEN", "")
JOINQUANT_SIGNAL_FILE = Path(_env_text("JOINQUANT_SIGNAL_FILE", str(CACHE_DIR / "joinquant" / "signals.json")))
JOINQUANT_ACCOUNT_FILE = Path(_env_text("JOINQUANT_ACCOUNT_FILE", str(CACHE_DIR / "joinquant" / "account_snapshot.json")))
JOINQUANT_MAX_SIGNAL_AGE_MIN_DEFAULT = _env_int("JOINQUANT_MAX_SIGNAL_AGE_MIN", 20)
JOINQUANT_ALLOW_BUY_DEFAULT = _env_bool("JOINQUANT_ALLOW_BUY", True)
JOINQUANT_ALLOW_SELL_DEFAULT = _env_bool("JOINQUANT_ALLOW_SELL", True)
JOINQUANT_DRY_RUN_DEFAULT = _env_bool("JOINQUANT_DRY_RUN", False)
JOINQUANT_MIN_SCORE_DEFAULT = _env_float("JOINQUANT_MIN_SCORE", 75.0)
JOINQUANT_MAX_POSITIONS_DEFAULT = _env_int("JOINQUANT_MAX_POSITIONS", 5)
JOINQUANT_MAX_TOTAL_POSITION_PCT_DEFAULT = _env_float("JOINQUANT_MAX_TOTAL_POSITION_PCT", 80.0)
JOINQUANT_REQUEST_TIMEOUT_DEFAULT = _env_int("JOINQUANT_REQUEST_TIMEOUT", 8)
JOINQUANT_HEALTH_SIGNAL_MAX_AGE_MIN_DEFAULT = _env_int("JOINQUANT_HEALTH_SIGNAL_MAX_AGE_MIN", 30)
JOINQUANT_HEALTH_SNAPSHOT_MAX_AGE_MIN_DEFAULT = _env_int("JOINQUANT_HEALTH_SNAPSHOT_MAX_AGE_MIN", 15)
JOINQUANT_HEALTH_FAILED_ORDER_LIMIT_DEFAULT = _env_int("JOINQUANT_HEALTH_FAILED_ORDER_LIMIT", 3)
JOINQUANT_TEMPLATE_VERSION = "2026-07-09.2-order-target-value"
ML_SIGNAL_SAMPLE_FILE = Path(_env_text("ML_SIGNAL_SAMPLE_FILE", str(CACHE_DIR / "ml" / "signal_samples.jsonl")))
ML_REVIEW_REPORT_FILE = Path(_env_text("ML_REVIEW_REPORT_FILE", str(OUTPUT_DIR / "ml_signal_review.md")))


# =============================
# 手机持仓网页配置
# =============================
# 手机端持仓回写目录，保存截图、持仓和操作日志。
PORTFOLIO_DIR = CACHE_DIR / "portfolio_web"

# 当前持仓文件，网页写入后策略可以直接读取。
POSITIONS_FILE = PORTFOLIO_DIR / "positions.json"

# 操作日志文件，记录每次买入、卖出、清仓和截图识别结果。
PORTFOLIO_EVENTS_FILE = PORTFOLIO_DIR / "events.jsonl"

# 截图上传目录，OCR 成功或失败都保留原图，方便回看。
PORTFOLIO_UPLOAD_DIR = PORTFOLIO_DIR / "uploads"

# 手机网页默认监听地址，Linux 服务器一般绑定 0.0.0.0。
PORTFOLIO_WEB_HOST_DEFAULT = _env_text("PORTFOLIO_WEB_HOST", "0.0.0.0")

# 手机网页默认端口。
PORTFOLIO_WEB_PORT_DEFAULT = _env_int("PORTFOLIO_WEB_PORT", 8000)

# 环境变量名，便于 Linux 用 EnvironmentFile 管理。
ENV_PORTFOLIO_WEB_HOST = "PORTFOLIO_WEB_HOST"
ENV_PORTFOLIO_WEB_PORT = "PORTFOLIO_WEB_PORT"
