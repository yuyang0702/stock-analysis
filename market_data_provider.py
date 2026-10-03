"""Provider contracts and local credentials for historical market data.

The strategy and backtest layers consume the canonical tables in
``HistoricalStore``.  Providers only fetch and normalize external evidence;
they never decide whether a dataset is strict or production-ready.
"""

from __future__ import annotations

import csv
import importlib
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol


class MarketDataProviderError(RuntimeError):
    """A provider cannot satisfy the requested data contract."""


@dataclass(frozen=True)
class DataSourceSettings:
    """Non-secret source selection and secret references.

    Passwords are intentionally excluded from the repr and from serialized
    metadata.  The env-file loader is local-only and never writes credentials.
    """

    provider: str = "akshare"
    username: str = ""
    password: str = ""
    env_file: str = "stock-analysis.env"

    def __post_init__(self) -> None:
        provider = str(self.provider).strip().lower()
        if provider not in {"akshare", "jqdata", "broker_historical"}:
            raise ValueError(f"unsupported market data provider: {provider}")
        object.__setattr__(self, "provider", provider)

    def __repr__(self) -> str:
        return (
            "DataSourceSettings("
            f"provider={self.provider!r}, username_configured={bool(self.username)}, "
            f"password_configured={bool(self.password)}, env_file={self.env_file!r})"
        )


def read_env_file(path: Path | str) -> dict[str, str]:
    """Read simple ``KEY=VALUE`` lines without printing or mutating secrets."""
    target = Path(path)
    if not target.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_data_source_settings(
    *, env_file: Path | str = "stock-analysis.env", provider: str | None = None
) -> DataSourceSettings:
    """Resolve provider selection from process env, then the ignored env file."""
    file_values = read_env_file(env_file)

    def value(name: str, default: str = "") -> str:
        return str(os.getenv(name, file_values.get(name, default)) or "").strip()

    return DataSourceSettings(
        provider=provider or value("BACKTEST_DATA_PROVIDER", "akshare"),
        username=value("JQDATA_USERNAME"),
        password=value("JQDATA_PASSWORD"),
        env_file=str(env_file),
    )


class HistoricalMarketDataProvider(Protocol):
    """Minimal provider boundary used by acquisition jobs."""

    provider_name: str

    def connect(self) -> None:
        """Authenticate or verify local provider availability."""

    def latest_trade_date(self) -> str:
        """Return the latest provider trade date in ISO format."""

    def fetch_daily(self, code: str, start: str, end: str, *, adjust: str = "") -> list[dict[str, object]]:
        """Return canonical daily bars for one code."""


class AkShareProvider:
    """Adapter for the existing bounded AkShare collector.

    The collector remains in ``history_acquisition`` because it owns the
    canonical normalization and provenance files.  Imports are lazy to keep
    the provider module usable in environments where AkShare is optional.
    """

    provider_name = "akshare"

    def __init__(self) -> None:
        self._connected = False

    def connect(self) -> None:
        try:
            importlib.import_module("akshare")
        except ImportError as exc:
            raise MarketDataProviderError("AKSHARE_NOT_INSTALLED") from exc
        self._connected = True

    def latest_trade_date(self) -> str:
        if not self._connected:
            self.connect()
        # AkShare does not expose a stable provider-wide trading-calendar API.
        # Callers should use the dated response rows instead of this hint.
        raise MarketDataProviderError("AKSHARE_LATEST_TRADE_DATE_UNSUPPORTED")

    def fetch_daily(self, code: str, start: str, end: str, *, adjust: str = "") -> list[dict[str, object]]:
        if not self._connected:
            self.connect()
        from history_acquisition import fetch_akshare_code

        return fetch_akshare_code(code, start, end, adjust=adjust)

    def discover_codes(self, *, as_of: str | None = None) -> list[str]:
        if not self._connected:
            self.connect()
        from history_acquisition import discover_akshare_a_share_codes

        return discover_akshare_a_share_codes()


class JQDataProvider:
    """JQData local API adapter for canonical daily bars.

    The adapter deliberately exposes only raw evidence.  Daily status and
    historical universe facts are best-effort and remain ``proxy_only`` until
    the strict monthly exporter proves point-in-time completeness.
    """

    provider_name = "jqdata"

    def __init__(self, username: str, password: str) -> None:
        if not str(username).strip() or not str(password):
            raise MarketDataProviderError("JQDATA_CREDENTIALS_REQUIRED")
        self.username = str(username).strip()
        self.password = str(password)
        self._jq: Any | None = None
        self._connected = False

    def connect(self) -> None:
        try:
            module = importlib.import_module("jqdatasdk")
        except ImportError as exc:
            raise MarketDataProviderError("JQDATA_SDK_NOT_INSTALLED") from exc
        try:
            accepted = module.auth(self.username, self.password)
        except Exception as exc:
            raise MarketDataProviderError("JQDATA_AUTH_FAILED") from exc
        if accepted is False:
            raise MarketDataProviderError("JQDATA_AUTH_REJECTED")
        self._jq = module
        self._connected = True

    def _api(self) -> Any:
        if not self._connected or self._jq is None:
            raise MarketDataProviderError("JQDATA_NOT_CONNECTED")
        return self._jq

    @staticmethod
    def jq_code(code: str) -> str:
        normalized = str(code).strip().upper()
        if normalized.endswith(".XSHG") or normalized.endswith(".XSHE"):
            return normalized
        prefix = normalized[:1]
        exchange = "XSHG" if prefix in {"5", "6", "9"} else "XSHE"
        return f"{normalized.zfill(6)}.{exchange}"

    @staticmethod
    def plain_code(code: object) -> str:
        value = str(code).strip().upper()
        return value.split(".", 1)[0].zfill(6)

    @staticmethod
    def _column(frame: Any, *names: str) -> str | None:
        columns = {str(value).strip().lower(): str(value) for value in getattr(frame, "columns", ())}
        for name in names:
            found = columns.get(name.strip().lower())
            if found is not None:
                return found
        return None

    def latest_trade_date(self) -> str:
        jq = self._api()
        try:
            days = jq.get_trade_days(end_date=date.today().isoformat(), count=1)
        except Exception as exc:
            raise MarketDataProviderError("JQDATA_TRADE_DAYS_FAILED") from exc
        if not days:
            raise MarketDataProviderError("JQDATA_NO_TRADE_DATE")
        return str(days[-1])[:10]

    def discover_codes(self, as_of: str | None = None) -> list[str]:
        jq = self._api()
        try:
            frame = jq.get_all_securities(types=["stock"], date=as_of)
        except Exception as exc:
            raise MarketDataProviderError("JQDATA_UNIVERSE_FAILED") from exc
        if frame is None or getattr(frame, "empty", True):
            raise MarketDataProviderError("JQDATA_UNIVERSE_EMPTY")
        values = [self.plain_code(value) for value in getattr(frame, "index", ())]
        result = sorted({value for value in values if re.fullmatch(r"\d{6}", value)})
        if not result:
            raise MarketDataProviderError("JQDATA_UNIVERSE_EMPTY")
        return result

    def fetch_daily(self, code: str, start: str, end: str, *, adjust: str = "") -> list[dict[str, object]]:
        jq = self._api()
        fields = [
            "open", "high", "low", "close", "pre_close", "volume", "money",
            "factor", "high_limit", "low_limit", "paused",
        ]
        try:
            frame = jq.get_price(
                self.jq_code(code),
                start_date=start,
                end_date=end,
                frequency="daily",
                fields=fields,
                skip_paused=False,
                fq=adjust or None,
                panel=False,
            )
        except TypeError:
            # Older JQData versions do not accept ``panel`` or ``fq=None``.
            try:
                frame = jq.get_price(
                    self.jq_code(code),
                    start_date=start,
                    end_date=end,
                    frequency="daily",
                    fields=fields,
                    skip_paused=False,
                    fq=adjust or None,
                )
            except Exception as exc:
                raise MarketDataProviderError("JQDATA_PRICE_QUERY_FAILED") from exc
        except Exception as exc:
            raise MarketDataProviderError("JQDATA_PRICE_QUERY_FAILED") from exc
        if frame is None or getattr(frame, "empty", True):
            return []
        date_column = self._column(frame, "date", "time", "trade_date")
        if date_column is None:
            dates = [str(value)[:10] for value in getattr(frame, "index", ())]
        else:
            dates = [str(value)[:10] for value in frame[date_column].tolist()]
        records = frame.to_dict("records")
        previous: float | None = None
        rows: list[dict[str, object]] = []
        for trade_date, raw in zip(dates, records):
            def number(*names: str, default: float | None = None) -> float | None:
                for name in names:
                    if name in raw and raw[name] is not None:
                        try:
                            value = float(raw[name])
                        except (TypeError, ValueError):
                            continue
                        if value == value and abs(value) != float("inf"):
                            return value
                return default

            close = number("close")
            if close is None or close <= 0:
                continue
            previous_value = number("pre_close", "prev_close", default=previous)
            if previous_value is None or previous_value <= 0:
                previous = close
                continue
            rows.append({
                "trade_date": trade_date,
                "code": self.plain_code(code),
                "open": number("open", default=close),
                "high": number("high", default=close),
                "low": number("low", default=close),
                "close": close,
                "prev_close": previous_value,
                "volume": number("volume", default=0.0),
                "amount": number("money", "amount", default=0.0),
                "adjust_factor": number("factor", "adjust_factor", default=1.0),
                "limit_up": number("high_limit", "limit_up", default=0.0),
                "limit_down": number("low_limit", "limit_down", default=0.0),
                "suspended": int(bool(number("paused", "suspended", default=0.0))),
            })
            previous = close
        return rows


class BrokerHistoricalProvider:
    """Provider boundary for a broker's exported historical files.

    Real broker SDK calls stay out of this module.  A broker adapter can write
    its audited export into the canonical ``bars.csv`` format and this reader
    gives the acquisition pipeline the same provider contract.  The dataset
    remains ``proxy_only`` until status, universe and availability evidence
    pass the strict validator.
    """

    provider_name = "broker"

    def __init__(self, export_dir: Path | str) -> None:
        self.export_dir = Path(export_dir)
        self._rows: list[dict[str, object]] = []

    def connect(self) -> None:
        path = self.export_dir / "bars.csv"
        if not path.is_file():
            raise MarketDataProviderError("BROKER_HISTORICAL_BARS_REQUIRED")
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            self._rows = [dict(row) for row in csv.DictReader(handle)]
        if not self._rows:
            raise MarketDataProviderError("BROKER_HISTORICAL_BARS_EMPTY")

    def latest_trade_date(self) -> str:
        if not self._rows:
            self.connect()
        return max(str(row.get("trade_date") or "")[:10] for row in self._rows)

    def fetch_daily(self, code: str, start: str, end: str, *, adjust: str = "") -> list[dict[str, object]]:
        if not self._rows:
            self.connect()
        wanted = self.plain_code(code)
        result: list[dict[str, object]] = []
        for row in self._rows:
            if self.plain_code(row.get("code")) != wanted:
                continue
            trade_date = str(row.get("trade_date") or "")[:10]
            if not start <= trade_date <= end:
                continue
            result.append({
                "trade_date": trade_date,
                "code": wanted,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "prev_close": float(row["prev_close"]),
                "volume": float(row.get("volume") or 0),
                "amount": float(row.get("amount") or 0),
                "adjust_factor": float(row.get("adjust_factor") or 1),
                "limit_up": float(row.get("limit_up") or 0),
                "limit_down": float(row.get("limit_down") or 0),
                "suspended": int(float(row.get("suspended") or 0)),
            })
        return result

    @staticmethod
    def plain_code(code: object) -> str:
        return str(code or "").strip().upper().split(".", 1)[0].zfill(6)


def provider_from_settings(settings: DataSourceSettings) -> HistoricalMarketDataProvider:
    if settings.provider == "akshare":
        return AkShareProvider()
    if settings.provider == "jqdata":
        return JQDataProvider(settings.username, settings.password)
    if settings.provider == "broker_historical":
        export_dir = os.getenv("BROKER_HISTORICAL_EXPORT_DIR", "cache/backtest/broker_export")
        return BrokerHistoricalProvider(export_dir)
    raise MarketDataProviderError(f"PROVIDER_NOT_IMPLEMENTED:{settings.provider}")
