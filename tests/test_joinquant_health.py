import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import joinquant_health


class JoinQuantHealthTest(unittest.TestCase):
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
                "strategy_template_version": "2026-07-09.2-order-target-value",
                "cash": 90000,
                "total_value": 101000,
                "positions": [{"code": "600000"}],
                "orders": [{"status": "submitted"}],
            }
            snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")
            history_file.write_text(json.dumps(snapshot, ensure_ascii=False) + "\n", encoding="utf-8")

            result = joinquant_health.build_health_report(
                signal_file,
                snapshot_file,
                history_file,
                report_file,
                now=now,
                signal_max_age_min=30,
                snapshot_max_age_min=15,
                failed_order_limit=1,
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
                        "strategy_template_version": "2026-07-09.2-order-target-value",
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
                        "strategy_template_version": "2026-07-09.2-order-target-value",
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
                        "strategy_template_version": "2026-07-09.2-order-target-value",
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
                "strategy_template_version": "2026-07-09.2-order-target-value",
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
                        "strategy_template_version": "2026-07-09.2-order-target-value",
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
            )

            self.assertEqual(result["expected_template_version"], "2026-07-09.2-order-target-value")
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


if __name__ == "__main__":
    unittest.main()
