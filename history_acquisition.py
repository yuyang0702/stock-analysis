"""Bounded history acquisition helpers.

The authoritative point-in-time source for this project remains the JoinQuant
strict-history package.  This module adds a reproducible AkShare daily-bar
fallback for ``price_core`` research only.  It deliberately emits provenance
warnings and never claims that current stock status or current universe data is
point-in-time safe.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping

from historical_data import HistoricalStore
from data_source_registry import register_dataset
from market_data_provider import (
    BrokerHistoricalProvider,
    JQDataProvider,
    load_data_source_settings,
)


BAR_FIELDS = [
    "trade_date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "amount",
    "adjust_factor",
]
STATUS_FIELDS = [
    "trade_date",
    "code",
    "listed",
    "st",
    "suspended",
    "limit_up",
    "limit_down",
]
UNIVERSE_FIELDS = ["trade_date", "code"]


def discover_akshare_a_share_codes(fetcher: Callable[..., object] | None = None) -> list[str]:
    """Discover the current A-share code universe for bounded research.

    This is intentionally a *current* universe discovery helper.  It never
    claims historical membership; strict runs must still import a dated PIT
    universe from the strict-history provider.
    """
    if fetcher is None:
        import akshare as ak  # type: ignore

        fetcher = getattr(ak, "stock_info_a_code_name", None)
    if not callable(fetcher):
        raise RuntimeError("AKSHARE_A_SHARE_UNIVERSE_UNAVAILABLE")
    frame = fetcher()
    code_col = _column(frame, ("code", "代码", "证券代码", "股票代码"))
    values = sorted({str(value).strip().zfill(6) for value in frame[code_col].tolist() if str(value).strip()})
    if not values:
        raise RuntimeError("AKSHARE_A_SHARE_UNIVERSE_EMPTY")
    return values


@dataclass(frozen=True)
class AcquisitionConfig:
    dataset_id: str
    start: str
    end: str
    output_dir: Path
    adjust: str = ""
    sleep_seconds: float = 0.2

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValueError("dataset_id is required")
        start = date.fromisoformat(self.start)
        end = date.fromisoformat(self.end)
        if start > end:
            raise ValueError("start must not be after end")
        if not math.isfinite(float(self.sleep_seconds)) or self.sleep_seconds < 0:
            raise ValueError("sleep_seconds must be finite and non-negative")


def _column(frame: object, names: Iterable[str]) -> str:
    columns = {str(name): str(name) for name in getattr(frame, "columns", ())}
    for name in names:
        if name in columns:
            return columns[name]
    lowered = {key.strip().lower(): value for key, value in columns.items()}
    for name in names:
        if name.strip().lower() in lowered:
            return lowered[name.strip().lower()]
    raise ValueError(f"AKSHARE_COLUMN_MISSING:{','.join(names)}")


def _number(value: object, field: str) -> float:
    if value is None or str(value).strip() == "":
        raise ValueError(f"AKSHARE_VALUE_MISSING:{field}")
    result = float(str(value).replace(",", ""))
    if not math.isfinite(result):
        raise ValueError(f"AKSHARE_VALUE_INVALID:{field}")
    return result


def normalize_akshare_daily_frame(frame: object, code: str, start: str, end: str) -> list[dict[str, object]]:
    """Convert one AkShare daily frame to the project's canonical raw bars.

    AkShare's daily endpoint does not provide a stable ``prev_close`` column
    across all versions.  We therefore derive it only from the previous row in
    the same raw-price series.  If that evidence is unavailable, the row is
    rejected instead of being filled with the current close.
    """
    date_col = _column(frame, ("日期", "date", "trade_date"))
    open_col = _column(frame, ("开盘", "open"))
    high_col = _column(frame, ("最高", "high"))
    low_col = _column(frame, ("最低", "low"))
    close_col = _column(frame, ("收盘", "close"))
    volume_col = _column(frame, ("成交量", "volume"))
    amount_col = _column(frame, ("成交额", "amount"))
    rows: list[dict[str, object]] = []
    ordered = sorted(frame.to_dict("records"), key=lambda row: str(row[date_col]))
    previous_close: float | None = None
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    for raw in ordered:
        trade_date = date.fromisoformat(str(raw[date_col])[:10].replace("/", "-"))
        close = _number(raw[close_col], "close")
        if previous_close is None or previous_close <= 0:
            previous_close = close
            continue
        if trade_date < start_date or trade_date > end_date:
            previous_close = close
            continue
        row = {
            "trade_date": trade_date.isoformat(),
            "code": str(code).strip().zfill(6),
            "open": _number(raw[open_col], "open"),
            "high": _number(raw[high_col], "high"),
            "low": _number(raw[low_col], "low"),
            "close": close,
            "prev_close": previous_close,
            "volume": _number(raw[volume_col], "volume"),
            "amount": _number(raw[amount_col], "amount"),
            "adjust_factor": 1.0,
        }
        rows.append(row)
        previous_close = close
    return rows


def fetch_akshare_code(
    code: str,
    start: str,
    end: str,
    *,
    adjust: str = "",
    fetcher: Callable[..., object] | None = None,
    source_report: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Fetch one code with a bounded lookback needed for ``prev_close``."""
    if fetcher is None:
        import akshare as ak  # type: ignore

        try:
            frame = ak.stock_zh_a_hist(
                symbol=str(code).zfill(6),
                period="daily",
                start_date=(date.fromisoformat(start) - timedelta(days=30)).strftime("%Y%m%d"),
                end_date=date.fromisoformat(end).strftime("%Y%m%d"),
                adjust=adjust,
            )
        except Exception as primary_error:
            # Eastmoney is the preferred AkShare endpoint, but it is often
            # unavailable from CI or a restricted network.  Tencent's AkShare
            # endpoint provides the same bounded OHLC history, though its
            # sixth field is volume in lots rather than traded amount.
            # Preserve that limitation explicitly by converting lots to
            # shares and constructing a volume*close amount proxy.
            tx = getattr(ak, "stock_zh_a_hist_tx", None)
            if not callable(tx):
                raise primary_error
            normalized = str(code).zfill(6)
            market = "sh" if normalized.startswith(("5", "6", "688", "689")) else "sz"
            frame = tx(
                symbol=market + normalized,
                start_date=(date.fromisoformat(start) - timedelta(days=30)).strftime("%Y%m%d"),
                end_date=date.fromisoformat(end).strftime("%Y%m%d"),
                adjust=adjust,
            )
            if hasattr(frame, "copy"):
                frame = frame.copy()
                close_column = _column(frame, ("收盘", "close"))
                volume_column = _column(frame, ("成交量", "volume", "amount"))
                frame["volume"] = frame[volume_column]
                frame["amount"] = frame[volume_column] * 100 * frame[close_column]
            if source_report is not None:
                fallback_codes = source_report.setdefault("tx_fallback_codes", [])
                if normalized not in fallback_codes:
                    fallback_codes.append(normalized)
        return normalize_akshare_daily_frame(frame, code, start, end)
    lookback = date.fromisoformat(start) - timedelta(days=30)
    frame = fetcher(
        symbol=str(code).zfill(6),
        period="daily",
        start_date=lookback.strftime("%Y%m%d"),
        end_date=date.fromisoformat(end).strftime("%Y%m%d"),
        adjust=adjust,
    )
    return normalize_akshare_daily_frame(frame, code, start, end)


def _limit_rate(code: str) -> float:
    normalized = str(code).zfill(6)
    if normalized.startswith(("300", "301", "688")):
        return 0.20
    if normalized.startswith(("8", "4", "43")):
        return 0.30
    return 0.10


def _canonical_rows(rows: Iterable[Mapping[str, object]], fields: list[str]) -> list[dict[str, object]]:
    return [
        {field: row.get(field) for field in fields}
        for row in sorted(rows, key=lambda item: tuple(str(item.get(field, "")) for field in fields[:2]))
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv_atomic(path: Path, fields: list[str], rows: list[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def acquire_akshare_daily(
    config: AcquisitionConfig,
    codes: Iterable[str],
    *,
    fetcher: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Fetch bounded raw bars and write proxy market scaffolding.

    The generated ``status`` and ``universe`` tables are explicitly marked as
    proxies: AkShare's endpoint does not provide historical ST/suspension or
    point-in-time membership in this call.  The output is suitable for
    ``price_core`` only until those tables are replaced by a licensed PIT
    source.
    """
    normalized_codes = sorted(
        {
            (match.group(1) if (match := re.search(r"(?<!\d)(\d{6})(?!\d)", str(code))) else str(code).strip().zfill(6))
            for code in codes
            if str(code).strip()
        }
    )
    if not normalized_codes:
        raise ValueError("CODES_REQUIRED")
    bars: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    source_report: dict[str, object] = {"tx_fallback_codes": []}
    for index, code in enumerate(normalized_codes):
        try:
            bars.extend(fetch_akshare_code(
                code,
                config.start,
                config.end,
                adjust=config.adjust,
                fetcher=fetcher,
                source_report=source_report,
            ))
        except Exception as exc:
            failures.append({"code": code, "error": " ".join(str(exc).split())[:240]})
        if config.sleep_seconds and index + 1 < len(normalized_codes):
            time.sleep(config.sleep_seconds)
    bars = _canonical_rows(bars, BAR_FIELDS)
    status: list[dict[str, object]] = []
    universe: list[dict[str, object]] = []
    for row in bars:
        rate = _limit_rate(str(row["code"]))
        previous = float(row["prev_close"])
        status.append(
            {
                "trade_date": row["trade_date"],
                "code": row["code"],
                "listed": 1,
                "st": 0,
                "suspended": 0,
                "limit_up": round(previous * (1 + rate), 2),
                "limit_down": round(previous * (1 - rate), 2),
            }
        )
        universe.append({"trade_date": row["trade_date"], "code": row["code"]})
    status = _canonical_rows(status, STATUS_FIELDS)
    universe = _canonical_rows(universe, UNIVERSE_FIELDS)
    output_dir = Path(config.output_dir)
    _write_csv_atomic(output_dir / "bars.csv", BAR_FIELDS, bars)
    _write_csv_atomic(output_dir / "status.csv", STATUS_FIELDS, status)
    _write_csv_atomic(output_dir / "universe.csv", UNIVERSE_FIELDS, universe)
    metadata = {
        "dataset_id": config.dataset_id,
        "source": "akshare",
        "adjust": config.adjust or "raw",
        "start": config.start,
        "end": config.end,
        "codes_requested": len(normalized_codes),
        "codes_with_rows": len({str(row["code"]) for row in bars}),
        "rows": {"bars": len(bars), "status": len(status), "universe": len(universe)},
        "failures": failures,
        "strict_eligible": False,
        "proxy_only": True,
        "warnings": [
            "historical_st_status_unavailable",
            "historical_universe_membership_unavailable",
            "historical_limit_prices_approximated_by_board_code",
            "point_in_time_fundamental_features_not_collected",
        ],
        "sha256": {
            name: _sha256(output_dir / name)
            for name in ("bars.csv", "status.csv", "universe.csv")
        },
    }
    if source_report["tx_fallback_codes"]:
        metadata["warnings"].append("akshare_tencent_fallback_amount_is_volume_price_proxy")
        metadata["tx_fallback_codes"] = sorted(source_report["tx_fallback_codes"])
    temporary = output_dir / "acquisition_metadata.json.tmp"
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(output_dir / "acquisition_metadata.json")
    return metadata


def acquire_jqdata_daily(
    config: AcquisitionConfig,
    codes: Iterable[str],
    *,
    username: str,
    password: str,
    provider: JQDataProvider | None = None,
) -> dict[str, object]:
    """Fetch JQData daily bars into the same bounded canonical artifacts.

    JQData's local API is a better raw price source than the free AkShare
    fallback, but this collector intentionally remains ``proxy_only`` until a
    dated point-in-time universe/status/features package is exported and
    verified.  It is therefore safe to compare providers without weakening
    the strict quality gate.
    """
    client = provider or JQDataProvider(username, password)
    client.connect()
    normalized_codes = sorted(
        {
            client.plain_code(code)
            for code in codes
            if str(code).strip()
        }
    )
    if not normalized_codes:
        normalized_codes = client.discover_codes(as_of=config.end)
    bars: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for index, code in enumerate(normalized_codes):
        try:
            bars.extend(
                client.fetch_daily(
                    code,
                    config.start,
                    config.end,
                    adjust=config.adjust,
                )
            )
        except Exception as exc:
            failures.append({"code": code, "error": " ".join(str(exc).split())[:240]})
        if config.sleep_seconds and index + 1 < len(normalized_codes):
            time.sleep(config.sleep_seconds)
    start_date = date.fromisoformat(config.start)
    end_date = date.fromisoformat(config.end)
    bars = [
        row for row in bars
        if start_date <= date.fromisoformat(str(row["trade_date"])[:10]) <= end_date
    ]
    raw_bars = list(bars)
    bars = _canonical_rows(bars, BAR_FIELDS)
    status: list[dict[str, object]] = []
    universe: list[dict[str, object]] = []
    for row in raw_bars:
        previous = float(row["prev_close"])
        limit_up = float(row.get("limit_up") or 0.0)
        limit_down = float(row.get("limit_down") or 0.0)
        if limit_up <= 0:
            limit_up = round(previous * (1 + _limit_rate(str(row["code"]))), 2)
        if limit_down <= 0:
            limit_down = round(previous * (1 - _limit_rate(str(row["code"]))), 2)
        status.append(
            {
                "trade_date": row["trade_date"],
                "code": row["code"],
                "listed": 1,
                "st": 0,
                "suspended": int(row.get("suspended") or 0),
                "limit_up": limit_up,
                "limit_down": limit_down,
            }
        )
        universe.append({"trade_date": row["trade_date"], "code": row["code"]})
    status = _canonical_rows(status, STATUS_FIELDS)
    universe = _canonical_rows(universe, UNIVERSE_FIELDS)
    output_dir = Path(config.output_dir)
    _write_csv_atomic(output_dir / "bars.csv", BAR_FIELDS, bars)
    _write_csv_atomic(output_dir / "status.csv", STATUS_FIELDS, status)
    _write_csv_atomic(output_dir / "universe.csv", UNIVERSE_FIELDS, universe)
    metadata = {
        "dataset_id": config.dataset_id,
        "source": "jqdata",
        "adjust": config.adjust or "raw",
        "start": config.start,
        "end": config.end,
        "codes_requested": len(normalized_codes),
        "codes_with_rows": len({str(row["code"]) for row in bars}),
        "rows": {"bars": len(bars), "status": len(status), "universe": len(universe)},
        "failures": failures,
        "strict_eligible": False,
        "proxy_only": True,
        "warnings": [
            "historical_st_status_not_collected",
            "historical_universe_membership_not_proven",
            "point_in_time_features_not_collected",
            "use_joinquant_strict_export_for_strict_mode",
        ],
        "sha256": {
            name: _sha256(output_dir / name)
            for name in ("bars.csv", "status.csv", "universe.csv")
        },
    }
    temporary = output_dir / "acquisition_metadata.json.tmp"
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(output_dir / "acquisition_metadata.json")
    return metadata


def acquire_broker_historical(
    config: AcquisitionConfig,
    *,
    input_dir: Path | str,
    provider: BrokerHistoricalProvider | None = None,
) -> dict[str, object]:
    """Normalize an audited broker historical export without calling a broker.

    This is the future broker-history seam.  A concrete broker connector only
    needs to write canonical ``bars.csv`` (and, when available, status and
    universe files) into the input directory; no strategy code changes.
    """
    client = provider or BrokerHistoricalProvider(input_dir)
    client.connect()
    source = Path(input_dir)
    with (source / "bars.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        raw_codes = sorted({str(row.get("code") or "").strip() for row in csv.DictReader(handle) if row.get("code")})
    if not raw_codes:
        raise ValueError("BROKER_HISTORICAL_CODES_REQUIRED")
    bars: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for code in raw_codes:
        try:
            bars.extend(client.fetch_daily(code, config.start, config.end, adjust=config.adjust))
        except Exception as exc:
            failures.append({"code": code, "error": " ".join(str(exc).split())[:240]})
    bars = _canonical_rows(bars, BAR_FIELDS)
    status: list[dict[str, object]] = []
    universe: list[dict[str, object]] = []
    for row in bars:
        status.append({
            "trade_date": row["trade_date"], "code": row["code"], "listed": 1,
            "st": 0, "suspended": 0,
            "limit_up": round(float(row["prev_close"]) * (1 + _limit_rate(str(row["code"]))), 2),
            "limit_down": round(float(row["prev_close"]) * (1 - _limit_rate(str(row["code"]))), 2),
        })
        universe.append({"trade_date": row["trade_date"], "code": row["code"]})
    output_dir = Path(config.output_dir)
    _write_csv_atomic(output_dir / "bars.csv", BAR_FIELDS, bars)
    _write_csv_atomic(output_dir / "status.csv", STATUS_FIELDS, _canonical_rows(status, STATUS_FIELDS))
    _write_csv_atomic(output_dir / "universe.csv", UNIVERSE_FIELDS, _canonical_rows(universe, UNIVERSE_FIELDS))
    metadata = {
        "dataset_id": config.dataset_id,
        "source": "broker",
        "adjust": config.adjust or "raw",
        "start": config.start,
        "end": config.end,
        "codes_requested": len(raw_codes),
        "codes_with_rows": len({str(row["code"]) for row in bars}),
        "rows": {"bars": len(bars), "status": len(status), "universe": len(universe)},
        "failures": failures,
        "strict_eligible": False,
        "proxy_only": True,
        "warnings": [
            "broker_history_adapter_is_export_only",
            "historical_status_and_universe_must_be_supplied_by_broker",
            "point_in_time_features_not_collected",
        ],
        "sha256": {name: _sha256(output_dir / name) for name in ("bars.csv", "status.csv", "universe.csv")},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / "acquisition_metadata.json.tmp"
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(output_dir / "acquisition_metadata.json")
    return metadata


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Acquire bounded market data for price_core research")
    parser.add_argument(
        "--provider",
        choices=("akshare", "jqdata", "broker_historical"),
        default=None,
        help="override BACKTEST_DATA_PROVIDER from the env file",
    )
    parser.add_argument("--env-file", default="stock-analysis.env")
    parser.add_argument("--codes-file")
    parser.add_argument(
        "--all-a-shares",
        action="store_true",
        help="discover the current A-share universe; historical membership remains proxy-only",
    )
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--input-dir", help="canonical broker export directory for broker_historical")
    parser.add_argument("--db")
    parser.add_argument("--registry", default="cache/backtest/datasets.json")
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_data_source_settings(env_file=args.env_file, provider=args.provider)
    provider_name = settings.provider
    if args.all_a_shares:
        if provider_name == "akshare":
            codes = discover_akshare_a_share_codes()
        elif provider_name == "jqdata":
            jq_provider = JQDataProvider(settings.username, settings.password)
            jq_provider.connect()
            codes = jq_provider.discover_codes(as_of=args.end)
        else:
            codes = []
    elif args.codes_file:
        codes = [line.strip() for line in Path(args.codes_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        raise ValueError("CODES_FILE_OR_ALL_A_SHARES_REQUIRED")
    config = AcquisitionConfig(
        dataset_id=args.dataset,
        start=args.start,
        end=args.end,
        output_dir=Path(args.output_dir),
        sleep_seconds=args.sleep_seconds,
    )
    if provider_name == "akshare":
        metadata = acquire_akshare_daily(config, codes)
        source_name = "akshare_canonical"
    elif provider_name == "jqdata":
        metadata = acquire_jqdata_daily(
            config,
            codes,
            username=settings.username,
            password=settings.password,
        )
        source_name = "jqdata_canonical"
    else:
        if not args.input_dir:
            raise ValueError("BROKER_HISTORICAL_INPUT_DIR_REQUIRED")
        metadata = acquire_broker_historical(config, input_dir=args.input_dir)
        source_name = "broker_canonical"
    if args.db and not metadata["failures"] and metadata["rows"]["bars"] > 0:
        store = HistoricalStore(Path(args.db))
        store.initialize()
        for kind in ("bars", "status", "universe"):
            store.import_csv(
                config.dataset_id,
                kind,
                config.output_dir / f"{kind}.csv",
                source_name,
                config.adjust or "raw",
            )
    register_dataset(metadata, output_dir=config.output_dir, registry_path=args.registry)
    print(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not metadata["failures"] and metadata["rows"]["bars"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
