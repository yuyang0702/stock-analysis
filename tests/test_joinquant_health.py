import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import config as app_config
import joinquant_health
from trading_store import TradingStore


class JoinQuantHealthTest(unittest.TestCase):
    def _ledger_with_signal_ids(self, path: Path, signal_ids: list[str]) -> None:
        store = TradingStore(path)
        store.initialize()
        with store.transaction() as conn:
            conn.execute("INSERT INTO strategy_runs(run_id, trade_date, started_at, created_at, updated_at) VALUES ('r1', '2026-07-09', '2026-07-09 09:30:00', datetime('now'), datetime('now'))")
            for signal_id in signal_ids:
                conn.execute("INSERT INTO signals(signal_id, run_id, trade_date, stock_code, jq_code, action, generated_at, raw_json, created_at) VALUES (?, 'r1', '2026-07-09', '600000', '600000.XSHG', 'buy', '2026-07-09 09:59:00', ?, datetime('now'))", (signal_id, json.dumps({"id": signal_id})))

    def test_reports_content_mismatch_for_same_signal_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "generated_at": "2026-07-09 09:59:00", "signals": [{"id": "s1", "action": "buy", "price": 11}]}), encoding="utf-8")
            db_file = base / "trading.db"
            store = TradingStore(db_file)
            store.initialize()
            with store.transaction() as conn:
                conn.execute("INSERT INTO strategy_runs(run_id, trade_date, started_at, created_at, updated_at) VALUES ('r1', '2026-07-09', '2026-07-09 09:30:00', datetime('now'), datetime('now'))")
                conn.execute("INSERT INTO signals(signal_id, run_id, trade_date, stock_code, jq_code, action, generated_at, raw_json, created_at) VALUES ('s1', 'r1', '2026-07-09', '600000', '600000.XSHG', 'buy', '2026-07-09 09:59:00', ?, datetime('now'))", (json.dumps({"id": "s1", "action": "buy", "price": 10}),))
            result = joinquant_health.build_health_report(signal_file, base / "missing.json", report_file=base / "report.md", now=datetime(2026, 7, 9, 10, 0), db_file=db_file, health_history_file=base / "health.jsonl")
            self.assertFalse(result["ledger_json_parity"])
            self.assertIn("ledger_json_signal_mismatch", result["issue_codes"])

    def test_reports_healthy_when_ledger_and_json_signal_ids_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "generated_at": "2026-07-09 09:59:00", "signals": [{"id": "s1"}, {"id": "s2"}]}), encoding="utf-8")
            db_file = base / "trading.db"
            self._ledger_with_signal_ids(db_file, ["s1", "s2"])

            result = joinquant_health.build_health_report(signal_file, base / "missing-account.json", report_file=base / "report.md", now=datetime(2026, 7, 9, 10, 0), db_file=db_file, health_history_file=base / "health.jsonl")

            self.assertTrue(result["ledger_ok"])
            self.assertTrue(result["ledger_json_parity"])
            self.assertNotIn("ledger_unavailable", result["issue_codes"])
            self.assertNotIn("ledger_json_signal_mismatch", result["issue_codes"])

    def test_reports_unavailable_ledger_as_trading_hours_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "generated_at": "2026-07-09 09:59:00", "signals": []}), encoding="utf-8")

            result = joinquant_health.build_health_report(signal_file, base / "missing-account.json", report_file=base / "report.md", now=datetime(2026, 7, 9, 10, 0), db_file=base / "missing" / "trading.db", health_history_file=base / "health.jsonl")

            self.assertFalse(result["ledger_ok"])
            self.assertIn("ledger_unavailable", result["issue_codes"])
            self.assertTrue(result["alert_required"])

    def test_reports_bounded_signal_id_mismatch_but_no_off_hours_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "generated_at": "2026-07-09 16:00:00", "signals": [{"id": "json-only", "action": "sell"}]}), encoding="utf-8")
            db_file = base / "trading.db"
            self._ledger_with_signal_ids(db_file, ["ledger-only"])
            snapshot_file = base / "account.json"
            snapshot_file.write_text(json.dumps({"schema_version": 1, "received_at": "2026-07-09 16:00:00", "strategy_template_version": app_config.JOINQUANT_TEMPLATE_VERSION, "positions": []}), encoding="utf-8")
            positions_file = base / "positions.json"
            positions_file.write_text('{"positions": []}', encoding="utf-8")

            result = joinquant_health.build_health_report(signal_file, snapshot_file, report_file=base / "report.md", now=datetime(2026, 7, 9, 16, 1), db_file=db_file, positions_file=positions_file, health_history_file=base / "health.jsonl")

            self.assertIn("ledger_json_signal_mismatch", result["issue_codes"])
            self.assertFalse(result["alert_required"])
    def test_reports_ok_when_signal_and_snapshot_are_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            snapshot_file = base / "account_snapshot.json"
            history_file = base / "account_snapshot_history.jsonl"
            report_file = base / "health.md"
            now = datetime(2026, 7, 9, 10, 0, 0)
            signal_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "generated_at": "2026-07-09 09:59:00",
                        "signals": [{"id": "s1"}],
                    }
                ),
                encoding="utf-8",
            )
            snapshot = {
                "schema_version": 1,
                "generated_at": "2026-07-09 09:58:00",
                "received_at": "2026-07-09 09:58:10",
                "strategy_template_version": app_config.JOINQUANT_TEMPLATE_VERSION,
                "cash": 90000,
                "total_value": 101000,
                "positions": [{"code": "600000"}],
                "orders": [{"status": "submitted"}],
            }
            snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")
            history_file.write_text(json.dumps(snapshot, ensure_ascii=False) + "\n", encoding="utf-8")
            db_file = base / "trading.db"
            self._ledger_with_signal_ids(db_file, ["s1"])

            result = joinquant_health.build_health_report(
                signal_file,
                snapshot_file,
                history_file,
                report_file,
                now=now,
                signal_max_age_min=30,
                snapshot_max_age_min=15,
                failed_order_limit=1,
                api_event_file=base / "missing_api_events.jsonl",
                positions_file=base / "missing_positions.json",
                health_history_file=base / "health_history.jsonl",
                db_file=db_file,
            )

            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["is_trading_time"])
            self.assertFalse(result["alert_required"])
            self.assertEqual(result["snapshot_count_today"], 1)
            self.assertEqual(result["position_count"], 1)
            self.assertIn("JoinQuant 健康检查", report_file.read_text(encoding="utf-8"))

    def test_reports_critical_when_snapshot_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            snapshot_file = base / "account_snapshot.json"
            report_file = base / "health.md"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "generated_at": "2026-07-09 09:59:00", "signals": []}),
                encoding="utf-8",
            )
            snapshot_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "received_at": "2026-07-09 09:20:00",
                        "strategy_template_version": app_config.JOINQUANT_TEMPLATE_VERSION,
                        "positions": [],
                        "orders": [],
                    }
                ),
                encoding="utf-8",
            )

            result = joinquant_health.build_health_report(
                signal_file,
                snapshot_file,
                base / "missing_history.jsonl",
                report_file,
                now=datetime(2026, 7, 9, 10, 0, 0),
                snapshot_max_age_min=15,
                api_event_file=base / "missing_api_events.jsonl",
                positions_file=base / "missing_positions.json",
                health_history_file=base / "health_history.jsonl",
            )

            self.assertEqual(result["status"], "critical")
            self.assertTrue(result["alert_required"])
            self.assertIn("snapshot_stale", result["issue_codes"])

    def test_stale_snapshot_outside_trading_time_does_not_require_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            snapshot_file = base / "account_snapshot.json"
            report_file = base / "health.md"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "generated_at": "2026-07-09 15:20:00", "signals": []}),
                encoding="utf-8",
            )
            snapshot_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "received_at": "2026-07-09 15:20:00",
                        "strategy_template_version": app_config.JOINQUANT_TEMPLATE_VERSION,
                        "positions": [],
                        "orders": [],
                    }
                ),
                encoding="utf-8",
            )

            result = joinquant_health.build_health_report(
                signal_file,
                snapshot_file,
                base / "missing_history.jsonl",
                report_file,
                now=datetime(2026, 7, 9, 21, 0, 0),
                snapshot_max_age_min=15,
                api_event_file=base / "missing_api_events.jsonl",
                positions_file=base / "missing_positions.json",
                health_history_file=base / "health_history.jsonl",
            )

            self.assertEqual(result["status"], "critical")
            self.assertFalse(result["is_trading_time"])
            self.assertFalse(result["alert_required"])
            self.assertIn("snapshot_stale", result["issue_codes"])

    def test_counts_failed_orders_from_today_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            snapshot_file = base / "account_snapshot.json"
            history_file = base / "account_snapshot_history.jsonl"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "generated_at": "2026-07-09 09:59:00", "signals": []}),
                encoding="utf-8",
            )
            snapshot_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "received_at": "2026-07-09 09:59:00",
                        "strategy_template_version": app_config.JOINQUANT_TEMPLATE_VERSION,
                        "positions": [],
                        "orders": [],
                    }
                ),
                encoding="utf-8",
            )
            history_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "schema_version": 1,
                                "received_at": "2026-07-09 09:40:00",
                                "orders": [{"status": "failed"}, {"status": "rejected"}],
                            }
                        ),
                        json.dumps(
                            {
                                "schema_version": 1,
                                "received_at": "2026-07-08 14:40:00",
                                "orders": [{"status": "failed"}],
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            result = joinquant_health.build_health_report(
                signal_file,
                snapshot_file,
                history_file,
                base / "health.md",
                now=datetime(2026, 7, 9, 10, 0, 0),
                failed_order_limit=1,
                api_event_file=base / "missing_api_events.jsonl",
                positions_file=base / "missing_positions.json",
                health_history_file=base / "health_history.jsonl",
            )

            self.assertEqual(result["failed_orders_today"], 2)
            self.assertIn("failed_orders_high", result["issue_codes"])

    def test_builds_daily_stability_metrics_from_api_events_and_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            snapshot_file = base / "account_snapshot.json"
            history_file = base / "account_snapshot_history.jsonl"
            api_event_file = base / "api_events.jsonl"
            positions_file = base / "positions.json"
            now = datetime(2026, 7, 9, 15, 30, 0)
            signal_file.write_text(
                json.dumps({"schema_version": 1, "generated_at": "2026-07-09 15:20:00", "signals": []}),
                encoding="utf-8",
            )
            snapshot = {
                "schema_version": 1,
                "received_at": "2026-07-09 15:25:00",
                "strategy_template_version": app_config.JOINQUANT_TEMPLATE_VERSION,
                "cash": 90000,
                "total_value": 101000,
                "positions": [{"code": "600000", "qty": 100}],
                "orders": [
                    {"action": "buy", "status": "failed", "reason": "limit_up_or_suspended"},
                    {"action": "sell", "status": "skipped", "reason": "t_plus_1"},
                ],
            }
            snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")
            history_file.write_text(json.dumps(snapshot, ensure_ascii=False) + "\n", encoding="utf-8")
            api_event_file.write_text(
                "\n".join(
                    [
                        json.dumps({"received_at": "2026-07-09 09:31:00", "endpoint": "signals", "status_code": 200}),
                        json.dumps({"received_at": "2026-07-09 09:32:00", "endpoint": "signals", "status_code": 200}),
                        json.dumps(
                            {"received_at": "2026-07-09 09:33:00", "endpoint": "account_snapshot", "status_code": 200}
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            positions_file.write_text(
                json.dumps({"positions": [{"code": "600000", "qty": 100}], "source": "joinquant"}),
                encoding="utf-8",
            )

            result = joinquant_health.build_health_report(
                signal_file,
                snapshot_file,
                history_file,
                base / "health.md",
                now=now,
                api_event_file=api_event_file,
                positions_file=positions_file,
                failed_order_limit=5,
                health_history_file=base / "health_history.jsonl",
            )

            self.assertEqual(result["signal_pull_count_today"], 2)
            self.assertEqual(result["snapshot_post_count_today"], 1)
            self.assertEqual(result["failed_order_breakdown"]["buy:limit_up_or_suspended"], 1)
            self.assertEqual(result["failed_order_breakdown"]["sell:t_plus_1"], 1)
            self.assertEqual(result["position_consistency"], "ok")
            self.assertGreaterEqual(result["stability_score"], 80)

    def test_reports_position_mismatch_between_snapshot_and_synced_holdings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            snapshot_file = base / "account_snapshot.json"
            positions_file = base / "positions.json"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "generated_at": "2026-07-09 10:00:00", "signals": []}),
                encoding="utf-8",
            )
            snapshot_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "received_at": "2026-07-09 10:00:00",
                        "strategy_template_version": app_config.JOINQUANT_TEMPLATE_VERSION,
                        "positions": [{"code": "600000", "qty": 100}],
                        "orders": [],
                    }
                ),
                encoding="utf-8",
            )
            positions_file.write_text(
                json.dumps({"positions": [{"code": "000001", "qty": 100}], "source": "joinquant"}),
                encoding="utf-8",
            )

            result = joinquant_health.build_health_report(
                signal_file,
                snapshot_file,
                base / "missing_history.jsonl",
                base / "health.md",
                now=datetime(2026, 7, 9, 10, 1, 0),
                positions_file=positions_file,
                api_event_file=base / "missing_api_events.jsonl",
                health_history_file=base / "health_history.jsonl",
            )

            self.assertEqual(result["position_consistency"], "mismatch")
            self.assertIn("position_mismatch", result["issue_codes"])

    def test_reports_template_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            snapshot_file = base / "account_snapshot.json"
            report_file = base / "health.md"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "generated_at": "2026-07-09 10:00:00", "signals": []}),
                encoding="utf-8",
            )
            snapshot_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "received_at": "2026-07-09 10:00:00",
                        "strategy_template_version": "2026-07-09.1-old",
                        "positions": [],
                        "orders": [],
                    }
                ),
                encoding="utf-8",
            )

            result = joinquant_health.build_health_report(
                signal_file,
                snapshot_file,
                base / "missing_history.jsonl",
                report_file,
                now=datetime(2026, 7, 9, 10, 1, 0),
                api_event_file=base / "missing_api_events.jsonl",
                positions_file=base / "missing_positions.json",
                health_history_file=base / "health_history.jsonl",
            )

            self.assertEqual(result["expected_template_version"], app_config.JOINQUANT_TEMPLATE_VERSION)
            self.assertEqual(result["strategy_template_version"], "2026-07-09.1-old")
            self.assertIn("template_version_mismatch", result["issue_codes"])
            self.assertIn("JoinQuant 网站模板未更新", "\n".join(result["issues"]))

    def test_alert_markdown_is_mobile_friendly(self) -> None:
        md = joinquant_health.build_alert_markdown(
            {
                "status": "critical",
                "generated_at": "2026-07-09 10:00:00",
                "issue_codes": ["snapshot_stale"],
                "issues": ["账户快照超时 40.0 分钟"],
                "snapshot_age_min": 40.0,
                "signal_age_min": 1.0,
                "snapshot_count_today": 0,
                "failed_orders_today": 0,
                "position_count": 0,
                "latest_total_value": 0,
                "latest_cash": 0,
            }
        )

        self.assertIn("【JoinQuant】健康异常", md)
        self.assertIn("账户快照超时", md)
        self.assertIn("快照年龄", md)


    def test_skipped_t_plus_one_does_not_count_as_failed_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            snapshot_file = base / "account_snapshot.json"
            history_file = base / "account_snapshot_history.jsonl"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "generated_at": "2026-07-09 10:00:00", "signals": []}),
                encoding="utf-8",
            )
            snapshot = {
                "schema_version": 1,
                "received_at": "2026-07-09 10:01:00",
                "strategy_template_version": app_config.JOINQUANT_TEMPLATE_VERSION,
                "positions": [],
                "orders": [
                    {"action": "buy", "status": "failed", "reason": "limit_up_or_suspended"},
                    {"action": "sell", "status": "skipped", "reason": "t_plus_1"},
                ],
            }
            snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")
            history_file.write_text(json.dumps(snapshot, ensure_ascii=False) + "\n", encoding="utf-8")

            result = joinquant_health.build_health_report(
                signal_file,
                snapshot_file,
                history_file,
                base / "health.md",
                now=datetime(2026, 7, 9, 10, 2, 0),
                failed_order_limit=5,
                api_event_file=base / "missing_api_events.jsonl",
                positions_file=base / "missing_positions.json",
                health_history_file=base / "health_history.jsonl",
            )

            self.assertEqual(result["failed_orders_today"], 1)
            self.assertEqual(result["failed_order_breakdown"]["buy:limit_up_or_suspended"], 1)
            self.assertEqual(result["failed_order_breakdown"]["sell:t_plus_1"], 1)
if __name__ == "__main__":
    unittest.main()
