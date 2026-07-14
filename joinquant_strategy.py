"""
Copy this file into JoinQuant as a strategy template.

Set these values in the platform before running:
SIGNAL_URL, SNAPSHOT_URL, SYNC_TOKEN, DRY_RUN.

If you expose the built-in signal service directly on port 8010, use http://.
Only use https:// after adding an HTTPS reverse proxy such as Nginx.
"""

from datetime import datetime, timedelta
import json
import urllib.parse
import urllib.request


SIGNAL_URL = "http://SERVER_IP:8010/joinquant/signals"
SNAPSHOT_URL = "http://SERVER_IP:8010/joinquant/account_snapshot"
SYNC_TOKEN = ""
DRY_RUN = False
STARTUP_SELF_TEST = True
MIN_SCORE = 75.0
MAX_SIGNAL_AGE_MIN = 20
MAX_POSITIONS = 5
MAX_TOTAL_POSITION_PCT = 80.0
STRATEGY_TEMPLATE_VERSION = "2026-07-14.2-p0-execution-contract"


def initialize(context):
    g.signals = []
    g.executed_signal_ids = set()
    g.order_events = []
    g.order_signal_ids = {}
    g.metrics_trade_date = datetime.now().strftime("%Y-%m-%d")
    g.day_start_value = float(context.portfolio.total_value or 0)
    g.peak_value = g.day_start_value
    g.last_total_value = g.day_start_value
    g.consecutive_losses = 0
    run_daily(post_account_snapshot, time="15:05")
    if STARTUP_SELF_TEST:
        startup_self_test(context)


def handle_data(context, data):
    fetch_and_execute(context)
    post_account_snapshot(context)


def fetch_and_execute(context):
    fetch_signals(context)
    return execute_signals(context)


def startup_self_test(context):
    fetch_signals(context)
    post_account_snapshot(context)
    log.info("startup self test ok")


def _url(base):
    return base + ("&" if "?" in base else "?") + urllib.parse.urlencode({"token": SYNC_TOKEN})


def _get_json(url):
    with urllib.request.urlopen(_url(url), timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url, payload):
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(
        _url(url),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_signals(context):
    try:
        payload = _get_json(SIGNAL_URL)
    except Exception as exc:
        log.warn("fetch signals failed: %s" % exc)
        g.signals = []
        return
    if payload.get("schema_version") != 1:
        log.warn("invalid signal schema")
        g.signals = []
        return
    g.signals = payload.get("signals", [])
    g.signal_trade_date = payload.get("trade_date")
    g.signal_generated_at = payload.get("generated_at")
    log.info("loaded %s signals" % len(g.signals))


def _signal_is_fresh(signal):
    text = getattr(g, "signal_generated_at", "") or ""
    try:
        generated_at = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    return datetime.now() - generated_at <= timedelta(
        minutes=int(signal.get("max_age_min") or MAX_SIGNAL_AGE_MIN)
    )


def _can_execute(context, signal):
    signal_id = signal.get("id")
    if not signal_id or signal_id in g.executed_signal_ids:
        return False, "duplicate"
    if not _signal_is_fresh(signal):
        return False, "stale"
    if float(signal.get("final_score") or 0) < MIN_SCORE and signal.get("action") == "buy":
        return False, "low_score"
    if float(signal.get("position_pct") or 0) <= 0 and signal.get("action") == "buy":
        return False, "bad_position"
    if float(signal.get("position_pct") or 0) > MAX_TOTAL_POSITION_PCT:
        return False, "position_limit"
    if signal.get("action") == "buy":
        if len(context.portfolio.positions) >= MAX_POSITIONS:
            return False, "max_positions"
        total_value = float(context.portfolio.total_value or 0)
        current_position_pct = (
            sum(float(_position_attr(position, "value", 0) or 0) for position in context.portfolio.positions.values())
            / total_value * 100 if total_value > 0 else 0
        )
        if current_position_pct + float(signal.get("position_pct") or 0) > MAX_TOTAL_POSITION_PCT:
            return False, "total_position_limit"
    jq_code = signal.get("jq_code")
    if not jq_code:
        return False, "missing_code"
    try:
        quote = get_current_data()[jq_code]
        current_price = float(_order_attr(quote, "last_price", 0) or 0)
        if bool(_order_attr(quote, "paused", False)):
            return False, "suspended"
        if current_price <= 0:
            return False, "price_invalid"
        if signal.get("action") == "sell" and current_price <= float(_order_attr(quote, "low_limit", 0) or 0):
            return False, "limit_down"
        if signal.get("action") == "buy":
            if current_price >= float(_order_attr(quote, "high_limit", float("inf")) or float("inf")):
                return False, "limit_up"
            target_value = context.portfolio.total_value * float(signal.get("position_pct") or 0) / 100.0
            if target_value > float(_position_attr(context.portfolio, "available_cash", context.portfolio.cash) or 0):
                return False, "insufficient_cash"
            entry = float(signal.get("entry_price") or signal.get("price") or 0)
            atr = float(signal.get("atr14") or 0)
            max_move = min(0.02, 0.5 * atr / entry if entry > 0 and atr > 0 else 0.02)
            if entry > 0 and current_price > entry * (1 + max_move):
                return False, "price_moved"
    except Exception:
        return False, "quote_unavailable"
    try:
        open_orders = get_open_orders()
    except Exception:
        open_orders = {}
    for order in open_orders.values():
        if str(_order_attr(order, "security", "")) == jq_code:
            return False, "pending_order"
    if signal.get("action") == "buy" and jq_code in context.portfolio.positions:
        return False, "already_holding"
    if signal.get("action") == "sell" and jq_code not in context.portfolio.positions:
        return False, "not_holding"
    return True, ""


def _order_attr(order, name, default=None):
    if order is None:
        return default
    try:
        return getattr(order, name)
    except Exception:
        try:
            return order.get(name, default)
        except Exception:
            return default


def _position_attr(position, name, default=0):
    return _order_attr(position, name, default)


def _attainable_sell_target(position, requested_target):
    current = int(_position_attr(position, "total_amount", 0) or 0)
    closeable = int(_position_attr(position, "closeable_amount", 0) or 0)
    target = max(0, int(requested_target or 0))
    required = max(0, current - target)
    if required and closeable <= 0:
        return None, "t_plus_one"
    sell_qty = min(required, closeable)
    return current - sell_qty, "partial_sellable" if sell_qty < required else ""


def _order_status_text(value):
    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:
        return ""
    status = text.split(".")[-1].lower() if text else ""
    return {"held": "submitted", "canceled": "cancelled"}.get(status, status)

def _record_order(signal, status, reason="", order=None):
    event = {
        "id": signal.get("id"),
        "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": signal.get("action"),
        "code": signal.get("code"),
        "jq_code": signal.get("jq_code"),
        "name": signal.get("name"),
        "target_pct": signal.get("position_pct"),
        "target_qty": signal.get("target_qty"),
        "status": _order_status_text(status) or str(status or ""),
        "reason": reason,
        "order_id": _order_attr(order, "order_id"),
        "amount": _order_attr(order, "amount"),
        "filled": _order_attr(order, "filled"),
        "price": _order_attr(order, "price"),
    }
    g.order_events.append(event)
    if event.get("order_id"):
        g.order_signal_ids[str(event["order_id"])] = signal.get("id")
    log.info(
        "record order %s %s status=%s filled=%s amount=%s reason=%s"
        % (
            event.get("action"),
            event.get("jq_code"),
            event.get("status"),
            event.get("filled"),
            event.get("amount"),
            event.get("reason"),
        )
    )


def execute_signals(context):
    event_count = 0
    for signal in list(getattr(g, "signals", [])):
        ok, reason = _can_execute(context, signal)
        if not ok:
            if reason == "duplicate":
                continue
            log.info("skip %s: %s" % (signal.get("id"), reason))
            explicit = {"suspended", "limit_down", "limit_up", "t_plus_one", "insufficient_cash", "price_moved"}
            _record_order(signal, reason if reason in explicit else "skipped", reason)
            if signal.get("action") == "buy" and signal.get("id"):
                g.executed_signal_ids.add(signal["id"])
            event_count += 1
            continue
        jq_code = signal["jq_code"]
        action = signal.get("action")
        if DRY_RUN:
            log.info("dry-run %s %s target=%s%%" % (action, jq_code, signal.get("position_pct")))
            _record_order(signal, "dry_run", "not_submitted")
        elif action == "buy":
            try:
                target_value = context.portfolio.total_value * float(signal.get("position_pct") or 0) / 100.0
                order = order_target_value(jq_code, target_value)
                if order is None:
                    _record_order(signal, "failed", "limit_up_or_suspended_or_rejected")
                else:
                    _record_order(signal, _order_attr(order, "status", "submitted"), order=order)
            except Exception as exc:
                _record_order(signal, "failed", str(exc))
        elif action == "sell":
            try:
                target_qty = signal.get("target_qty")
                target_qty, reason = _attainable_sell_target(
                    context.portfolio.positions[jq_code], target_qty,
                )
                if reason == "t_plus_one":
                    _record_order(signal, "t_plus_one", reason)
                    event_count += 1
                    continue
                order = order_target(jq_code, target_qty)
                if order is None:
                    _record_order(signal, "failed", "suspended_or_rejected")
                else:
                    _record_order(signal, _order_attr(order, "status", "submitted"), order=order)
            except Exception as exc:
                _record_order(signal, "failed", str(exc))
        if action == "buy":
            g.executed_signal_ids.add(signal["id"])
        event_count += 1
    return event_count


def post_account_snapshot(context):
    positions = []
    for jq_code, pos in context.portfolio.positions.items():
        positions.append(
            {
                "code": jq_code[:6],
                "jq_code": jq_code,
                "name": str(pos.security),
                "qty": pos.total_amount,
                "closeable_amount": _position_attr(pos, "closeable_amount", 0),
                "locked_amount": _position_attr(pos, "locked_amount", 0),
                "today_amount": _position_attr(pos, "today_amount", 0),
                "avg_cost": pos.avg_cost,
                "price": pos.price,
                "market_value": pos.value,
                "pnl": pos.value - pos.avg_cost * pos.total_amount,
            }
        )
    metrics = _account_metrics(context)
    order_events = list(getattr(g, "order_events", [])) + _platform_order_events()
    payload = {
        "schema_version": 1,
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "joinquant",
        "strategy_template_version": STRATEGY_TEMPLATE_VERSION,
        "cash": context.portfolio.cash,
        "available_cash": _position_attr(context.portfolio, "available_cash", context.portfolio.cash),
        "total_value": context.portfolio.total_value,
        "daily_turnover_pct": metrics["daily_turnover_pct"],
        "daily_pnl_pct": metrics["daily_pnl_pct"],
        "account_drawdown_pct": metrics["account_drawdown_pct"],
        "consecutive_losses": metrics["consecutive_losses"],
        "pending_buy_position_pct": metrics["pending_buy_position_pct"],
        "pending_buy_risk_pct": metrics["pending_buy_risk_pct"],
        "positions": positions,
        "orders": order_events,
        "trades": _platform_trade_events(),
    }
    try:
        _post_json(SNAPSHOT_URL, payload)
        log.info("post snapshot ok orders=%s positions=%s" % (len(payload["orders"]), len(positions)))
        g.order_events = []
    except Exception as exc:
        log.warn("post snapshot failed: %s" % exc)


def _platform_order_events():
    try:
        orders = get_orders().values()
    except Exception:
        try:
            orders = get_open_orders().values()
        except Exception:
            return []
    events = []
    for order in orders:
        order_id = str(_order_attr(order, "order_id", "") or "")
        security = str(_order_attr(order, "security", "") or "")
        is_buy = bool(_order_attr(order, "is_buy", False))
        amount = abs(float(_order_attr(order, "amount", 0) or 0))
        filled = abs(float(_order_attr(order, "filled", 0) or 0))
        status = _order_status_text(_order_attr(order, "status", "unknown"))
        if 0 < filled < amount:
            status = "partial"
        elif amount > 0 and filled >= amount:
            status = "filled"
        events.append({
            "id": getattr(g, "order_signal_ids", {}).get(order_id) or "jq-order-" + order_id,
            "datetime": str(_order_attr(order, "add_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))),
            "action": "buy" if is_buy else "sell",
            "code": security[:6],
            "jq_code": security,
            "status": status,
            "reason": str(_order_attr(order, "message", "") or ""),
            "order_id": order_id,
            "amount": amount,
            "filled": filled,
            "price": _order_attr(order, "price", 0),
        })
    return events


def _platform_trade_events():
    try:
        trades = get_trades().values()
    except Exception:
        return []
    try:
        orders = {str(_order_attr(order, "order_id", "") or ""): order for order in get_orders().values()}
    except Exception:
        orders = {}
    events = []
    for trade in trades:
        order_id = str(_order_attr(trade, "order_id", "") or "")
        order = orders.get(order_id)
        security = str(_order_attr(trade, "security", _order_attr(order, "security", "")) or "")
        is_buy = bool(_order_attr(trade, "is_buy", _order_attr(order, "is_buy", False)))
        trade_id = str(_order_attr(trade, "trade_id", _order_attr(trade, "id", "")) or "")
        events.append({
            "trade_id": trade_id,
            "order_id": order_id,
            "signal_id": getattr(g, "order_signal_ids", {}).get(order_id),
            "datetime": str(_order_attr(trade, "time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))),
            "action": "buy" if is_buy else "sell",
            "code": security[:6],
            "jq_code": security,
            "amount": abs(float(_order_attr(trade, "amount", 0) or 0)),
            "price": float(_order_attr(trade, "price", 0) or 0),
            "commission": float(_order_attr(trade, "commission", 0) or 0),
            "stamp_tax": float(_order_attr(trade, "tax", _order_attr(trade, "stamp_tax", 0)) or 0),
            "other_fee": float(_order_attr(trade, "other_fee", 0) or 0),
        })
    return events


def _account_metrics(context):
    today = datetime.now().strftime("%Y-%m-%d")
    total_value = float(context.portfolio.total_value or 0)
    if getattr(g, "metrics_trade_date", today) != today:
        previous_start = float(getattr(g, "day_start_value", total_value) or total_value)
        previous_end = float(getattr(g, "last_total_value", total_value) or total_value)
        if previous_start > 0 and previous_end < previous_start:
            g.consecutive_losses = int(getattr(g, "consecutive_losses", 0)) + 1
        else:
            g.consecutive_losses = 0
        g.metrics_trade_date = today
        g.day_start_value = total_value
    g.last_total_value = total_value
    g.peak_value = max(float(getattr(g, "peak_value", total_value) or total_value), total_value)
    start_value = float(getattr(g, "day_start_value", total_value) or total_value)
    try:
        turnover_value = sum(
            abs(float(_order_attr(trade, "amount", 0) or 0) * float(_order_attr(trade, "price", 0) or 0))
            for trade in get_trades().values()
            if str(_order_attr(trade, "time", ""))[:10] == today
        )
    except Exception:
        turnover_value = 0
    pending_buy_value = 0
    try:
        for order in get_open_orders().values():
            if not bool(_order_attr(order, "is_buy", False)):
                continue
            remaining = max(0, float(_order_attr(order, "amount", 0) or 0) - float(_order_attr(order, "filled", 0) or 0))
            pending_buy_value += remaining * float(_order_attr(order, "price", 0) or 0)
    except Exception:
        pending_buy_value = 0
    return {
        "daily_turnover_pct": turnover_value / start_value * 100 if start_value > 0 else 0,
        "daily_pnl_pct": (total_value / start_value - 1) * 100 if start_value > 0 else 0,
        "account_drawdown_pct": (total_value / g.peak_value - 1) * 100 if g.peak_value > 0 else 0,
        "consecutive_losses": int(getattr(g, "consecutive_losses", 0)),
        "pending_buy_position_pct": pending_buy_value / total_value * 100 if total_value > 0 else 0,
        "pending_buy_risk_pct": pending_buy_value / total_value * 9 if total_value > 0 else 0,
    }
