import unittest

from execution_state import classify_exit_execution, trading_minutes_between


class ExecutionStateTest(unittest.TestCase):
    def test_trading_minutes_exclude_lunch_overnight_and_weekend(self) -> None:
        self.assertEqual(trading_minutes_between(
            "2026-07-15 11:29:00", "2026-07-15 13:01:00", set()
        ), 2.0)
        self.assertEqual(trading_minutes_between(
            "2026-07-15 14:59:00", "2026-07-16 09:31:00", set()
        ), 2.0)
        self.assertEqual(trading_minutes_between(
            "2026-07-17 14:59:00", "2026-07-20 09:31:00", set()
        ), 2.0)

    def test_hard_stop_delivery_escalates_after_effective_window(self) -> None:
        intent = {
            "signal_id": "s-1", "reason": "hard_stop", "target_qty": 0,
            "published_at": "2026-07-15 09:30:00", "validated_at": "2026-07-15 09:30:00",
            "created_at": "2026-07-15 09:30:00",
        }
        warning = classify_exit_execution(intent, [], 600, "2026-07-15 09:32:00", set())
        error = classify_exit_execution(intent, [], 600, "2026-07-15 09:33:00", set())
        self.assertEqual((warning["state"], warning["severity"]), ("SIGNAL_DELIVERY_PENDING", "WARNING"))
        self.assertEqual(error["severity"], "ERROR")

    def test_platform_states_are_distinguished(self) -> None:
        intent = {
            "signal_id": "s-1", "reason": "time_stop", "target_qty": 0,
            "published_at": "2026-07-15 09:30:00", "created_at": "2026-07-15 09:30:00",
        }
        stale = classify_exit_execution(intent, [{
            "signal_id": "s-1", "status": "skipped", "reason": "stale",
            "updated_at": "2026-07-15 09:31:00", "filled_qty": 0,
        }], 600, "2026-07-15 09:36:00", set())
        partial = classify_exit_execution(intent, [{
            "signal_id": "s-1", "status": "partial", "reason": "",
            "first_submitted_at": "2026-07-15 09:31:00", "updated_at": "2026-07-15 09:32:00",
            "filled_qty": 100,
        }], 500, "2026-07-15 09:36:00", set())
        blocked = classify_exit_execution(intent, [{
            "signal_id": "s-1", "status": "t_plus_one", "reason": "t_plus_one",
            "updated_at": "2026-07-15 09:31:00", "filled_qty": 0,
        }], 600, "2026-07-15 10:00:00", set())
        done = classify_exit_execution(intent, [], 0, "2026-07-15 09:32:00", set())
        self.assertEqual(stale["state"], "SIGNAL_STALE")
        self.assertEqual(partial["state"], "PARTIAL_FILL_PENDING")
        self.assertEqual((blocked["state"], blocked["severity"]), ("MARKET_BLOCKED_T1", "WARNING"))
        self.assertTrue(done["complete"])

    def test_stage_start_uses_first_submission_and_latest_material_partial_fill(self) -> None:
        intent = {
            "signal_id": "s-1", "reason": "hard_stop", "target_qty": 0,
            "published_at": "2026-07-15 09:30:00",
        }
        submitted = classify_exit_execution(intent, [{
            "signal_id": "s-1", "status": "submitted", "filled_qty": 0,
            "first_submitted_at": "2026-07-15 09:31:00",
            "updated_at": "2026-07-15 09:32:00",
        }], 600, "2026-07-15 09:33:00", set())
        partial = classify_exit_execution(intent, [{
            "signal_id": "s-1", "status": "partial", "filled_qty": 100,
            "first_submitted_at": "2026-07-15 09:31:00",
            "updated_at": "2026-07-15 09:32:00",
        }], 500, "2026-07-15 09:33:00", set())
        self.assertEqual(submitted["stage_started_at"], "2026-07-15 09:31:00")
        self.assertEqual(partial["stage_started_at"], "2026-07-15 09:32:00")


if __name__ == "__main__":
    unittest.main()
