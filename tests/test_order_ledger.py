import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from order_ledger import client_order_id, fill_id, normalize_fill, normalize_order
from trading_store import FillConflictError, TradingStore


class OrderLedgerTest(unittest.TestCase):
    def test_signal_order_id_uses_approved_hash(self) -> None:
        event = {"id": "sig-1", "action": "buy", "jq_code": "600000.XSHG"}
        expected = hashlib.sha256(
            "v1".encode("utf-8") + "2026-07-14".encode("utf-8")
            + "sig-1".encode("utf-8") + "buy".encode("utf-8")
            + "600000.XSHG".encode("utf-8")
        ).hexdigest()[:32]
        self.assertEqual(client_order_id(event, "2026-07-14", "v1"), expected)

    def test_explicit_client_order_id_takes_precedence_without_rewriting(self) -> None:
        event = {
            "client_order_id": "intent:scope-A:0001",
            "id": "sig-1",
            "order_id": "jq-9",
            "action": "buy",
            "jq_code": "600000.XSHG",
        }
        self.assertEqual(
            client_order_id(event, "2026-07-14", "v1"),
            "intent:scope-A:0001",
        )
        self.assertEqual(
            normalize_order(
                {**event, "amount": 100, "status": "submitted"},
                trade_date="2026-07-14",
                strategy_version="v1",
            )["client_order_id"],
            "intent:scope-A:0001",
        )

    def test_invalid_explicit_client_order_id_fails_closed(self) -> None:
        base = {"id": "sig-1", "action": "buy", "code": "600000"}
        for value in (123, [], {}, "   ", " leading", "trailing ", "bad\nvalue"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "client_order_id",
            ):
                client_order_id(
                    {**base, "client_order_id": value}, "2026-07-14", "v1",
                )

    def test_never_submitted_terminal_statuses_are_not_rewritten_as_active(self) -> None:
        for status in ("not_submitted", "expired"):
            with self.subTest(status=status):
                order = normalize_order(
                    {
                        "client_order_id": f"intent-{status}",
                        "code": "600000", "action": "buy", "amount": 100,
                        "filled": 0, "status": status,
                        "datetime": "2026-07-14 10:00:00",
                    },
                    trade_date="2026-07-14", strategy_version="v1",
                )
                self.assertEqual(order["status"], status)

    def test_submission_evidence_preserves_explicit_first_attempt(self) -> None:
        submitted = normalize_order(
            {
                "client_order_id": "intent-late-fill", "code": "600000",
                "action": "buy", "amount": 100, "filled": 100,
                "status": "filled", "submit_count": 1,
                "first_submitted_at": "2026-07-14 10:00:00",
                "datetime": "2026-07-14 10:06:00",
            },
            trade_date="2026-07-14", strategy_version="v1",
        )
        expired = normalize_order(
            {
                "client_order_id": "intent-expired", "code": "600001",
                "action": "buy", "amount": 100, "filled": 0,
                "status": "expired", "submit_count": 0,
                "datetime": "2026-07-14 10:06:00",
            },
            trade_date="2026-07-14", strategy_version="v1",
        )

        self.assertEqual(
            submitted["first_submitted_at"], "2026-07-14 10:00:00",
        )
        self.assertIsNone(expired["first_submitted_at"])

    def test_local_admission_status_progresses_without_regression(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            base = {
                "client_order_id": "intent-1", "signal_id": None,
                "order_id": None, "stock_code": "600000", "action": "buy",
                "target_qty": 100, "requested_qty": 100, "filled_qty": 0,
                "average_fill_price": 0, "submit_count": 0, "reason": "",
                "first_submitted_at": None, "completed_at": None,
                "raw_json": "{}",
            }
            with store.transaction() as conn:
                for index, status in enumerate(
                    ("ready", "submitting", "submit_unknown", "submitted")
                ):
                    store.upsert_order(conn, {
                        **base, "status": status,
                        "submit_count": int(status != "ready"),
                        "first_submitted_at": (
                            None if status == "ready"
                            else "2026-07-14T10:00:01+08:00"
                        ),
                        "updated_at": f"2026-07-14T10:00:0{index}+08:00",
                    })
                store.upsert_order(conn, {
                    **base, "status": "ready",
                    "updated_at": "2026-07-14T09:59:59+08:00",
                })
                row = conn.execute(
                    "SELECT status FROM orders WHERE client_order_id='intent-1'"
                ).fetchone()
            self.assertEqual(row["status"], "submitted")

    def test_fill_client_order_id_must_match_linked_order(self) -> None:
        orders = {
            "jq-9": {
                "client_order_id": "intent-1",
                "signal_id": "sig-1",
            },
        }
        fill = normalize_fill(
            {
                "client_order_id": "intent-1",
                "trade_id": "fill-1",
                "order_id": "jq-9",
                "code": "600000",
                "action": "buy",
                "amount": 100,
                "price": 10,
            },
            orders=orders,
        )
        self.assertEqual(fill["client_order_id"], "intent-1")
        with self.assertRaisesRegex(ValueError, "client_order_id"):
            normalize_fill(
                {
                    "client_order_id": "intent-2",
                    "trade_id": "fill-1",
                    "order_id": "jq-9",
                    "code": "600000",
                    "action": "buy",
                    "amount": 100,
                    "price": 10,
                },
                orders=orders,
            )

    def test_manual_platform_order_uses_order_id_without_signal(self) -> None:
        row = normalize_order(
            {"order_id": "jq-9", "code": "600000", "action": "sell", "amount": 100, "status": "submitted"},
            trade_date="2026-07-14",
            strategy_version="v1",
        )
        self.assertEqual(row["client_order_id"], "manual:jq-9")
        self.assertIsNone(row["signal_id"])

    def test_terminal_order_does_not_regress_but_fill_quantity_advances(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            filled = normalize_order(
                {"id": "sig-1", "order_id": "o-1", "code": "600000", "jq_code": "600000.XSHG",
                 "action": "buy", "amount": 100, "filled": 100, "status": "filled", "datetime": "2026-07-14 10:00:00"},
                trade_date="2026-07-14", strategy_version="v1",
            )
            stale = {**filled, "status": "submitted", "filled_qty": 50, "updated_at": "2026-07-14 09:59:00"}
            with store.transaction() as conn:
                store.upsert_order(conn, filled)
                store.upsert_order(conn, stale)
                row = conn.execute("SELECT status, filled_qty FROM orders").fetchone()
            self.assertEqual((row[0], row[1]), ("filled", 100))

    def test_partial_order_progresses_to_filled(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            base = {"id": "sig-1", "order_id": "o-1", "code": "600000", "jq_code": "600000.XSHG",
                    "action": "buy", "amount": 100, "datetime": "2026-07-14 10:00:00"}
            partial = normalize_order({**base, "filled": 40, "status": "partial"}, trade_date="2026-07-14", strategy_version="v1")
            stale = normalize_order({
                **base, "filled": 0, "status": "submitted",
                "datetime": "2026-07-14 09:59:00",
            }, trade_date="2026-07-14", strategy_version="v1")
            filled = normalize_order({**base, "filled": 100, "status": "filled"}, trade_date="2026-07-14", strategy_version="v1")
            with store.transaction() as conn:
                store.upsert_order(conn, partial)
                store.upsert_order(conn, stale)
                current = conn.execute(
                    "SELECT status, filled_qty FROM orders"
                ).fetchone()
                self.assertEqual(tuple(current), ("partial", 40))
                store.upsert_order(conn, filled)
                row = conn.execute("SELECT status, filled_qty FROM orders").fetchone()
            self.assertEqual((row[0], row[1]), ("filled", 100))

    def test_fill_is_immutable_and_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            trade = normalize_fill({
                "trade_id": "t-1", "order_id": "o-1", "code": "600000", "action": "buy",
                "amount": 100, "price": 10, "datetime": "2026-07-14 10:00:01",
            }, orders={})
            with store.transaction() as conn:
                self.assertTrue(store.insert_fill(conn, trade))
                self.assertFalse(store.insert_fill(conn, trade))
                with self.assertRaises(FillConflictError):
                    store.insert_fill(conn, {**trade, "price": 11.0})

    def test_missing_fill_id_is_deterministic(self) -> None:
        trade = {"order_id": "o-1", "code": "600000", "action": "buy", "amount": 100,
                 "price": 10, "datetime": "2026-07-14 10:00:01"}
        self.assertEqual(fill_id(trade), fill_id(dict(trade)))

    def test_fill_before_order_links_when_order_arrives(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            trade = normalize_fill({
                "trade_id": "t-1", "order_id": "o-1", "code": "600000", "action": "buy",
                "amount": 100, "price": 10, "datetime": "2026-07-14 10:00:01",
            }, orders={})
            order = normalize_order(
                {"order_id": "o-1", "code": "600000", "action": "buy", "amount": 100,
                 "filled": 100, "status": "filled", "datetime": "2026-07-14 10:00:00"},
                trade_date="2026-07-14", strategy_version="v1",
            )
            with store.transaction() as conn:
                store.insert_fill(conn, trade)
                store.upsert_order(conn, order)
                linked = conn.execute("SELECT client_order_id FROM fills WHERE fill_id='t-1'").fetchone()[0]
            self.assertEqual(linked, "manual:o-1")


if __name__ == "__main__":
    unittest.main()
