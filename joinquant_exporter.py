from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import config as app_config


def clean_code(value: Any) -> str:
    digits = "".join(filter(str.isdigit, str(value or "")))[:6]
    return digits.zfill(6) if digits else ""


def to_jq_code(code: Any) -> str:
    code = clean_code(code)
    if not code:
        return ""
    if code.startswith("6"):
        return f"{code}.XSHG"
    if code.startswith(("4", "8")):
        return f"{code}.XBJG"
    return f"{code}.XSHE"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _is_sell(row: pd.Series) -> bool:
    action = _text(row.get("signal_action")).lower()
    return action in {"sell", "stop_loss", "take_profit", "time_stop"} or "sell" in action


def _has_holding(row: pd.Series) -> bool:
    if bool(row.get("has_holding")):
        return True
    return _text(row.get("hold_status")).lower() in {"holding", "partial_sell"}


def _buy_reject_reason(row: pd.Series, min_score: float, allow_buy: bool = True, account_total_value: float = 0.0) -> str:
    if not allow_buy:
        return "buy_disabled"
    code = clean_code(row.get("code"))
    price = _num(row.get("price"))
    entry = _num(row.get("entry_price"), price)
    take = _num(row.get("take_profit"))
    if not code or price <= 0 or entry <= 0:
        return "buy_invalid_price"
    if take > 0 and take <= entry:
        return "buy_invalid_take_profit"
    if _is_sell(row):
        return "not_buy_sell_signal"
    if _num(row.get("final_score")) < min_score:
        return "buy_low_score"
    if _num(row.get("position_pct")) <= 0:
        return "buy_bad_position"
    if _num(row.get("pct_chg")) >= 9.8:
        return "buy_near_limit_up"
    if price < entry:
        return "buy_not_reached_entry"
    if account_total_value > 0:
        target_value = account_total_value * _num(row.get("position_pct")) / 100.0
        if target_value < entry * 100:
            return "buy_too_small_for_board_lot"
    return ""


def _can_buy(row: pd.Series, min_score: float, allow_buy: bool = True) -> bool:
    return _buy_reject_reason(row, min_score, allow_buy=allow_buy) == ""


def _base_payload(run_id: str | None, trade_date: str | None, dry_run: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trade_date": trade_date or datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "run_id": run_id,
        "source": "a_share_strategy",
        "dry_run": dry_run,
        "signals": [],
    }


def _signal_id(run_id: str | None, code: str, action: str, index: int) -> str:
    prefix = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{code}-{action}-{index:04d}"


def _buy_signal(row: pd.Series, run_id: str | None, index: int) -> dict[str, Any]:
    code = clean_code(row.get("code"))
    price = _num(row.get("price"))
    entry = _num(row.get("entry_price"), price)
    stop = _num(row.get("stop_loss"))
    take = _num(row.get("take_profit"))
    return {
        "id": _signal_id(run_id, code, "buy", index),
        "code": code,
        "jq_code": to_jq_code(code),
        "name": _text(row.get("name")),
        "action": "buy",
        "price": round(price, 2),
        "entry_price": round(entry, 2),
        "stop_loss": round(stop, 2) if stop > 0 else None,
        "take_profit": round(take, 2) if take > 0 else None,
        "position_pct": round(_num(row.get("position_pct")), 2),
        "final_score": round(_num(row.get("final_score")), 1),
        "enhanced_score": round(_num(row.get("enhanced_score")), 1) if _num(row.get("enhanced_score")) else None,
        "signal_type": _text(row.get("mode")) or _text(row.get("buy_state")) or "signal",
        "reason": _text(row.get("risk_reason") or row.get("buy_reason") or row.get("entry_reason")),
    }


def _sell_signal(row: pd.Series, run_id: str | None, index: int) -> dict[str, Any] | None:
    code = clean_code(row.get("code"))
    price = _num(row.get("price"))
    if not code or price <= 0:
        return None
    return {
        "id": _signal_id(run_id, code, "sell", index),
        "code": code,
        "jq_code": to_jq_code(code),
        "name": _text(row.get("name")),
        "action": "sell",
        "price": round(price, 2),
        "reason": _text(row.get("risk_reason") or row.get("signal_note") or row.get("buy_reason")),
    }


def export_signals(
    df: pd.DataFrame,
    run_id: str | None = None,
    trade_date: str | None = None,
    dry_run: bool | None = None,
    min_score: float | None = None,
    output_path: Path | None = None,
    ml_sample_path: Path | None = None,
    allow_buy: bool = True,
    account_total_value: float = 0.0,
) -> Path:
    dry_run = app_config.JOINQUANT_DRY_RUN_DEFAULT if dry_run is None else dry_run
    min_score = app_config.JOINQUANT_MIN_SCORE_DEFAULT if min_score is None else min_score
    output_path = output_path or app_config.JOINQUANT_SIGNAL_FILE
    payload = _base_payload(run_id, trade_date, dry_run)

    if df is not None and not df.empty:
        signals: list[dict[str, Any]] = []
        sample_rows: list[tuple[pd.Series, dict[str, Any]]] = []
        reject_reasons: Counter[str] = Counter()
        for index, (_, row) in enumerate(df.iterrows()):
            buy_reject_reason = _buy_reject_reason(row, min_score, allow_buy=allow_buy, account_total_value=account_total_value)
            if not buy_reject_reason:
                signal = _buy_signal(row, run_id, index)
                signals.append(signal)
                sample_rows.append((row, signal))
            elif _is_sell(row) and _has_holding(row):
                sell = _sell_signal(row, run_id, index)
                if sell:
                    signals.append(sell)
                    sample_rows.append((row, sell))
            elif _is_sell(row):
                reject_reasons["sell_without_holding"] += 1
            else:
                reject_reasons[buy_reject_reason] += 1
        payload["signals"] = signals
        payload["diagnostics"] = {
            "candidate_count": int(len(df)),
            "allow_buy": bool(allow_buy),
            "min_score": float(min_score),
            "account_total_value": float(account_total_value or 0.0),
            "reject_reasons": dict(reject_reasons),
        }
        try:
            from ml_dataset import append_signal_samples

            append_signal_samples(sample_rows, payload, ml_sample_path)
        except Exception as exc:
            print(f"ML sample append skipped: {exc}", flush=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp_path.replace(output_path)
    return output_path


if __name__ == "__main__":
    demo = pd.DataFrame(
        [
            {
                "code": "600000",
                "name": "PF Bank",
                "price": 10.0,
                "entry_price": 10.0,
                "position_pct": 10,
                "final_score": 90,
                "signal_action": "continue",
            }
        ]
    )
    print(export_signals(demo))
