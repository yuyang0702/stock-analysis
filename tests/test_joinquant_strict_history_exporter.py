from __future__ import annotations

import ast
import csv
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import joinquant_strict_history_exporter as exporter_module
from historical_data import HistoricalStore, strict_table_hash as imported_table_hash
from joinquant_strict_history_exporter import (
    REQUIRED_DAILY_FEATURES,
    REQUIRED_ML_FEATURES,
    REQUIRED_RULE_AUDIT_FEATURES,
    DecisionContext,
    ExportConfig,
    StrictExportError,
    build_candidate_rows,
    build_daily_feature_rows,
    build_strict_manifest,
    canonical_hash,
    export_month,
    normalize_candidate_price,
    strict_table_hash,
    verify_strict_package,
)
from ml_contracts import CandidateSample, TimedFeature, canonical_hash as contract_hash


DECISION_AT = "2025-07-01T10:00:00+08:00"


def _config(**changes: object) -> ExportConfig:
    values: dict[str, object] = {
        "dataset_id": "jq-strict-test-v1",
        "month": "2025-07",
        "strategy_version": "strategy-v1",
        "parameter_version": "params-v1",
        "feature_schema_version": "features-v1",
        "market_data_version": "market-v1",
        "code_hash": "code-sha",
        "generator_hash": "generator-sha",
        "decision_times": ("10:00",),
    }
    values.update(changes)
    return ExportConfig(**values)


def _feature_values(decision_at: str = DECISION_AT) -> dict[str, dict[str, object]]:
    categorical = {
        "pressure_label": "突破/新高",
        "theme_label": "测试题材",
        "theme_heat_level": "中",
        "market_state": "强势进攻",
        "signal_state": "active",
        "buy_state": "ready",
        "market_regime": "NORMAL",
    }
    features = {
        name: {
            "value": categorical.get(name, 1.0),
            "available_at": decision_at,
        }
        for name in REQUIRED_ML_FEATURES
    }
    for name, value in {
        "rule_order": 0,
        "rule_slot_count": 5,
        "rule_target_qty": 100,
    }.items():
        features[name] = {"value": value, "available_at": decision_at}
    self_check = REQUIRED_RULE_AUDIT_FEATURES.difference(features)
    if self_check:
        raise AssertionError(self_check)
    return features


def _raw_candidate(decision_at: str = DECISION_AT) -> dict[str, object]:
    return {
        "code": "000001.XSHE",
        "features": _feature_values(decision_at),
        "selected": True,
        "rejection_stage": "selected",
        "rejection_code": "",
        "final_action": "selected",
    }


def _price(code: str = "000001", bar_at: str = "2025-07-01T10:05:00+08:00") -> dict[str, object]:
    return {
        "code": code,
        "bar_at": bar_at,
        "available_at": bar_at,
        "open": 10.0,
        "high": 10.2,
        "low": 9.9,
        "close": 10.1,
        "volume": 1000,
        "amount": 10100,
        "paused": 0,
        "limit_up": 11.0,
        "limit_down": 9.0,
    }


class JoinQuantStrictHistoryExporterContractTest(unittest.TestCase):
    def test_atomic_json_freezes_python36_mapping_views_deterministically(self) -> None:
        source = {"b": 2, "a": 1}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metadata.json"
            exporter_module._atomic_json(path, {
                "values": source.values(),
                "keys": source.keys(),
                "items": source.items(),
            })
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["values"], [1, 2])
        self.assertEqual(payload["keys"], ["a", "b"])
        self.assertEqual(payload["items"], [["a", 1], ["b", 2]])

    def test_file_backed_pit_history_preserves_codes_and_blocks_future(self) -> None:
        frame = pd.DataFrame([
            {
                "time": "2025-06-30", "_trade_date": date(2025, 6, 30),
                "code": "000001.XSHE", "open": 9.8, "high": 10.1,
                "low": 9.7, "close": 10.0, "factor": 1.0,
            },
            {
                "time": "2025-07-01", "_trade_date": date(2025, 7, 1),
                "code": "000001.XSHE", "open": 10.0, "high": 10.3,
                "low": 9.9, "close": 10.2, "factor": 1.0,
            },
            {
                "time": "2025-07-02", "_trade_date": date(2025, 7, 2),
                "code": "600000.XSHG", "open": 8.0, "high": 8.2,
                "low": 7.9, "close": 8.1, "factor": 1.0,
            },
        ])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pit-history"
            exporter_module._spill_strict_pit_history(
                frame, root, date(2025, 7, 2)
            )
            try:
                chosen = exporter_module.strict_pit_history_provider(
                    ["000001", "600000"], "2025-07-01"
                )
            finally:
                exporter_module._clear_strict_pit_history_store(root)
        self.assertEqual(chosen["code"].tolist(), ["000001.XSHE"])
        self.assertEqual(
            pd.to_datetime(chosen["time"]).dt.strftime("%Y-%m-%d").tolist(),
            ["2025-06-30"],
        )

    def test_research_source_falls_back_to_joinquant_apis_module(self) -> None:
        class FakeJoinQuantApis:
            get_trade_days = staticmethod(lambda *args, **kwargs: [])
            get_all_securities = staticmethod(lambda *args, **kwargs: None)
            get_price = staticmethod(lambda *args, **kwargs: None)
            get_extras = staticmethod(lambda *args, **kwargs: None)

        with patch.object(exporter_module, "_JOINQUANT_APIS", FakeJoinQuantApis):
            source = exporter_module.JoinQuantResearchSource({})
        for name in (
            "get_trade_days", "get_all_securities", "get_price", "get_extras",
        ):
            self.assertTrue(callable(source.namespace[name]))

    def test_price_frame_does_not_duplicate_existing_time_column(self) -> None:
        frame = pd.DataFrame([{
            "time": datetime(2025, 7, 1, 15, 0),
            "code": "000001.XSHE",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "pre_close": 10.0,
            "volume": 1000,
            "money": 10100,
            "paused": 0,
            "high_limit": 11.0,
            "low_limit": 9.0,
            "factor": 1.0,
        }])
        normalized = exporter_module._strict_normalize_price_frame(
            frame, ["000001.XSHE"], frequency="daily"
        )
        self.assertFalse(normalized.columns.duplicated().any())
        self.assertEqual(normalized.loc[0, "code"], "000001.XSHE")

    def test_research_source_does_not_request_pre_close_for_batched_prices(self) -> None:
        requested = []

        def get_price(*args, **kwargs):
            requested.extend(kwargs["fields"])
            self.assertNotIn("pre_close", kwargs["fields"])
            self.assertNotIn("paused", kwargs["fields"])
            self.assertNotIn("high_limit", kwargs["fields"])
            self.assertNotIn("low_limit", kwargs["fields"])
            self.assertNotIn("factor", kwargs["fields"])
            return pd.DataFrame([{
                "time": datetime(2025, 7, 1, 10, 0),
                "code": "000001.XSHE",
                "open": 10.0,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "volume": 1000,
                "money": 10100,
                "paused": 0,
                "high_limit": 11.0,
                "low_limit": 9.0,
                "factor": 1.0,
            }])

        source = exporter_module.JoinQuantResearchSource({
            "get_trade_days": lambda *args, **kwargs: [],
            "get_all_securities": lambda *args, **kwargs: None,
            "get_price": get_price,
            "get_extras": lambda *args, **kwargs: None,
        })
        frame = source.prices(
            ["000001.XSHE"],
            datetime(2025, 7, 1, 9, 30),
            datetime(2025, 7, 1, 15, 0),
            frequency="5m",
        )
        self.assertTrue(requested)
        self.assertNotIn("pre_close", frame.columns)
        self.assertNotIn("paused", frame.columns)

    def test_minute_status_fields_are_merged_from_same_day_daily_history(self) -> None:
        minute = pd.DataFrame([{
            "time": datetime(2025, 7, 1, 10, 0),
            "code": "000001.XSHE",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "volume": 1000,
            "money": 10100,
        }])
        daily = pd.DataFrame([{
            "time": datetime(2025, 7, 1),
            "code": "000001.XSHE",
            "open": 9.9,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 1000,
            "money": 10100,
            "paused": 0,
            "high_limit": 11.0,
            "low_limit": 9.0,
            "factor": 1.0,
        }])
        merged = exporter_module._add_minute_daily_status_fields(minute, daily)
        self.assertEqual(merged.loc[0, "paused"], 0)
        self.assertEqual(merged.loc[0, "high_limit"], 11.0)
        self.assertEqual(merged.loc[0, "low_limit"], 9.0)

    def test_decision_snapshots_advance_without_future_bars(self) -> None:
        frame = pd.DataFrame([
            {"time": datetime(2025, 7, 1, 9, 35), "code": "000001.XSHE", "close": 10.0},
            {"time": datetime(2025, 7, 1, 9, 40), "code": "000001.XSHE", "close": 10.2},
            {"time": datetime(2025, 7, 1, 9, 40), "code": "000002.XSHE", "close": 8.0},
        ])
        snapshots = list(exporter_module._iter_decision_snapshots(
            frame, ("09:35", "09:40")
        ))
        first = snapshots[0][1].set_index("code")
        second = snapshots[1][1].set_index("code")
        self.assertEqual(list(first.index), ["000001.XSHE"])
        self.assertEqual(first.loc["000001.XSHE", "close"], 10.0)
        self.assertEqual(second.loc["000001.XSHE", "close"], 10.2)
        self.assertEqual(second.loc["000002.XSHE", "close"], 8.0)

    def test_exporter_remains_python_36_notebook_compatible(self) -> None:
        source_path = Path(__file__).parents[1] / "joinquant_strict_history_exporter.py"
        source = source_path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(source_path), feature_version=(3, 6))
        self.assertNotIn("from dataclasses import", source)
        self.assertNotIn("from __future__ import annotations", source)
        self.assertNotIn("fromisoformat(", source)
        self.assertIn("ipykernel_launcher.py", source)
        notebook_globals = {
            "__name__": "__main__",
            "get_ipython": lambda: object(),
            "all": lambda value: value,
            "any": lambda value: value,
            "sum": lambda value: value,
        }
        with patch.object(sys, "argv", ["ipykernel_launcher.py"]):
            exec(compile(source, str(source_path), "exec"), notebook_globals)
        self.assertIn("export_month", notebook_globals)
        normalized = notebook_globals["normalize_candidate_price"](
            _price(),
            dataset_id="jq-strict-test-v1",
            adjustment_version="raw-v1",
        )
        self.assertEqual(normalized["close"], 10.1)
        self.assertIn("_builtins.sum", source)

    def test_candidate_identity_and_table_hash_match_project_contract(self) -> None:
        config = _config()
        row = build_candidate_rows(config, DECISION_AT, [_raw_candidate()])[0]
        sample = CandidateSample.from_values(
            source="strict_history",
            dataset_id=config.dataset_id,
            decision_at=DECISION_AT,
            code="000001",
            strategy_version=config.strategy_version,
            parameter_version=config.parameter_version,
            feature_schema_version=config.feature_schema_version,
            features={
                name: TimedFeature(value["value"], str(value["available_at"]))
                for name, value in _feature_values().items()
            },
            selected=True,
            rejection_stage="selected",
            rejection_code="",
            final_action="selected",
            universe_hash=str(row["universe_hash"]),
            market_data_version=config.market_data_version,
            code_hash=config.code_hash,
            generator_hash=config.generator_hash,
        )
        self.assertEqual(row["sample_id"], sample.sample_id)
        self.assertEqual(canonical_hash(row), contract_hash(sample))
        self.assertEqual(
            strict_table_hash("decision_candidates", [row]),
            imported_table_hash("decision_candidates", [row]),
        )

    def test_strict_candidate_rejects_missing_and_future_features(self) -> None:
        missing = _raw_candidate()
        del missing["features"]["news_score"]  # type: ignore[index]
        with self.assertRaisesRegex(StrictExportError, "MISSING_STRICT_ML_FEATURES"):
            build_candidate_rows(_config(), DECISION_AT, [missing])

        future = _raw_candidate()
        future["features"]["news_score"]["available_at"] = "2025-07-01T10:00:01+08:00"  # type: ignore[index]
        with self.assertRaisesRegex(StrictExportError, "FEATURE_FROM_FUTURE"):
            build_candidate_rows(_config(), DECISION_AT, [future])

        naive = _raw_candidate()
        naive["features"]["news_score"]["available_at"] = "2025-07-01T10:00:00"  # type: ignore[index]
        with self.assertRaisesRegex(StrictExportError, "TIMEZONE_AWARE_TIMESTAMP_REQUIRED"):
            build_candidate_rows(_config(), DECISION_AT, [naive])

    def test_generated_manifest_is_accepted_by_history_schema_v2(self) -> None:
        config = _config()
        candidates = build_candidate_rows(config, DECISION_AT, [_raw_candidate()])
        prices = [normalize_candidate_price(
            _price(),
            dataset_id=config.dataset_id,
            adjustment_version=config.adjustment_version,
        )]
        self.assertEqual(
            strict_table_hash("candidate_prices", prices),
            imported_table_hash("candidate_prices", prices),
        )
        manifest = build_strict_manifest(config, candidates, prices)
        with tempfile.TemporaryDirectory() as directory:
            store = HistoricalStore(Path(directory) / "history.db")
            store.initialize()
            self.assertEqual(store.import_candidate_cohorts(candidates, manifest=manifest), 1)
            self.assertEqual(store.import_candidate_prices(prices, manifest=manifest), 1)
            self.assertEqual(store.candidate_cohort(config.dataset_id, DECISION_AT)[0].sample_id, candidates[0]["sample_id"])


class _FakeJoinQuantSource:
    def trade_days(self, start: date, end: date) -> list[date]:
        result = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                result.append(current)
            current += timedelta(days=1)
        return result

    def universe(self, day: date) -> pd.DataFrame:
        return pd.DataFrame(
            {"display_name": ["测试股份"], "start_date": [date(2000, 1, 1)]},
            index=["000001.XSHE"],
        )

    def st_flags(self, codes: list[str], day: date) -> dict[str, bool]:
        return {code: False for code in codes}

    def prices(
        self,
        codes: list[str],
        start: datetime | date,
        end: datetime | date,
        *,
        frequency: str,
    ) -> pd.DataFrame:
        start_day = start.date() if isinstance(start, datetime) else start
        end_day = end.date() if isinstance(end, datetime) else end
        rows = []
        current = start_day
        ordinal = 0
        while current <= end_day:
            if current.weekday() < 5:
                for jq_code in codes:
                    price = 10.0 + ordinal * 0.01
                    rows.append({
                        "time": (
                            datetime.combine(current, time(10, 0))
                            if frequency == "5m"
                            else datetime.combine(current, time())
                        ),
                        "code": jq_code,
                        "open": price,
                        "high": price + 0.1,
                        "low": price - 0.1,
                        "close": price + 0.05,
                        "volume": 1000.0,
                        "money": 10000.0,
                        "paused": 0,
                        "high_limit": price * 1.1,
                        "low_limit": price * 0.9,
                        "factor": 1.0,
                    })
                ordinal += 1
            current += timedelta(days=1)
        return pd.DataFrame(rows)


class _MissingDerivedPriorCloseSource(_FakeJoinQuantSource):
    def prices(
        self,
        codes: list[str],
        start: datetime | date,
        end: datetime | date,
        *,
        frequency: str,
    ) -> pd.DataFrame:
        frame = super().prices(codes, start, end, frequency=frequency)
        if frequency == "daily" and not frame.empty:
            missing = frame["time"].map(lambda value: value.date() == date(2025, 6, 30))
            frame.loc[missing, "close"] = float("nan")
        return frame


class _ContextFeatureSource(_FakeJoinQuantSource):
    def industries(self, codes: list[str], day: date) -> dict[str, str]:
        return {str(code).replace(".XSHE", "").replace(".XSHG", ""): "银行" for code in codes}

    def valuations(self, codes: list[str], day: date) -> dict[str, dict[str, float]]:
        return {
            str(code).replace(".XSHE", "").replace(".XSHG", ""): {
                "market_cap": 1_000_000_000.0,
                "circulating_market_cap": 500_000_000.0,
            }
            for code in codes
        }


class JoinQuantStrictHistoryExporterIntegrationTest(unittest.TestCase):
    @staticmethod
    def _candidate_builder(context: DecisionContext) -> list[dict[str, object]]:
        return [_raw_candidate(context.decision_at)]

    @staticmethod
    def _daily_features(
        trade_date: str,
        code: str,
        _bar: dict[str, object],
    ) -> list[dict[str, object]]:
        available_at = f"{trade_date}T15:00:00+08:00"
        return [
            {
                "feature_name": name,
                "feature_value": (
                    "NORMAL" if name == "market_regime" else
                    "short" if name == "strategy_mode" else
                    "测试" if name in {"industry", "theme"} else
                    1.0
                ),
                "event_at": available_at,
                "available_at": available_at,
            }
            for name in REQUIRED_DAILY_FEATURES
        ]

    def test_builtin_daily_feature_builder_is_complete_and_point_in_time(self) -> None:
        dates = pd.date_range("2025-05-01", periods=35, freq="B")
        history = pd.DataFrame([
            {
                "time": value.to_pydatetime(),
                "code": "000001.XSHE",
                "open": 10.0 + index * 0.02,
                "high": 10.2 + index * 0.02,
                "low": 9.8 + index * 0.02,
                "close": 10.1 + index * 0.02,
                "volume": 100000.0,
                "money": 1000000.0,
                "factor": 1.0,
            }
            for index, value in enumerate(dates)
        ])
        trade_date = dates[-1].date().isoformat()
        bar = {
            "trade_date": trade_date,
            "code": "000001",
            "open": 10.7,
            "high": 10.9,
            "low": 10.6,
            "close": 10.8,
            "prev_close": 10.7,
            "volume": 100000.0,
            "amount": 1000000.0,
            "adjust_factor": 1.0,
        }
        rows = build_daily_feature_rows(
            trade_date,
            "000001",
            bar,
            {
                "trade_date": trade_date,
                "history": history,
                "market_frame": pd.DataFrame([{
                    "time": dates[-1].to_pydatetime(),
                    "code": "000001.XSHG",
                    "close": 3500.0,
                    "prev_close": 3490.0,
                }]),
                "today_frame": pd.DataFrame([{
                    "code": "000001.XSHE",
                    "pct_chg": 0.9,
                }]),
                "industry": "银行",
                "theme": "银行",
                "pct_rank": {"000001": 1.0},
                "amount_rank": {"000001": 1.0},
                "turnover": {"000001": 0.25},
            },
        )
        self.assertEqual(
            {row["feature_name"] for row in rows},
            set(REQUIRED_DAILY_FEATURES),
        )
        self.assertTrue(all(row["available_at"].startswith(trade_date) for row in rows))
        self.assertEqual(
            {row["feature_name"]: row["feature_value"] for row in rows}["industry"],
            "银行",
        )

    def test_month_export_wires_builtin_daily_builder_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(
                output_root=directory,
                require_daily_features=True,
                max_candidate_rows=10_000,
                max_candidate_price_rows=10_000,
            )
            result = export_month(
                config,
                candidate_builder=self._candidate_builder,
                daily_feature_builder=build_daily_feature_rows,
                source=_ContextFeatureSource(),  # type: ignore[arg-type]
            )
            self.assertTrue(result["complete_daily_features"])
            metadata = json.loads(
                (Path(result["output_dir"]) / "metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                metadata["daily_feature_builder_version"],
                exporter_module.DAILY_FEATURE_BUILDER_VERSION,
            )
            self.assertEqual(
                metadata["daily_feature_policy"]["valuation_date"],
                "prior_trade_date",
            )

    def test_month_export_is_hash_verified_and_idempotently_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(
                output_root=directory,
                require_daily_features=True,
                max_candidate_rows=10_000,
                max_candidate_price_rows=10_000,
            )
            source = _FakeJoinQuantSource()
            first = export_month(
                config,
                candidate_builder=self._candidate_builder,
                daily_feature_builder=self._daily_features,
                source=source,  # type: ignore[arg-type]
            )
            second = export_month(
                config,
                candidate_builder=self._candidate_builder,
                daily_feature_builder=self._daily_features,
                source=source,  # type: ignore[arg-type]
            )
            self.assertTrue(first["complete_daily_features"])
            self.assertEqual(first["sha256"], second["sha256"])
            verified = verify_strict_package(first["archive"])
            self.assertTrue(verified["accepted"])
            self.assertGreater(verified["candidate_rows"], 0)
            self.assertGreater(verified["candidate_price_rows"], 0)

    def test_daily_prev_close_skips_nan_and_uses_earlier_real_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(
                output_root=directory,
                require_daily_features=False,
                max_candidate_rows=10_000,
                max_candidate_price_rows=10_000,
            )
            result = export_month(
                config,
                candidate_builder=self._candidate_builder,
                source=_MissingDerivedPriorCloseSource(),  # type: ignore[arg-type]
            )

            output = Path(result["output_dir"])
            with (output / "bars.csv").open(encoding="utf-8", newline="") as handle:
                bars = list(csv.DictReader(handle))
            first = next(
                row for row in bars
                if row["trade_date"] == "2025-07-01" and row["code"] == "000001"
            )
            self.assertGreater(float(first["prev_close"]), 0)

            metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
            self.assertFalse(metadata["daily_features_required"])
            self.assertFalse(metadata["daily_features_complete"])

    def test_market_intraday_prev_close_uses_only_prior_daily_close(self) -> None:
        minute = pd.DataFrame([{
            "time": datetime(2025, 7, 1, 10, 0),
            "code": "000001.XSHG",
            "open": 3501.0,
            "high": 3520.0,
            "low": 3490.0,
            "close": 3510.0,
            "volume": 1.0,
            "money": 3510.0,
        }])
        daily = pd.DataFrame([
            {
                "time": datetime(2025, 6, 30), "code": "000001.XSHG",
                "close": 3500.0, "paused": 0, "high_limit": 4000.0,
                "low_limit": 3000.0,
            },
            {
                "time": datetime(2025, 7, 1), "code": "000001.XSHG",
                "close": 3600.0, "paused": 0, "high_limit": 4000.0,
                "low_limit": 3000.0,
            },
            {
                "time": datetime(2025, 7, 2), "code": "000001.XSHG",
                "close": 9999.0, "paused": 0, "high_limit": 9999.0,
                "low_limit": 1.0,
            },
        ])

        result = exporter_module._add_intraday_snapshot_fields(
            minute, daily, date(2025, 7, 1),
        )

        self.assertEqual(float(result.iloc[0]["prev_close"]), 3500.0)
        self.assertAlmostEqual(float(result.iloc[0]["pct_chg"]), 10.0 / 35.0)


if __name__ == "__main__":
    unittest.main()
