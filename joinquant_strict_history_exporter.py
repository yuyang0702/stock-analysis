"""JoinQuant Research exporter for strict point-in-time ML history.

This module is intentionally self-contained so it can be copied into a
JoinQuant Research notebook.  It has two responsibilities:

1. export raw daily/minute market evidence from JoinQuant with explicit
   timestamps and raw-price adjustment metadata;
2. turn a caller supplied *historical* candidate builder into the exact
   ``decision_candidates`` / ``candidate_prices`` contract accepted by
   :mod:`historical_data`.

The exporter does not invent historical news, theme or project state.  A full
strict run therefore requires ``candidate_builder`` to provide every feature
in :data:`REQUIRED_ML_FEATURES`, each with an ``available_at`` timestamp no
later than the decision time.  ``market_core_candidate_builder`` is provided
only for API/provenance probing and is rejected when ``strict=True``.

Minimal JoinQuant Research usage::

    from joinquant_strict_history_exporter import ExportConfig, export_month

    cfg = ExportConfig(
        dataset_id="jq-strict-20250701-20260731-v1",
        month="2025-07",
        output_root="jq_strict_exports",
        strategy_version="a-share-strategy-v1",
        parameter_version="risk-observe-v1:...",
        feature_schema_version="live-candidate-v1",
        market_data_version="joinquant-raw-5m-v1",
        code_hash="...",
        generator_hash="...",
    )
    result = export_month(cfg, candidate_builder=my_strict_candidate_builder)
    print(result["archive"])

The builder receives one :class:`DecisionContext` and returns mappings with
``code``, ``features``, ``selected``, ``rejection_stage``,
``rejection_code`` and ``final_action``.  Each feature must be a mapping like
``{"value": 12.3, "available_at": "2025-07-01T10:00:00+08:00"}``.
Strict mode requires every name in ``REQUIRED_ML_FEATURES``, plus
``rule_order`` and ``rule_slot_count`` for all candidates and a positive
``rule_target_qty`` for selected candidates.  ``features.csv`` is allowed to
remain header-only for the ML five-minute path; set
``require_daily_features=True`` and provide ``daily_feature_builder`` when the
same package must also satisfy the separate full-daily strict backtest gate.
"""

import csv
import argparse
import builtins as _builtins
import gc
import hashlib
import inspect
import json
import math
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence


EXPORTER_VERSION = "2026-10-02.1-daily-features"
DAILY_FEATURE_BUILDER_VERSION = "2026-10-02.1-daily-features"
STRICT_PRIOR_TRADE_DAYS = 90
STRICT_PIT_HISTORY_BUCKETS = 256
SHANGHAI_TZ = timezone(timedelta(hours=8))
STRICT_SOURCE = "strict_history"
ADJUSTMENT_VERSION = "joinquant-raw-plus-factor-v1"

# The generated one-file script constructs its replay engine before
# ``export_month`` has fetched JoinQuant's daily bars.  Keep the provider
# function stable in ``globals()`` and replace only this tiny descriptor once
# the monthly bars have been spilled.  The engine's copied namespace can then
# call the provider without retaining the 396k-row DataFrame.
_STRICT_PIT_HISTORY_STORE = None

try:
    from jqdata import apis as _JOINQUANT_APIS
except ImportError:
    _JOINQUANT_APIS = None

DAILY_COLUMNS = (
    "trade_date", "code", "open", "high", "low", "close", "prev_close",
    "volume", "amount", "adjust_factor",
)
STATUS_COLUMNS = (
    "trade_date", "code", "listed", "st", "suspended", "limit_up", "limit_down",
)
UNIVERSE_COLUMNS = ("trade_date", "code")
DAILY_FEATURE_COLUMNS = (
    "trade_date", "code", "feature_name", "feature_value", "event_at", "available_at",
)

# This is the current default allowlist in ml_maintenance.py.  A strict export
# must provide the whole frozen set; callers may add audit-only features.
REQUIRED_ML_FEATURES = frozenset({
    "price", "pct_chg", "amount", "turnover", "market_cap", "score",
    "final_score", "global_risk_score", "trade_score", "news_score",
    "risk_reward", "entry_price", "stop_loss", "take_profit", "pressure_pct",
    "pressure_label", "ma5", "ma10", "ma20", "ma30", "atr14", "theme_label",
    "theme_heat_level", "theme_heat_score", "market_state", "signal_state",
    "signal_age_days", "buy_state", "market_regime",
})

REQUIRED_DAILY_FEATURES = frozenset({
    "score", "news_score", "pct_chg", "turnover", "position_pct",
    "entry_price", "stop_loss", "take_profit", "atr14", "support_level",
    "strategy_mode", "market_regime", "industry", "theme",
})

REQUIRED_RULE_AUDIT_FEATURES = frozenset({
    "rule_order", "rule_slot_count",
})

FINAL_ACTIONS = frozenset({
    "selected", "score_rejected", "risk_rejected", "tradability_rejected",
    "execution_rejected", "buy_published", "rule_rejected", "sell_published",
    "sell_rejected_no_holding", "sell_blocked_disabled",
    "sell_blocked_kill_switch", "buy_blocked_disabled",
    "buy_blocked_kill_switch",
})
MARKET_REGIMES = frozenset({"NORMAL", "CAUTION", "RISK_OFF"})


class StrictExportError(ValueError):
    """Stable fail-closed exporter error."""


DEFAULT_DECISION_TIMES = (
    "09:35", "09:40", "09:45", "09:50", "09:55",
    "10:00", "10:05", "10:10", "10:15", "10:20", "10:25", "10:30",
    "10:35", "10:40", "10:45", "10:50", "10:55", "11:00", "11:05",
    "11:10", "11:15", "11:20", "11:25", "11:30", "13:05", "13:10",
    "13:15", "13:20", "13:25", "13:30", "13:35", "13:40", "13:45",
    "13:50", "13:55", "14:00", "14:05", "14:10", "14:15", "14:20",
    "14:25", "14:30", "14:35", "14:40", "14:45", "14:50", "14:55",
    "15:00",
)


class ExportConfig:
    """Python 3.6-compatible replacement for the original dataclass."""

    def __init__(
        self,
        dataset_id,
        month,
        output_root="jq_strict_exports",
        strategy_version="",
        parameter_version="",
        feature_schema_version="",
        market_data_version="",
        code_hash="",
        generator_hash="",
        adjustment_version=ADJUSTMENT_VERSION,
        decision_times=DEFAULT_DECISION_TIMES,
        strict=True,
        export_daily_core=True,
        require_daily_features=False,
        forward_trade_days=10,
        security_batch_size=300,
        max_candidate_rows=100_000,
        max_candidate_price_rows=2_000_000,
        max_archive_bytes=3_000_000_000,
        overwrite=True,
    ):
        self.dataset_id = dataset_id
        self.month = month
        self.output_root = output_root
        self.strategy_version = strategy_version
        self.parameter_version = parameter_version
        self.feature_schema_version = feature_schema_version
        self.market_data_version = market_data_version
        self.code_hash = code_hash
        self.generator_hash = generator_hash
        self.adjustment_version = adjustment_version
        self.decision_times = tuple(decision_times)
        self.strict = bool(strict)
        self.export_daily_core = bool(export_daily_core)
        self.require_daily_features = bool(require_daily_features)
        self.forward_trade_days = int(forward_trade_days)
        self.security_batch_size = int(security_batch_size)
        self.max_candidate_rows = int(max_candidate_rows)
        self.max_candidate_price_rows = int(max_candidate_price_rows)
        self.max_archive_bytes = int(max_archive_bytes)
        self.overwrite = bool(overwrite)

        required = {
            "dataset_id": self.dataset_id,
            "strategy_version": self.strategy_version,
            "parameter_version": self.parameter_version,
            "feature_schema_version": self.feature_schema_version,
            "market_data_version": self.market_data_version,
            "code_hash": self.code_hash,
            "generator_hash": self.generator_hash,
            "adjustment_version": self.adjustment_version,
        }
        missing = sorted(key for key, value in required.items() if not str(value).strip())
        if missing:
            raise StrictExportError("EXPORT_CONFIG_INCOMPLETE: " + ",".join(missing))
        _month_bounds(self.month)
        if not self.decision_times:
            raise StrictExportError("DECISION_TIMES_REQUIRED")
        normalized = tuple(_clock(value) for value in self.decision_times)
        if normalized != tuple(sorted(set(normalized))):
            raise StrictExportError("DECISION_TIMES_NOT_SORTED_UNIQUE")
        if not 1 <= int(self.security_batch_size) <= 1_000:
            raise StrictExportError("INVALID_SECURITY_BATCH_SIZE")
        if int(self.forward_trade_days) < 10:
            raise StrictExportError("D10_PRICE_PATH_REQUIRED")
        if min(
            int(self.max_candidate_rows),
            int(self.max_candidate_price_rows),
            int(self.max_archive_bytes),
        ) <= 0:
            raise StrictExportError("INVALID_EXPORT_CAPACITY")


class DecisionContext:
    """Point-in-time input passed to a caller-supplied candidate builder."""

    def __init__(
        self, dataset_id, decision_at, trade_date, snapshot, daily_history,
        universe_codes, market_snapshot=None, metadata=None,
    ):
        self.dataset_id = dataset_id
        self.decision_at = decision_at
        self.trade_date = trade_date
        self.snapshot = snapshot
        self.daily_history = daily_history
        self.universe_codes = tuple(universe_codes)
        self.market_snapshot = market_snapshot
        self.metadata = dict(metadata or {})


class ExportResult:
    """Simple result record that keeps the former dataclass API."""

    def __init__(
        self, dataset_id, month, output_dir, archive, trade_days,
        candidate_rows, candidate_price_rows, complete_daily_features, sha256,
    ):
        self.dataset_id = dataset_id
        self.month = month
        self.output_dir = output_dir
        self.archive = archive
        self.trade_days = trade_days
        self.candidate_rows = candidate_rows
        self.candidate_price_rows = candidate_price_rows
        self.complete_daily_features = complete_daily_features
        self.sha256 = sha256


class _DailyExportSpill:
    """Write daily-core rows without retaining monthly Python dictionaries."""

    def __init__(self, paths, export_daily_core):
        self.paths = dict(paths)
        self.export_daily_core = bool(export_daily_core)
        self.handles = {}
        self.writers = {}
        self.counts = {"bars": 0, "status": 0, "universe": 0, "features": 0}
        self.source_bar_count = 0
        self.complete_feature_bar_count = 0
        self.st_codes_by_day = {}
        specifications = (
            ("bars", "bars.csv", DAILY_COLUMNS, self.export_daily_core),
            ("status", "status.csv", STATUS_COLUMNS, self.export_daily_core),
            ("universe", "universe.csv", UNIVERSE_COLUMNS, self.export_daily_core),
            ("features", "features.csv", DAILY_FEATURE_COLUMNS, True),
        )
        try:
            for kind, name, columns, enabled in specifications:
                path = Path(self.paths[name])
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("w", encoding="utf-8", newline="")
                self.handles[kind] = handle
                writer = csv.DictWriter(
                    handle, fieldnames=list(columns), extrasaction="ignore"
                )
                writer.writeheader()
                self.writers[kind] = writer if enabled else None
        except Exception:
            self.close()
            raise

    def _write(self, kind, row):
        writer = self.writers.get(kind)
        if writer is None:
            return
        writer.writerow(row)
        self.counts[kind] += 1

    def write_universe(self, row):
        self._write("universe", row)

    def write_bar(self, row):
        self.source_bar_count += 1
        self._write("bars", row)

    def write_status(self, row):
        self._write("status", row)
        if bool(row.get("st")):
            self.st_codes_by_day.setdefault(str(row["trade_date"]), set()).add(
                str(row["code"])
            )

    def write_features(self, rows):
        names = set()
        for row in rows:
            self._write("features", row)
            names.add(str(row["feature_name"]))
        if REQUIRED_DAILY_FEATURES.issubset(names):
            self.complete_feature_bar_count += 1

    @property
    def complete_daily_features(self):
        return (
            self.source_bar_count > 0
            and self.complete_feature_bar_count == self.source_bar_count
        )

    def close(self):
        for handle in list(self.handles.values()):
            try:
                handle.close()
            except Exception:
                pass
        self.handles = {}


class _StrictJsonlSpill:
    """Write a sorted strict table and calculate its canonical hash online."""

    def __init__(self, kind, path):
        if kind not in ("decision_candidates", "candidate_prices"):
            raise StrictExportError("UNKNOWN_STRICT_TABLE: " + str(kind))
        self.kind = str(kind)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8", newline="\n")
        self.digest = hashlib.sha256()
        self.digest.update(b"[")
        self.count = 0
        self.previous_key = None
        self.closed = False
        self._table_hash = ""
        self.cohorts = {}
        self.candidate_codes = set()

    def write(self, raw):
        if self.closed:
            raise StrictExportError("STRICT_SPILL_ALREADY_CLOSED")
        if self.kind == "decision_candidates":
            normalized = _validate_candidate_row(raw)
            key = (str(normalized["decision_at"]), str(normalized["code"]))
            hash_item = canonical_hash(normalized)
            decision_at, code = key
            cohort = self.cohorts.get(decision_at)
            universe_hash = str(normalized["universe_hash"])
            if cohort is None:
                cohort = {"codes": [], "universe_hash": universe_hash}
                self.cohorts[decision_at] = cohort
            elif str(cohort["universe_hash"]) != universe_hash:
                raise StrictExportError("MIXED_COHORT_UNIVERSE_HASH")
            cohort["codes"].append(code)
            self.candidate_codes.add(code)
        else:
            normalized = normalize_candidate_price(
                raw,
                dataset_id=str(raw.get("dataset_id") or ""),
                adjustment_version=str(raw.get("adjustment_version") or ""),
            )
            key = (str(normalized["code"]), str(normalized["bar_at"]))
            hash_item = normalized
        if self.previous_key is not None and key <= self.previous_key:
            raise StrictExportError(
                "STRICT_TABLE_NOT_SORTED_UNIQUE: " + self.kind
            )
        if self.count:
            self.digest.update(b",")
        self.digest.update(_canonical_json(hash_item).encode("utf-8"))
        self.handle.write(_canonical_json(normalized) + "\n")
        self.previous_key = key
        self.count += 1

    def close(self):
        if self.closed:
            return
        self.digest.update(b"]")
        self._table_hash = self.digest.hexdigest()
        self.handle.close()
        self.closed = True

    @property
    def table_hash(self):
        if not self.closed:
            raise StrictExportError("STRICT_SPILL_NOT_CLOSED")
        if not self.count:
            raise StrictExportError("EMPTY_STRICT_TABLE: " + self.kind)
        return self._table_hash

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def canonical_hash(value):
    payload = _canonical_json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def candidate_sample_id(row):
    return canonical_hash({
        "source": row["source"],
        "dataset_id": row["dataset_id"],
        "trade_date": row["trade_date"],
        "decision_at": row["decision_at"],
        "code": row["code"],
        "strategy_version": row["strategy_version"],
        "parameter_version": row["parameter_version"],
        "feature_schema_version": row["feature_schema_version"],
    })


def build_candidate_rows(
    config,
    decision_at,
    raw_candidates,
):
    decision = _aware_iso(decision_at, "decision_at")
    trade_date = decision[:10]
    prepared = []
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            raise StrictExportError("INVALID_CANDIDATE_ROW")
        code = _code(raw.get("code"))
        raw_features = raw.get("features")
        if not isinstance(raw_features, Mapping):
            raise StrictExportError(f"FEATURES_NOT_MAPPING: {code}")
        features = {}
        for raw_name, raw_feature in raw_features.items():
            name = str(raw_name).strip()
            if not name:
                raise StrictExportError(f"EMPTY_FEATURE_NAME: {code}")
            if not isinstance(raw_feature, Mapping) or "available_at" not in raw_feature:
                raise StrictExportError(f"FEATURE_TIME_REQUIRED: {code}:{name}")
            available_at = _aware_iso(raw_feature["available_at"], f"{code}:{name}")
            if _aware_datetime(available_at) > _aware_datetime(decision):
                raise StrictExportError(f"FEATURE_FROM_FUTURE: {code}:{name}")
            value = _json_scalar(raw_feature.get("value"), f"{code}:{name}")
            features[name] = {"value": value, "available_at": available_at}

        if config.strict:
            missing = sorted(
                (REQUIRED_ML_FEATURES | REQUIRED_RULE_AUDIT_FEATURES).difference(features)
            )
            if missing:
                raise StrictExportError(
                    f"MISSING_STRICT_ML_FEATURES: {code}:" + ",".join(missing)
                )
        regime = str(features.get("market_regime", {}).get("value") or "").upper()
        if config.strict and regime not in MARKET_REGIMES:
            raise StrictExportError(f"INVALID_MARKET_REGIME: {code}:{regime}")

        selected = _strict_bool(raw.get("selected"), f"selected:{code}")
        stage = str(raw.get("rejection_stage") or "").strip()
        rejection_code = str(raw.get("rejection_code") or "").strip()
        final_action = str(raw.get("final_action") or "").strip()
        if final_action not in FINAL_ACTIONS:
            raise StrictExportError(f"UNKNOWN_FINAL_ACTION: {code}:{final_action}")
        valid_decision = (
            selected and stage == "selected" and not rejection_code
        ) or (
            not selected and bool(stage) and stage != "selected" and bool(rejection_code)
        )
        if not valid_decision:
            raise StrictExportError(f"CANDIDATE_DECISION_MISMATCH: {code}")
        if config.strict and selected and "rule_target_qty" not in features:
            raise StrictExportError(f"MISSING_RULE_TARGET_QTY: {code}")
        if config.strict:
            for audit_name in REQUIRED_RULE_AUDIT_FEATURES:
                _nonnegative_integer(features[audit_name]["value"], f"{code}:{audit_name}")
            if selected and _nonnegative_integer(
                features["rule_target_qty"]["value"], f"{code}:rule_target_qty"
            ) <= 0:
                raise StrictExportError(f"INVALID_RULE_TARGET_QTY: {code}")
        prepared.append({
            "sample_id": "",
            "source": STRICT_SOURCE,
            "dataset_id": config.dataset_id,
            "trade_date": trade_date,
            "decision_at": decision,
            "code": code,
            "strategy_version": config.strategy_version,
            "parameter_version": config.parameter_version,
            "feature_schema_version": config.feature_schema_version,
            "features": features,
            "selected": selected,
            "rejection_stage": stage,
            "rejection_code": rejection_code,
            "final_action": final_action,
            "universe_hash": "",
            "market_data_version": config.market_data_version,
            "code_hash": config.code_hash,
            "generator_hash": config.generator_hash,
        })

    prepared.sort(key=lambda row: str(row["code"]))
    codes = [str(row["code"]) for row in prepared]
    if not codes:
        raise StrictExportError(f"EMPTY_STRICT_COHORT: {decision}")
    if len(set(codes)) != len(codes):
        raise StrictExportError(f"DUPLICATE_COHORT_CODE: {decision}")
    universe_hash = canonical_hash(codes)
    for row in prepared:
        row["universe_hash"] = universe_hash
        row["sample_id"] = candidate_sample_id(row)
    return prepared


def normalize_candidate_price(
    value,
    *,
    dataset_id,
    adjustment_version,
):
    if not isinstance(value, Mapping):
        raise StrictExportError("INVALID_CANDIDATE_PRICE_ROW")
    bar_at = _aware_iso(value.get("bar_at"), "bar_at")
    available_at = _aware_iso(value.get("available_at"), "available_at")
    if _aware_datetime(available_at) < _aware_datetime(bar_at):
        raise StrictExportError("PRICE_AVAILABLE_BEFORE_BAR")
    paused = _zero_one(value.get("paused"), "paused")
    row = {
        "dataset_id": str(dataset_id),
        "code": _code(value.get("code")),
        "bar_at": bar_at,
        "available_at": available_at,
    }
    for name in ("open", "high", "low", "close", "volume", "amount", "limit_up", "limit_down"):
        row[name] = _optional_number(value.get(name), name)
    row["paused"] = paused
    row["adjustment_version"] = str(adjustment_version).strip()
    if not row["adjustment_version"]:
        raise StrictExportError("ADJUSTMENT_VERSION_REQUIRED")
    prices = [row[name] for name in ("open", "high", "low", "close")]
    if not paused and _builtins.any(item is None for item in prices + [row["volume"], row["amount"]]):
        raise StrictExportError("MISSING_TRADABLE_PRICE")
    if paused and _builtins.any(item is None for item in prices) and not _builtins.all(item is None for item in prices):
        raise StrictExportError("INCOMPLETE_PAUSED_OHLC")
    if _builtins.all(item is not None for item in prices):
        open_, high, low, close = (float(item) for item in prices)
        if (
            min(open_, high, low, close) <= 0
            or high < max(open_, low, close)
            or low > min(open_, high, close)
        ):
            raise StrictExportError("INVALID_PRICE_OHLC")
    return row


def strict_table_hash(kind, rows):
    if kind == "decision_candidates":
        normalized = [_validate_candidate_row(row) for row in rows]
        normalized.sort(key=lambda row: (str(row["decision_at"]), str(row["code"])))
        return canonical_hash([canonical_hash(row) for row in normalized])
    if kind == "candidate_prices":
        normalized = [
            normalize_candidate_price(
                row,
                dataset_id=str(row.get("dataset_id") or ""),
                adjustment_version=str(row.get("adjustment_version") or ""),
            )
            for row in rows
        ]
        normalized.sort(key=lambda row: (str(row["code"]), str(row["bar_at"])))
        return canonical_hash(normalized)
    raise StrictExportError(f"UNKNOWN_STRICT_TABLE: {kind}")


def build_strict_manifest(
    config,
    candidates,
    candidate_prices,
):
    normalized = [_validate_candidate_row(row) for row in candidates]
    normalized.sort(key=lambda row: (str(row["decision_at"]), str(row["code"])))
    if not normalized:
        raise StrictExportError("EMPTY_STRICT_IMPORT")
    cohorts = {}
    for decision_at in sorted({str(row["decision_at"]) for row in normalized}):
        rows = [row for row in normalized if str(row["decision_at"]) == decision_at]
        hashes = {str(row["universe_hash"]) for row in rows}
        if len(hashes) != 1:
            raise StrictExportError("MIXED_COHORT_UNIVERSE_HASH")
        cohorts[decision_at] = {
            "codes": [str(row["code"]) for row in rows],
            "universe_hash": hashes.pop(),
        }
    return {
        "dataset_id": config.dataset_id,
        "source": STRICT_SOURCE,
        "strategy_version": config.strategy_version,
        "parameter_version": config.parameter_version,
        "feature_schema_version": config.feature_schema_version,
        "market_data_version": config.market_data_version,
        "code_hash": config.code_hash,
        "generator_hash": config.generator_hash,
        "adjustment_version": config.adjustment_version,
        "cohorts": cohorts,
        "table_hashes": {
            "decision_candidates": strict_table_hash("decision_candidates", normalized),
            "candidate_prices": strict_table_hash("candidate_prices", candidate_prices),
        },
    }


def _build_streamed_strict_manifest(config, candidate_spill, price_spill):
    if not candidate_spill.cohorts:
        raise StrictExportError("EMPTY_STRICT_IMPORT")
    return {
        "dataset_id": config.dataset_id,
        "source": STRICT_SOURCE,
        "strategy_version": config.strategy_version,
        "parameter_version": config.parameter_version,
        "feature_schema_version": config.feature_schema_version,
        "market_data_version": config.market_data_version,
        "code_hash": config.code_hash,
        "generator_hash": config.generator_hash,
        "adjustment_version": config.adjustment_version,
        "cohorts": {
            decision_at: {
                "codes": list(candidate_spill.cohorts[decision_at]["codes"]),
                "universe_hash": str(
                    candidate_spill.cohorts[decision_at]["universe_hash"]
                ),
            }
            for decision_at in sorted(candidate_spill.cohorts)
        },
        "table_hashes": {
            "decision_candidates": candidate_spill.table_hash,
            "candidate_prices": price_spill.table_hash,
        },
    }


def verify_strict_package(path):
    root, cleanup = _package_root(Path(path))
    try:
        manifest = _read_json_object(root / "strict_manifest.json")
        candidate_hash, candidate_rows = _stream_strict_table_hash(
            "decision_candidates", root / "decision_candidates.jsonl"
        )
        price_hash, candidate_price_rows = _stream_strict_table_hash(
            "candidate_prices", root / "candidate_prices.jsonl"
        )
        table_hashes = manifest.get("table_hashes")
        if not isinstance(table_hashes, Mapping):
            raise StrictExportError("MANIFEST_TABLE_HASHES_REQUIRED")
        if str(table_hashes.get("decision_candidates") or "") != candidate_hash:
            raise StrictExportError("TABLE_HASH_MISMATCH: decision_candidates")
        if str(table_hashes.get("candidate_prices") or "") != price_hash:
            raise StrictExportError("TABLE_HASH_MISMATCH: candidate_prices")
        metadata = _read_json_object(root / "metadata.json")
        expected_files = metadata.get("sha256")
        if not isinstance(expected_files, Mapping):
            raise StrictExportError("METADATA_SHA256_REQUIRED")
        for name, expected in expected_files.items():
            target = root / str(name)
            if not target.is_file() or _sha256_file(target) != str(expected):
                raise StrictExportError(f"FILE_HASH_MISMATCH: {name}")
        return {
            "accepted": True,
            "dataset_id": manifest.get("dataset_id"),
            "candidate_rows": candidate_rows,
            "candidate_price_rows": candidate_price_rows,
            "table_hashes": dict(table_hashes),
        }
    finally:
        cleanup()


def _stream_strict_table_hash(kind, path):
    """Hash an already sorted JSONL table without retaining the full month."""
    digest = hashlib.sha256()
    digest.update(b"[")
    count = 0
    previous_key = None
    for raw in _read_jsonl(Path(path)):
        if kind == "decision_candidates":
            normalized = _validate_candidate_row(raw)
            key = (str(normalized["decision_at"]), str(normalized["code"]))
            item = canonical_hash(normalized)
        elif kind == "candidate_prices":
            normalized = normalize_candidate_price(
                raw,
                dataset_id=str(raw.get("dataset_id") or ""),
                adjustment_version=str(raw.get("adjustment_version") or ""),
            )
            key = (str(normalized["code"]), str(normalized["bar_at"]))
            item = normalized
        else:
            raise StrictExportError(f"UNKNOWN_STRICT_TABLE: {kind}")
        if previous_key is not None and key <= previous_key:
            raise StrictExportError(f"STRICT_TABLE_NOT_SORTED_UNIQUE: {kind}")
        if count:
            digest.update(b",")
        digest.update(_canonical_json(item).encode("utf-8"))
        previous_key = key
        count += 1
    digest.update(b"]")
    if not count:
        raise StrictExportError(f"EMPTY_STRICT_TABLE: {kind}")
    return digest.hexdigest(), count


class JoinQuantResearchSource:
    """Small adapter around functions injected by JoinQuant Research."""

    def __init__(self, namespace=None):
        self.namespace = dict(namespace or globals())
        required_names = (
            "get_trade_days", "get_all_securities", "get_price", "get_extras",
        )
        if _JOINQUANT_APIS is not None:
            for name in required_names:
                if not callable(self.namespace.get(name)):
                    candidate = getattr(_JOINQUANT_APIS, name, None)
                    if callable(candidate):
                        self.namespace[name] = candidate
        for name in required_names:
            if not callable(self.namespace.get(name)):
                raise StrictExportError(f"JOINQUANT_API_REQUIRED: {name}")

    def trade_days(self, start, end):
        values = self.namespace["get_trade_days"](start_date=start, end_date=end)
        return sorted({_date_value(value) for value in values})

    def universe(self, day):
        frame = self.namespace["get_all_securities"](types=["stock"], date=day)
        if frame is None or not hasattr(frame, "index"):
            raise StrictExportError(f"INVALID_JOINQUANT_UNIVERSE: {day}")
        frame.index = [str(value) for value in frame.index]
        return frame

    def st_flags(self, codes, day):
        if not codes:
            return {}
        frame = self.namespace["get_extras"](
            "is_st", list(codes), start_date=day, end_date=day, df=True
        )
        if frame is None or getattr(frame, "empty", True):
            raise StrictExportError(f"MISSING_ST_STATUS: {day}")
        row = frame.iloc[-1]
        return {str(code): bool(row.get(code, False)) for code in codes}

    def industries(self, codes, day):
        """Return date-scoped industry names for the requested securities."""
        getter = self.namespace.get("get_industry")
        if not callable(getter):
            try:
                from jqdata import apis
                getter = getattr(apis, "get_industry", None)
            except ImportError:
                getter = None
        if not callable(getter):
            raise StrictExportError("JOINQUANT_INDUSTRY_API_REQUIRED")
        result = {}
        for offset in range(0, len(codes), 500):
            batch = [_jq_code(code) for code in codes[offset:offset + 500]]
            values = getter(batch, date=day) or {}
            for jq_code in batch:
                details = values.get(jq_code) or {}
                chosen = (
                    details.get("sw_l1")
                    or details.get("jq_l1")
                    or details.get("zjw")
                    or {}
                )
                result[_code(jq_code)] = str(chosen.get("industry_name") or "").strip()
        missing = [str(code) for code in codes if not result.get(_code(code))]
        if missing:
            raise StrictExportError(
                "HISTORICAL_INDUSTRY_MISSING: " + ",".join(missing[:10])
            )
        return result

    def valuations(self, codes, day):
        """Return date-scoped market-cap evidence for turnover calculation."""
        try:
            from jqdata import apis
            get_fundamentals = self.namespace.get("get_fundamentals") or getattr(
                apis, "get_fundamentals", None
            )
            query = self.namespace.get("query") or getattr(apis, "query", None)
            valuation = self.namespace.get("valuation") or getattr(apis, "valuation", None)
        except ImportError:
            get_fundamentals = self.namespace.get("get_fundamentals")
            query = self.namespace.get("query")
            valuation = self.namespace.get("valuation")
        if not callable(get_fundamentals) or not callable(query) or valuation is None:
            raise StrictExportError("JOINQUANT_VALUATION_API_REQUIRED")
        result = {}
        for offset in range(0, len(codes), 500):
            batch = [_jq_code(code) for code in codes[offset:offset + 500]]
            request = query(
                valuation.code,
                valuation.market_cap,
                valuation.circulating_market_cap,
            ).filter(valuation.code.in_(batch))
            frame = get_fundamentals(request, date=day)
            if frame is None:
                continue
            for _, row in frame.iterrows():
                code = _code(row.get("code"))
                result[code] = {
                    "market_cap": _finite(row.get("market_cap"), "market_cap") * 100000000.0,
                    "circulating_market_cap": _finite(
                        row.get("circulating_market_cap"), "circulating_market_cap"
                    ) * 100000000.0,
                }
        missing = [str(code) for code in codes if _positive_finite_or_none(
            result.get(_code(code), {}).get("circulating_market_cap")
        ) is None]
        if missing:
            raise StrictExportError(
                "HISTORICAL_CIRCULATING_MARKET_CAP_MISSING: " + ",".join(missing[:10])
            )
        return result

    def prices(
        self,
        codes,
        start,
        end,
        *,
        frequency,
    ):
        fields = ["open", "high", "low", "close", "volume", "money"]
        if str(frequency).lower() in {"daily", "1d"}:
            fields.extend(["paused", "high_limit", "low_limit", "factor"])
        try:
            frame = self.namespace["get_price"](
                list(codes), start_date=start, end_date=end, frequency=frequency,
                fields=fields, skip_paused=False, fq=None, panel=False,
            )
        except TypeError:
            # Some Research runtimes removed ``panel`` while keeping the same
            # long DataFrame result for multi-security requests.
            frame = self.namespace["get_price"](
                list(codes), start_date=start, end_date=end, frequency=frequency,
                fields=fields, skip_paused=False, fq=None,
            )
        return _strict_normalize_price_frame(
            frame, codes, frequency=frequency
        )


def _strict_pit_history_bucket(value, bucket_count=STRICT_PIT_HISTORY_BUCKETS):
    """Return a stable, evenly distributed bucket for one A-share code."""
    clean = _code(value)
    if clean.isdigit():
        return int(clean) % int(bucket_count)
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % int(bucket_count)


def _spill_strict_pit_history(frame, root, last_trade_day):
    """Persist compact technical history without retaining the monthly frame."""
    global _STRICT_PIT_HISTORY_STORE
    root = Path(root)
    if root.exists():
        shutil.rmtree(str(root), ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    columns = [
        "time", "code", "open", "high", "low", "close", "volume", "money",
        "paused", "high_limit", "low_limit", "factor", "_derived_prev_close",
    ]
    working = frame.copy()
    optional_defaults = {
        "volume": float("nan"),
        "money": float("nan"),
        "paused": 0.0,
        "high_limit": float("nan"),
        "low_limit": float("nan"),
        "factor": 1.0,
    }
    for name, default in optional_defaults.items():
        if name not in working.columns:
            working[name] = default
    if "_derived_prev_close" not in working.columns:
        working.sort_values(["code", "time"], inplace=True)
        valid_close = working["close"].where(working["close"] > 0)
        valid_close = valid_close.replace(
            [float("inf"), float("-inf")], float("nan")
        )
        filled_close = valid_close.groupby(working["code"]).ffill()
        working["_derived_prev_close"] = filled_close.groupby(
            working["code"]
        ).shift(1)
        del valid_close
        del filled_close
    eligible = working["_trade_date"] <= last_trade_day
    buckets = working["code"].map(_strict_pit_history_bucket)
    paths = {}
    for raw_bucket in sorted(set(buckets.loc[eligible].tolist())):
        bucket = int(raw_bucket)
        path = root / ("bucket-%03d.csv" % bucket)
        payload = working.loc[
            eligible & (buckets == bucket), columns
        ].sort_values(["time", "code"])
        payload.to_csv(str(path), index=False, encoding="utf-8")
        paths[bucket] = path
        del payload
    del eligible
    del buckets
    del working
    _STRICT_PIT_HISTORY_STORE = {
        "bucket_count": STRICT_PIT_HISTORY_BUCKETS,
        "paths": paths,
    }
    _release_memory_pages()
    return root


def strict_pit_history_provider(codes, trade_date):
    """Load only requested codes and only bars before ``trade_date``.

    This provider is deliberately file-backed.  Re-querying JoinQuant daily
    history for each replay day leaves opaque platform cache pages in the 1 GB
    Research kernel.  The monthly daily query has already supplied identical
    raw bars and factors, so reusing that audited snapshot is both stricter and
    substantially more memory efficient.
    """
    store = _STRICT_PIT_HISTORY_STORE
    if not isinstance(store, Mapping):
        raise StrictExportError("STRICT_PIT_HISTORY_STORE_NOT_CONFIGURED")
    try:
        import pandas as pd
    except ImportError:
        raise StrictExportError("PANDAS_REQUIRED")
    wanted = set(_code(value) for value in codes)
    if not wanted:
        return pd.DataFrame(columns=(
            "time", "code", "open", "high", "low", "close", "volume", "money",
            "paused", "high_limit", "low_limit", "factor", "prev_close",
        ))
    cutoff = datetime.strptime(str(trade_date)[:10], "%Y-%m-%d").date()
    bucket_count = int(store["bucket_count"])
    wanted_by_bucket = {}
    for code in wanted:
        bucket = _strict_pit_history_bucket(code, bucket_count)
        wanted_by_bucket.setdefault(bucket, set()).add(code)
    frames = []
    paths = store.get("paths") or {}
    for bucket in sorted(wanted_by_bucket):
        path = paths.get(bucket)
        if path is None or not Path(path).exists():
            continue
        frame = pd.read_csv(str(path), dtype={"code": str})
        frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
        clean_codes = frame["code"].map(_code)
        mask = clean_codes.isin(wanted_by_bucket[bucket]) & (
            frame["time"].dt.date < cutoff
        )
        chosen = frame.loc[mask].copy()
        if not chosen.empty:
            frames.append(chosen)
        del clean_codes
        del mask
        del frame
    if not frames:
        return pd.DataFrame(columns=(
            "time", "code", "open", "high", "low", "close", "volume", "money",
            "paused", "high_limit", "low_limit", "factor", "prev_close",
        ))
    result = pd.concat(frames, ignore_index=True)
    del frames
    result.rename(columns={"_derived_prev_close": "prev_close"}, inplace=True)
    return result.sort_values(["time", "code"]).reset_index(drop=True)


def _clear_strict_pit_history_store(root=None):
    global _STRICT_PIT_HISTORY_STORE
    _STRICT_PIT_HISTORY_STORE = None
    if root is not None and Path(root).exists():
        shutil.rmtree(str(root), ignore_errors=True)
    _release_memory_pages()


def export_month(
    config,
    *,
    candidate_builder,
    daily_feature_builder=None,
    source=None,
):
    """Export one deterministic monthly package from JoinQuant Research.

    The implementation keeps one month bounded, writes stable filenames, and
    refuses to publish the final archive until its hashes round-trip locally.
    A failed rerun leaves the last valid archive untouched.
    """
    if not callable(candidate_builder):
        raise StrictExportError("STRICT_CANDIDATE_BUILDER_REQUIRED")
    if config.strict and candidate_builder is market_core_candidate_builder:
        raise StrictExportError("MARKET_CORE_IS_NOT_STRICT")
    if config.require_daily_features and not callable(daily_feature_builder):
        raise StrictExportError("STRICT_DAILY_FEATURE_BUILDER_REQUIRED")

    source = source or JoinQuantResearchSource(_caller_globals())
    month_start, month_end = _month_bounds(config.month)
    trade_days = source.trade_days(month_start, month_end)
    if not trade_days:
        raise StrictExportError("NO_TRADE_DAYS_IN_MONTH")
    prior_trade_days = source.trade_days(
        trade_days[0] - timedelta(days=240),
        trade_days[0] - timedelta(days=1),
    )
    if len(prior_trade_days) < STRICT_PRIOR_TRADE_DAYS:
        raise StrictExportError("PREVIOUS_TRADE_DAY_REQUIRED")
    # The multipath replay contract consumes at most 90 completed daily bars
    # for confirmed swing pivots.  The frame is immediately bucket-spilled so
    # the 1 GB Research kernel never retains per-code history copies.
    prior_trade_days = prior_trade_days[-STRICT_PRIOR_TRADE_DAYS:]
    horizon_days = source.trade_days(
        trade_days[-1] + timedelta(days=1),
        trade_days[-1] + timedelta(days=max(30, config.forward_trade_days * 3)),
    )
    if len(horizon_days) < config.forward_trade_days:
        raise StrictExportError("D10_PRICE_HORIZON_NOT_MATURE")
    price_end = horizon_days[config.forward_trade_days - 1]

    output_dir = Path(config.output_root) / config.dataset_id / config.month.replace("-", "")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir.parent / f"{config.dataset_id}-{config.month}.zip"
    if archive.exists() and not config.overwrite:
        raise StrictExportError(f"EXPORT_ALREADY_EXISTS: {archive}")

    universes = {}
    all_jq_codes = set()
    jq_code_by_code = {}
    print("STRICT_EXPORT_STAGE UNIVERSES START", flush=True)
    for day_number, day in enumerate(trade_days, start=1):
        frame = source.universe(day)
        allowed = [code for code in frame.index if _is_a_share_jq_code(code)]
        useful_columns = [
            name for name in ("display_name", "name", "start_date")
            if name in frame.columns
        ]
        frame = frame.loc[allowed, useful_columns].copy()
        gc.collect()
        if frame.empty:
            raise StrictExportError(f"EMPTY_A_SHARE_UNIVERSE: {day}")
        universe_path = output_dir / (
            ".pending-universe-context-" + day.strftime("%Y%m%d") + ".csv"
        )
        frame.to_csv(str(universe_path), encoding="utf-8")
        universes[day.isoformat()] = universe_path
        for raw_code in frame.index:
            jq_code = str(raw_code)
            code = _code(jq_code)
            existing = jq_code_by_code.get(code)
            if existing is not None and existing != jq_code:
                raise StrictExportError(f"AMBIGUOUS_JOINQUANT_CODE: {code}")
            jq_code_by_code[code] = jq_code
            all_jq_codes.add(jq_code)
        print(
            "STRICT_EXPORT_STAGE UNIVERSES {}/{} {} codes={}".format(
                day_number, len(trade_days), day.isoformat(), len(frame),
            ),
            flush=True,
        )
        del frame
        _release_memory_pages()

    print("STRICT_EXPORT_STAGE DAILY_PRICES START", flush=True)
    daily_frame = _fetch_batched_prices(
        source,
        sorted(all_jq_codes),
        prior_trade_days[0],
        price_end,
        frequency="daily",
        batch_size=config.security_batch_size,
    )
    print(
        "STRICT_EXPORT_STAGE DAILY_PRICES DONE rows={}".format(len(daily_frame)),
        flush=True,
    )
    del all_jq_codes
    _release_memory_pages()
    market_daily_frame = _fetch_batched_prices(
        source,
        ["000001.XSHG"],
        prior_trade_days[0],
        price_end,
        frequency="daily",
        batch_size=1,
    )
    if market_daily_frame.empty:
        raise StrictExportError("MISSING_MARKET_INDEX_DAILY_HISTORY")
    staged_daily_files = {
        "bars.csv": output_dir / ".pending-bars.csv",
        "status.csv": output_dir / ".pending-status.csv",
        "universe.csv": output_dir / ".pending-universe.csv",
        "features.csv": output_dir / ".pending-features.csv",
    }
    daily_spill = _DailyExportSpill(
        staged_daily_files, config.export_daily_core
    )
    print("STRICT_EXPORT_STAGE DAILY_EXPORTS START", flush=True)
    try:
        daily_rows, status_rows, universe_rows, daily_features, daily_core_audit = _daily_exports(
            config,
            trade_days,
            universes,
            daily_frame,
            source,
            daily_feature_builder,
            sink=daily_spill,
            market_frame=market_daily_frame,
        )
    finally:
        daily_spill.close()
    print(
        "STRICT_EXPORT_STAGE DAILY_EXPORTS DONE rows={} statuses={}".format(
            daily_spill.source_bar_count, daily_spill.counts["status"],
        ),
        flush=True,
    )
    daily_features_complete = daily_spill.complete_daily_features
    staged_daily_counts = dict(daily_spill.counts)
    st_codes_by_day = daily_spill.st_codes_by_day
    del daily_rows
    del status_rows
    del universe_rows
    del daily_features
    gc.collect()
    print(
        "STRICT_EXPORT_STAGE DAILY_EXPORTS SPILLED rows={}".format(
            _builtins.sum(staged_daily_counts.values())
        ),
        flush=True,
    )

    # The full 396k-row daily frame is only needed to produce the audited
    # daily tables.  Persist the small per-day snapshot fields used by minute
    # replay, plus a lightweight status table for the later candidate-price
    # path, then release the monthly frame before processing 5-minute bars.
    daily_contexts = {}
    context_columns = [
        "time", "code", "paused", "high_limit", "low_limit",
        "_derived_prev_close",
    ]
    for day in trade_days:
        day_key = day.isoformat()
        daily_context = daily_frame[
            daily_frame["_trade_date"] == day
        ].loc[:, context_columns].copy()
        daily_context_path = output_dir / (
            ".pending-daily-context-" + day.strftime("%Y%m%d") + ".csv"
        )
        daily_context.to_csv(
            str(daily_context_path), index=False, encoding="utf-8"
        )
        daily_contexts[day_key] = daily_context_path
        del daily_context
        _release_memory_pages()
    pit_history_root = _spill_strict_pit_history(
        daily_frame,
        output_dir / ".pending-pit-history",
        trade_days[-1],
    )
    print("STRICT_EXPORT_STAGE PIT_HISTORY SPILLED", flush=True)
    candidate_status_path = output_dir / ".pending-candidate-daily-status.csv"
    candidate_daily_status = daily_frame[
        (daily_frame["_trade_date"] >= trade_days[0])
        & (daily_frame["_trade_date"] <= price_end)
    ].loc[:, [
        "time", "code", "paused", "high_limit", "low_limit",
    ]].copy()
    candidate_daily_status.to_csv(
        str(candidate_status_path), index=False, encoding="utf-8"
    )
    del candidate_daily_status
    del daily_frame
    _release_memory_pages()
    print("STRICT_EXPORT_STAGE DAILY_CONTEXTS SPILLED", flush=True)

    staged_jsonl_files = {
        "decision_candidates.jsonl": output_dir / ".pending-decision-candidates.jsonl",
        "candidate_prices.jsonl": output_dir / ".pending-candidate-prices.jsonl",
    }
    candidate_spill = _StrictJsonlSpill(
        "decision_candidates", staged_jsonl_files["decision_candidates.jsonl"]
    )
    candidate_builder_sha256 = _callable_hash(candidate_builder)
    daily_feature_builder_sha256 = (
        _callable_hash(daily_feature_builder)
        if callable(daily_feature_builder) else ""
    )
    for day_number, day in enumerate(trade_days, start=1):
        day_key = day.isoformat()
        st_codes = st_codes_by_day.get(day_key, set())
        print(
            "STRICT_EXPORT_PROGRESS {}/{} {} START".format(
                day_number, len(trade_days), day_key,
            ),
            flush=True,
        )
        universe_frame = _load_universe_context(universes[day_key])
        jq_codes = sorted(str(code) for code in universe_frame.index)
        universe_codes = tuple(sorted(_code(code) for code in jq_codes))
        universe_code_set = set(universe_codes)
        if "display_name" in universe_frame.columns:
            name_series = universe_frame["display_name"]
        elif "name" in universe_frame.columns:
            name_series = universe_frame["name"]
        else:
            name_series = None
        names = (
            {
                str(index): str(value or "")
                for index, value in name_series.items()
            }
            if name_series is not None else {}
        )
        if "start_date" in universe_frame.columns:
            starts = {
                str(index): _date_value(value)
                for index, value in universe_frame["start_date"].items()
            }
        else:
            starts = {}
        snapshot_root = Path(tempfile.mkdtemp(prefix="jq-strict-snapshots-"))
        market_minute_frame = _fetch_batched_prices(
            source,
            ["000001.XSHG"],
            datetime.combine(day, time(9, 30)),
            datetime.combine(day, time(15, 0)),
            frequency="5m",
            batch_size=1,
        )
        market_minute_frame = _add_intraday_snapshot_fields(
            market_minute_frame, market_daily_frame, day
        )
        if market_minute_frame.empty:
            raise StrictExportError(f"MISSING_MARKET_INDEX_MINUTE_HISTORY: {day}")
        daily_context_frame = _load_daily_context(daily_contexts[day_key])
        history = None
        try:
            stock_snapshots = iter(_iter_spilled_decision_snapshots(
                source,
                jq_codes,
                day,
                daily_context_frame,
                config.decision_times,
                config.security_batch_size,
                snapshot_root,
            ))
            market_snapshots = iter(
                _iter_decision_snapshots(market_minute_frame, config.decision_times)
            )
            for clock_text in config.decision_times:
                decision = datetime.combine(day, _time_value(clock_text), tzinfo=SHANGHAI_TZ)
                stock_clock, snapshot = _builtins.next(stock_snapshots)
                market_clock, market_snapshot = _builtins.next(market_snapshots)
                if stock_clock != clock_text or market_clock != clock_text:
                    raise StrictExportError("DECISION_SNAPSHOT_SCHEDULE_MISMATCH")
                if snapshot.empty:
                    raise StrictExportError(f"MISSING_DECISION_SNAPSHOT: {decision.isoformat()}")
                if market_snapshot.empty:
                    raise StrictExportError(
                        f"MISSING_MARKET_INDEX_SNAPSHOT: {decision.isoformat()}"
                    )
                snapshot["is_st"] = snapshot["code"].map(
                    lambda value: _code(value) in st_codes
                )
                snapshot["delisting"] = False
                snapshot["name"] = snapshot["code"].map(names)
                if starts:
                    snapshot["listing_days"] = snapshot["code"].map(
                        lambda value: max(0, (day - starts.get(str(value), day)).days)
                    )
                else:
                    snapshot["listing_days"] = 0
                context = DecisionContext(
                    dataset_id=config.dataset_id,
                    decision_at=decision.isoformat(),
                    trade_date=day_key,
                    snapshot=snapshot,
                    daily_history=history,
                    universe_codes=universe_codes,
                    market_snapshot=market_snapshot,
                    metadata={
                        "exporter_version": EXPORTER_VERSION,
                        "builder_sha256": candidate_builder_sha256,
                    },
                )
                built = build_candidate_rows(
                    config, decision.isoformat(), candidate_builder(context)
                )
                outside = sorted(
                    str(row["code"])
                    for row in built
                    if str(row["code"]) not in universe_code_set
                )
                if outside:
                    raise StrictExportError(
                        "CANDIDATE_OUTSIDE_DAILY_UNIVERSE: " + ",".join(outside[:10])
                    )
                for row in built:
                    candidate_spill.write(row)
                    if candidate_spill.count > config.max_candidate_rows:
                        raise StrictExportError("CANDIDATE_ROW_LIMIT_EXCEEDED")
        finally:
            shutil.rmtree(str(snapshot_root), ignore_errors=True)
        # The final loop variables otherwise keep the full 5,200-row snapshot
        # and the historical frame alive until the next day.  Release them at
        # the day boundary and return free libc pages in JoinQuant's 1 GB
        # Python 3.6 kernel.
        del stock_snapshots
        del market_snapshots
        del snapshot
        del market_snapshot
        del context
        del built
        del history
        del market_minute_frame
        del daily_context_frame
        del universe_frame
        del universe_codes
        del universe_code_set
        del jq_codes
        del names
        del starts
        universe_context_path = Path(universes.pop(day_key))
        if universe_context_path.exists():
            universe_context_path.unlink()
        daily_context_path = Path(daily_contexts.pop(day_key))
        if daily_context_path.exists():
            daily_context_path.unlink()
        _release_memory_pages()
        print(
            "STRICT_EXPORT_PROGRESS {}/{} {} DONE candidates={}".format(
                day_number, len(trade_days), day_key, candidate_spill.count,
            ),
            flush=True,
        )

    _clear_strict_pit_history_store(pit_history_root)
    candidate_spill.close()
    jq_candidate_codes = [
        jq_code_by_code.get(code, _jq_code(code))
        for code in sorted(candidate_spill.candidate_codes)
    ]
    price_spill = _StrictJsonlSpill(
        "candidate_prices", staged_jsonl_files["candidate_prices.jsonl"]
    )
    candidate_daily_status = _load_daily_context(candidate_status_path)
    price_batch_size = min(50, int(config.security_batch_size))
    price_batch_count = int(math.ceil(
        len(jq_candidate_codes) / float(price_batch_size)
    )) if jq_candidate_codes else 0
    print(
        "STRICT_EXPORT_STAGE CANDIDATE_PRICES START codes={} batches={}".format(
            len(jq_candidate_codes), price_batch_count,
        ),
        flush=True,
    )
    for price_index in range(0, len(jq_candidate_codes), price_batch_size):
        price_codes = jq_candidate_codes[
            price_index:price_index + price_batch_size
        ]
        minute_prices = _fetch_batched_prices(
            source,
            price_codes,
            datetime.combine(trade_days[0], time(9, 30)),
            datetime.combine(price_end, time(15, 0)),
            frequency="5m",
            batch_size=price_batch_size,
        )
        minute_prices = _add_minute_daily_status_fields(
            minute_prices, candidate_daily_status
        )
        for raw in _candidate_price_rows(minute_prices):
            price_spill.write(normalize_candidate_price(
                raw,
                dataset_id=config.dataset_id,
                adjustment_version=config.adjustment_version,
            ))
            if price_spill.count > config.max_candidate_price_rows:
                raise StrictExportError("CANDIDATE_PRICE_ROW_LIMIT_EXCEEDED")
        del minute_prices
        _release_memory_pages()
        print(
            "STRICT_EXPORT_STAGE CANDIDATE_PRICES {}/{} rows={}".format(
                int(price_index / price_batch_size) + 1,
                price_batch_count,
                price_spill.count,
            ),
            flush=True,
        )
    price_spill.close()
    print(
        "STRICT_EXPORT_STAGE CANDIDATE_PRICES DONE rows={}".format(
            price_spill.count
        ),
        flush=True,
    )
    del candidate_daily_status
    if Path(candidate_status_path).exists():
        Path(candidate_status_path).unlink()
    _release_memory_pages()

    manifest = _build_streamed_strict_manifest(
        config, candidate_spill, price_spill
    )
    files = _write_month_files(
        output_dir,
        staged_daily_files=staged_daily_files,
        staged_daily_counts=staged_daily_counts,
        staged_jsonl_files=staged_jsonl_files,
        staged_jsonl_counts={
            "decision_candidates": candidate_spill.count,
            "candidate_prices": price_spill.count,
        },
        manifest=manifest,
        metadata_base={
            "dataset_id": config.dataset_id,
            "source": "joinquant_research",
            "strict_source": STRICT_SOURCE,
            "month": config.month,
            "start": trade_days[0].isoformat(),
            "end": trade_days[-1].isoformat(),
            "price_path_end": price_end.isoformat(),
            "trade_day_count": len(trade_days),
            "decision_times_per_day": len(config.decision_times),
            "strict": config.strict,
            "daily_features_required": config.require_daily_features,
            "daily_features_complete": daily_features_complete,
            "daily_core_audit": daily_core_audit,
            "exporter_version": EXPORTER_VERSION,
            "candidate_builder_sha256": candidate_builder_sha256,
            "daily_feature_builder_version": (
                DAILY_FEATURE_BUILDER_VERSION if daily_feature_builder is not None else ""
            ),
            "daily_feature_builder_sha256": daily_feature_builder_sha256,
            "daily_feature_policy": (
                {
                    "decision_timestamp": "trade_dateT15:00:00+08:00",
                    "news_score": "neutral_zero_without_date_scoped_news_feed",
                    "theme": "historical_industry_fallback_without_date_scoped_concept_feed",
                    "valuation_date": "prior_trade_date",
                }
                if daily_feature_builder is not None else {}
            ),
            "retention": {
                "unit": "one stable package per dataset month",
                "rerun": "atomic overwrite of the same monthly archive",
                "automatic_deletion": False,
                "history_import_limit_bytes": 3_000_000_000,
            },
        },
    )
    _write_zip_atomic(output_dir, archive, files, config.max_archive_bytes)
    verification = verify_strict_package(archive)
    result = ExportResult(
        dataset_id=config.dataset_id,
        month=config.month,
        output_dir=str(output_dir),
        archive=str(archive),
        trade_days=len(trade_days),
        candidate_rows=candidate_spill.count,
        candidate_price_rows=price_spill.count,
        complete_daily_features=daily_features_complete,
        sha256={name: _sha256_file(output_dir / name) for name in files},
    )
    payload = dict(result.__dict__)
    payload["verification"] = verification
    return payload


def market_core_candidate_builder(context):
    """Build a bounded price-only probe cohort; never valid for strict mode."""
    frame = context.snapshot.copy()
    frame = frame[(frame["paused"] == 0) & (frame["close"] > 0)]
    frame = frame[frame["pct_chg"] >= 4].copy()
    if frame.empty:
        return []
    frame["score"] = (
        frame["pct_chg"].rank(pct=True) * 60
        + frame["cum_amount"].rank(pct=True) * 40
    )
    frame = frame.sort_values(["score", "code"], ascending=[False, True]).head(30)
    decision_at = context.decision_at
    result = []
    for order, (_, row) in enumerate(frame.iterrows(), start=1):
        score = float(row["score"])
        selected = score >= 75 and order <= 5
        result.append({
            "code": row["code"],
            "features": {
                "price": {"value": float(row["close"]), "available_at": decision_at},
                "pct_chg": {"value": float(row["pct_chg"]), "available_at": decision_at},
                "amount": {"value": float(row["cum_amount"]), "available_at": decision_at},
                "score": {"value": score, "available_at": decision_at},
                "market_regime": {"value": "NORMAL", "available_at": decision_at},
                "training_eligible": {"value": False, "available_at": decision_at},
                "rule_order": {"value": order, "available_at": decision_at},
            },
            "selected": selected,
            "rejection_stage": "selected" if selected else "score",
            "rejection_code": "" if selected else "MARKET_CORE_PROBE_ONLY",
            "final_action": "selected" if selected else "score_rejected",
        })
    return result


def derive_callable_hash(function):
    """Public helper for freezing a notebook builder before export."""
    return _callable_hash(function)


def _daily_builder_accepts_context(builder):
    try:
        parameters = inspect.signature(builder).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = [item for item in parameters if item.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )]
    return any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters) or len(positional) >= 4


def _daily_timestamp(day_text):
    return str(day_text)[:10] + "T15:00:00+08:00"


def _daily_numeric_frame(history):
    try:
        import pandas as pd
    except ImportError as exc:
        raise StrictExportError("PANDAS_REQUIRED") from exc
    if history is None or getattr(history, "empty", True):
        raise StrictExportError("STRICT_DAILY_HISTORY_INSUFFICIENT")
    frame = history.copy().sort_values("time")
    for column in ("open", "high", "low", "close", "factor"):
        if column not in frame.columns:
            raise StrictExportError("STRICT_DAILY_HISTORY_FIELD_MISSING: " + column)
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close", "factor"])
    frame = frame[frame["factor"] > 0]
    if len(frame) < 30:
        raise StrictExportError("STRICT_DAILY_HISTORY_INSUFFICIENT")
    return frame


def _daily_technical_features(history, current_price):
    try:
        import pandas as pd
    except ImportError as exc:
        raise StrictExportError("PANDAS_REQUIRED") from exc
    frame = _daily_numeric_frame(history)
    current_factor = float(frame.iloc[-1]["factor"])
    for column in ("open", "high", "low", "close"):
        frame["adj_" + column] = frame[column] * frame["factor"] / current_factor
    closes = frame["adj_close"]
    previous = closes.shift(1)
    true_range = pd.concat([
        (frame["adj_high"] - frame["adj_low"]).abs(),
        (frame["adj_high"] - previous).abs(),
        (frame["adj_low"] - previous).abs(),
    ], axis=1).max(axis=1)
    ma5 = float(closes.tail(5).mean())
    ma10 = float(closes.tail(10).mean())
    ma20 = float(closes.tail(20).mean())
    ma30 = float(closes.tail(30).mean())
    atr14 = float(true_range.tail(14).mean())
    pressure = float(frame["adj_high"].tail(20).max())
    support_candidates = [
        float(frame["adj_low"].tail(10).min()), ma20, ma30,
    ]
    support_candidates = [
        value for value in support_candidates
        if value > 0 and value <= float(current_price) * 1.05
    ]
    support = max(support_candidates) if support_candidates else float(current_price) * 0.97
    if float(current_price) >= ma5 >= ma10 >= ma20 >= ma30:
        trend = "STRONG_UP"
    elif float(current_price) >= ma20 and ma5 >= ma10:
        trend = "RECOVERY"
    elif float(current_price) < ma20 and ma5 < ma10:
        trend = "WEAK"
    else:
        trend = "SIDEWAYS"
    return {
        "ma5": round(ma5, 6),
        "ma10": round(ma10, 6),
        "ma20": round(ma20, 6),
        "ma30": round(ma30, 6),
        "atr14": round(max(atr14, 0.0001), 6),
        "support_level": round(max(support, 0.01), 6),
        "pressure_level": round(max(pressure, 0.01), 6),
        "trend_state": trend,
        "breakout": bool(float(current_price) >= float(closes.tail(21).iloc[:-1].max()))
        if len(closes) >= 21 else False,
    }


def _daily_market_regime(context):
    try:
        import pandas as pd
    except ImportError as exc:
        raise StrictExportError("PANDAS_REQUIRED") from exc
    market = context.get("market_frame")
    day = _date_value(context["trade_date"])
    if market is None or getattr(market, "empty", True):
        return "NORMAL"
    rows = market.loc[market["time"].map(_date_value) <= day].copy()
    if rows.empty:
        return "NORMAL"
    row = rows.sort_values("time").iloc[-1]
    close = _positive_finite_or_none(row.get("close"))
    previous = _positive_finite_or_none(row.get("prev_close"))
    if previous is None:
        previous = _positive_finite_or_none(row.get("pre_close"))
    if previous is None:
        previous = _positive_finite_or_none(row.get("_derived_prev_close"))
    if close is None or previous is None:
        return "NORMAL"
    index_pct = (close / previous - 1.0) * 100.0
    snapshot = context.get("today_frame")
    if snapshot is None or getattr(snapshot, "empty", True):
        return "CAUTION" if index_pct <= 0 else "NORMAL"
    pct = snapshot.get("pct_chg")
    if pct is None:
        return "CAUTION" if index_pct <= 0 else "NORMAL"
    pct = pd.to_numeric(pct, errors="coerce").dropna()
    if pct.empty:
        return "CAUTION" if index_pct <= 0 else "NORMAL"
    up = int((pct > 0).sum())
    down = int((pct < 0).sum())
    median = float(pct.median())
    if index_pct > 0.8 and up > down and median >= 0:
        return "NORMAL"
    if index_pct <= -0.8:
        return "RISK_OFF"
    return "CAUTION"


def build_daily_feature_rows(trade_date, code, bar, context):
    """Build the full daily feature contract from data visible on trade_date.

    ``context`` is prepared by :func:`_daily_exports` and contains only bars
    whose dates are no later than ``trade_date``.  Current-day close data is
    considered available at the post-close decision timestamp; no future bar
    or current snapshot is consulted.
    """
    history = context.get("history")
    technical = _daily_technical_features(history, float(bar["close"]))
    previous = _positive_finite_or_none(bar.get("prev_close"))
    close = _finite(bar.get("close"), "close")
    if previous is None or close <= 0:
        raise StrictExportError("STRICT_DAILY_PREVIOUS_CLOSE_REQUIRED")
    pct_chg = (close / previous - 1.0) * 100.0
    amount = _finite(bar.get("amount"), "amount")
    pct_rank = float(context.get("pct_rank", {}).get(_code(code), 0.0))
    amount_rank = float(context.get("amount_rank", {}).get(_code(code), 0.0))
    score = 70.0 + 10.0 * float(
        technical["trend_state"] in ("STRONG_UP", "RECOVERY")
    ) + 8.0 * float(technical["breakout"]) + 5.0 * pct_rank + 2.0 * amount_rank
    mode = "short" if technical["breakout"] or pct_chg >= 5.0 else "mid"
    cap = 20.0 if mode == "short" else 15.0
    board = (
        "growth" if _code(code).startswith(("300", "301", "688"))
        else ("main_low" if technical["atr14"] / close <= 0.02 else "main_active")
    )
    atr_mult, max_loss = {
        "main_low": (1.8, 0.06),
        "main_active": (2.0, 0.07),
        "growth": (2.5, 0.09),
    }[board]
    entry = round(close, 6)
    stop_candidates = [
        technical["support_level"] * 0.99,
        entry - atr_mult * technical["atr14"],
        entry * (1.0 - max_loss),
    ]
    stop = round(min(max(min(stop_candidates), 0.01), entry - 0.01), 6)
    risk = max(entry - stop, 0.01)
    take = round(entry + risk * 2.0, 6)
    regime = _daily_market_regime(context)
    if regime == "RISK_OFF":
        cap = 0.0
    industry = str(context.get("industry") or "UNKNOWN").strip() or "UNKNOWN"
    # The live PIT policy currently uses industry as the deterministic theme
    # fallback when no date-scoped concept feed is available.
    theme = str(context.get("theme") or industry).strip() or "UNKNOWN"
    available_at = _daily_timestamp(trade_date)
    values = {
        "score": round(score, 6),
        "news_score": 0.0,
        "pct_chg": round(pct_chg, 6),
        "turnover": round(float(context.get("turnover", {}).get(_code(code), 0.0)), 6),
        "position_pct": round(cap, 6),
        "entry_price": entry,
        "stop_loss": stop,
        "take_profit": take,
        "atr14": technical["atr14"],
        "support_level": technical["support_level"],
        "strategy_mode": mode,
        "market_regime": regime,
        "industry": industry,
        "theme": theme,
    }
    return [
        {
            "feature_name": name,
            "feature_value": value,
            "event_at": available_at,
            "available_at": available_at,
        }
        for name, value in sorted(values.items())
    ]


def _daily_exports(
    config,
    trade_days,
    universes,
    frame,
    source,
    feature_builder,
    sink=None,
    market_frame=None,
):
    try:
        import pandas as pd
    except ImportError as exc:
        raise StrictExportError("PANDAS_REQUIRED") from exc
    bars = []
    statuses = []
    universe_rows = []
    feature_rows = []
    skip_counts = {}
    skip_examples = []
    builder_accepts_context = (
        feature_builder is not None and _daily_builder_accepts_context(feature_builder)
    )

    def record_skip(day_text, code, reason):
        skip_counts[reason] = int(skip_counts.get(reason, 0)) + 1
        if len(skip_examples) < 20:
            skip_examples.append({
                "trade_date": day_text,
                "code": code,
                "reason": reason,
            })
    # Prepare one vectorized previous-close column instead of retaining a
    # second full copy of the 100+ trading-day frame split into thousands of
    # per-code DataFrames.  The latter exceeds JoinQuant's 1 GB notebook cap.
    frame["_trade_date"] = frame["time"].map(_date_value)
    frame.sort_values(["code", "time"], inplace=True)
    valid_close = frame["close"].map(_positive_finite_or_none)
    filled_close = valid_close.groupby(frame["code"]).ffill()
    frame["_derived_prev_close"] = filled_close.groupby(frame["code"]).shift(1)
    del valid_close
    del filled_close
    _release_memory_pages()
    for day in trade_days:
        day_text = day.isoformat()
        universe_frame = _load_universe_context(universes[day_text])
        jq_codes = sorted(str(code) for code in universe_frame.index)
        st = source.st_flags(jq_codes, day)
        today_all = frame[frame["_trade_date"] == day].copy()
        today = today_all
        if not today.empty:
            today = today.groupby("code", as_index=False).tail(1).set_index("code")
        industries = {}
        valuations = {}
        pct_rank = {}
        amount_rank = {}
        turnover = {}
        if builder_accepts_context:
            if not callable(getattr(source, "industries", None)):
                raise StrictExportError("JOINQUANT_INDUSTRY_API_REQUIRED")
            if not callable(getattr(source, "valuations", None)):
                raise StrictExportError("JOINQUANT_VALUATION_API_REQUIRED")
            industries = source.industries([_code(code) for code in jq_codes], day)
            prior_days = sorted({
                _date_value(value) for value in frame.loc[
                    frame["_trade_date"] < day, "_trade_date"
                ].tolist()
            })
            if not prior_days:
                raise StrictExportError("PREVIOUS_TRADE_DAY_REQUIRED")
            valuations = source.valuations(
                [_code(code) for code in jq_codes], prior_days[-1]
            )
            if not today_all.empty:
                previous_column = (
                    "pre_close" if "pre_close" in today_all.columns
                    else (
                        "prev_close" if "prev_close" in today_all.columns
                        else "_derived_prev_close"
                    )
                )
                previous_values = pd.to_numeric(
                    today_all[previous_column].map(_positive_finite_or_none),
                    errors="coerce",
                )
                today_all["_pct_chg"] = (
                    pd.to_numeric(today_all["close"], errors="coerce")
                    / previous_values
                    - 1.0
                ) * 100.0
                today_all["_amount"] = today_all["money"].astype(float)
                pct_values = today_all.set_index("code")["_pct_chg"]
                amount_values = today_all.set_index("code")["_amount"]
                pct_rank = {
                    _code(code): float(value)
                    for code, value in pct_values.rank(pct=True).fillna(0.0).items()
                }
                amount_rank = {
                    _code(code): float(value)
                    for code, value in amount_values.rank(pct=True).fillna(0.0).items()
                }
                for raw_code, value in amount_values.items():
                    clean = _code(raw_code)
                    cap = _positive_finite_or_none(
                        valuations.get(clean, {}).get("circulating_market_cap")
                    )
                    turnover[clean] = (
                        float(value) / cap * 100.0 if cap is not None else 0.0
                    )
            market_for_day = market_frame
        else:
            market_for_day = None
        current = None
        bar = None
        status = None
        built_features = None
        for jq_code in jq_codes:
            code = _code(jq_code)
            universe_row = {"trade_date": day_text, "code": code}
            if sink is None:
                universe_rows.append(universe_row)
            else:
                sink.write_universe(universe_row)
            if today.empty or jq_code not in today.index:
                record_skip(day_text, code, "CURRENT_DAILY_BAR_MISSING")
                continue
            current = today.loc[jq_code]
            prev_close = _positive_finite_or_none(current.get("pre_close"))
            if prev_close is None:
                prev_close = _positive_finite_or_none(
                    current.get("_derived_prev_close")
                )
            if prev_close is None:
                record_skip(day_text, code, "PREVIOUS_CLOSE_UNAVAILABLE")
                continue
            try:
                bar = {
                    "trade_date": day_text,
                    "code": code,
                    "open": _finite(current["open"], "open"),
                    "high": _finite(current["high"], "high"),
                    "low": _finite(current["low"], "low"),
                    "close": _finite(current["close"], "close"),
                    "prev_close": prev_close,
                    "volume": _finite(current["volume"], "volume"),
                    "amount": _finite(current["money"], "amount"),
                    "adjust_factor": _finite(current["factor"], "adjust_factor"),
                }
                status = {
                    "trade_date": day_text,
                    "code": code,
                    "listed": True,
                    "st": bool(st.get(jq_code, False)),
                    "suspended": bool(_zero_one(current["paused"], "paused")),
                    "limit_up": _finite(current["high_limit"], "high_limit"),
                    "limit_down": _finite(current["low_limit"], "low_limit"),
                }
            except StrictExportError as exc:
                reason = str(exc)
                if not reason.startswith(("INVALID_NUMBER:", "BOOLEAN_REQUIRED:")):
                    raise
                record_skip(day_text, code, "INVALID_DAILY_EVIDENCE:" + reason)
                continue
            built_features = []
            if feature_builder is not None:
                context = {
                    "trade_date": day_text,
                    "code": code,
                    "bar": dict(bar),
                    "status": dict(status),
                    "history": frame[
                        (frame["code"].map(_code) == code)
                        & (frame["_trade_date"] <= day)
                    ].copy(),
                    "today_frame": today_all.copy(),
                    "market_frame": market_for_day,
                    "industry": industries.get(code, ""),
                    "theme": industries.get(code, ""),
                    "valuation": valuations.get(code, {}),
                    "pct_rank": pct_rank,
                    "amount_rank": amount_rank,
                    "turnover": turnover,
                }
                try:
                    built = list(
                        feature_builder(day_text, code, bar, context)
                        if builder_accepts_context
                        else feature_builder(day_text, code, bar)
                    )
                except StrictExportError as exc:
                    reason = str(exc)
                    if reason in (
                        "STRICT_DAILY_HISTORY_INSUFFICIENT",
                        "STRICT_DAILY_PREVIOUS_CLOSE_REQUIRED",
                    ):
                        record_skip(day_text, code, reason)
                        continue
                    raise
                for item in built:
                    row = dict(item)
                    row.setdefault("trade_date", day_text)
                    row.setdefault("code", code)
                    built_features.append(_daily_feature_row(row))
            built_features.sort(key=lambda row: (
                str(row["feature_name"]), str(row["available_at"]),
            ))
            if builder_accepts_context and not built_features:
                record_skip(day_text, code, "EMPTY_DAILY_FEATURES")
                continue
            if sink is None:
                bars.append(bar)
                statuses.append(status)
            else:
                sink.write_bar(bar)
                sink.write_status(status)
            if sink is None:
                feature_rows.extend(built_features)
            else:
                sink.write_features(built_features)
        del universe_frame
        del jq_codes
        del st
        del today
        del current
        del bar
        del status
        del built_features
        _release_memory_pages()
    bars.sort(key=lambda row: (str(row["trade_date"]), str(row["code"])))
    statuses.sort(key=lambda row: (str(row["trade_date"]), str(row["code"])))
    universe_rows.sort(key=lambda row: (str(row["trade_date"]), str(row["code"])))
    feature_rows.sort(key=lambda row: (
        str(row["trade_date"]), str(row["code"]), str(row["feature_name"]),
        str(row["available_at"]),
    ))
    if config.require_daily_features:
        complete = (
            _daily_feature_completeness(bars, feature_rows)
            if sink is None else sink.complete_daily_features
        )
        if not complete:
            raise StrictExportError("INCOMPLETE_DAILY_STRICT_FEATURES")
    audit = {
        "skipped_rows": sum(skip_counts.values()),
        "skip_reasons": {
            key: skip_counts[key] for key in sorted(skip_counts)
        },
        "skip_examples": skip_examples,
    }
    return bars, statuses, universe_rows, feature_rows, audit


def _daily_feature_row(value):
    row = {
        "trade_date": _date_value(value.get("trade_date")).isoformat(),
        "code": _code(value.get("code")),
        "feature_name": str(value.get("feature_name") or "").strip(),
        "feature_value": _json_scalar(value.get("feature_value"), "feature_value"),
        "event_at": _aware_iso(value.get("event_at"), "event_at"),
        "available_at": _aware_iso(value.get("available_at"), "available_at"),
    }
    if not row["feature_name"]:
        raise StrictExportError("DAILY_FEATURE_NAME_REQUIRED")
    if _aware_datetime(str(row["available_at"])).date() > _date_value(row["trade_date"]):
        raise StrictExportError("FUTURE_DAILY_FEATURE")
    return row


def _load_universe_context(path):
    try:
        import pandas as pd
    except ImportError as exc:
        raise StrictExportError("PANDAS_REQUIRED") from exc
    target = Path(path)
    if not target.is_file():
        raise StrictExportError("UNIVERSE_CONTEXT_MISSING: " + str(target))
    frame = pd.read_csv(str(target), index_col=0, encoding="utf-8")
    frame.index = frame.index.map(str)
    if frame.empty:
        raise StrictExportError("EMPTY_A_SHARE_UNIVERSE_CONTEXT")
    return frame


def _load_daily_context(path):
    try:
        import pandas as pd
    except ImportError as exc:
        raise StrictExportError("PANDAS_REQUIRED") from exc
    target = Path(path)
    if not target.is_file():
        raise StrictExportError("DAILY_CONTEXT_MISSING: " + str(target))
    frame = pd.read_csv(str(target), encoding="utf-8")
    if frame.empty:
        raise StrictExportError("EMPTY_DAILY_CONTEXT")
    frame["code"] = frame["code"].astype(str)
    return frame


def _daily_feature_completeness(
    bars,
    features,
):
    if not bars:
        return False
    available = {
        (str(row["trade_date"]), str(row["code"]), str(row["feature_name"]))
        for row in features
    }
    return _builtins.all(
        (str(bar["trade_date"]), str(bar["code"]), name) in available
        for bar in bars
        for name in REQUIRED_DAILY_FEATURES
    )


def _fetch_batched_prices(
    source,
    codes,
    start,
    end,
    *,
    frequency,
    batch_size,
):
    try:
        import pandas as pd
    except ImportError as exc:
        raise StrictExportError("PANDAS_REQUIRED") from exc
    frames = []
    for index in range(0, len(codes), int(batch_size)):
        batch = codes[index:index + int(batch_size)]
        frame = source.prices(batch, start, end, frequency=frequency)
        if frame is not None and not frame.empty:
            frames.append(frame)
    if not frames:
        columns = [
            "time", "code", "open", "high", "low", "close", "volume", "money",
        ]
        if str(frequency).lower() in {"daily", "1d"}:
            columns.extend(["paused", "high_limit", "low_limit", "factor"])
        return pd.DataFrame(columns=columns)
    combined = pd.concat(frames, ignore_index=True)
    del frames
    gc.collect()
    combined.sort_values(["time", "code"], inplace=True)
    return combined


def _strict_normalize_price_frame(frame, requested_codes, frequency="daily"):
    try:
        import pandas as pd
    except ImportError as exc:
        raise StrictExportError("PANDAS_REQUIRED") from exc
    if frame is None:
        return pd.DataFrame()
    if not hasattr(frame, "reset_index"):
        raise StrictExportError("INVALID_JOINQUANT_PRICE_FRAME")
    result = frame.reset_index().copy()
    rename = {
        "date": "time", "datetime": "time", "index": "time",
        "security": "code", "order_book_id": "code", "money": "money",
    }
    result.rename(
        columns={
            key: value
            for key, value in rename.items()
            if key in result.columns and (key == value or value not in result.columns)
        },
        inplace=True,
    )
    if "time" not in result.columns:
        datetime_columns = [
            column for column in result.columns
            if str(result[column].dtype).startswith("datetime")
        ]
        if len(datetime_columns) == 1:
            result.rename(columns={datetime_columns[0]: "time"}, inplace=True)
    if "code" not in result.columns and len(requested_codes) == 1:
        result["code"] = str(requested_codes[0])
    aliases = {"amount": "money", "limit_up": "high_limit", "limit_down": "low_limit"}
    result.rename(columns={key: value for key, value in aliases.items() if key in result.columns and value not in result.columns}, inplace=True)
    required = {
        "time", "code", "open", "high", "low", "close", "volume",
        "money",
    }
    if str(frequency).lower() in {"daily", "1d"}:
        required.update({"paused", "high_limit", "low_limit", "factor"})
    missing = sorted(required.difference(result.columns))
    if missing:
        raise StrictExportError("JOINQUANT_PRICE_FIELDS_MISSING: " + ",".join(missing))
    result = result.loc[:, sorted(required)].copy()
    result["code"] = result["code"].astype(str)
    return result.sort_values(["time", "code"]).reset_index(drop=True)


def _add_minute_daily_status_fields(minute, daily):
    """Attach historical daily status to base-only minute bars.

    JoinQuant Research rejects special status fields when they are mixed into
    a multi-row minute ``get_price`` request.  The same fields are supported by
    daily history, where they are constant for the trading day.  Merge only on
    the explicit historical ``(trade_date, code)`` key; never infer suspension
    from volume or use current platform state.
    """
    frame = minute
    if "time" not in frame.columns or "code" not in frame.columns:
        return frame
    if daily.empty:
        for field in ("paused", "high_limit", "low_limit"):
            frame[field] = None
        return frame
    frame["_trade_date"] = frame["time"].map(
        lambda value: _date_value(value).isoformat()
    )
    needed_codes = set(frame["code"].astype(str).unique())
    needed_dates = set(frame["_trade_date"].astype(str).unique())
    if "_trade_date" in daily.columns:
        daily_dates = daily["_trade_date"].map(lambda value: _date_value(value).isoformat())
    else:
        daily_dates = daily["time"].map(lambda value: _date_value(value).isoformat())
    daily_status = daily[
        daily["code"].astype(str).isin(needed_codes)
        & daily_dates.isin(needed_dates)
    ].loc[:, ["time", "code", "paused", "high_limit", "low_limit"]].copy()
    daily_status["_trade_date"] = daily_status["time"].map(
        lambda value: _date_value(value).isoformat()
    )
    daily_status = daily_status.sort_values(["_trade_date", "code", "time"])
    daily_status = daily_status.groupby(
        ["_trade_date", "code"], as_index=False
    ).tail(1)
    for field in ("paused", "high_limit", "low_limit"):
        if field in frame.columns:
            frame.drop(field, axis=1, inplace=True)
    keys = list(zip(frame["_trade_date"].astype(str), frame["code"].astype(str)))
    status_keys = list(zip(
        daily_status["_trade_date"].astype(str), daily_status["code"].astype(str),
    ))
    for field in ("paused", "high_limit", "low_limit"):
        values = dict(zip(status_keys, daily_status[field]))
        frame[field] = [values.get(key) for key in keys]
    frame.drop("_trade_date", axis=1, inplace=True)
    frame.sort_values(["time", "code"], inplace=True)
    frame.reset_index(drop=True, inplace=True)
    return frame


def _add_intraday_snapshot_fields(minute, daily, day):
    # This path handles an all-market minute day.  Never copy or merge the
    # entire month of daily history here: select the one historical day once
    # and map its constant status/previous-close fields onto the minute frame.
    frame = minute
    frame.sort_values(["code", "time"], inplace=True)
    frame["cum_amount"] = frame.groupby("code")["money"].cumsum()
    frame["cum_volume"] = frame.groupby("code")["volume"].cumsum()
    if "_trade_date" in daily.columns:
        current = daily[daily["_trade_date"] == day]
    else:
        current = daily[daily["time"].map(_date_value) == day]
    current = current.sort_values(["code", "time"])
    current = current.groupby("code", as_index=False).tail(1).set_index("code")
    for field in ("paused", "high_limit", "low_limit"):
        frame[field] = frame["code"].map(current[field].to_dict())
    prev_close_by_code = {}
    for code in frame["code"].astype(str).unique():
        direct = (
            _positive_finite_or_none(current.loc[code, "pre_close"])
            if code in current.index and "pre_close" in current.columns else None
        )
        fallback = (
            _positive_finite_or_none(current.loc[code, "_derived_prev_close"])
            if code in current.index and "_derived_prev_close" in current.columns else None
        )
        previous = direct if direct is not None else fallback
        if previous is None:
            # The dedicated market-index daily frame does not pass through
            # _daily_exports, so it has no _derived_prev_close column.  Recover
            # the last real close strictly before this trading day.  This is a
            # historical fallback only: the current and future rows are
            # explicitly excluded.
            prior = daily[
                (daily["code"].astype(str) == code)
                & (daily["time"].map(_date_value) < day)
            ]
            if not prior.empty:
                prior = prior.sort_values("time")
                valid_prior = prior["close"].map(_positive_finite_or_none).dropna()
                if not valid_prior.empty:
                    previous = float(valid_prior.iloc[-1])
        prev_close_by_code[code] = previous
    frame["prev_close"] = frame["code"].map(prev_close_by_code)
    frame["pct_chg"] = (
        (frame["close"] / frame["prev_close"] - 1.0) * 100.0
    )
    return frame


def _iter_decision_snapshots(frame, decision_times):
    """Yield latest per-code snapshots without rescanning the full day 48 times.

    JoinQuant's Python 3.6 pandas build attempts an expensive timezone-aware
    datetime cast when ``Series.map`` returns aware datetimes.  Represent the
    already historical bar time as an integer minute-of-day and advance the
    latest-per-code state monotonically.  A bar is incorporated only when its
    minute key is no later than the requested decision clock.
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise StrictExportError("PANDAS_REQUIRED") from exc
    def minute_key(value):
        timestamp = _joinquant_datetime(value)
        return timestamp.hour * 60 + timestamp.minute

    # The one-day all-market minute frame is the largest object in the
    # JoinQuant 1 GB notebook.  Mutate and sort it in place so snapshot
    # generation does not temporarily duplicate the full frame.
    ordered = frame
    ordered["_minute_key"] = ordered["time"].map(minute_key)
    ordered.sort_values(["_minute_key", "code", "time"], inplace=True)
    minute_values = ordered["_minute_key"].values
    consumed_rows = 0
    current = ordered.iloc[0:0].copy()
    last_clock_key = -1
    for clock_text in decision_times:
        clock = _time_value(clock_text)
        clock_key = clock.hour * 60 + clock.minute
        if clock_key <= last_clock_key:
            raise StrictExportError("DECISION_TIMES_NOT_STRICTLY_INCREASING")
        last_clock_key = clock_key
        visible_rows = int(minute_values.searchsorted(clock_key, side="right"))
        if visible_rows > consumed_rows:
            additions = ordered.iloc[consumed_rows:visible_rows]
            current = pd.concat([current, additions], ignore_index=True)
            current.drop_duplicates(subset=["code"], keep="last", inplace=True)
            consumed_rows = visible_rows
        snapshot = current.drop("_minute_key", axis=1).copy()
        yield str(clock_text), snapshot


def _iter_spilled_decision_snapshots(
    source,
    codes,
    day,
    daily_frame,
    decision_times,
    batch_size,
    snapshot_root,
):
    """Yield all-market snapshots while retaining only one security batch.

    Each API batch still fetches the same historical bars as the in-memory
    implementation.  Its 48 point-in-time cross-sections are appended to
    temporary per-clock CSV files, then one full cross-section is read back at
    a time.  This changes storage, not data or decision timing.
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise StrictExportError("PANDAS_REQUIRED") from exc
    root = Path(snapshot_root)
    root.mkdir(parents=True, exist_ok=True)
    paths = [root / ("snapshot-%02d.csv" % index) for index in range(len(decision_times))]
    start = datetime.combine(day, time(9, 30))
    end = datetime.combine(day, time(15, 0))
    for batch_start in range(0, len(codes), int(batch_size)):
        batch = codes[batch_start:batch_start + int(batch_size)]
        minute = _fetch_batched_prices(
            source,
            batch,
            start,
            end,
            frequency="5m",
            batch_size=len(batch),
        )
        if minute.empty:
            continue
        minute = _add_intraday_snapshot_fields(minute, daily_frame, day)
        for index, (clock_text, snapshot) in enumerate(
            _iter_decision_snapshots(minute, decision_times)
        ):
            if str(clock_text) != str(decision_times[index]):
                raise StrictExportError("DECISION_SNAPSHOT_SCHEDULE_MISMATCH")
            if snapshot.empty:
                continue
            snapshot.to_csv(
                str(paths[index]),
                mode="a",
                header=not paths[index].exists(),
                index=False,
                encoding="utf-8",
            )
        del minute
        gc.collect()
    for index, clock_text in enumerate(decision_times):
        path = paths[index]
        if not path.is_file():
            yield str(clock_text), pd.DataFrame()
            continue
        snapshot = pd.read_csv(str(path), encoding="utf-8")
        snapshot["time"] = pd.to_datetime(snapshot["time"])
        path.unlink()
        yield str(clock_text), snapshot


def _candidate_price_rows(frame):
    for _, row in frame.sort_values(["code", "time"]).iterrows():
        timestamp = _joinquant_datetime(row["time"]).isoformat()
        yield {
            "code": row["code"],
            "bar_at": timestamp,
            "available_at": timestamp,
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "amount": row["money"],
            "paused": row["paused"],
            "limit_up": row["high_limit"],
            "limit_down": row["low_limit"],
        }


def _write_month_files(
    output_dir,
    *,
    staged_daily_files,
    staged_daily_counts,
    staged_jsonl_files,
    staged_jsonl_counts,
    manifest,
    metadata_base,
):
    for name in ("bars.csv", "status.csv", "universe.csv", "features.csv"):
        staged = Path(staged_daily_files[name])
        if not staged.is_file():
            raise StrictExportError("STAGED_DAILY_FILE_MISSING: " + name)
        os.replace(str(staged), str(output_dir / name))
    for name in ("decision_candidates.jsonl", "candidate_prices.jsonl"):
        staged = Path(staged_jsonl_files[name])
        if not staged.is_file():
            raise StrictExportError("STAGED_JSONL_FILE_MISSING: " + name)
        os.replace(str(staged), str(output_dir / name))
    _atomic_json(output_dir / "strict_manifest.json", manifest)
    data_files = (
        "bars.csv", "status.csv", "universe.csv", "features.csv",
        "decision_candidates.jsonl", "candidate_prices.jsonl", "strict_manifest.json",
    )
    metadata = {
        **dict(metadata_base),
        "rows": {
            "bars": int(staged_daily_counts["bars"]),
            "status": int(staged_daily_counts["status"]),
            "universe": int(staged_daily_counts["universe"]),
            "features": int(staged_daily_counts["features"]),
            "decision_candidates": int(
                staged_jsonl_counts["decision_candidates"]
            ),
            "candidate_prices": int(staged_jsonl_counts["candidate_prices"]),
        },
        "sha256": {name: _sha256_file(output_dir / name) for name in data_files},
    }
    _atomic_json(output_dir / "metadata.json", metadata)
    return (*data_files, "metadata.json")


def _write_zip_atomic(
    source_dir,
    archive,
    files,
    max_bytes,
):
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix(archive.suffix + ".tmp")
    try:
        raw_bytes = _builtins.sum((source_dir / name).stat().st_size for name in files)
        if raw_bytes > int(max_bytes):
            raise StrictExportError("MONTHLY_UNCOMPRESSED_LIMIT_EXCEEDED")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as handle:
            for name in files:
                handle.write(source_dir / name, arcname=name)
        if temporary.stat().st_size > int(max_bytes):
            raise StrictExportError("MONTHLY_ARCHIVE_LIMIT_EXCEEDED")
        verify_strict_package(temporary)
        os.replace(str(temporary), str(archive))
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({name: row.get(name) for name in columns})
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(_canonical_json(row) + "\n")
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(
                _canonical_value(value), ensure_ascii=False, sort_keys=True,
                indent=2, allow_nan=False,
            ),
            encoding="utf-8",
        )
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_candidate_row(value):
    if not isinstance(value, Mapping):
        raise StrictExportError("INVALID_CANDIDATE_ROW")
    required = (
        "source", "dataset_id", "trade_date", "decision_at", "code",
        "strategy_version", "parameter_version", "feature_schema_version",
        "features", "selected", "rejection_stage", "rejection_code", "final_action",
        "universe_hash", "market_data_version", "code_hash", "generator_hash",
    )
    missing = [name for name in required if name not in value]
    if missing:
        raise StrictExportError("CANDIDATE_FIELDS_MISSING: " + ",".join(missing))
    row = {name: value[name] for name in ("sample_id", *required) if name in value}
    row["source"] = str(row["source"])
    if row["source"] != STRICT_SOURCE:
        raise StrictExportError("STRICT_HISTORY_SOURCE_REQUIRED")
    row["decision_at"] = _aware_iso(row["decision_at"], "decision_at")
    row["trade_date"] = _date_value(row["trade_date"]).isoformat()
    if row["trade_date"] != str(row["decision_at"])[:10]:
        raise StrictExportError("TRADE_DATE_MISMATCH")
    row["code"] = _code(row["code"])
    features = row["features"]
    if not isinstance(features, Mapping):
        raise StrictExportError("FEATURES_NOT_MAPPING")
    normalized_features = {}
    for name, raw in features.items():
        if not isinstance(raw, Mapping):
            raise StrictExportError(f"FEATURE_TIME_REQUIRED: {name}")
        available_at = _aware_iso(raw.get("available_at"), f"features.{name}.available_at")
        if _aware_datetime(available_at) > _aware_datetime(str(row["decision_at"])):
            raise StrictExportError(f"FEATURE_FROM_FUTURE: {name}")
        normalized_features[str(name)] = {
            "value": _json_scalar(raw.get("value"), str(name)),
            "available_at": available_at,
        }
    row["features"] = normalized_features
    row["selected"] = _strict_bool(row["selected"], "selected")
    for name in (
        "dataset_id", "strategy_version", "parameter_version", "feature_schema_version",
        "rejection_stage", "rejection_code", "final_action", "universe_hash",
        "market_data_version", "code_hash", "generator_hash",
    ):
        row[name] = str(row[name])
    if row["final_action"] not in FINAL_ACTIONS:
        raise StrictExportError(f"UNKNOWN_FINAL_ACTION: {row['final_action']}")
    valid_decision = (
        row["selected"]
        and row["rejection_stage"] == "selected"
        and not row["rejection_code"]
    ) or (
        not row["selected"]
        and bool(row["rejection_stage"])
        and row["rejection_stage"] != "selected"
        and bool(row["rejection_code"])
    )
    if not valid_decision:
        raise StrictExportError("CANDIDATE_DECISION_MISMATCH")
    expected = candidate_sample_id(row)
    supplied = str(row.get("sample_id") or "")
    if supplied and supplied != expected:
        raise StrictExportError("SAMPLE_ID_MISMATCH")
    row["sample_id"] = expected
    # Keep the field order identical to CandidateSample for human review; the
    # canonical hash itself sorts mapping keys.
    return {
        "sample_id": row["sample_id"],
        "source": row["source"],
        "dataset_id": row["dataset_id"],
        "trade_date": row["trade_date"],
        "decision_at": row["decision_at"],
        "code": row["code"],
        "strategy_version": row["strategy_version"],
        "parameter_version": row["parameter_version"],
        "feature_schema_version": row["feature_schema_version"],
        "features": row["features"],
        "selected": row["selected"],
        "rejection_stage": row["rejection_stage"],
        "rejection_code": row["rejection_code"],
        "final_action": row["final_action"],
        "universe_hash": row["universe_hash"],
        "market_data_version": row["market_data_version"],
        "code_hash": row["code_hash"],
        "generator_hash": row["generator_hash"],
    }


def _package_root(path):
    if path.is_dir():
        return path, lambda: None
    if not path.is_file() or not zipfile.is_zipfile(path):
        raise StrictExportError(f"STRICT_PACKAGE_NOT_FOUND: {path}")
    temporary = Path(tempfile.mkdtemp(prefix="strict-export-verify-"))
    with zipfile.ZipFile(path, "r") as handle:
        names = handle.namelist()
        if _builtins.any(
            Path(name).is_absolute() or ".." in Path(name).parts
            for name in names
        ):
            shutil.rmtree(temporary, ignore_errors=True)
            raise StrictExportError("UNSAFE_ARCHIVE_PATH")
        handle.extractall(temporary)
    return temporary, lambda: shutil.rmtree(temporary, ignore_errors=True)


def _read_json_object(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StrictExportError(f"INVALID_JSON_FILE: {path.name}") from exc
    if not isinstance(value, dict):
        raise StrictExportError(f"JSON_OBJECT_REQUIRED: {path.name}")
    return value


def _read_jsonl(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise StrictExportError(f"JSONL_OBJECT_REQUIRED: {path.name}:{line_number}")
                yield value
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StrictExportError(f"INVALID_JSONL_FILE: {path.name}") from exc


def _canonical_json(value):
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_value(value):
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    # Python 3 mapping views are not JSON serializable, and Python 3.6 exposes
    # them from several JoinQuant/pandas metadata paths.  Freeze them before
    # atomic JSON writes.  Values and keys are sorted canonically so package
    # hashes remain deterministic even when the source mapping order is not.
    if isinstance(value, (type({}.values()), type({}.keys()))):
        items = [_canonical_value(item) for item in value]
        return sorted(items, key=_canonical_json)
    if isinstance(value, type({}.items())):
        items = [
            [_canonical_value(key), _canonical_value(item)]
            for key, item in value
        ]
        return sorted(items, key=_canonical_json)
    if isinstance(value, (set, frozenset)):
        items = [_canonical_value(item) for item in value]
        return sorted(items, key=_canonical_json)
    if isinstance(value, datetime):
        return _aware_iso(value, "datetime")
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return _canonical_value(value.item())
        except Exception:
            pass
    return value


def _json_scalar(value, field_name):
    value = _canonical_value(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise StrictExportError(f"NON_FINITE_FEATURE: {field_name}")
    if isinstance(value, (str, int, float, bool, list, dict)) or value is None:
        # Round-trip now so unsupported notebook objects fail before files are
        # written or their hashes are declared.
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise StrictExportError(f"UNSUPPORTED_FEATURE_VALUE: {field_name}") from exc
        return value
    raise StrictExportError(f"UNSUPPORTED_FEATURE_VALUE: {field_name}")


def _aware_iso(value, field_name):
    try:
        parsed = _aware_datetime(value)
    except (TypeError, ValueError) as exc:
        raise StrictExportError(f"TIMEZONE_AWARE_TIMESTAMP_REQUIRED: {field_name}") from exc
    return parsed.isoformat()


def _aware_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = _parse_iso_datetime(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return parsed.astimezone(SHANGHAI_TZ)


def _joinquant_datetime(value):
    """Interpret JoinQuant's timezone-naive bar labels as Asia/Shanghai."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = _parse_iso_datetime(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ)


def _date_value(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _time_value(value):
    return datetime.strptime(_clock(value), "%H:%M").time()


def _clock(value):
    text = str(value).strip()
    parsed = None
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, pattern).time()
            break
        except ValueError:
            pass
    if parsed is None:
        raise StrictExportError(f"INVALID_DECISION_TIME: {value}")
    if parsed.second or parsed.microsecond:
        raise StrictExportError(f"INVALID_DECISION_TIME: {value}")
    return parsed.strftime("%H:%M")


def _parse_iso_datetime(value):
    """Parse the ISO timestamps used by the export contract on Python 3.6."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    offset = None
    if (
        len(text) >= 6
        and text[-6] in ("+", "-")
        and text[-3] == ":"
        and text[-5:-3].isdigit()
        and text[-2:].isdigit()
    ):
        sign = 1 if text[-6] == "+" else -1
        offset = timezone(sign * timedelta(
            hours=int(text[-5:-3]), minutes=int(text[-2:])
        ))
        text = text[:-6]
    elif (
        len(text) >= 5
        and text[-5] in ("+", "-")
        and text[-4:].isdigit()
    ):
        sign = 1 if text[-5] == "+" else -1
        offset = timezone(sign * timedelta(
            hours=int(text[-4:-2]), minutes=int(text[-2:])
        ))
        text = text[:-5]

    parsed = None
    for pattern in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(text, pattern)
            break
        except ValueError:
            pass
    if parsed is None:
        raise ValueError("invalid ISO datetime: " + str(value))
    if offset is not None:
        parsed = parsed.replace(tzinfo=offset)
    return parsed


def _month_bounds(value):
    try:
        start = datetime.strptime(str(value), "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise StrictExportError("MONTH_MUST_BE_YYYY_MM") from exc
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, next_month - timedelta(days=1)


def _code(value):
    digits = "".join(character for character in str(value or "") if character.isdigit()).zfill(6)
    if len(digits) != 6:
        raise StrictExportError(f"INVALID_STOCK_CODE: {value}")
    return digits


def _jq_code(value):
    code = _code(value)
    if code.startswith("6"):
        return code + ".XSHG"
    if code.startswith(("4", "8", "920")):
        return code + ".XBJG"
    return code + ".XSHE"


def _is_a_share_jq_code(value):
    text = str(value).upper()
    code = _code(text)
    if text.endswith(".XSHG"):
        return code.startswith("6")
    if text.endswith(".XSHE"):
        return code.startswith(("000", "001", "002", "003", "300", "301"))
    if text.endswith((".XBJG", ".XBJ")):
        return code.startswith(("4", "8", "920"))
    return False


def _strict_bool(value, field_name):
    if value is True or value == 1 or value == "1":
        return True
    if value is False or value == 0 or value == "0":
        return False
    raise StrictExportError(f"BOOLEAN_REQUIRED: {field_name}")


def _zero_one(value, field_name):
    return int(_strict_bool(value, field_name))


def _optional_number(value, field_name):
    if value is None or value == "":
        return None
    return _finite(value, field_name)


def _positive_finite_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _release_memory_pages():
    """Collect dead objects and, when available, return glibc pages to Linux."""
    gc.collect()
    if os.name != "posix":
        return
    try:
        import ctypes
        trim = getattr(ctypes.CDLL("libc.so.6"), "malloc_trim", None)
        if callable(trim):
            trim(0)
    except (ImportError, AttributeError, OSError, TypeError):
        pass


def _finite(value, field_name):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StrictExportError(f"INVALID_NUMBER: {field_name}") from exc
    if not math.isfinite(number):
        raise StrictExportError(f"INVALID_NUMBER: {field_name}")
    return number


def _nonnegative_integer(value, field_name):
    if isinstance(value, bool):
        raise StrictExportError(f"INVALID_NONNEGATIVE_INTEGER: {field_name}")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StrictExportError(f"INVALID_NONNEGATIVE_INTEGER: {field_name}") from exc
    try:
        if float(value) != number or number < 0:
            raise StrictExportError(f"INVALID_NONNEGATIVE_INTEGER: {field_name}")
    except (TypeError, ValueError, OverflowError) as exc:
        raise StrictExportError(f"INVALID_NONNEGATIVE_INTEGER: {field_name}") from exc
    return number


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _callable_hash(function):
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        identity = {
            "module": getattr(function, "__module__", ""),
            "qualname": getattr(function, "__qualname__", repr(function)),
            "exporter_version": EXPORTER_VERSION,
        }
        return canonical_hash(identity)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _caller_globals():
    frame = inspect.currentframe()
    try:
        if frame is None or frame.f_back is None or frame.f_back.f_back is None:
            return globals()
        return frame.f_back.f_back.f_globals
    finally:
        del frame


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify a JoinQuant strict-history monthly package"
    )
    subparsers = parser.add_subparsers(dest="command")
    verifier = subparsers.add_parser("verify")
    verifier.add_argument("package")
    subparsers.add_parser("required-features")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.command:
        parser.error("a command is required")
    if args.command == "required-features":
        print(json.dumps({
            "ml": sorted(REQUIRED_ML_FEATURES),
            "rule_audit": sorted(REQUIRED_RULE_AUDIT_FEATURES),
            "daily": sorted(REQUIRED_DAILY_FEATURES),
        }, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(
        verify_strict_package(args.package),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ))
    return 0


__all__ = [
    "ADJUSTMENT_VERSION",
    "DAILY_FEATURE_BUILDER_VERSION",
    "DecisionContext",
    "EXPORTER_VERSION",
    "ExportConfig",
    "ExportResult",
    "JoinQuantResearchSource",
    "REQUIRED_DAILY_FEATURES",
    "REQUIRED_ML_FEATURES",
    "REQUIRED_RULE_AUDIT_FEATURES",
    "StrictExportError",
    "build_candidate_rows",
    "build_daily_feature_rows",
    "build_strict_manifest",
    "candidate_sample_id",
    "canonical_hash",
    "derive_callable_hash",
    "export_month",
    "market_core_candidate_builder",
    "main",
    "normalize_candidate_price",
    "strict_table_hash",
    "verify_strict_package",
]


if (
    __name__ == "__main__"
    and not _builtins.str(sys.argv[0]).endswith("ipykernel_launcher.py")
):
    raise SystemExit(main())
