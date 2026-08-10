from __future__ import annotations

import ast
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import config as app_config
import joinquant_exporter
import strategy_snapshot_runtime as runtime
import trade_safety
from candidate_core import score_candidate_frame


DECISION_AT = "2025-07-01T10:05:00+08:00"


def _parameters() -> dict:
    return runtime.build_safe_strategy_parameters(app_config)


def _state(**changes: object) -> dict:
    result = {
        "allow_buy": True,
        "account_total_value": 100_000.0,
        "current_position_pct": 0.0,
        "current_open_risk_pct": 0.0,
        "current_position_count": 0,
        "sector_exposure_pct": {},
        "theme_exposure_pct": {},
        "cooldown_codes": set(),
        "available_cash": 100_000.0,
        "new_positions_today": 0,
        "orders_today": 0,
        "daily_turnover_pct": 0.0,
        "daily_pnl_pct": 0.0,
        "account_drawdown_pct": 0.0,
        "consecutive_losses": 0,
    }
    result.update(changes)
    return result


def _row(**changes: object) -> dict:
    result = {
        "code": "600000",
        "name": "PF Bank",
        "price": 10.0,
        "entry_price": 10.0,
        "stop_loss": 9.5,
        "take_profit": 11.0,
        "position_pct": 10.0,
        "final_score": 95.0,
        "signal_action": "continue",
        "execution_plan_version": runtime.EXECUTION_PLAN_VERSION,
        "execution_allowed": True,
        "market_state": "NORMAL",
        "market_regime": "NORMAL",
        "board_type": "main_active",
        "atr14": 0.2,
        "prev_close": 10.0,
        "industry": "bank",
        "theme_label": "dividend",
        "pct_chg": 2.0,
        "amount": 100_000_000,
    }
    result.update(changes)
    return result


def _features(**changes: object) -> dict:
    values = {name: 1.0 for name in runtime.REQUIRED_PORTABLE_FEATURES}
    values.update({
        "price": 10.0,
        "pct_chg": 2.0,
        "amount": 100_000_000,
        "turnover": 3.0,
        "market_cap": 1_000_000_000,
        "score": 90.0,
        "final_score": 0.0,
        "news_score": 0.0,
        "entry_price": 10.0,
        "stop_loss": 9.5,
        "take_profit": 11.0,
        "position_pct": 10.0,
        "atr14": 0.2,
        "theme_label": "dividend",
        "theme_heat_level": "中",
        "pressure_label": "正常",
        "market_state": "NORMAL",
        "market_regime": "NORMAL",
        "signal_state": "active",
        "buy_state": "已到买点",
        "execution_plan_version": runtime.EXECUTION_PLAN_VERSION,
        "execution_allowed": True,
    })
    values.update(changes)
    return {
        name: {"value": value, "available_at": DECISION_AT}
        for name, value in values.items()
    }


class StrategySnapshotRuntimeTest(unittest.TestCase):
    def test_runtime_source_parses_as_python_36(self) -> None:
        source = Path(runtime.__file__).read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 6))
        self.assertNotIn("dataclass", source)
        self.assertNotIn("from __future__ import annotations", source)

    def test_shared_score_is_exactly_the_live_score(self) -> None:
        frame = pd.DataFrame([
            {"score": 70, "news_score": 1, "pct_chg": 3, "turnover": 4},
            {"score": 70, "news_score": 0, "pct_chg": 3, "turnover": 2},
            {"score": 68, "news_score": -1, "pct_chg": 1, "turnover": 2},
        ])
        expected = score_candidate_frame(frame)
        actual = runtime.score_candidate_frame(frame)
        pd.testing.assert_frame_equal(actual, expected)

    def test_shared_tradability_and_rejection_stages_match_live(self) -> None:
        for changes in (
            {"paused": True},
            {"paused": "false"},
            {"is_st": True},
            {"listing_days": 2},
            {"quote_age_sec": 121},
            {"amount": 1_000_000},
            {},
        ):
            row = _row(**changes)
            self.assertEqual(
                runtime.tradability_reject_reason(row, _parameters()),
                trade_safety.tradability_reject_reason(row),
            )
        for stage, reasons in joinquant_exporter._REJECTION_STAGES.items():
            for reason in reasons:
                self.assertEqual(runtime.rejection_stage(reason), stage)

    def test_portable_non_gap_decisions_match_live_rejection_chain(self) -> None:
        cases = (
            ({}, {}, ""),
            ({"final_score": 70}, {}, "buy_low_score"),
            ({"paused": True}, {}, "buy_suspended"),
            ({"is_st": True}, {}, "buy_st"),
            ({"amount": 1_000_000}, {}, "buy_illiquid"),
            ({"market_state": "RISK_OFF"}, {}, "buy_disabled"),
            ({"pct_chg": 9.8}, {}, "buy_near_limit_up"),
            ({"execution_allowed": float("nan")}, {}, "buy_execution_plan_missing"),
            ({}, {"current_position_count": app_config.JOINQUANT_MAX_POSITIONS_DEFAULT}, "buy_max_positions"),
            ({}, {"available_cash": 1}, "buy_insufficient_available_cash"),
        )
        parameters = _parameters()
        for row_changes, state_changes, expected in cases:
            row = _row(**row_changes)
            state = _state(**state_changes)
            actual, _ = runtime.buy_reject_reason(row, state, parameters)
            live = joinquant_exporter._buy_reject_reason(
                pd.Series(row),
                parameters["ml"]["min_score"],
                allow_buy=state["allow_buy"],
                account_total_value=state["account_total_value"],
                current_position_pct=state["current_position_pct"],
                current_open_risk_pct=state["current_open_risk_pct"],
                current_position_count=state["current_position_count"],
                sector_exposure_pct=state["sector_exposure_pct"],
                theme_exposure_pct=state["theme_exposure_pct"],
                cooldown_codes=state["cooldown_codes"],
                available_cash=state["available_cash"],
                new_positions_today=state["new_positions_today"],
                orders_today=state["orders_today"],
                daily_turnover_pct=state["daily_turnover_pct"],
                daily_pnl_pct=state["daily_pnl_pct"],
                account_drawdown_pct=state["account_drawdown_pct"],
                consecutive_losses=state["consecutive_losses"],
                enforce_execution_contract=True,
            )
            self.assertEqual(actual, live)
            self.assertEqual(actual, expected)

    def test_generated_builder_requires_timed_features_and_portfolio_state(self) -> None:
        manifest = {
            "strategy_version": "a_share_strategy-v1",
            "parameter_version": "risk-observe-v1:test",
            "feature_schema_version": "live-candidate-v1",
            "market_data_version": "joinquant-raw-5m-v1",
            "code_hash": "a" * 64,
            "snapshot_id": "b" * 64,
        }
        runtime.configure_snapshot(manifest, _parameters())
        market = pd.DataFrame([
            {"code": "600000.XSHG", "close": 10.0, "pct_chg": 2.0,
             "cum_amount": 100_000_000, "paused": 0, "prev_close": 9.8,
             "high_limit": 10.78, "low_limit": 8.82, "is_st": False},
            {"code": "000001.XSHE", "close": 8.0, "pct_chg": 1.0,
             "cum_amount": 80_000_000, "paused": 0, "prev_close": 7.9,
             "high_limit": 8.69, "low_limit": 7.11, "is_st": False},
        ])
        context = SimpleNamespace(decision_at=DECISION_AT, snapshot=market)

        def provider(_context: object) -> list[dict]:
            return [
                {"code": "600000", "features": _features(score=95)},
                {"code": "000001", "features": _features(
                    price=8.0, entry_price=8.0, stop_loss=7.5, take_profit=9.0,
                    score=40, amount=80_000_000,
                )},
            ]

        runtime.configure_strict_providers(provider, lambda _: _state())
        rows = runtime.my_strict_candidate_builder(context)
        self.assertEqual([row["code"] for row in rows], ["600000", "000001"])
        self.assertTrue(rows[0]["selected"])
        self.assertEqual(rows[1]["rejection_code"], "buy_low_score")
        self.assertGreater(rows[0]["features"]["rule_target_qty"]["value"], 0)
        future = _features()
        future["news_score"]["available_at"] = "2025-07-01T10:06:00+08:00"
        runtime.configure_strict_providers(
            lambda _: [{"code": "600000", "features": future}],
            lambda _: _state(),
        )
        with self.assertRaisesRegex(ValueError, "FEATURE_FROM_FUTURE"):
            runtime.my_strict_candidate_builder(context)


if __name__ == "__main__":
    unittest.main()
