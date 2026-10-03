"""Bounded local registry for reproducible historical datasets.

The registry contains provenance and selection metadata only.  Raw bars stay
in their provider-specific acquisition directory and are imported into the
separate ``HistoricalStore`` database.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


REGISTRY_VERSION = 1
MAX_DATASETS = 200


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"schema_version": REGISTRY_VERSION, "datasets": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("DATASET_REGISTRY_INVALID_JSON") from exc
    if not isinstance(value, Mapping) or not isinstance(value.get("datasets"), Mapping):
        raise ValueError("DATASET_REGISTRY_INVALID_SHAPE")
    return {"schema_version": REGISTRY_VERSION, "datasets": dict(value["datasets"])}


def _write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def register_dataset(
    metadata: Mapping[str, object],
    *,
    output_dir: Path | str,
    registry_path: Path | str = "cache/backtest/datasets.json",
) -> dict[str, object]:
    dataset_id = str(metadata.get("dataset_id") or "").strip()
    if not dataset_id:
        raise ValueError("DATASET_ID_REQUIRED")
    provider = str(metadata.get("source") or "").strip().lower()
    if provider not in {"akshare", "jqdata", "broker"}:
        raise ValueError("DATASET_PROVIDER_REQUIRED")
    output = Path(output_dir).resolve()
    entry = {
        "dataset_id": dataset_id,
        "provider": provider,
        "mode": "strict" if metadata.get("strict_eligible") is True else "price_core",
        "strict_eligible": bool(metadata.get("strict_eligible")),
        "proxy_only": bool(metadata.get("proxy_only")),
        "start": str(metadata.get("start") or ""),
        "end": str(metadata.get("end") or ""),
        "adjust": str(metadata.get("adjust") or "raw"),
        "output_dir": str(output),
        "rows": dict(metadata.get("rows") or {}),
        "warnings": list(metadata.get("warnings") or []),
        "sha256": dict(metadata.get("sha256") or {}),
        "registered_at": _now(),
    }
    path = Path(registry_path)
    payload = _read(path)
    datasets = dict(payload.get("datasets") or {})
    datasets[dataset_id] = entry
    if len(datasets) > MAX_DATASETS:
        ordered = sorted(
            datasets.items(),
            key=lambda item: str((item[1] or {}).get("registered_at") or ""),
            reverse=True,
        )[:MAX_DATASETS]
        datasets = dict(ordered)
    result = {"schema_version": REGISTRY_VERSION, "datasets": datasets}
    _write(path, result)
    return entry


def load_registry(path: Path | str = "cache/backtest/datasets.json") -> dict[str, object]:
    return _read(Path(path))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect the bounded historical dataset registry")
    parser.add_argument("--registry", default="cache/backtest/datasets.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = load_registry(args.registry)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
