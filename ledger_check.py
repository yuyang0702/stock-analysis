from __future__ import annotations

import json
import uuid

import config
from trading_store import SCHEMA_VERSION, TradingStore


def run() -> dict[str, object]:
    store = TradingStore(config.TRADING_DB_FILE)
    store.initialize()
    health = store.health()
    if not health.ok or health.schema_version != SCHEMA_VERSION:
        raise RuntimeError(
            f"ledger schema mismatch: expected={SCHEMA_VERSION} actual={health.schema_version}"
        )

    probe = f"ledger_check_probe_{uuid.uuid4().hex}"
    with store.transaction() as conn:
        store.set_system_state(conn, probe, "ok", "deployment writable probe")
        conn.execute("DELETE FROM system_state WHERE key = ?", (probe,))

    with store.connect() as conn:
        states = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT state, COUNT(*) FROM notification_outbox GROUP BY state"
            )
        }
        capacity = store.notification_capacity(conn)
        marker_row = conn.execute(
            "SELECT value FROM system_state WHERE key=?",
            ("notification_outbox_write_failure",),
        ).fetchone()
        marker_text = str(marker_row[0] or "") if marker_row is not None else ""
        marker: dict[str, object] = {}
        if marker_text:
            parsed = json.loads(marker_text)
            if isinstance(parsed, dict):
                marker = parsed

    return {
        "schema_version": health.schema_version,
        "health": "ok",
        "writable_probe": "ok",
        "pending": states.get("pending", 0),
        "leased": states.get("leased", 0),
        "sent": states.get("sent", 0),
        "dead": states.get("dead", 0),
        "cancelled": states.get("cancelled", 0),
        "gaps": capacity.unresolved_gap_rows,
        "high_gaps": capacity.high_unresolved_gap_rows,
        "dead_detail_rows": capacity.dead_rows,
        "dead_detail_bytes": capacity.dead_bytes,
        "high_dead": capacity.high_dead_rows,
        "tombstones": capacity.tombstone_rows,
        "write_failure_marker": bool(marker_text),
        "write_failure_requires_manual_resolution": bool(
            marker.get("requires_manual_resolution")
        ),
    }


def main() -> int:
    result = run()
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
