from __future__ import annotations

import ast
import copy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import config as app_config
import joinquant_point_in_time as pit
import strategy_snapshot_runtime as runtime


SHANGHAI = timezone(timedelta(hours=8))


def _parameters() -> dict:
    value = runtime.build_safe_strategy_parameters(app_config)
    value["scan"]["min_amount"] = 1_000_000
    value["historical_replay"] = {
        "initial_cash_yuan": 200_000.0,
    }
    return value


def _manifest() -> dict:
    return {
        "strategy_version": "a_share_strategy-v1-pit-replay",
        "parameter_version": "test-params",
        "feature_schema_version": pit.PIT_FEATURE_SCHEMA_VERSION,
        "market_data_version": pit.PIT_MARKET_DATA_VERSION,
        "code_hash": "a" * 64,
        "snapshot_id": "b" * 64,
    }


def _history() -> pd.DataFrame:
    rows = []
    start = datetime(2025, 5, 1)
    for offset in range(45):
        day = start + timedelta(days=offset)
        price = 8.0 + offset * 0.04
        rows.append({
            "time": day,
            "code": "600000.XSHG",
            "open": price - 0.05,
            "high": price + 0.10,
            "low": price - 0.10,
            "close": price,
            "volume": 1_000_000,
            "money": 10_000_000,
            "paused": 0,
            "high_limit": price * 1.1,
            "low_limit": price * 0.9,
            "factor": 1.0,
        })
    # This deliberately future row must never enter the technical snapshot.
    rows.append({
        **rows[-1],
        "time": datetime(2025, 7, 2),
        "open": 1000.0,
        "high": 1000.0,
        "low": 1000.0,
        "close": 1000.0,
    })
    return pd.DataFrame(rows)


def _context(clock: str = "10:00") -> pit.PortableDecisionContext:
    decision = datetime.strptime(
        "2025-07-01 " + clock, "%Y-%m-%d %H:%M"
    ).replace(tzinfo=SHANGHAI)
    snapshot = pd.DataFrame([{
        "time": decision.replace(tzinfo=None),
        "code": "600000.XSHG",
        "open": 11.8,
        "high": 12.1,
        "low": 11.7,
        "close": 12.0,
        "volume": 500_000,
        "money": 6_000_000,
        "cum_volume": 2_000_000,
        "cum_amount": 60_000_000,
        "paused": 0,
        "high_limit": 12.32,
        "low_limit": 10.0,
        "factor": 1.0,
        "prev_close": 11.2,
        "pct_chg": 7.14,
        "name": "浦发银行",
        "is_st": False,
        "delisting": False,
        "listing_days": 5000,
    }])
    market = pd.DataFrame([{
        "time": decision.replace(tzinfo=None),
        "code": "000001.XSHG",
        "open": 3500.0,
        "high": 3540.0,
        "low": 3490.0,
        "close": 3535.0,
        "volume": 1,
        "money": 1,
        "paused": 0,
        "high_limit": 4000.0,
        "low_limit": 3000.0,
        "factor": 1.0,
        "prev_close": 3500.0,
        "pct_chg": 1.0,
        "cum_amount": 1,
        "cum_volume": 1,
    }])
    return pit.PortableDecisionContext(
        dataset_id="test-dataset",
        decision_at=decision.isoformat(),
        trade_date="2025-07-01",
        snapshot=snapshot,
        daily_history=_history(),
        universe_codes=("600000",),
        market_snapshot=market,
    )


class JoinQuantPointInTimeTest(unittest.TestCase):
    def test_file_history_provider_replaces_joinquant_daily_requery(self) -> None:
        calls = []

        def provider(codes, trade_date):
            calls.append((tuple(codes), trade_date))
            return _history()

        engine = pit.PointInTimeReplayEngine(
            _parameters(),
            _manifest(),
            namespace={
                "strict_pit_history_provider": provider,
                "get_price": lambda *args, **kwargs: self.fail(
                    "daily JoinQuant API must not be called"
                ),
            },
        )
        context = _context()
        context.daily_history = pd.DataFrame()
        engine._prime_histories(context, ["600000"])
        chosen = engine._history_for(context, "600000")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], (("600000",), "2025-07-01"))
        self.assertEqual(len(chosen), 45)
        self.assertTrue(
            all(
                value.date().isoformat() < "2025-07-01"
                for value in chosen["time"]
            )
        )

    def test_source_parses_as_python_36_without_dataclasses(self) -> None:
        source = Path(pit.__file__).read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 6))
        self.assertNotIn("from dataclasses", source)
        self.assertNotIn("from __future__ import annotations", source)
        self.assertNotIn("from jqdata import get_fundamentals", source)

    def test_historical_limit_prices_use_only_known_board_rules(self) -> None:
        self.assertEqual(
            pit._daily_limit_prices(10.0, "600000", False, "2025-07-01", 100),
            (11.0, 9.0),
        )
        self.assertEqual(
            pit._daily_limit_prices(10.0, "300001", False, "2025-07-01", 100),
            (12.0, 8.0),
        )
        self.assertEqual(
            pit._daily_limit_prices(10.0, "600000", True, "2025-07-01", 100),
            (10.5, 9.5),
        )

    def test_timezone_parser_is_python_36_compatible(self) -> None:
        parsed = pit._aware_datetime("2025-07-01T02:00:00+00:00")
        self.assertEqual(parsed.isoformat(), "2025-07-01T10:00:00+08:00")
        with self.assertRaisesRegex(ValueError, "TIMEZONE_REQUIRED"):
            pit._aware_datetime("2025-07-01T10:00:00")

    def test_snapshot_lookup_only_materializes_requested_positions(self) -> None:
        engine = pit.PointInTimeReplayEngine(
            _parameters(), _manifest(), namespace={}, initial_cash=200_000,
        )
        snapshot = pd.DataFrame([
            {"code": "000001.XSHE", "close": 10.0},
            {"code": "600000.XSHG", "close": 12.0},
            {"code": "300001.XSHE", "close": 15.0},
        ])
        with patch.object(
            pd.DataFrame, "iterrows", side_effect=AssertionError("full scan forbidden")
        ):
            by_code = engine._snapshot_by_code(snapshot, {"600000"})
        self.assertEqual(list(by_code), ["600000"])
        self.assertEqual(by_code["600000"]["close"], 12.0)

    def test_new_day_evicts_only_stale_history_cache(self) -> None:
        engine = pit.PointInTimeReplayEngine(
            _parameters(), _manifest(), namespace={}, initial_cash=200_000,
        )
        engine.current_day = "2025-06-30"
        engine._history_cache[("2025-06-30", "600000")] = _history()

        engine._new_day("2025-07-01", _context().snapshot)

        self.assertEqual(engine._history_cache, {})
        self.assertEqual(engine.current_day, "2025-07-01")

    def test_complete_provider_is_no_future_and_schedules_selected_order(self) -> None:
        valuation_days: list[str] = []

        def valuations(codes: list[str], day) -> dict:
            valuation_days.append(day.isoformat())
            return {
                code: {
                    "market_cap": 100_000_000_000.0,
                    "circulating_market_cap": 50_000_000_000.0,
                }
                for code in codes
            }

        namespace = {
            "pit_valuation_provider": valuations,
            "pit_industry_provider": lambda codes, day: {
                code: "银行" for code in codes
            },
            "order_target": lambda security, target: None,
        }
        parameters = _parameters()
        manifest = _manifest()
        engine = pit.PointInTimeReplayEngine(
            parameters, manifest, namespace=namespace, initial_cash=200_000
        )
        runtime.configure_snapshot(manifest, parameters)
        runtime.configure_strict_providers(
            engine.feature_provider,
            engine.portfolio_state_provider,
            engine.decision_observer,
        )
        rows = runtime.my_strict_candidate_builder(_context())
        self.assertEqual(len(rows), 1)
        self.assertIsNone(engine.last_context)
        self.assertTrue(rows[0]["selected"])
        features = rows[0]["features"]
        self.assertEqual(features["news_score"]["value"], 0.0)
        self.assertEqual(
            features["news_data_status"]["value"],
            pit.PIT_NEWS_POLICY,
        )
        self.assertLess(features["ma30"]["value"], 100.0)
        self.assertEqual(valuation_days, ["2025-06-14"])
        self.assertEqual(engine.drain_platform_intents(), [])
        next_context = _context("10:05")
        runtime.my_strict_candidate_builder(next_context)
        intents = engine.drain_platform_intents()
        self.assertEqual([(item["side"], item["code"]) for item in intents], [
            ("buy", "600000")
        ])

    def test_future_feature_timestamp_is_still_rejected_by_runtime(self) -> None:
        features = {
            name: {"value": 1.0, "available_at": "2025-07-01T10:00:00+08:00"}
            for name in runtime.REQUIRED_PORTABLE_FEATURES
        }
        features["news_score"]["available_at"] = "2025-07-01T10:01:00+08:00"
        runtime.configure_snapshot(_manifest(), _parameters())
        runtime.configure_strict_providers(
            lambda context: [{"code": "600000", "features": features}],
            lambda context: {
                "allow_buy": True,
                "account_total_value": 200000,
                "current_position_pct": 0,
                "current_open_risk_pct": 0,
                "current_position_count": 0,
                "sector_exposure_pct": {},
                "theme_exposure_pct": {},
                "cooldown_codes": [],
                "available_cash": 200000,
                "new_positions_today": 0,
                "orders_today": 0,
                "daily_turnover_pct": 0,
                "daily_pnl_pct": 0,
                "account_drawdown_pct": 0,
                "consecutive_losses": 0,
            },
        )
        with self.assertRaisesRegex(ValueError, "FEATURE_FROM_FUTURE"):
            runtime.my_strict_candidate_builder(_context())

    def test_early_session_exports_counterfactual_rejection_instead_of_empty(self) -> None:
        context = _context("09:35")
        context.snapshot.loc[:, "cum_amount"] = 100_000
        context.snapshot.loc[:, "pct_chg"] = 1.0
        namespace = {
            "pit_valuation_provider": lambda codes, day: {
                code: {
                    "market_cap": 100_000_000_000.0,
                    "circulating_market_cap": 50_000_000_000.0,
                }
                for code in codes
            },
            "pit_industry_provider": lambda codes, day: {
                code: "银行" for code in codes
            },
        }
        parameters = _parameters()
        manifest = _manifest()
        engine = pit.PointInTimeReplayEngine(
            parameters, manifest, namespace=namespace, initial_cash=200_000
        )
        runtime.configure_snapshot(manifest, parameters)
        runtime.configure_strict_providers(
            engine.feature_provider,
            engine.portfolio_state_provider,
            engine.decision_observer,
        )
        rows = runtime.my_strict_candidate_builder(context)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["selected"])
        self.assertEqual(
            rows[0]["rejection_code"],
            "buy_pool_amount_below_threshold",
        )
        self.assertEqual(rows[0]["rejection_stage"], "score")
        self.assertEqual(engine.drain_platform_intents(), [])

    def test_reserve_member_below_live_pool_score_cutoff_is_explained(self) -> None:
        scan = _parameters()["scan"]
        reason = pit._pool_pre_rejection_code(
            {"cum_amount": scan["min_amount"] * 2, "pct_chg": 6.0},
            False,
            scan,
        )
        self.assertEqual(reason, "buy_pool_score_below_cutoff")
        self.assertIn(reason, runtime.REJECTION_STAGES["score"])

    def test_incomplete_new_stock_history_is_skipped_and_replaced(self) -> None:
        context = _context()
        incomplete = context.snapshot.iloc[0].copy()
        incomplete["code"] = "300999.XSHE"
        incomplete["pct_chg"] = 9.0
        incomplete["cum_amount"] = 90_000_000
        incomplete["listing_days"] = 35
        context.snapshot = pd.concat(
            [pd.DataFrame([incomplete]), context.snapshot], ignore_index=True,
        )
        context.universe_codes = ("300999", "600000")
        namespace = {
            "pit_valuation_provider": lambda codes, day: {
                code: {
                    "market_cap": 100_000_000_000.0,
                    "circulating_market_cap": 50_000_000_000.0,
                }
                for code in codes
            },
            "pit_industry_provider": lambda codes, day: {
                code: "银行" for code in codes
            },
        }
        engine = pit.PointInTimeReplayEngine(
            _parameters(), _manifest(), namespace=namespace, initial_cash=200_000,
        )
        rows = engine.feature_provider(context)
        self.assertEqual([row["code"] for row in rows], ["600000"])
        self.assertEqual(engine.skipped_candidate_events[0]["code"], "300999")
        self.assertEqual(
            engine.skipped_candidate_events[0]["reason"],
            "STRICT_DAILY_HISTORY_REQUIRED",
        )


if __name__ == "__main__":
    unittest.main()
