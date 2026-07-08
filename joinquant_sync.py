from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config as app_config


def _code(value: Any) -> str:
    digits = "".join(filter(str.isdigit, str(value or "")))[:6]
    return digits.zfill(6) if digits else ""


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_snapshot(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("invalid JoinQuant account snapshot")
    return raw


def _position(item: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    code = _code(item.get("code") or item.get("jq_code"))
    avg_cost = _num(item.get("avg_cost"))
    price = _num(item.get("price"))
    stop_price = round(avg_cost * 0.965, 2) if avg_cost else None
    take_price = round(avg_cost * 1.07, 2) if avg_cost else None
    return {
        "code": code,
        "jq_code": str(item.get("jq_code") or "").strip(),
        "name": str(item.get("name") or "").strip(),
        "qty": int(_num(item.get("qty"), 0) or 0),
        "cost_price": avg_cost,
        "current_price": price,
        "stop_pct": 3.5,
        "take_pct": 7.0,
        "stop_price": stop_price,
        "take_price": take_price,
        "position_ratio": _num(item.get("position_ratio")),
        "market_value": _num(item.get("market_value")),
        "pnl": _num(item.get("pnl"), 0.0),
        "status": "holding",
        "source": "joinquant",
        "note": f"JoinQuant snapshot {snapshot.get('trade_date', '')}".strip(),
        "entry_time": str(snapshot.get("generated_at") or _now()),
        "updated_at": _now(),
        "raw": item,
    }


def sync_account_snapshot(
    account_file: Path | None = None,
    positions_file: Path | None = None,
    events_file: Path | None = None,
) -> int:
    account_file = account_file or app_config.JOINQUANT_ACCOUNT_FILE
    positions_file = positions_file or app_config.POSITIONS_FILE
    events_file = events_file or app_config.PORTFOLIO_EVENTS_FILE
    snapshot = _load_snapshot(account_file)

    positions = []
    for item in snapshot.get("positions", []):
        if isinstance(item, dict):
            pos = _position(item, snapshot)
            if pos["code"] and pos["qty"] > 0:
                positions.append(pos)

    payload = {
        "updated_at": _now(),
        "source": "joinquant",
        "account": {
            "cash": _num(snapshot.get("cash")),
            "total_value": _num(snapshot.get("total_value")),
            "trade_date": snapshot.get("trade_date"),
            "generated_at": snapshot.get("generated_at"),
        },
        "positions": sorted(positions, key=lambda item: item["code"]),
    }
    positions_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = positions_file.with_suffix(positions_file.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(positions_file)

    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _now(), "action": "joinquant_sync", "count": len(positions)}, ensure_ascii=False) + "\n")
    return len(positions)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync JoinQuant snapshot to local portfolio file")
    parser.add_argument("--account-file", type=Path, default=app_config.JOINQUANT_ACCOUNT_FILE)
    parser.add_argument("--positions-file", type=Path, default=app_config.POSITIONS_FILE)
    parser.add_argument("--events-file", type=Path, default=app_config.PORTFOLIO_EVENTS_FILE)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    count = sync_account_snapshot(args.account_file, args.positions_file, args.events_file)
    print(f"Synced {count} JoinQuant positions")


if __name__ == "__main__":
    main()
