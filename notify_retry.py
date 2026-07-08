from __future__ import annotations

import argparse
import json
from pathlib import Path

import config as app_config
from notifier import retry_failed_notifications


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Retry failed WeCom markdown notifications")
    parser.add_argument("--queue-file", type=Path, default=app_config.CACHE_DIR / "notify_failed_queue.jsonl")
    parser.add_argument("--state-file", type=Path, default=app_config.CACHE_DIR / "wecom_notify_state.json")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    sent = retry_failed_notifications(
        app_config.WECOM_WEBHOOK_URL,
        args.queue_file,
        args.state_file,
        cooldown_sec=0,
        timeout_sec=app_config.WECOM_TIMEOUT_SEC,
    )
    print(json.dumps({"sent": sent}, ensure_ascii=False))


if __name__ == "__main__":
    main()
