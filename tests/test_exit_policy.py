import unittest

import exit_policy


class ExitPolicyTest(unittest.TestCase):
    def state(self, **changes):
        values = {
            "code": "600000",
            "mode": "short",
            "initial_qty": 1000,
            "current_qty": 1000,
            "entry_price": 10.0,
            "initial_stop_price": 9.0,
            "highest_price": 10.0,
            "atr14": 0.4,
            "take_profit_stage": 0,
            "holding_trade_days": 1,
        }
        values.update(changes)
        return exit_policy.PositionExitState(**values)

    def test_hard_stop_has_highest_priority_and_targets_zero(self) -> None:
        decision = exit_policy.evaluate_exit(self.state(highest_price=13.0), 8.9, "NORMAL")
        self.assertEqual(decision.action, "hard_stop")
        self.assertEqual(decision.target_qty, 0)

    def test_two_r_sells_half_by_board_lot(self) -> None:
        decision = exit_policy.evaluate_exit(self.state(), 12.0, "NORMAL")
        self.assertEqual(decision.action, "take_profit_1")
        self.assertEqual(decision.target_qty, 500)

    def test_one_board_lot_is_fully_sold_at_first_take_profit(self) -> None:
        decision = exit_policy.evaluate_exit(
            self.state(initial_qty=100, current_qty=100), 12.0, "NORMAL"
        )
        self.assertEqual(decision.target_qty, 0)

    def test_trailing_stop_only_applies_after_first_take_profit(self) -> None:
        state = self.state(current_qty=500, highest_price=13.0, take_profit_stage=1)
        decision = exit_policy.evaluate_exit(state, 12.1, "NORMAL")
        self.assertEqual(decision.action, "trailing_stop")
        self.assertEqual(decision.target_qty, 0)
        self.assertAlmostEqual(decision.trailing_stop_price, 12.2)

    def test_risk_off_tightens_trailing_stop_without_lowering_initial_stop(self) -> None:
        state = self.state(current_qty=500, highest_price=13.0, take_profit_stage=1)
        normal = exit_policy.evaluate_exit(state, 12.3, "NORMAL")
        risk_off = exit_policy.evaluate_exit(state, 12.3, "RISK_OFF")
        self.assertEqual(normal.action, "hold")
        self.assertEqual(risk_off.action, "trailing_stop")
        self.assertGreater(risk_off.trailing_stop_price, normal.trailing_stop_price)
        self.assertGreaterEqual(risk_off.trailing_stop_price, state.initial_stop_price)

    def test_short_time_stop_uses_trading_days_and_requires_low_progress(self) -> None:
        decision = exit_policy.evaluate_exit(
            self.state(holding_trade_days=3), 10.4, "NORMAL"
        )
        self.assertEqual(decision.action, "time_stop")
        self.assertEqual(decision.target_qty, 0)

    def test_mid_time_stop_waits_ten_trading_days(self) -> None:
        before = exit_policy.evaluate_exit(
            self.state(mode="mid", holding_trade_days=9), 10.5, "NORMAL"
        )
        due = exit_policy.evaluate_exit(
            self.state(mode="mid", holding_trade_days=10), 10.5, "NORMAL"
        )
        self.assertEqual(before.action, "hold")
        self.assertEqual(due.action, "time_stop")

    def test_initial_stop_uses_structure_atr_and_maximum_loss_cap(self) -> None:
        stop = exit_policy.initial_stop_price(
            entry_price=10.0,
            support_price=8.5,
            atr14=1.0,
            board="main_active",
        )
        self.assertEqual(stop, 9.3)

    def test_risk_position_sizes_from_account_loss_budget(self) -> None:
        normal = exit_policy.risk_position_pct(
            entry_price=10.0, stop_price=9.5, board="main_active",
            original_cap_pct=20.0, market_state="NORMAL",
        )
        caution = exit_policy.risk_position_pct(
            entry_price=10.0, stop_price=9.5, board="main_active",
            original_cap_pct=20.0, market_state="CAUTION",
        )
        risk_off = exit_policy.risk_position_pct(
            entry_price=10.0, stop_price=9.5, board="main_active",
            original_cap_pct=20.0, market_state="RISK_OFF",
        )
        self.assertEqual(normal, 10.0)
        self.assertEqual(caution, 5.0)
        self.assertEqual(risk_off, 0.0)


if __name__ == "__main__":
    unittest.main()
