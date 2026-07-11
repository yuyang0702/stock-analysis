from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config as app_config
from trading_store import TradingStore


def _load(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, "invalid_json"
    return (data, "") if isinstance(data, dict) else ({}, "invalid_shape")


def build_report(
    signal_file: Path | None = None,
    snapshot_file: Path | None = None,
    report_file: Path | None = None,
    *,
    db_file: Path | None = None,
) -> dict[str, Any]:
    signal_file = signal_file or app_config.JOINQUANT_SIGNAL_FILE
    snapshot_file = snapshot_file or app_config.JOINQUANT_ACCOUNT_FILE
    report_file = report_file or app_config.OUTPUT_DIR / f"joinquant_readiness_{datetime.now().strftime('%Y%m%d')}.md"
    db_file = db_file or app_config.TRADING_DB_FILE
    ledger_health = TradingStore(db_file).health()
    signals_payload, signal_error = _load(signal_file)
    snapshot_payload, snapshot_error = _load(snapshot_file)

    signals = signals_payload.get("signals", []) if not signal_error else []
    try:
        ledger_signal_count, ledger_json_parity = TradingStore(db_file).current_signal_parity(signals) if ledger_health.ok else (0, False)
    except Exception as exc:
        ledger_signal_count, ledger_json_parity = 0, False
        ledger_health = type(ledger_health)(False, ledger_health.schema_version, str(exc))
    positions = snapshot_payload.get("positions", []) if not snapshot_error else []
    duplicate_ids = len(signals) - len({item.get("id") for item in signals if isinstance(item, dict)})
    over_position = [
        item for item in signals
        if isinstance(item, dict) and float(item.get("position_pct") or 0) > app_config.JOINQUANT_MAX_TOTAL_POSITION_PCT_DEFAULT
    ]
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signal_ok": not signal_error and signals_payload.get("schema_version") == 1,
        "snapshot_ok": not snapshot_error and snapshot_payload.get("schema_version") == 1,
        "signal_error": signal_error,
        "snapshot_error": snapshot_error,
        "signal_count": len(signals),
        "position_count": len(positions),
        "duplicate_signal_ids": duplicate_ids,
        "position_violation_count": len(over_position),
        "ledger_ok": ledger_health.ok,
        "ledger_schema_version": ledger_health.schema_version,
        "ledger_signal_count": ledger_signal_count,
        "ledger_json_parity": ledger_json_parity,
        "ledger_error": " ".join(ledger_health.error.replace("\r", " ").replace("\n", " ").split())[:240],
    }
    if result["signal_ok"] and result["snapshot_ok"] and result["ledger_ok"] and ledger_json_parity and duplicate_ids == 0 and not over_position:
        conclusion = "can_small_position_trial"
    else:
        conclusion = "keep_dry_run"
    result["conclusion"] = conclusion

    lines = [
        "# JoinQuant Readiness",
        "",
        f"- generated_at: {result['generated_at']}",
        f"- conclusion: {conclusion}",
        f"- signal_ok: {result['signal_ok']}",
        f"- snapshot_ok: {result['snapshot_ok']}",
        f"- signal_count: {result['signal_count']}",
        f"- position_count: {result['position_count']}",
        f"- duplicate_signal_ids: {duplicate_ids}",
        f"- position_violation_count: {len(over_position)}",
        f"- SQLite 交易账本: {'正常' if result['ledger_ok'] else '未就绪'}",
        f"- ledger_schema_version: {result['ledger_schema_version']}",
    ]
    if signal_error:
        lines.append(f"- signal_error: {signal_error}")
    if snapshot_error:
        lines.append(f"- snapshot_error: {snapshot_error}")
    if result["ledger_error"]:
        lines.append(f"- ledger_error: {result['ledger_error']}")
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build JoinQuant readiness report")
    parser.add_argument("--signal-file", type=Path, default=app_config.JOINQUANT_SIGNAL_FILE)
    parser.add_argument("--snapshot-file", type=Path, default=app_config.JOINQUANT_ACCOUNT_FILE)
    parser.add_argument("--report-file", type=Path)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    result = build_report(args.signal_file, args.snapshot_file, args.report_file)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
