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
MAX_TOTAL_POSITION_PCT = 80.0
STRATEGY_TEMPLATE_VERSION = "2026-07-09.2-order-target-value"


def initialize(context):
    g.signals = []
    g.executed_signal_ids = set()
    g.order_events = []
    run_daily(post_account_snapshot, time="15:05")
    if STARTUP_SELF_TEST:
        startup_self_test(context)


def handle_data(context, data):
    fetch_and_execute(context)


def fetch_and_execute(context):
    fetch_signals(context)
    event_count = execute_signals(context)
    if event_count or getattr(g, "order_events", []):
        post_account_snapshot(context)


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
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
    return datetime.now() - generated_at <= timedelta(minutes=MAX_SIGNAL_AGE_MIN)


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
    jq_code = signal.get("jq_code")
    if not jq_code:
        return False, "missing_code"
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


def _record_order(signal, status, reason="", order=None):
    event = {
        "id": signal.get("id"),
        "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": signal.get("action"),
        "code": signal.get("code"),
        "jq_code": signal.get("jq_code"),
        "name": signal.get("name"),
        "target_pct": signal.get("position_pct"),
        "status": status,
        "reason": reason,
        "order_id": _order_attr(order, "order_id"),
        "amount": _order_attr(order, "amount"),
        "filled": _order_attr(order, "filled"),
        "price": _order_attr(order, "price"),
    }
    g.order_events.append(event)
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
            _record_order(signal, "skipped", reason)
            if signal.get("id"):
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
                order = order_target(jq_code, 0)
                if order is None:
                    _record_order(signal, "failed", "suspended_or_rejected")
                else:
                    _record_order(signal, _order_attr(order, "status", "submitted"), order=order)
            except Exception as exc:
                _record_order(signal, "failed", str(exc))
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
                "avg_cost": pos.avg_cost,
                "price": pos.price,
                "market_value": pos.value,
                "pnl": pos.value - pos.avg_cost * pos.total_amount,
            }
        )
    payload = {
        "schema_version": 1,
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "joinquant",
        "strategy_template_version": STRATEGY_TEMPLATE_VERSION,
        "cash": context.portfolio.cash,
        "total_value": context.portfolio.total_value,
        "positions": positions,
        "orders": list(getattr(g, "order_events", [])),
        "trades": [],
    }
    try:
        _post_json(SNAPSHOT_URL, payload)
        log.info("post snapshot ok orders=%s positions=%s" % (len(payload["orders"]), len(positions)))
        g.order_events = []
    except Exception as exc:
        log.warn("post snapshot failed: %s" % exc)
