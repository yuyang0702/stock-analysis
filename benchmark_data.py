"""Independent index benchmark loading and bounded AkShare collection.

Benchmarks are kept outside the stock universe so excess-return research never
uses the strategy's own candidate set as a market proxy.  The resulting CSV is
research evidence; callers still need to record its provider and hash.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Mapping


BENCHMARK_FIELDS = ["trade_date", "benchmark", "open", "high", "low", "close", "volume"]

BENCHMARK_SYMBOLS = {
    "000300.XSHG": "sh000300",
    "000905.XSHG": "sh000905",
    "000852.XSHG": "sh000852",
    "399905.XSHE": "sz399905",
    "399006.XSHE": "sz399006",
}


def _field(frame: object, *names: str) -> str:
    columns = {str(value).strip().lower(): str(value) for value in getattr(frame, "columns", ())}
    for name in names:
        if name.strip().lower() in columns:
            return columns[name.strip().lower()]
    raise ValueError(f"benchmark field missing: {', '.join(names)}")


def normalize_benchmark_frame(
    frame: object,
    benchmark: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, object]]:
    """Normalize AkShare/pandas-like index rows to the canonical schema."""
    trade = _field(frame, "trade_date", "date", "日期")
    close = _field(frame, "close", "收盘")
    open_ = _field(frame, "open", "开盘")
    high = _field(frame, "high", "最高")
    low = _field(frame, "low", "最低")
    volume = _field(frame, "volume", "成交量")
    rows: list[dict[str, object]] = []
    for raw in frame.to_dict("records"):
        trade_date = str(raw.get(trade) or "")[:10]
        if not trade_date:
            continue
        if start and trade_date < str(start)[:10]:
            continue
        if end and trade_date > str(end)[:10]:
            continue
        try:
            values = {
                "open": float(raw.get(open_) or 0),
                "high": float(raw.get(high) or 0),
                "low": float(raw.get(low) or 0),
                "close": float(raw.get(close) or 0),
                "volume": float(raw.get(volume) or 0),
            }
        except (TypeError, ValueError):
            continue
        if values["close"] <= 0:
            continue
        rows.append({"trade_date": trade_date, "benchmark": str(benchmark), **values})
    return sorted(rows, key=lambda row: (str(row["trade_date"]), str(row["benchmark"])))


def fetch_akshare_benchmark(
    benchmark: str,
    start: str,
    end: str,
    *,
    fetcher: Callable[..., object] | None = None,
) -> list[dict[str, object]]:
    """Fetch one independent index series through AkShare."""
    symbol = BENCHMARK_SYMBOLS.get(str(benchmark).strip().upper(), str(benchmark).strip())
    if fetcher is None:
        import akshare as ak  # type: ignore

        fetcher = getattr(ak, "stock_zh_index_daily", None)
    if not callable(fetcher):
        raise RuntimeError("AKSHARE_INDEX_PROVIDER_UNAVAILABLE")
    return normalize_benchmark_frame(fetcher(symbol=symbol), benchmark, start=start, end=end)


def write_benchmark_csv(path: Path | str, rows: Iterable[Mapping[str, object]]) -> dict[str, object]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    canonical = [
        {field: row.get(field) for field in BENCHMARK_FIELDS}
        for row in sorted(rows, key=lambda item: (str(item.get("trade_date")), str(item.get("benchmark"))))
    ]
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BENCHMARK_FIELDS)
        writer.writeheader()
        writer.writerows(canonical)
    temporary.replace(target)
    return {
        "rows": len(canonical),
        "benchmarks": sorted({str(row["benchmark"]) for row in canonical}),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "path": str(target),
    }


def load_benchmark_csv(path: Path | str) -> dict[str, dict[str, float]]:
    """Load a canonical benchmark CSV keyed by benchmark/date."""
    values: dict[str, dict[str, float]] = {}
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(BENCHMARK_FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError("benchmark columns missing: " + ",".join(sorted(missing)))
        for row in reader:
            benchmark = str(row["benchmark"]).strip()
            trade_date = str(row["trade_date"])[:10]
            close = float(row["close"])
            if benchmark and close > 0:
                values.setdefault(benchmark, {})[trade_date] = close
    return values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch independent A-share index benchmark data")
    parser.add_argument("--benchmark", default="000300.XSHG")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    rows = fetch_akshare_benchmark(args.benchmark, args.start, args.end)
    metadata = write_benchmark_csv(args.output, rows)
    metadata.update({"benchmark": args.benchmark, "start": args.start, "end": args.end})
    print(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
