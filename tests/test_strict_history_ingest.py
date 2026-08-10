from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import joinquant_strict_history_exporter as exporter
from historical_data import HistoricalStore
from joinquant_strict_history_exporter import (
    REQUIRED_DAILY_FEATURES,
    REQUIRED_ML_FEATURES,
    REQUIRED_RULE_AUDIT_FEATURES,
    ExportConfig,
    build_candidate_rows,
    build_strict_manifest,
    normalize_candidate_price,
)
from strict_history_ingest import StrictHistoryIngestError, ingest_package


DECISION_AT = "2025-07-01T10:00:00+08:00"


def _features() -> dict[str, dict[str, object]]:
    values: dict[str, dict[str, object]] = {}
    for name in sorted(REQUIRED_ML_FEATURES | REQUIRED_RULE_AUDIT_FEATURES):
        if name == "market_regime":
            value: object = "NORMAL"
        elif name in {"industry", "theme"}:
            value = "测试"
        elif name == "strategy_mode":
            value = "short"
        elif name == "rule_selected":
            value = True
        elif name in {"rule_rejection_stage", "rule_rejection_code"}:
            value = ""
        elif name == "rule_final_action":
            value = "selected"
        elif name in {"rule_order", "rule_target_qty", "rule_slot_count"}:
            value = 1
        else:
            value = 1.0
        values[name] = {"value": value, "available_at": DECISION_AT}
    values["rule_target_qty"] = {"value": 100, "available_at": DECISION_AT}
    return values


def _build_package(
    root: Path,
    *,
    dataset_id: str = "strict-upload-test-v1",
    month: str = "2025-07",
    invalid_bar: bool = False,
    daily_features_complete: bool = True,
    daily_features_required: bool | None = None,
) -> Path:
    config = ExportConfig(
        dataset_id=dataset_id,
        month=month,
        strategy_version="strategy-v1",
        parameter_version="params-v1",
        feature_schema_version="features-v1",
        market_data_version="market-v1",
        code_hash="code-sha",
        generator_hash="generator-sha",
    )
    candidate = build_candidate_rows(
        config,
        DECISION_AT,
        [{
            "code": "000001",
            "features": _features(),
            "selected": True,
            "rejection_stage": "selected",
            "rejection_code": "",
            "final_action": "selected",
        }],
    )
    prices = [normalize_candidate_price(
        {
            "code": "000001",
            "bar_at": "2025-07-01T10:05:00+08:00",
            "available_at": "2025-07-01T10:05:00+08:00",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "volume": 1000,
            "amount": 10100,
            "paused": 0,
            "limit_up": 11.0,
            "limit_down": 9.0,
        },
        dataset_id=dataset_id,
        adjustment_version=config.adjustment_version,
    )]
    daily_features = [{
        "trade_date": "2025-07-01",
        "code": "000001",
        "feature_name": name,
        "feature_value": "NORMAL" if name == "market_regime" else "1",
        "event_at": "2025-07-01T15:00:00+08:00",
        "available_at": "2025-07-01T15:00:00+08:00",
    } for name in sorted(REQUIRED_DAILY_FEATURES)] if daily_features_complete else []
    bars = [{
        "trade_date": "2025-07-01",
        "code": "000001",
        "open": "bad" if invalid_bar else 10.0,
        "high": 10.2,
        "low": 9.9,
        "close": 10.1,
        "prev_close": 10.0,
        "volume": 1000,
        "amount": 10100,
        "adjust_factor": 1.0,
    }]
    status = [{
        "trade_date": "2025-07-01",
        "code": "000001",
        "listed": 1,
        "st": 0,
        "suspended": 0,
        "limit_up": 11.0,
        "limit_down": 9.0,
    }]
    universe = [{"trade_date": "2025-07-01", "code": "000001"}]
    manifest = build_strict_manifest(config, candidate, prices)
    package_dir = root / f"package-{month}-{int(invalid_bar)}"
    package_dir.mkdir(parents=True)
    metadata = {
        "dataset_id": dataset_id,
        "source": "joinquant_research",
        "strict_source": "strict_history",
        "month": month,
        "start": "2025-07-01",
        "end": "2025-07-01",
        "price_path_end": "2025-07-15",
        "trade_day_count": 1,
        "decision_times_per_day": 1,
        "strict": True,
        "daily_features_complete": daily_features_complete,
        "exporter_version": exporter.EXPORTER_VERSION,
        "candidate_builder_sha256": "builder-sha",
        "retention": {"unit": "one stable package per dataset month"},
    }
    if daily_features_required is not None:
        metadata["daily_features_required"] = daily_features_required
    staged_daily = {
        "bars.csv": package_dir / ".pending-bars.csv",
        "status.csv": package_dir / ".pending-status.csv",
        "universe.csv": package_dir / ".pending-universe.csv",
        "features.csv": package_dir / ".pending-features.csv",
    }
    exporter._atomic_csv(staged_daily["bars.csv"], exporter.DAILY_COLUMNS, bars)
    exporter._atomic_csv(staged_daily["status.csv"], exporter.STATUS_COLUMNS, status)
    exporter._atomic_csv(
        staged_daily["universe.csv"], exporter.UNIVERSE_COLUMNS, universe
    )
    exporter._atomic_csv(
        staged_daily["features.csv"],
        exporter.DAILY_FEATURE_COLUMNS,
        daily_features,
    )
    staged_jsonl = {
        "decision_candidates.jsonl": package_dir / ".pending-decision-candidates.jsonl",
        "candidate_prices.jsonl": package_dir / ".pending-candidate-prices.jsonl",
    }
    exporter._atomic_jsonl(staged_jsonl["decision_candidates.jsonl"], candidate)
    exporter._atomic_jsonl(staged_jsonl["candidate_prices.jsonl"], prices)
    files = exporter._write_month_files(
        package_dir,
        staged_daily_files=staged_daily,
        staged_daily_counts={
            "bars": len(bars),
            "status": len(status),
            "universe": len(universe),
            "features": len(daily_features),
        },
        staged_jsonl_files=staged_jsonl,
        staged_jsonl_counts={
            "decision_candidates": len(candidate),
            "candidate_prices": len(prices),
        },
        manifest=manifest,
        metadata_base=metadata,
    )
    archive = root / f"{dataset_id}-{month}-{int(invalid_bar)}.zip"
    exporter._write_zip_atomic(package_dir, archive, files, 50_000_000)
    return archive


class StrictHistoryIngestTest(unittest.TestCase):
    def test_candidate_history_package_explicitly_allows_header_only_daily_features(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            db = project / "cache" / "backtest" / "history.db"
            package = _build_package(
                root,
                daily_features_complete=False,
                daily_features_required=False,
            )

            result = ingest_package(
                package,
                db_path=db,
                archive_root=project / "cache" / "backtest" / "imports",
                backup_root=root / "backups" / "history",
                project_root=project,
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["dataset_counts"]["point_in_time_features"], 0)

    def test_legacy_incomplete_daily_features_remain_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            package = _build_package(root, daily_features_complete=False)

            with self.assertRaisesRegex(
                StrictHistoryIngestError, "COMPLETE_DAILY_FEATURES_REQUIRED"
            ):
                ingest_package(
                    package,
                    db_path=project / "cache" / "backtest" / "history.db",
                    archive_root=project / "cache" / "backtest" / "imports",
                    backup_root=root / "backups" / "history",
                    project_root=project,
                )

    def test_ingest_is_backed_up_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            db = project / "cache" / "backtest" / "history.db"
            package = _build_package(root)
            kwargs = {
                "db_path": db,
                "archive_root": project / "cache" / "backtest" / "imports",
                "backup_root": root / "backups" / "history",
                "project_root": project,
            }

            with patch.object(
                HistoricalStore,
                "import_candidate_cohorts",
                side_effect=AssertionError("non-streaming cohort import used"),
            ), patch.object(
                HistoricalStore,
                "import_candidate_prices",
                side_effect=AssertionError("non-streaming price import used"),
            ):
                first = ingest_package(package, **kwargs)
            second = ingest_package(package, **kwargs)

            self.assertEqual(first["status"], "success")
            self.assertEqual(first["integrity_check"], "ok")
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["dataset_hash"], second["dataset_hash"])
            self.assertTrue(Path(str(first["archive_file"])).is_file())
            self.assertTrue(Path(str(second["backup_file"])).is_file())
            self.assertEqual(
                HistoricalStore(db).dataset_counts("strict-upload-test-v1"),
                first["dataset_counts"],
            )

    def test_failed_import_does_not_change_live_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            db = project / "cache" / "backtest" / "history.db"
            store = HistoricalStore(db)
            store.initialize()
            before = store.dataset_hash("strict-upload-test-v1")
            package = _build_package(root, invalid_bar=True)

            with self.assertRaises(Exception):
                ingest_package(
                    package,
                    db_path=db,
                    archive_root=project / "cache" / "backtest" / "imports",
                    backup_root=root / "backups" / "history",
                    project_root=project,
                )

            self.assertEqual(store.dataset_hash("strict-upload-test-v1"), before)
            self.assertEqual(
                store.dataset_counts("strict-upload-test-v1")["daily_bars"], 0
            )

    def test_failed_first_import_does_not_create_live_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            db = project / "cache" / "backtest" / "history.db"
            package = _build_package(root, invalid_bar=True)

            with self.assertRaises(Exception):
                ingest_package(
                    package,
                    db_path=db,
                    archive_root=project / "cache" / "backtest" / "imports",
                    backup_root=root / "backups" / "history",
                    project_root=project,
                )

            self.assertFalse(db.exists())

    def test_archive_rejects_unsafe_or_extra_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            package = root / "unsafe.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("../metadata.json", json.dumps({}))

            with self.assertRaisesRegex(
                StrictHistoryIngestError, "FILE_SET|UNSAFE"
            ):
                ingest_package(
                    package,
                    db_path=project / "cache" / "backtest" / "history.db",
                    archive_root=project / "cache" / "backtest" / "imports",
                    backup_root=root / "backups" / "history",
                    project_root=project,
                )


if __name__ == "__main__":
    unittest.main()
