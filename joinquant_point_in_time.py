"""Python 3.6-compatible point-in-time replay engine for JoinQuant.

The engine is deliberately deterministic and credential free.  It reproduces
the live intraday watch-pool path with one explicit historical policy:

* per-stock news and LHB are not expanded in the live intraday watch pool, so
  their score is exactly zero and the status is recorded as
  ``intraday_watch_unexpanded``;
* same-day market news is not consumed because JoinQuant's daily CCTV table
  does not prove an intraday publication time.  Theme/news context therefore
  stays neutral instead of leaking an evening article into a morning decision;
* valuation and industry inputs are queried only for the previous completed
  trade day (valuation) or the requested historical date (industry);
* daily technical indicators consume only bars strictly before the decision
  date, adjusted with factors already known at the decision time.

Both the native JoinQuant backtest and the strict monthly exporter embed this
same module.  The internal replay ledger is used for rule admission in both
paths; the native strategy additionally mirrors its intents to JoinQuant's
matching engine so platform performance remains visible.
"""

import math
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

try:
    from collections.abc import Mapping
except ImportError:  # Python 3.6
    from collections import Mapping

if "EXECUTION_PLAN_VERSION" not in globals():
    try:
        from strategy_snapshot_runtime import (
            EXECUTION_PLAN_VERSION,
            build_candidate_pool_frame,
            initial_stop_price,
            market_regime,
            risk_position_pct,
            score_candidate_frame,
        )
    except ImportError:
        # Generated one-file artifacts place strategy_snapshot_runtime above
        # this source, so the names already exist in the global namespace.
        pass

if "build_factor_channel_rows" not in globals():
    try:
        from candidate_channels import (
            build_factor_channel_rows,
            merge_candidate_channels,
        )
        from factor_contracts import (
            FACTOR_PATH_LIMITDOWN,
            FACTOR_PATH_MOMENTUM,
            FACTOR_PATH_WAVE3,
        )
        from strategy_exit_runtime import evaluate_factor_exit
    except ImportError:
        # Generated one-file artifacts place the portable factor modules above
        # this source.
        pass


PIT_ENGINE_VERSION = "2026-08-10.1-multipath"
PIT_FEATURE_SCHEMA_VERSION = "live-candidate-v6-pit-multipath-wave3-limitdown-economics"
PIT_MARKET_DATA_VERSION = "joinquant-raw-factor-5m-v2"
PIT_NEWS_POLICY = "intraday_watch_unexpanded"
PIT_MARKET_NEWS_POLICY = "neutral_no_intraday_timestamp"
SHANGHAI_TZ = timezone(timedelta(hours=8))


class PointInTimeReplayError(ValueError):
    """Stable fail-closed replay error."""


class PortableDecisionContext:
    def __init__(
        self,
        dataset_id,
        decision_at,
        trade_date,
        snapshot,
        daily_history,
        universe_codes,
        market_snapshot=None,
        metadata=None,
    ):
        self.dataset_id = str(dataset_id)
        self.decision_at = str(decision_at)
        self.trade_date = str(trade_date)
        self.snapshot = snapshot
        self.daily_history = daily_history
        self.universe_codes = tuple(universe_codes)
        self.market_snapshot = market_snapshot
        self.metadata = dict(metadata or {})


class _PoolConfig:
    def __init__(self, mode, min_price, min_amount, limit):
        self.mode = mode
        self.min_price = float(min_price)
        self.min_amount = float(min_amount)
        self.limit = int(limit)


def _number(value, default=0.0):
    if value is None or isinstance(value, bool):
        return default
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _flag(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(value)


def _clean_code(value):
    digits = "".join(filter(str.isdigit, str(value or "")))[:6]
    return digits.zfill(6) if digits else ""


def _pool_pre_rejection_code(market, in_live_pool, scan):
    """Explain every analyzable reserve member without weakening strict mode.

    A reserve row can satisfy the raw amount and percentage filters yet sit
    outside the bounded live watch pool.  That is a normal score-cutoff
    rejection, not an unexplained cohort membership.
    """
    if in_live_pool:
        return ""
    if _number(market.get("cum_amount")) < float(scan["min_amount"]):
        return "buy_pool_amount_below_threshold"
    if _number(market.get("pct_chg")) < 4.0:
        return "buy_pool_pct_below_threshold"
    return "buy_pool_score_below_cutoff"


def _jq_code(value):
    code = _clean_code(value)
    if not code:
        return ""
    return code + (".XSHG" if code.startswith(("5", "6", "9")) else ".XSHE")


def _aware_datetime(value):
    text = _text(value).replace("Z", "+00:00")
    if len(text) < 6 or text[-6] not in ("+", "-") or text[-3] != ":":
        raise PointInTimeReplayError("TIMEZONE_REQUIRED")
    body = text[:-6]
    sign = 1 if text[-6] == "+" else -1
    try:
        offset = sign * (int(text[-5:-3]) * 60 + int(text[-2:]))
    except (TypeError, ValueError):
        raise PointInTimeReplayError("INVALID_DECISION_TIME")
    parsed = None
    for pattern in (
        "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(body, pattern)
            break
        except ValueError:
            pass
    if parsed is None:
        raise PointInTimeReplayError("INVALID_DECISION_TIME")
    parsed = parsed.replace(tzinfo=timezone(timedelta(minutes=offset)))
    return parsed.astimezone(SHANGHAI_TZ)


def _date_value(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise PointInTimeReplayError("INVALID_DATE")


def _timestamp_for_day(day, clock=time(15, 0)):
    return datetime.combine(_date_value(day), clock).replace(tzinfo=SHANGHAI_TZ).isoformat()


def _timed(value, available_at):
    return {"value": value, "available_at": str(available_at)}


def _daily_limit_prices(previous_close, code, is_st, trade_day, listing_days):
    previous = _number(previous_close)
    if previous <= 0:
        raise PointInTimeReplayError("PREVIOUS_CLOSE_REQUIRED")
    if int(_number(listing_days)) < 5:
        return 999999.0, 0.01
    clean = _clean_code(code)
    if _flag(is_st):
        rate = Decimal("0.05")
    elif clean.startswith("688"):
        rate = Decimal("0.20")
    elif clean.startswith(("300", "301")) and _date_value(trade_day) >= date(2020, 8, 24):
        rate = Decimal("0.20")
    else:
        rate = Decimal("0.10")
    base = Decimal(str(previous))
    high = (base * (Decimal("1") + rate)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )
    low = (base * (Decimal("1") - rate)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )
    return float(high), float(low)


def _normalize_price_frame(frame, requested_codes):
    try:
        import pandas as pd
    except ImportError:
        raise PointInTimeReplayError("PANDAS_REQUIRED")
    if frame is None:
        return pd.DataFrame()
    if not hasattr(frame, "reset_index"):
        raise PointInTimeReplayError("INVALID_JOINQUANT_PRICE_FRAME")
    result = frame.reset_index().copy()
    for source, target in (
        ("date", "time"), ("datetime", "time"), ("security", "code"),
        ("order_book_id", "code"), ("amount", "money"),
        ("limit_up", "high_limit"), ("limit_down", "low_limit"),
    ):
        if source in result.columns and target not in result.columns:
            result.rename(columns={source: target}, inplace=True)
    if "time" not in result.columns:
        candidates = [
            column for column in result.columns
            if str(result[column].dtype).startswith("datetime")
        ]
        if len(candidates) == 1:
            result.rename(columns={candidates[0]: "time"}, inplace=True)
        elif "index" in result.columns:
            result.rename(columns={"index": "time"}, inplace=True)
    if "code" not in result.columns and len(requested_codes) == 1:
        result["code"] = str(requested_codes[0])
    required = {
        "time", "code", "open", "high", "low", "close", "volume", "money",
        "paused", "high_limit", "low_limit", "factor",
    }
    missing = sorted(required.difference(result.columns))
    if missing:
        raise PointInTimeReplayError(
            "JOINQUANT_PRICE_FIELDS_MISSING: " + ",".join(missing)
        )
    result = result.loc[:, sorted(required)].copy()
    result["code"] = result["code"].astype(str)
    return result.sort_values(["time", "code"]).reset_index(drop=True)


def _technical_snapshot(history, current_price):
    try:
        import pandas as pd
    except ImportError:
        raise PointInTimeReplayError("PANDAS_REQUIRED")
    if history is None or history.empty:
        raise PointInTimeReplayError("STRICT_DAILY_HISTORY_REQUIRED")
    frame = history.copy().sort_values("time")
    for column in ("open", "high", "low", "close", "factor"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close", "factor"])
    if len(frame) < 30:
        raise PointInTimeReplayError("STRICT_DAILY_HISTORY_INSUFFICIENT")
    current_factor = _number(frame.iloc[-1]["factor"], 0.0)
    if current_factor <= 0:
        raise PointInTimeReplayError("STRICT_ADJUSTMENT_FACTOR_REQUIRED")
    for column in ("open", "high", "low", "close"):
        frame["adj_" + column] = frame[column] * frame["factor"] / current_factor
    closes = frame["adj_close"]
    ma5 = float(closes.tail(5).mean())
    ma10 = float(closes.tail(10).mean())
    ma20 = float(closes.tail(20).mean())
    ma30 = float(closes.tail(30).mean())
    previous = closes.shift(1)
    tr = pd.concat([
        (frame["adj_high"] - frame["adj_low"]).abs(),
        (frame["adj_high"] - previous).abs(),
        (frame["adj_low"] - previous).abs(),
    ], axis=1).max(axis=1)
    atr14 = float(tr.tail(14).mean())
    recent = frame.tail(20)
    pressure = float(recent["adj_high"].max())
    prior_close = float(closes.iloc[-1])
    if prior_close >= pressure:
        pressure_pct, pressure_label = 0.0, "突破/新高"
    else:
        pressure_pct = round((pressure - prior_close) / prior_close * 100.0, 2)
        if pressure_pct <= 5:
            pressure_label = "贴近前高"
        elif pressure_pct <= 12:
            pressure_label = "近端压力"
        else:
            pressure_label = "上方套牢重"
    support_candidates = [float(frame["adj_low"].tail(10).min()), ma20, ma30]
    support_candidates = [
        value for value in support_candidates
        if value > 0 and value <= current_price * 1.05
    ]
    support = max(support_candidates) if support_candidates else current_price * 0.97
    if current_price >= ma5 >= ma10 >= ma20 >= ma30:
        trend = "强势上行"
    elif current_price >= ma20 and ma5 >= ma10:
        trend = "趋势修复"
    elif current_price < ma20 and ma5 < ma10:
        trend = "弱势整理"
    else:
        trend = "震荡"
    return {
        "ma5": round(ma5, 2),
        "ma10": round(ma10, 2),
        "ma20": round(ma20, 2),
        "ma30": round(ma30, 2),
        "atr14": round(atr14, 4),
        "support_level": round(support, 2),
        "pressure_level": round(pressure, 2),
        "pressure_pct": pressure_pct,
        "pressure_label": pressure_label,
        "trend_state": trend,
    }


def _limit_quality(row):
    pct = _number(row.get("pct_chg"))
    price = _number(row.get("price"))
    high = _number(row.get("high"))
    low = _number(row.get("low"))
    if pct < 4:
        return "趋势观察"
    if pct < 9.5:
        return "强势拉升"
    if high == price and low == price:
        return "一字涨停"
    if high == price:
        return "封板较强"
    return "炸板/回落"


def _classify_mode(row, market_state):
    breakout = 0
    if _text(row.get("limit_quality")) in ("一字涨停", "封板较强", "强势拉升"):
        breakout += 2
    if _text(row.get("pressure_label")) in ("突破/新高", "贴近前高"):
        breakout += 2
    if _number(row.get("pct_chg")) >= 5:
        breakout += 1
    trend = 0
    if _text(row.get("trend_state")) in ("强势上行", "趋势修复", "回踩企稳"):
        trend += 2
    if _number(row.get("ma5")) >= _number(row.get("ma10")) > 0:
        trend += 1
    if _number(row.get("ma10")) >= _number(row.get("ma20")) > 0:
        trend += 1
    if market_state in ("强势进攻", "温和修复"):
        trend += 1
    if breakout >= 4 and _text(row.get("limit_quality")) in (
        "一字涨停", "封板较强", "强势拉升",
    ):
        return "short"
    if breakout >= max(3, trend):
        return "short"
    return "mid"


def _risk_decision(row, market_state):
    mode = _classify_mode(row, market_state)
    profile = {
        "short": (1.8, 2.0, 1.0, 0.3, 20.0, 1.0, 0.3, 0.8, 1.4),
        "mid": (2.5, 3.0, 1.5, 1.0, 15.0, 1.5, 0.5, 1.0, 1.6),
    }[mode]
    stop_mult, take_r, max_loss, confirm, cap, support_buffer, pressure_buffer, atr_floor, min_rr = profile
    price = _number(row.get("price"))
    support = _number(row.get("support_level"), price * 0.97)
    pressure = _number(row.get("pressure_level"), price * 1.03)
    atr = _number(row.get("atr14"), max(price * atr_floor / 100.0, 0.01))
    allowed = price > 0 and _number(row.get("amount")) >= 30000000
    if market_state == "风险释放":
        allowed = False
    if _text(row.get("trend_state")) == "明显破坏":
        allowed = False
    breakout = mode == "short" or _text(row.get("limit_quality")) in (
        "一字涨停", "封板较强", "强势拉升",
    )
    base_entry = (
        pressure * (1 + confirm / 100.0)
        if breakout and pressure > 0
        else support * (1 + confirm / 100.0)
    )
    entry = round(max(price, base_entry), 2)
    stop = min(
        entry - atr * stop_mult,
        support * (1 - support_buffer / 100.0) if support > 0 else entry,
    )
    if stop >= entry:
        stop = entry * (1 - max_loss / 100.0)
    stop = round(max(stop, 0.01), 2)
    risk = round(max(entry - stop, 0.01), 2)
    raw_take = entry + risk * take_r
    if mode == "mid" and pressure > 0:
        take = min(raw_take, pressure * (1 - pressure_buffer / 100.0))
        if take <= entry:
            take = entry
    else:
        take = raw_take
    take = round(take, 2)
    risk_reward = round((take - entry) / risk, 2) if risk > 0 else 0.0
    stop_pct = risk / entry * 100.0 if entry > 0 else 0.0
    position = min(cap, round(max_loss / stop_pct * 100.0, 2)) if stop_pct > 0 else 0.0
    support_gap = (price - support) / support * 100.0 if support > 0 else 0.0
    if mode == "mid" and support_gap > max(5.0, confirm + 4.0):
        allowed = False
    if risk_reward < min_rr:
        allowed = False
    if _text(row.get("trend_state")) in ("明显破坏", "弱势整理") and mode == "mid":
        allowed = False
    if allowed and price >= entry:
        buy_state = "已到买点"
    elif allowed and entry > price and (entry - price) / price * 100.0 <= 1.2:
        buy_state = "临近买点"
    elif allowed:
        buy_state = "等待确认"
    else:
        buy_state = "不建议介入"
    return {
        "mode": mode,
        "allowed": bool(allowed),
        "entry_price": entry,
        "risk_reward": risk_reward,
        "position_cap_pct": position,
        "buy_state": buy_state,
    }


def _theme_heat(row):
    score = 0.0
    rank = _number(row.get("amount_rank_pct"))
    if rank >= 0.85:
        score += 2.5
    elif rank >= 0.65:
        score += 1.5
    elif rank >= 0.45:
        score += 0.8
    quality = _text(row.get("limit_quality"))
    if quality in ("封板较强", "一字涨停"):
        score += 1.0
    elif quality == "炸板/回落":
        score -= 0.8
    if _text(row.get("pressure_label")) == "突破/新高":
        score += 0.8
    if score >= 6:
        level = "高"
    elif score >= 3:
        level = "中"
    elif score >= 1:
        level = "低"
    else:
        level = "待确认"
    return round(score, 2), level


def _trade_score(row, market_state):
    tech = 20.0
    quality = _text(row.get("limit_quality"))
    if quality == "一字涨停":
        tech += 16
    elif quality == "封板较强":
        tech += 14
    elif quality == "强势拉升":
        tech += 8
    elif quality == "炸板/回落":
        tech -= 10
    pressure = _text(row.get("pressure_label"))
    if pressure == "突破/新高":
        tech += 8
    elif pressure in ("贴近前高", "近端压力"):
        tech += 4
    elif pressure == "上方套牢重":
        tech -= 4
    pct = _number(row.get("pct_chg"))
    tech += 5 if pct >= 7 else (3 if pct >= 4 else (-3 if pct < 3 else 0))
    rank = _number(row.get("amount_rank_pct"))
    tech += 5 if rank >= 0.8 else (3 if rank >= 0.6 else (-2 if rank < 0.3 else 0))
    news = 10.0
    fund = 8.0  # live intraday watch path marks LHB as not checked
    theme = 8.0
    heat = _number(row.get("theme_heat_score"))
    level = _text(row.get("theme_heat_level"))
    if level == "高" or heat >= 6:
        theme += 6
    elif level == "中" or heat >= 3:
        theme += 4
    elif level == "低" or heat >= 1:
        theme += 2
    else:
        theme -= 2
    market = 4.0
    if market_state == "强势进攻":
        market += 6
    elif market_state == "温和修复":
        market += 4
    elif market_state == "弱势震荡":
        market += 1
    else:
        market -= 4
    return max(0.0, min(100.0, round(tech + news + fund + theme + market, 1)))


class PointInTimeReplayEngine:
    """Shared feature provider, replay ledger and JoinQuant adapter."""

    def __init__(self, parameters, manifest, namespace=None, initial_cash=200000.0):
        if not isinstance(parameters, Mapping) or not isinstance(manifest, Mapping):
            raise PointInTimeReplayError("PIT_CONFIGURATION_REQUIRED")
        self.parameters = dict(parameters)
        self.manifest = dict(manifest)
        self.namespace = dict(namespace or {})
        self.platform_mirror_enabled = callable(self.namespace.get("order_target"))
        self.initial_cash = float(initial_cash)
        if self.initial_cash <= 0:
            raise PointInTimeReplayError("PIT_INITIAL_CASH_INVALID")
        self.cash = self.initial_cash
        self.positions = {}
        self.pending = []
        self.cooldown = {}
        self.signal_first_seen = {}
        self.last_decision = None
        self.last_feature_decision = ""
        self.last_observed_decision = ""
        self.last_rows = []
        self.last_context = None
        self.market_state = "弱势震荡"
        self.peak_equity = self.initial_cash
        self.day_start_equity = self.initial_cash
        self.current_day = ""
        self.seen_trade_days = []
        self.new_positions_today = 0
        self.orders_today = 0
        self.turnover_today = 0.0
        self.consecutive_losses = 0
        self.platform_intents = []
        self.exit_intents = []
        self.skipped_candidate_events = []
        self.factor_audit = []
        self._history_cache = {}
        self._valuation_cache = {}
        self._industry_cache = {}
        self._jq_day_cache = {}
        self._intraday_amount = {}
        self._factor_intraday_bars = {}
        self.factor_new_positions_today = {
            "wave3_v1": 0,
            "limitdown_exhaustion_v1": 0,
        }

    def _api(self, name):
        candidate = self.namespace.get(name)
        if callable(candidate):
            return candidate
        try:
            from jqdata import apis
            candidate = getattr(apis, name, None)
        except ImportError:
            candidate = None
        if callable(candidate):
            return candidate
        raise PointInTimeReplayError("JOINQUANT_API_REQUIRED: " + name)

    def _new_day(self, day, snapshot):
        if day == self.current_day:
            return
        # Histories are keyed by trade date and are never reused by a later
        # decision day.  Evict them here so a month replay retains one day's
        # technical-history frames instead of accumulating all prior days.
        self._history_cache.clear()
        # Valuation and industry evidence is day-scoped as well.  Earlier-day
        # dictionaries are never consulted after the replay clock advances.
        self._valuation_cache.clear()
        self._industry_cache.clear()
        if day not in self.seen_trade_days:
            self.seen_trade_days.append(day)
        self.current_day = day
        self.new_positions_today = 0
        self.orders_today = 0
        self.turnover_today = 0.0
        self.factor_new_positions_today = {
            "wave3_v1": 0,
            "limitdown_exhaustion_v1": 0,
        }
        self._factor_intraday_bars = {}
        self.day_start_equity = self._equity(snapshot)

    def _snapshot_by_code(self, snapshot, codes):
        wanted = set(_clean_code(code) for code in codes)
        if snapshot is None or not wanted:
            return {}
        result = {}
        clean_codes = [_clean_code(value) for value in snapshot["code"].tolist()]
        for index, code in enumerate(clean_codes):
            if code not in wanted:
                continue
            result[code] = snapshot.iloc[index]
            if len(result) == len(wanted):
                break
        return result

    def _record_factor_intraday_bars(self, context):
        """Retain at most one trading day's completed 5-minute evidence."""
        if context.snapshot is None or getattr(context.snapshot, "empty", True):
            return
        decision_at = _aware_datetime(context.decision_at).isoformat()
        for _, market in context.snapshot.iterrows():
            code = _clean_code(market.get("code"))
            if not code:
                continue
            rows = self._factor_intraday_bars.setdefault(code, [])
            if rows and rows[-1]["available_at"] == decision_at:
                continue
            rows.append({
                "available_at": decision_at,
                "open": _number(market.get("open")),
                "high": _number(market.get("high")),
                "low": _number(market.get("low")),
                "close": _number(market.get("close")),
                "volume": _number(market.get("volume")),
                "money": _number(market.get("money")),
                "amount": _number(market.get("money")),
                "prev_close": _number(market.get("prev_close")),
                "pre_close": _number(market.get("prev_close")),
                "low_limit": _number(market.get("low_limit")),
                "high_limit": _number(market.get("high_limit")),
            })
            if len(rows) > 60:
                del rows[:-60]

    def _equity(self, snapshot):
        if not self.positions:
            return max(self.cash, 0.0)
        by_code = self._snapshot_by_code(snapshot, self.positions)
        value = self.cash
        for code, position in self.positions.items():
            market = by_code.get(code)
            price = _number(market.get("close")) if market is not None else _number(position.get("last_price"))
            value += int(position["qty"]) * price
        return max(value, 0.0)

    def _fees(self, side, price, qty):
        schedule = self.parameters.get("execution", {}).get("fee_schedule", {})
        notional = float(price) * int(qty)
        commission = max(
            _number(schedule.get(
                "buy_minimum_commission_yuan" if side == "buy" else "sell_minimum_commission_yuan"
            ), 5.0),
            notional * _number(schedule.get(
                "buy_commission_rate" if side == "buy" else "sell_commission_rate"
            ), 0.0003),
        )
        stamp = notional * _number(schedule.get("stamp_tax_rate"), 0.0005) if side == "sell" else 0.0
        other = notional * (
            _number(schedule.get("transfer_fee_rate"))
            + _number(schedule.get("other_fee_rate"))
        )
        return round(commission + stamp + other, 2)

    def _advance_pending(self, context):
        if not self.pending:
            return
        decision = _aware_datetime(context.decision_at)
        pending_codes = set(item["code"] for item in self.pending)
        by_code = self._snapshot_by_code(context.snapshot, pending_codes)
        remaining = []
        executed = []
        for intent in sorted(self.pending, key=lambda item: 0 if item["side"] == "sell" else 1):
            if _aware_datetime(intent["created_at"]) >= decision:
                remaining.append(intent)
                continue
            code = intent["code"]
            market = by_code.get(code)
            if market is None or _flag(market.get("paused")):
                continue
            open_price = _number(market.get("open"))
            if open_price <= 0:
                continue
            if intent["side"] == "buy":
                if open_price >= _number(market.get("high_limit")):
                    continue
                qty = int(intent["target_qty"])
                slippage = _number(
                    self.parameters.get("execution", {}).get("fee_schedule", {}).get("buy_slippage_rate"),
                    0.001,
                )
                price = open_price * (1.0 + slippage)
                fee = self._fees("buy", price, qty)
                required = price * qty + fee
                if qty <= 0 or required > self.cash:
                    continue
                self.cash -= required
                self.positions[code] = {
                    "code": code,
                    "qty": qty,
                    "initial_qty": qty,
                    "entry_price": price,
                    "entry_date": context.trade_date,
                    "entry_day_index": len(self.seen_trade_days) - 1,
                    "stop_loss": _number(intent.get("stop_loss")),
                    "take_profit": _number(intent.get("take_profit")),
                    "atr14": _number(intent.get("atr14")),
                    "mode": _text(intent.get("mode")) or "short",
                    "industry": _text(intent.get("industry")),
                    "theme": _text(intent.get("theme")),
                    "highest": open_price,
                    "stage": 0,
                    "profit_protection": False,
                    "last_price": open_price,
                    "factor_path": _text(intent.get("factor_path")) or "momentum_v1",
                    "factor_setup_id": _text(intent.get("factor_setup_id")),
                }
                factor_path = _text(intent.get("factor_path"))
                if factor_path in self.factor_new_positions_today:
                    self.factor_new_positions_today[factor_path] += 1
                self.new_positions_today += 1
                self.orders_today += 1
                self.turnover_today += price * qty
                executed.append({
                    "side": "buy", "code": code, "target_qty": qty,
                    "reason": intent.get("reason", "selected"),
                    "created_at": context.decision_at,
                })
            else:
                position = self.positions.get(code)
                if position is None or context.trade_date <= position["entry_date"]:
                    if position is not None:
                        remaining.append(intent)
                    continue
                if open_price <= _number(market.get("low_limit")):
                    remaining.append(intent)
                    continue
                target = max(0, min(int(intent["target_qty"]), int(position["qty"])))
                sell_qty = int(position["qty"]) - target
                if sell_qty <= 0:
                    continue
                slippage = _number(
                    self.parameters.get("execution", {}).get("fee_schedule", {}).get("sell_slippage_rate"),
                    0.001,
                )
                price = open_price * (1.0 - slippage)
                fee = self._fees("sell", price, sell_qty)
                self.cash += price * sell_qty - fee
                pnl = (price - _number(position["entry_price"])) * sell_qty - fee
                self.turnover_today += price * sell_qty
                self.orders_today += 1
                position["qty"] = target
                position["last_price"] = open_price
                if pnl < 0:
                    self.consecutive_losses += 1
                else:
                    self.consecutive_losses = 0
                if target <= 0:
                    self.positions.pop(code, None)
                    self.cooldown[code] = context.trade_date
                else:
                    position["stage"] = max(1, int(position.get("stage", 0)))
                executed.append({
                    "side": "sell", "code": code, "target_qty": target,
                    "reason": intent.get("reason", "exit"),
                    "created_at": context.decision_at,
                })
        self.pending = remaining
        if self.platform_mirror_enabled and executed:
            self.platform_intents.extend(executed)

    def _exit_targets(self, context):
        result = []
        by_code = self._snapshot_by_code(context.snapshot, self.positions)
        pending_sells = {item["code"] for item in self.pending if item["side"] == "sell"}
        for code, position in list(self.positions.items()):
            market = by_code.get(code)
            if market is None:
                continue
            price = _number(market.get("close"))
            high = _number(market.get("high"), price)
            position["highest"] = max(_number(position.get("highest")), high)
            position["last_price"] = price
            if code in pending_sells or price <= 0:
                continue
            entry = _number(position["entry_price"])
            stop = _number(position["stop_loss"])
            risk = max(entry - stop, 0.0)
            r_multiple = (price - entry) / risk if risk > 0 else 0.0
            target = None
            reason = ""
            factor_path = _text(position.get("factor_path")) or "momentum_v1"
            factor_exit = evaluate_factor_exit(
                factor_path,
                entry,
                stop,
                price,
                _number(position.get("highest")),
                _number(position.get("atr14")),
                max(0, len(self.seen_trade_days) - 1 - int(position["entry_day_index"])),
                market_regime(self.market_state),
            )
            if factor_exit is not None:
                if factor_exit["action"] == "raise_stop":
                    position["stop_loss"] = max(
                        stop, _number(factor_exit.get("new_stop_price"))
                    )
                elif factor_exit["action"] == "exit_all":
                    target, reason = 0, _text(factor_exit.get("reason"))
            elif stop > 0 and price <= stop:
                target, reason = 0, "hard_stop"
            elif market_regime(self.market_state) == "RISK_OFF":
                target, reason = 0, "market_risk_exit"
            else:
                trailing = 0.0
                if (int(position.get("stage", 0)) >= 1 or position.get("profit_protection")) and _number(position.get("atr14")) > 0:
                    multiple = 2.0 if position.get("mode") == "short" else 3.0
                    trailing = max(stop, _number(position["highest"]) - multiple * _number(position["atr14"]))
                if trailing > 0 and price <= trailing:
                    target, reason = 0, "trailing_stop"
                elif int(position.get("stage", 0)) == 0 and r_multiple >= 2.0:
                    initial = int(position["initial_qty"])
                    lot = int(self.parameters.get("ml", {}).get("board_lot_size", 100))
                    minimum = (initial + 1) // 2
                    first_target = min(initial, ((minimum + lot - 1) // lot) * lot)
                    if first_target == int(position["qty"]):
                        position["profit_protection"] = True
                        position["stage"] = 1
                    elif first_target < int(position["qty"]):
                        target, reason = first_target, "take_profit_1"
                if target is None:
                    held_days = max(0, len(self.seen_trade_days) - 1 - int(position["entry_day_index"]))
                    limit_days = 3 if position.get("mode") == "short" else 10
                    progress = 0.5 if position.get("mode") == "short" else 1.0
                    if held_days >= limit_days and r_multiple < progress:
                        target, reason = 0, "time_stop"
            if target is not None:
                result.append({
                    "side": "sell", "code": code, "target_qty": int(target),
                    "reason": reason, "created_at": context.decision_at,
                })
        return result

    def _market_state(self, context):
        market = context.market_snapshot
        if market is None or getattr(market, "empty", True):
            raise PointInTimeReplayError("STRICT_MARKET_INDEX_REQUIRED")
        row = market.sort_values("time").iloc[-1]
        previous = _number(row.get("prev_close"))
        close = _number(row.get("close"))
        if previous <= 0 or close <= 0:
            raise PointInTimeReplayError("STRICT_MARKET_INDEX_PREV_CLOSE_REQUIRED")
        sh_pct = (close / previous - 1.0) * 100.0
        pct = context.snapshot["pct_chg"]
        up = int((pct > 0).sum())
        down = int((pct < 0).sum())
        median = float(pct.median())
        if sh_pct > 0.8 and up > down and median >= 0:
            return "强势进攻"
        if sh_pct > 0:
            return "温和修复"
        if sh_pct > -0.8:
            return "弱势震荡"
        return "风险释放"

    def _history_for(self, context, code):
        key = (context.trade_date, code)
        if key in self._history_cache:
            return self._history_cache[key]
        frame = context.daily_history
        if frame is not None and not getattr(frame, "empty", True):
            chosen = frame[
                frame["code"].map(_clean_code) == code
            ].copy()
            chosen = chosen.loc[[
                _date_value(value) < _date_value(context.trade_date)
                for value in chosen["time"]
            ]]
        else:
            provider = (
                self.namespace.get("pit_history_provider")
                or self.namespace.get("strict_pit_history_provider")
            )
            if callable(provider):
                chosen = provider([code], context.trade_date)
            else:
                end = _date_value(context.trade_date) - timedelta(days=1)
                start = end - timedelta(days=180)
                chosen = self._fetch_prices(
                    [_jq_code(code)], start, end, "daily"
                )
            chosen = chosen.loc[[
                _date_value(value) < _date_value(context.trade_date)
                for value in chosen["time"]
            ]]
        self._history_cache[key] = chosen.sort_values("time").tail(90).copy()
        return self._history_cache[key]

    def _prime_histories(self, context, codes):
        frame = context.daily_history
        if frame is not None and not getattr(frame, "empty", True):
            return
        day = _date_value(context.trade_date)
        missing = [
            code for code in codes
            if (context.trade_date, code) not in self._history_cache
        ]
        if not missing:
            return
        provider = (
            self.namespace.get("pit_history_provider")
            or self.namespace.get("strict_pit_history_provider")
        )
        if callable(provider):
            fetched = provider(missing, context.trade_date)
        else:
            fetched = self._fetch_prices(
                [_jq_code(code) for code in missing],
                day - timedelta(days=180),
                day - timedelta(days=1),
                "daily",
            )
        for code in missing:
            chosen = fetched[
                fetched["code"].map(_clean_code) == code
            ].copy()
            chosen = chosen.loc[[
                _date_value(value) < day for value in chosen["time"]
            ]]
            self._history_cache[(context.trade_date, code)] = (
                chosen.sort_values("time").tail(90).copy()
            )

    def _previous_trade_day(self, context):
        frame = context.daily_history
        if frame is not None and not getattr(frame, "empty", True):
            dates = sorted({
                _date_value(value) for value in frame["time"]
                if _date_value(value) < _date_value(context.trade_date)
            })
            if dates:
                return dates[-1]
        days = self._api("get_trade_days")(
            start_date=_date_value(context.trade_date) - timedelta(days=15),
            end_date=_date_value(context.trade_date) - timedelta(days=1),
        )
        if len(days) == 0:
            raise PointInTimeReplayError("PREVIOUS_TRADE_DAY_REQUIRED")
        return _date_value(days[-1])

    def _valuation(self, context, codes):
        prior = self._previous_trade_day(context)
        cache_key = prior.isoformat()
        cache = self._valuation_cache.setdefault(cache_key, {})
        missing = [code for code in codes if code not in cache]
        provider = self.namespace.get("pit_valuation_provider")
        if missing and callable(provider):
            cache.update(provider(missing, prior))
        elif missing:
            try:
                from jqdata import apis as jq_apis
            except ImportError:
                raise PointInTimeReplayError("JOINQUANT_VALUATION_API_REQUIRED")
            get_fundamentals = getattr(jq_apis, "get_fundamentals", None)
            query = getattr(jq_apis, "query", None)
            valuation = getattr(jq_apis, "valuation", None)
            if not callable(get_fundamentals) or not callable(query) or valuation is None:
                raise PointInTimeReplayError("JOINQUANT_VALUATION_API_REQUIRED")
            for offset in range(0, len(missing), 500):
                batch = [_jq_code(code) for code in missing[offset:offset + 500]]
                request = query(
                    valuation.code,
                    valuation.market_cap,
                    valuation.circulating_market_cap,
                ).filter(valuation.code.in_(batch))
                result = get_fundamentals(request, date=prior)
                if result is None:
                    continue
                for _, row in result.iterrows():
                    code = _clean_code(row.get("code"))
                    cache[code] = {
                        "market_cap": _number(row.get("market_cap")) * 100000000.0,
                        "circulating_market_cap": _number(row.get("circulating_market_cap")) * 100000000.0,
                    }
        absent = [code for code in codes if _number(cache.get(code, {}).get("market_cap")) <= 0]
        if absent:
            raise PointInTimeReplayError(
                "STRICT_MARKET_CAP_REQUIRED: " + ",".join(absent[:10])
            )
        return cache, _timestamp_for_day(prior)

    def _industries(self, context, codes):
        day = context.trade_date
        cache = self._industry_cache.setdefault(day, {})
        missing = [code for code in codes if code not in cache]
        provider = self.namespace.get("pit_industry_provider")
        if missing and callable(provider):
            cache.update(provider(missing, _date_value(day)))
        elif missing:
            getter = self._api("get_industry")
            for offset in range(0, len(missing), 500):
                batch = [_jq_code(code) for code in missing[offset:offset + 500]]
                values = getter(batch, date=day) or {}
                for jq_code in batch:
                    details = values.get(jq_code) or {}
                    chosen = details.get("sw_l1") or details.get("jq_l1") or details.get("zjw") or {}
                    cache[_clean_code(jq_code)] = _text(chosen.get("industry_name")) or "未识别"
        for code in missing:
            cache.setdefault(code, "未识别")
        return cache, _timestamp_for_day(day, time(9, 30))

    def _signal_lifecycle(self, row, decision):
        code = _clean_code(row.get("code"))
        mode = _text(row.get("mode")) or "mid"
        key = (code, mode)
        first = self.signal_first_seen.setdefault(key, decision.date())
        age = max(0, (decision.date() - first).days)
        policy = {"short": (1, 2, 3), "mid": (3, 5, 10)}[mode]
        fresh, stale, stop_days = policy
        if age <= fresh:
            state, action = "fresh", "continue"
        elif age <= stale:
            state, action = "watch", "watch"
        elif age <= stop_days:
            state, action = "stale", "reevaluate"
        else:
            state, action = "time_stop", "time_stop"
        price = _number(row.get("price"))
        if price >= _number(row.get("take_profit")) > 0:
            state, action = "target_hit", "take_profit"
        elif 0 < price <= _number(row.get("stop_loss")):
            state, action = "stop_hit", "stop_loss"
        return state, action, age

    def feature_provider(self, context):
        try:
            import pandas as pd
        except ImportError:
            raise PointInTimeReplayError("PANDAS_REQUIRED")
        if self.last_feature_decision == context.decision_at:
            return self.last_rows
        decision = _aware_datetime(context.decision_at)
        if self.last_decision is not None and decision <= self.last_decision:
            raise PointInTimeReplayError("DECISION_TIME_NOT_MONOTONIC")
        self._new_day(context.trade_date, context.snapshot)
        self._advance_pending(context)
        self.market_state = self._market_state(context)
        self.exit_intents = self._exit_targets(context)
        frame = context.snapshot.copy()
        frame["price"] = frame["close"]
        frame["amount"] = frame["cum_amount"]
        frame["gap"] = (
            (frame["open"] / frame["prev_close"] - 1.0) * 100.0
        )
        self._record_factor_intraday_bars(context)
        scan = self.parameters["scan"]
        momentum_limit = max(
            int(scan["top"]) * int(scan["intraday_watch_multiplier"]),
            int(scan["top"]) + 12,
        )
        multipath = dict(self.parameters.get("multipath") or {})
        if multipath.get("enabled"):
            momentum_limit = min(
                momentum_limit, int(multipath.get("momentum_max", 30))
            )
        live_pool = build_candidate_pool_frame(
            frame,
            _PoolConfig(
                "intraday", scan["min_price"], scan["min_amount"],
                momentum_limit,
            ),
        )
        live_codes = set(_clean_code(value) for value in live_pool["code"])
        eligible = frame.copy()
        eligible = eligible[
            ~eligible["name"].str.contains("ST|退", regex=True, na=False)
        ]
        eligible = eligible[
            (eligible["price"] >= float(scan["min_price"]))
            & (eligible["prev_close"] > 0)
            & (eligible["listing_days"] >= 30)
        ].copy()
        if eligible.empty:
            raise PointInTimeReplayError("EMPTY_STRICT_CANDIDATE_COHORT")
        eligible["cohort_score"] = (
            eligible["pct_chg"].rank(pct=True).fillna(0) * 60
            + eligible["amount"].rank(pct=True).fillna(0) * 40
        )
        factor_pool = pd.DataFrame()
        self.factor_audit = []
        if multipath.get("enabled"):
            factor_screen = eligible[
                (eligible["amount"] >= 20000000.0)
                & (eligible["pct_chg"] >= -8.0)
                & (eligible["pct_chg"] <= 9.8)
            ].sort_values(
                ["amount", "pct_chg", "code"],
                ascending=[False, False, True],
            ).head(int(multipath.get("factor_screen_max", 60))).copy()
            factor_codes_for_history = [
                _clean_code(value) for value in factor_screen["code"]
            ]
            self._prime_histories(context, factor_codes_for_history)
            factor_industries, _ = self._industries(
                context, factor_codes_for_history,
            )
            factor_screen["industry"] = factor_screen["code"].map(
                lambda value: factor_industries.get(_clean_code(value), "未识别")
            )
            industry_medians = factor_screen.groupby("industry")["pct_chg"].median()
            market_median = float(eligible["pct_chg"].median())
            relative_by_code = {}
            for _, factor_market in factor_screen.iterrows():
                industry = _text(factor_market.get("industry"))
                peer_count = int((factor_screen["industry"] == industry).sum())
                benchmark = (
                    float(industry_medians.loc[industry])
                    if peer_count >= 2 else market_median
                )
                relative_by_code[_clean_code(factor_market.get("code"))] = (
                    _number(factor_market.get("pct_chg")) - benchmark
                ) / 100.0
            factor_input = frame.copy()
            factor_input["industry_relative_strength"] = factor_input["code"].map(
                lambda value: relative_by_code.get(_clean_code(value), 0.0)
            )
            factor_input["theme_heat_score"] = 0.0

            def factor_history_provider(code):
                return self._history_for(context, _clean_code(code))

            def factor_intraday_provider(code):
                return list(
                    self._factor_intraday_bars.get(_clean_code(code), ())
                )

            factor_pool, self.factor_audit = build_factor_channel_rows(
                factor_input,
                context.decision_at,
                factor_history_provider,
                factor_intraday_provider,
                market_state=market_regime(self.market_state),
                disclosure_provider=lambda code: "unavailable_neutral",
                wave3_enabled=bool(multipath.get("wave3_enabled")),
                limitdown_enabled=bool(multipath.get("limitdown_enabled")),
                settings=multipath,
            )
        active_pool = merge_candidate_channels(
            live_pool,
            factor_pool,
            multipath if multipath.get("enabled") else {
                "momentum_max": momentum_limit,
                "wave3_max": 0,
                "limitdown_max": 0,
                "total_max": momentum_limit,
            },
        )
        factor_audit_by_code = {}
        for audit_item in self.factor_audit:
            audit_code = _clean_code(audit_item.get("code"))
            factor_audit_by_code.setdefault(audit_code, []).append(audit_item)
        if not active_pool.empty:
            active_pool["factor_screen_audit"] = active_pool["code"].map(
                lambda value: factor_audit_by_code.get(_clean_code(value), [])
            )
        active_codes = set(
            _clean_code(value) for value in active_pool.get("code", ())
        )
        filler = eligible[
            ~eligible["code"].map(_clean_code).isin(active_codes)
        ].sort_values(["cohort_score", "code"], ascending=[False, True])
        total_limit = (
            int(multipath.get("total_max", 45))
            if multipath.get("enabled") else momentum_limit + 12
        )
        needed = max(0, total_limit - len(active_pool))
        pool = active_pool.copy()
        if needed:
            pool = pd.concat([pool, filler.head(needed)], ignore_index=True, sort=False)
        if pool.empty:
            raise PointInTimeReplayError("EMPTY_STRICT_CANDIDATE_COHORT")
        if "cohort_score" not in pool.columns:
            pool["cohort_score"] = pool["score"]
        if "score" not in pool.columns:
            pool["score"] = pool["cohort_score"]
        pool["score"] = pool["score"].fillna(pool["cohort_score"])
        pool["amount_rank_pct"] = pool["amount"].rank(pct=True).fillna(0)
        codes = [_clean_code(value) for value in pool["code"]]
        self._prime_histories(context, codes)
        valuation, valuation_at = self._valuation(context, codes)
        industries, industry_at = self._industries(context, codes)
        regime = market_regime(self.market_state)
        now = decision.isoformat()
        rows = []
        for _, market in pool.iterrows():
            code = _clean_code(market.get("code"))
            price = _number(market.get("close"))
            history = self._history_for(context, code)
            try:
                tech = _technical_snapshot(history, price)
            except PointInTimeReplayError as exc:
                reason = _text(exc)
                if reason not in (
                    "STRICT_DAILY_HISTORY_REQUIRED",
                    "STRICT_DAILY_HISTORY_INSUFFICIENT",
                    "STRICT_ADJUSTMENT_FACTOR_REQUIRED",
                ):
                    raise
                self.skipped_candidate_events.append({
                    "decision_at": context.decision_at,
                    "code": code,
                    "reason": reason,
                })
                continue
            row = dict(market)
            row.update(tech)
            row["code"] = code
            row["price"] = price
            row["amount"] = _number(market.get("cum_amount"))
            in_live_pool = code in live_codes
            factor_path = _text(market.get("factor_path")) or "momentum_v1"
            factor_triggered = _flag(market.get("factor_triggered"))
            if factor_path in ("wave3_v1", "limitdown_exhaustion_v1"):
                pre_rejection = (
                    "" if factor_triggered
                    else _text(market.get("factor_rejection_code"))
                    or "factor_trigger_required"
                )
            else:
                pre_rejection = _pool_pre_rejection_code(
                    market, in_live_pool, scan,
                )
            row["limit_quality"] = _limit_quality(row)
            row["market_state"] = self.market_state
            row["industry"] = industries[code]
            row["theme_label"] = industries[code] if industries[code] != "未识别" else "题材待确认"
            row["news_score"] = 0.0
            row["amount_rank_pct"] = _number(market.get("amount_rank_pct"))
            heat, heat_level = _theme_heat(row)
            row["theme_heat_score"] = heat
            row["theme_heat_level"] = heat_level
            risk = _risk_decision(row, self.market_state)
            row["mode"] = risk["mode"]
            row["entry_price"] = risk["entry_price"]
            board = (
                "growth" if code.startswith(("300", "301", "688"))
                else ("main_low" if price > 0 and tech["atr14"] / price <= 0.02 else "main_active")
            )
            stop = initial_stop_price(
                risk["entry_price"], tech["support_level"], tech["atr14"], board,
            )
            risk_per_share = max(risk["entry_price"] - stop, 0.0)
            take = round(risk["entry_price"] + 2.0 * risk_per_share, 2)
            position = risk_position_pct(
                risk["entry_price"], stop, board,
                risk["position_cap_pct"], regime,
            )
            if factor_path in ("wave3_v1", "limitdown_exhaustion_v1"):
                factor_entry = _number(market.get("entry_price"))
                factor_stop = _number(market.get("stop_loss"))
                factor_take = _number(market.get("take_profit"))
                factor_risk = max(factor_entry - factor_stop, 0.0)
                factor_distance = (
                    factor_risk / factor_entry * 100.0
                    if factor_entry > 0 else 0.0
                )
                factor_cap = _number(market.get("factor_position_cap_pct"))
                factor_budget = _number(market.get("factor_risk_budget_pct"))
                factor_position = (
                    min(factor_cap, factor_budget / factor_distance * 100.0)
                    if factor_distance > 0 else 0.0
                )
                factor_valid = (
                    factor_triggered
                    and 0 < factor_stop < factor_entry < factor_take
                    and factor_position > 0
                )
                row["mode"] = (
                    "mid" if factor_path == "wave3_v1" else "short"
                )
                row["entry_price"] = factor_entry if factor_valid else 0.0
                stop = factor_stop if factor_valid else 0.0
                take = factor_take if factor_valid else 0.0
                risk_per_share = factor_risk if factor_valid else 0.0
                position = round(factor_position, 2) if factor_valid else 0.0
            held = code in self.positions
            row["execution_plan_version"] = EXECUTION_PLAN_VERSION
            row["execution_allowed"] = bool(
                risk["allowed"] and position > 0 and not held
                and (
                    factor_triggered
                    if factor_path in ("wave3_v1", "limitdown_exhaustion_v1")
                    else True
                )
            )
            row["stop_loss"] = stop
            row["take_profit"] = take
            row["risk_reward"] = 2.0 if risk_per_share > 0 else 0.0
            row["position_pct"] = position
            row["board_type"] = board
            row["market_regime"] = regime
            row["buy_state"] = "持仓观察" if held else risk["buy_state"]
            state, action, age = self._signal_lifecycle(row, decision)
            row["signal_state"] = state
            row["signal_action"] = action
            row["signal_age_days"] = age
            row["trade_score"] = _trade_score(row, self.market_state)
            circulating = _number(valuation[code].get("circulating_market_cap"))
            if circulating <= 0:
                raise PointInTimeReplayError("STRICT_CIRCULATING_MARKET_CAP_REQUIRED: " + code)
            shares = circulating / max(_number(history.iloc[-1].get("close")), 0.01)
            turnover = _number(market.get("cum_volume")) / shares * 100.0
            feature_values = {
                "price": price,
                "pct_chg": _number(market.get("pct_chg")),
                "amount": _number(market.get("cum_amount")),
                "turnover": round(turnover, 6),
                "market_cap": _number(valuation[code].get("market_cap")),
                "score": _number(market.get("score")),
                "final_score": 0.0,
                "global_risk_score": {"NORMAL": 20.0, "CAUTION": 60.0, "RISK_OFF": 100.0}[regime],
                "trade_score": row["trade_score"],
                "news_score": 0.0,
                "risk_reward": row["risk_reward"],
                "entry_price": row["entry_price"],
                "stop_loss": row["stop_loss"],
                "take_profit": row["take_profit"],
                "pressure_pct": tech["pressure_pct"],
                "pressure_label": tech["pressure_label"],
                "ma5": tech["ma5"], "ma10": tech["ma10"],
                "ma20": tech["ma20"], "ma30": tech["ma30"],
                "atr14": tech["atr14"],
                "theme_label": row["theme_label"],
                "theme_heat_level": heat_level,
                "theme_heat_score": heat,
                "market_state": self.market_state,
                "signal_state": state,
                "signal_age_days": age,
                "buy_state": row["buy_state"],
                "market_regime": regime,
                "position_pct": position,
                "execution_plan_version": EXECUTION_PLAN_VERSION,
                "execution_allowed": row["execution_allowed"],
                "factor_path": factor_path,
                "candidate_channels": _text(market.get("candidate_channels")) or factor_path,
                "factor_setup_id": _text(market.get("factor_setup_id")),
                "factor_state": _text(market.get("factor_state")),
                "factor_score": _number(market.get("factor_score")),
                "factor_triggered": factor_triggered,
                "factor_rejection_code": _text(market.get("factor_rejection_code")),
                "factor_position_cap_pct": _number(market.get("factor_position_cap_pct")),
                "factor_risk_budget_pct": _number(market.get("factor_risk_budget_pct")),
                "factor_max_hold_days": int(_number(market.get("factor_max_hold_days"))),
                "factor_max_concurrent": int(_number(market.get("factor_max_concurrent"))),
                "factor_max_new_per_day": int(_number(market.get("factor_max_new_per_day"))),
                "simulation_only": _flag(market.get("simulation_only")),
                "industry_relative_strength": _number(market.get("industry_relative_strength")),
                "factor_disclosure_status": "unavailable_neutral",
                "factor_attributions": (
                    market.get("factor_attributions")
                    if isinstance(market.get("factor_attributions"), list)
                    else []
                ),
                "factor_screen_audit": (
                    market.get("factor_screen_audit")
                    if isinstance(market.get("factor_screen_audit"), list)
                    else []
                ),
            }
            for factor_name in (
                "wave3_structure_score", "wave3_price_volume_score",
                "wave3_duration_similarity", "wave3_gain_similarity",
                "wave3_gain1_pct", "wave3_gain2_pct",
                "wave3_pullback1_pct", "wave3_pullback2_pct",
                "wave3_pullback_volume_ratio1", "wave3_pullback_volume_ratio2",
                "wave3_setup_age_days", "breakout_price", "vwap",
                "chase_pct", "chase_limit", "limitdown_locked_days",
                "limitdown_event_age_days", "limitdown_turnover_ratio",
                "limitdown_close_location", "limitdown_rebound_from_low_pct",
                "limitdown_avg_amount_20d", "limitdown_next_day_gap_pct",
                "limitdown_next_day_vwap", "limitdown_next_day_low",
                "limitdown_resealed_last_60m",
            ):
                if factor_name in market.index and not (
                    isinstance(market.get(factor_name), float)
                    and math.isnan(market.get(factor_name))
                ):
                    feature_values[factor_name] = market.get(factor_name)
            features = {name: _timed(value, now) for name, value in feature_values.items()}
            for name in ("ma5", "ma10", "ma20", "ma30", "atr14", "pressure_pct", "pressure_label"):
                features[name]["available_at"] = _timestamp_for_day(
                    history["time"].map(_date_value).max()
                )
            features["market_cap"]["available_at"] = valuation_at
            features["turnover"]["available_at"] = now
            features["industry_source"] = _timed("joinquant_historical", industry_at)
            features["news_data_status"] = _timed(PIT_NEWS_POLICY, now)
            features["market_news_data_status"] = _timed(PIT_MARKET_NEWS_POLICY, now)
            features["pit_engine_version"] = _timed(PIT_ENGINE_VERSION, now)
            rows.append({
                "code": code,
                "features": features,
                "decision_fields": {
                    "candidate_cohort_status": (
                        "live_watch_pool" if in_live_pool
                        else "counterfactual_pool_rejection"
                    ),
                    "pre_rejection_code": pre_rejection,
                    "name": _text(market.get("name")),
                    "industry": row["industry"],
                    "theme_label": row["theme_label"],
                    "support_level": tech["support_level"],
                    "pressure_level": tech["pressure_level"],
                    "trend_state": tech["trend_state"],
                    "limit_quality": row["limit_quality"],
                    "mode": row["mode"],
                    "signal_action": action,
                    "risk_per_share": risk_per_share,
                    "board_type": board,
                    "quote_age_sec": 0,
                    "listing_days": int(_number(market.get("listing_days"))),
                    "execution_plan_version": EXECUTION_PLAN_VERSION,
                    "execution_allowed": row["execution_allowed"],
                    "position_pct": position,
                    "entry_price": row["entry_price"],
                    "stop_loss": stop,
                    "take_profit": take,
                    "market_state": self.market_state,
                    "signal_state": state,
                    "buy_state": row["buy_state"],
                    "factor_path": factor_path,
                    "candidate_channels": feature_values["candidate_channels"],
                    "factor_setup_id": feature_values["factor_setup_id"],
                    "factor_state": feature_values["factor_state"],
                    "factor_score": feature_values["factor_score"],
                    "factor_triggered": factor_triggered,
                    "factor_rejection_code": feature_values["factor_rejection_code"],
                    "factor_position_cap_pct": feature_values["factor_position_cap_pct"],
                    "factor_risk_budget_pct": feature_values["factor_risk_budget_pct"],
                    "factor_max_hold_days": feature_values["factor_max_hold_days"],
                    "factor_max_concurrent": feature_values["factor_max_concurrent"],
                    "factor_max_new_per_day": feature_values["factor_max_new_per_day"],
                    "simulation_only": feature_values["simulation_only"],
                },
            })
            if len(rows) >= total_limit:
                break
        if not rows:
            raise PointInTimeReplayError("EMPTY_ANALYZABLE_CANDIDATE_COHORT")
        if live_codes:
            live_plain = []
            live_row_refs = []
            for item in rows:
                if item["code"] not in live_codes:
                    continue
                values = {
                    name: timed["value"]
                    for name, timed in item["features"].items()
                    if isinstance(timed, Mapping) and "value" in timed
                }
                live_plain.append(values)
                live_row_refs.append(item)
            exact_scores = score_candidate_frame(pd.DataFrame(live_plain))
            for index, item in enumerate(live_row_refs):
                item["decision_fields"]["strict_final_score_override"] = float(
                    exact_scores.iloc[index]["final_score"]
                )
        self.last_decision = decision
        self.last_feature_decision = context.decision_at
        self.last_rows = rows
        # No later decision consumes the full context.  Retaining it pins the
        # all-market snapshot and historical frame in a 1 GB Research kernel.
        self.last_context = None
        return rows

    def portfolio_state_provider(self, context):
        if self.last_feature_decision != context.decision_at:
            raise PointInTimeReplayError("FEATURE_PROVIDER_MUST_RUN_FIRST")
        equity = self._equity(context.snapshot)
        self.peak_equity = max(self.peak_equity, equity)
        by_code = self._snapshot_by_code(context.snapshot, self.positions)
        position_value = 0.0
        open_risk = 0.0
        sectors = {}
        themes = {}
        factor_counts = {
            "wave3_v1": 0,
            "limitdown_exhaustion_v1": 0,
        }
        for code, position in self.positions.items():
            market = by_code.get(code)
            price = _number(market.get("close")) if market is not None else _number(position.get("last_price"))
            value = int(position["qty"]) * price
            position_value += value
            open_risk += max(price - _number(position.get("stop_loss")), 0.0) * int(position["qty"])
            pct = value / equity * 100.0 if equity > 0 else 0.0
            industry = _text(position.get("industry"))
            theme = _text(position.get("theme"))
            if industry:
                sectors[industry] = sectors.get(industry, 0.0) + pct
            if theme:
                themes[theme] = themes.get(theme, 0.0) + pct
            if not industry and not theme:
                sectors["__UNCATEGORIZED__"] = sectors.get("__UNCATEGORIZED__", 0.0) + pct
            factor_path = _text(position.get("factor_path"))
            if factor_path in factor_counts:
                factor_counts[factor_path] += 1
        cooldown_codes = [
            code for code, closed_day in self.cooldown.items()
            if (_date_value(context.trade_date) - _date_value(closed_day)).days <= 3
        ]
        return {
            "allow_buy": True,
            "account_total_value": equity,
            "current_position_pct": position_value / equity * 100.0 if equity > 0 else 0.0,
            "current_open_risk_pct": open_risk / equity * 100.0 if equity > 0 else 0.0,
            "current_position_count": len(self.positions),
            "sector_exposure_pct": sectors,
            "theme_exposure_pct": themes,
            "cooldown_codes": cooldown_codes,
            "available_cash": self.cash,
            "new_positions_today": self.new_positions_today,
            "orders_today": self.orders_today,
            "daily_turnover_pct": self.turnover_today / max(self.day_start_equity, 0.01) * 100.0,
            "daily_pnl_pct": (equity / max(self.day_start_equity, 0.01) - 1.0) * 100.0,
            "account_drawdown_pct": (equity / max(self.peak_equity, 0.01) - 1.0) * 100.0,
            "consecutive_losses": self.consecutive_losses,
            "held_codes": sorted(self.positions),
            "factor_position_counts": factor_counts,
            "factor_new_positions_today": dict(self.factor_new_positions_today),
        }

    def decision_observer(self, context, rows):
        if self.last_observed_decision == context.decision_at:
            return
        intents = []
        for exit_intent in self.exit_intents:
            self.pending.append(dict(exit_intent))
            intents.append(dict(exit_intent))
        pending_codes = {item["code"] for item in self.pending}
        raw_by_code = {item["code"]: item for item in self.last_rows}
        for row in rows:
            if not row.get("selected"):
                continue
            code = _clean_code(row.get("code"))
            if code in pending_codes or code in self.positions:
                continue
            feature_values = {
                name: item.get("value")
                for name, item in row.get("features", {}).items()
                if isinstance(item, Mapping)
            }
            source = raw_by_code.get(code, {})
            fields = source.get("decision_fields", {})
            intent = {
                "side": "buy",
                "code": code,
                "target_qty": int(_number(feature_values.get("rule_target_qty"))),
                "entry_price": _number(feature_values.get("entry_price")),
                "stop_loss": _number(feature_values.get("stop_loss")),
                "take_profit": _number(feature_values.get("take_profit")),
                "atr14": _number(feature_values.get("atr14")),
                "mode": _text(fields.get("mode")) or "short",
                "industry": _text(fields.get("industry")),
                "theme": _text(fields.get("theme_label")),
                "reason": "selected",
                "created_at": context.decision_at,
                "factor_path": _text(fields.get("factor_path")) or "momentum_v1",
                "factor_setup_id": _text(fields.get("factor_setup_id")),
            }
            if intent["target_qty"] > 0:
                self.pending.append(intent)
                intents.append(dict(intent))
        self.last_observed_decision = context.decision_at

    def drain_platform_intents(self):
        result = list(self.platform_intents)
        self.platform_intents = []
        return result

    def _fetch_prices(self, codes, start, end, frequency, count=None):
        intraday = str(frequency).lower() not in ("daily", "1d", "day")
        if intraday:
            fields = ["open", "high", "low", "close", "volume", "money"]
        else:
            fields = [
                "open", "high", "low", "close", "volume", "money", "paused",
                "high_limit", "low_limit", "factor",
            ]
        getter = self._api("get_price")
        frames = []
        for offset in range(0, len(codes), 800):
            batch = list(codes[offset:offset + 800])
            kwargs = {
                "security": batch,
                "frequency": frequency,
                "fields": fields,
                "skip_paused": False,
                "fq": None,
                "panel": False,
            }
            if count is None:
                kwargs.update({"start_date": start, "end_date": end})
            else:
                kwargs.update({"end_date": end, "count": int(count)})
            try:
                frame = getter(**kwargs)
            except TypeError:
                kwargs.pop("panel", None)
                frame = getter(**kwargs)
            if intraday and frame is not None:
                frame = frame.copy()
                frame["paused"] = (
                    (frame["volume"].fillna(0) <= 0)
                    & (frame["money"].fillna(0) <= 0)
                ).astype(int)
                frame["high_limit"] = 0.0
                frame["low_limit"] = 0.0
                frame["factor"] = 1.0
            normalized = _normalize_price_frame(frame, batch)
            if not normalized.empty:
                frames.append(normalized)
        try:
            import pandas as pd
        except ImportError:
            raise PointInTimeReplayError("PANDAS_REQUIRED")
        if not frames:
            return pd.DataFrame(columns=sorted({
                "time", "code", "open", "high", "low", "close", "volume",
                "money", "paused", "high_limit", "low_limit", "factor",
            }))
        return pd.concat(frames, ignore_index=True).sort_values(["time", "code"])

    def build_joinquant_context(self, decision_at):
        """Build one no-future context inside a native JoinQuant backtest."""
        try:
            import pandas as pd
        except ImportError:
            raise PointInTimeReplayError("PANDAS_REQUIRED")
        decision = decision_at
        if decision.tzinfo is None or decision.utcoffset() is None:
            decision = decision.replace(tzinfo=SHANGHAI_TZ)
        else:
            decision = decision.astimezone(SHANGHAI_TZ)
        day = decision.date()
        day_key = day.isoformat()
        cache = self._jq_day_cache.get(day_key)
        if cache is None:
            securities = self._api("get_all_securities")(types=["stock"], date=day)
            if securities is None or securities.empty:
                raise PointInTimeReplayError("EMPTY_A_SHARE_UNIVERSE")
            securities = securities.copy()
            securities.index = [str(value) for value in securities.index]
            codes = [
                code for code in securities.index
                if code.endswith((".XSHG", ".XSHE")) and _clean_code(code)
            ]
            prior_days = self._api("get_trade_days")(
                start_date=day - timedelta(days=15), end_date=day - timedelta(days=1)
            )
            if len(prior_days) == 0:
                raise PointInTimeReplayError("PREVIOUS_TRADE_DAY_REQUIRED")
            prior = _date_value(prior_days[-1])
            previous = self._fetch_prices(codes, prior, prior, "daily")
            previous = previous.sort_values(["code", "time"]).groupby("code", as_index=False).tail(1)
            previous_by_code = {
                str(row["code"]): row for _, row in previous.iterrows()
            }
            extras = self._api("get_extras")(
                "is_st", codes, start_date=day, end_date=day, df=True
            )
            st_row = extras.iloc[-1] if extras is not None and not extras.empty else None
            cache = {
                "securities": securities,
                "codes": codes,
                "prior": prior,
                "previous": previous_by_code,
                "st": {
                    code: bool(st_row.get(code, False)) if st_row is not None else False
                    for code in codes
                },
            }
            self._jq_day_cache = {day_key: cache}
            self._intraday_amount = {}
        current = self._fetch_prices(
            cache["codes"], None, decision.replace(tzinfo=None), "5m", count=1
        )
        if current.empty:
            raise PointInTimeReplayError("MISSING_DECISION_SNAPSHOT")
        rows = []
        for _, bar in current.iterrows():
            jq_code = str(bar["code"])
            previous = cache["previous"].get(jq_code)
            if previous is None:
                continue
            key = (day_key, jq_code, decision.isoformat())
            amount = self._intraday_amount.get((day_key, jq_code), 0.0) + _number(bar.get("money"))
            volume = self._intraday_amount.get((day_key, jq_code, "volume"), 0.0) + _number(bar.get("volume"))
            self._intraday_amount[(day_key, jq_code)] = amount
            self._intraday_amount[(day_key, jq_code, "volume")] = volume
            security = cache["securities"].loc[jq_code]
            prev_close = _number(previous.get("close"))
            if prev_close <= 0:
                continue
            listing_days = max(
                0, (day - _date_value(security.get("start_date"))).days,
            )
            high_limit, low_limit = _daily_limit_prices(
                prev_close,
                jq_code,
                cache["st"].get(jq_code, False),
                day,
                listing_days,
            )
            rows.append({
                **dict(bar),
                "prev_close": prev_close,
                "pct_chg": (_number(bar.get("close")) / prev_close - 1.0) * 100.0,
                "cum_amount": amount,
                "cum_volume": volume,
                "name": _text(security.get("display_name") or security.get("name")),
                "is_st": cache["st"].get(jq_code, False),
                "delisting": False,
                "listing_days": listing_days,
                "high_limit": high_limit,
                "low_limit": low_limit,
            })
        snapshot = pd.DataFrame(rows)
        index_bar = self._fetch_prices(
            ["000001.XSHG"], None, decision.replace(tzinfo=None), "5m", count=1
        )
        previous_index = self._fetch_prices(
            ["000001.XSHG"], cache["prior"], cache["prior"], "daily"
        )
        if index_bar.empty or previous_index.empty:
            raise PointInTimeReplayError("STRICT_MARKET_INDEX_REQUIRED")
        index_bar = index_bar.copy()
        index_bar["prev_close"] = _number(previous_index.iloc[-1]["close"])
        return PortableDecisionContext(
            dataset_id="joinquant-native-backtest",
            decision_at=decision.isoformat(),
            trade_date=day_key,
            snapshot=snapshot,
            daily_history=pd.DataFrame(),
            universe_codes=tuple(sorted(_clean_code(code) for code in cache["codes"])),
            market_snapshot=index_bar,
            metadata={"pit_engine_version": PIT_ENGINE_VERSION},
        )


__all__ = [
    "PIT_ENGINE_VERSION",
    "PIT_FEATURE_SCHEMA_VERSION",
    "PIT_MARKET_DATA_VERSION",
    "PIT_MARKET_NEWS_POLICY",
    "PIT_NEWS_POLICY",
    "PointInTimeReplayEngine",
    "PointInTimeReplayError",
    "PortableDecisionContext",
]
