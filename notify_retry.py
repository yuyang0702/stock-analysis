from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import config as app_config


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit legacy WeCom notification files without replaying them",
    )
    parser.add_argument("--legacy-audit", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--queue-file",
        type=Path,
        default=app_config.CACHE_DIR / "notify_failed_queue.jsonl",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=app_config.CACHE_DIR / "wecom_notify_state.json",
    )
    parser.add_argument(
        "--db-file",
        type=Path,
        default=app_config.TRADING_DB_FILE,
    )
    return parser


def run_legacy_audit(
    state_file: Path,
    queue_file: Path,
    db_file: Path,
    *,
    dry_run: bool = False,
) -> Any:
    from notification_worker import legacy_audit
    from trading_store import TradingStore

    store = TradingStore(db_file)
    if not dry_run:
        store.initialize()
    return legacy_audit(
        store,
        state_file,
        queue_file,
        datetime.now().astimezone(),
        dry_run=dry_run,
    )


def _result_payload(result: Any) -> dict[str, Any]:
    if is_dataclass(result):
        return asdict(result)
    return dict(vars(result))


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    if not args.legacy_audit:
        print(json.dumps({"code": "LEGACY_JSON_REPLAY_DISABLED", "sent": 0}))
        return
    result = run_legacy_audit(
        args.state_file,
        args.queue_file,
        args.db_file,
        dry_run=args.dry_run,
    )
    print(json.dumps(_result_payload(result), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
