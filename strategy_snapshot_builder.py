"""Build and verify deterministic, non-secret JoinQuant strategy snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import config as app_config
import joinquant_exporter
from ml_contracts import canonical_hash
from strategy_snapshot_runtime import (
    EXECUTION_PLAN_VERSION,
    SNAPSHOT_RUNTIME_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    build_safe_strategy_parameters,
    canonical_json,
    canonical_sha256,
)
from joinquant_point_in_time import (
    PIT_ENGINE_VERSION,
    PIT_FEATURE_SCHEMA_VERSION,
    PIT_MARKET_DATA_VERSION,
    PIT_MARKET_NEWS_POLICY,
    PIT_NEWS_POLICY,
)


BUILDER_VERSION = "2026-08-10.29-multipath"
MAX_SNAPSHOTS = 24
MAX_PACKAGE_BYTES = 20_000_000
PACKAGE_MEMBERS = frozenset({
    "strategy_snapshot.py",
    "joinquant_native_backtest.py",
    "joinquant_strict_export.py",
    "strategy_snapshot.json",
    "README_聚宽使用.txt",
    "manifest.json",
})
STRATEGY_SOURCE_FILES = (
    "a_share_strategy.py",
    "candidate_core.py",
    "joinquant_exporter.py",
    "ml_dataset.py",
    "trade_safety.py",
    "exit_policy.py",
    "trading_store.py",
    "gap_reentry.py",
    "config.py",
    "strategy_snapshot_runtime.py",
    "joinquant_point_in_time.py",
    "joinquant_strict_history_exporter.py",
    "factor_contracts.py",
    "factor_registry.py",
    "factor_wave3.py",
    "factor_limitdown.py",
    "candidate_channels.py",
    "strategy_economics.py",
    "strategy_exit_runtime.py",
    "strategy_attribution.py",
    "factor_research.py",
)
PORTABLE_FACTOR_SOURCE_FILES = (
    "factor_contracts.py",
    "factor_wave3.py",
    "factor_limitdown.py",
    "candidate_channels.py",
    "strategy_economics.py",
    "strategy_exit_runtime.py",
    "factor_registry.py",
)
SENSITIVE_ENV_TOKENS = (
    "token", "secret", "password", "passwd", "webhook", "private_key",
    "api_key", "cookie", "credential",
)


class StrategySnapshotBuildError(ValueError):
    """Stable fail-closed builder error."""


@dataclass(frozen=True)
class ActiveServiceEvidence:
    service: str
    active_state: str
    sub_state: str
    main_pid: int
    active_enter_timestamp: str
    active_enter_epoch: float
    exec_start: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _run(root: Path, arguments: list[str]) -> str:
    completed = subprocess.run(
        arguments,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise StrategySnapshotBuildError(
            "COMMAND_FAILED: " + " ".join(arguments[:3])
        )
    return completed.stdout.strip()


def _git_commit(root: Path) -> str:
    return _run(root, ["git", "rev-parse", "HEAD"])


def _strategy_worktree_status(root: Path) -> list[str]:
    output = _run(
        root,
        ["git", "status", "--porcelain", "--", *STRATEGY_SOURCE_FILES],
    )
    return [line.rstrip() for line in output.splitlines() if line.strip()]


def _systemctl_value(service: str, property_name: str) -> str:
    completed = subprocess.run(
        ["systemctl", "show", service, "--property=" + property_name, "--value"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise StrategySnapshotBuildError("ACTIVE_SERVICE_CHECK_FAILED")
    return completed.stdout.strip()


def _local_epoch(timestamp: str) -> float:
    value = timestamp.strip()
    if not value:
        raise StrategySnapshotBuildError("ACTIVE_SERVICE_TIMESTAMP_REQUIRED")
    fields = value.split()
    if len(fields) < 3:
        raise StrategySnapshotBuildError("ACTIVE_SERVICE_TIMESTAMP_INVALID")
    text = " ".join(fields[:3])
    try:
        parsed = datetime.strptime(text, "%a %Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise StrategySnapshotBuildError("ACTIVE_SERVICE_TIMESTAMP_INVALID") from exc
    return time.mktime(parsed.timetuple())


def active_service_evidence(
    root: Path,
    *,
    service: str = "stock-analysis.service",
    env_file: Path | None = None,
) -> ActiveServiceEvidence:
    active = _systemctl_value(service, "ActiveState")
    sub = _systemctl_value(service, "SubState")
    pid_text = _systemctl_value(service, "MainPID")
    started = _systemctl_value(service, "ActiveEnterTimestamp")
    exec_start = _systemctl_value(service, "ExecStart")
    try:
        pid = int(pid_text)
    except ValueError as exc:
        raise StrategySnapshotBuildError("ACTIVE_SERVICE_PID_INVALID") from exc
    if active != "active" or sub != "running" or pid <= 0:
        raise StrategySnapshotBuildError("STRATEGY_SERVICE_NOT_RUNNING")
    if "a_share_strategy.py" not in exec_start:
        raise StrategySnapshotBuildError("UNEXPECTED_STRATEGY_SERVICE_ENTRYPOINT")
    start_epoch = _local_epoch(started)
    checked = [root / name for name in STRATEGY_SOURCE_FILES]
    if env_file is not None:
        checked.append(env_file)
    for path in checked:
        resolved = path.resolve()
        if not resolved.is_file():
            raise StrategySnapshotBuildError("ACTIVE_SOURCE_FILE_MISSING: " + path.name)
        if resolved.stat().st_mtime > start_epoch + 2:
            raise StrategySnapshotBuildError("ACTIVE_SOURCE_NEWER_THAN_SERVICE: " + path.name)
    return ActiveServiceEvidence(
        service=service,
        active_state=active,
        sub_state=sub,
        main_pid=pid,
        active_enter_timestamp=started,
        active_enter_epoch=start_epoch,
        exec_start=exec_start,
    )


def _source_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in STRATEGY_SOURCE_FILES:
        path = (root / name).resolve()
        if path.parent != root.resolve() or not path.is_file():
            raise StrategySnapshotBuildError("STRATEGY_SOURCE_FILE_MISSING: " + name)
        result[name] = _sha256_file(path)
    return result


def _assert_no_sensitive_environment_values(serialized: str) -> None:
    lowered = serialized.lower()
    for name, value in os.environ.items():
        key = name.lower()
        if not any(token in key for token in SENSITIVE_ENV_TOKENS):
            continue
        secret = str(value or "")
        if len(secret) >= 6 and secret.lower() in lowered:
            raise StrategySnapshotBuildError("SENSITIVE_ENVIRONMENT_VALUE_DETECTED")


def _snapshot_payload(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    parameters = build_safe_strategy_parameters(app_config)
    parameters["historical_replay"] = {
        "pit_engine_version": PIT_ENGINE_VERSION,
        "news_policy": PIT_NEWS_POLICY,
        "market_news_policy": PIT_MARKET_NEWS_POLICY,
        "decision_schedule": "all_closed_5m_bars",
        "fill_policy": "decision_intent_next_5m_open",
        "initial_cash_yuan": 200_000.0,
        "portfolio_state": "deterministic_internal_replay",
        "platform_mirror": "joinquant_native_orders",
    }
    parameter_version = "multipath-simulation-v1:" + canonical_hash(parameters)[:12]
    payload: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "runtime_version": SNAPSHOT_RUNTIME_VERSION,
        "strategy_version": "multipath-simulation-v1-pit-replay",
        "execution_strategy_version": joinquant_exporter.EXACT_STRATEGY_VERSION,
        "execution_plan_version": EXECUTION_PLAN_VERSION,
        "parameter_version": parameter_version,
        "feature_schema_version": PIT_FEATURE_SCHEMA_VERSION,
        "market_data_version": PIT_MARKET_DATA_VERSION,
        "source_commit": _git_commit(root),
        "source_file_sha256": _source_hashes(root),
        "code_hash": canonical_sha256({
            "live_ml_code_hash": joinquant_exporter._ml_code_hash(),
            "pit_engine_sha256": _sha256_file(root / "joinquant_point_in_time.py"),
            "strict_exporter_sha256": _sha256_file(
                root / "joinquant_strict_history_exporter.py"
            ),
            "factor_source_sha256": {
                name: _sha256_file(root / name)
                for name in PORTABLE_FACTOR_SOURCE_FILES
            },
        }),
        "parameter_sha256": canonical_sha256(parameters),
        "provider_contract": {
            "strict_feature_provider_required": False,
            "portfolio_state_provider_required": False,
            "built_in_point_in_time_providers": True,
            "current_cache_forbidden": True,
            "future_features_forbidden": True,
            "news_policy": PIT_NEWS_POLICY,
            "market_news_policy": PIT_MARKET_NEWS_POLICY,
        },
    }
    payload["snapshot_id"] = canonical_sha256(payload)
    return payload, parameters


def _generated_runtime(root: Path, payload: Mapping[str, Any], parameters: Mapping[str, Any]) -> bytes:
    source = _portable_factor_sources(root) + "\n\n" + (
        root / "strategy_snapshot_runtime.py"
    ).read_text(encoding="utf-8")
    package_data = canonical_json({"snapshot": payload, "parameters": parameters})
    generated = (
        "# Generated deterministically by strategy_snapshot_builder.py.\n"
        "# Upload this file to JoinQuant Research; it contains no server credentials.\n\n"
        + source
        + "\n\nconfigure_snapshot("
        + repr(dict(payload))
        + ", "
        + repr(dict(parameters))
        + ")\n"
        + "SNAPSHOT_PACKAGE_SHA256 = canonical_sha256(json.loads("
        + repr(package_data)
        + "))\n"
    )
    return generated.encode("utf-8")


def _generated_header(description: str) -> str:
    return (
        "# Generated deterministically by strategy_snapshot_builder.py.\n"
        "# " + description + "\n"
        "# Python 3.6 compatible; contains no server credentials.\n\n"
    )


def _configured_sources(
    root: Path,
    payload: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> str:
    runtime = _portable_factor_sources(root) + "\n\n" + (
        root / "strategy_snapshot_runtime.py"
    ).read_text(encoding="utf-8")
    pit = (root / "joinquant_point_in_time.py").read_text(encoding="utf-8")
    package_data = canonical_json({"snapshot": payload, "parameters": parameters})
    return (
        runtime
        + "\n\n"
        + pit
        + "\n\nconfigure_snapshot("
        + repr(dict(payload))
        + ", "
        + repr(dict(parameters))
        + ")\n"
        + "SNAPSHOT_PACKAGE_SHA256 = canonical_sha256(json.loads("
        + repr(package_data)
        + "))\n"
    )


def _portable_factor_sources(root: Path) -> str:
    return "\n\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in PORTABLE_FACTOR_SOURCE_FILES
    )


def _generated_native_backtest(
    root: Path,
    payload: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> bytes:
    wrapper = r'''

NATIVE_BACKTEST_VERSION = "2026-08-10.1-multipath"
NATIVE_DECISION_TIMES = frozenset((
    "09:35", "09:40", "09:45", "09:50", "09:55",
    "10:00", "10:05", "10:10", "10:15", "10:20", "10:25", "10:30",
    "10:35", "10:40", "10:45", "10:50", "10:55", "11:00", "11:05",
    "11:10", "11:15", "11:20", "11:25", "11:30", "13:05", "13:10",
    "13:15", "13:20", "13:25", "13:30", "13:35", "13:40", "13:45",
    "13:50", "13:55", "14:00", "14:05", "14:10", "14:15", "14:20",
    "14:25", "14:30", "14:35", "14:40", "14:45", "14:50", "14:55",
))


def initialize(context):
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0.0005,
            open_commission=0.0003,
            close_commission=0.0003,
            close_today_commission=0,
            min_commission=5,
        ),
        type="stock",
    )
    set_slippage(PriceRelatedSlippage(0.001), type="stock")
    g.pit_engine = PointInTimeReplayEngine(
        SNAPSHOT_PARAMETERS,
        SNAPSHOT_MANIFEST,
        namespace=globals(),
        initial_cash=float(context.portfolio.starting_cash),
    )
    g.pit_last_decision = ""
    g.pit_signal_count = 0
    configure_strict_providers(
        g.pit_engine.feature_provider,
        g.pit_engine.portfolio_state_provider,
        g.pit_engine.decision_observer,
    )
    log.info(
        "PIT_NATIVE_READY snapshot_id=%s no_future=1 feature_schema=%s"
        % (SNAPSHOT_MANIFEST["snapshot_id"], SNAPSHOT_MANIFEST["feature_schema_version"])
    )


def handle_data(context, data):
    clock = context.current_dt.strftime("%H:%M")
    if clock not in NATIVE_DECISION_TIMES:
        return
    decision_key = context.current_dt.strftime("%Y-%m-%d %H:%M")
    if decision_key == g.pit_last_decision:
        return
    g.pit_last_decision = decision_key
    pit_context = g.pit_engine.build_joinquant_context(context.current_dt)
    rows = my_strict_candidate_builder(pit_context)
    intents = g.pit_engine.drain_platform_intents()
    submitted = 0
    for intent in sorted(intents, key=lambda item: 0 if item["side"] == "sell" else 1):
        security = _jq_code(intent["code"])
        target = int(intent["target_qty"])
        positions = context.portfolio.positions
        position = positions[security] if security in positions else None
        current_qty = int(getattr(position, "total_amount", 0) or 0)
        if intent["side"] == "sell":
            closeable_qty = int(getattr(position, "closeable_amount", 0) or 0)
            target = max(min(target, current_qty), current_qty - closeable_qty)
            if current_qty <= 0 or closeable_qty <= 0 or target >= current_qty:
                continue
        elif target <= current_qty:
            continue
        order_target(security, target)
        submitted += 1
    selected = [row for row in rows if row.get("selected")]
    g.pit_signal_count += len(selected)
    log.info(
        "PIT_DECISION at=%s cohort=%s selected=%s intents=%s submitted=%s"
        % (pit_context.decision_at, len(rows), len(selected), len(intents), submitted)
    )


def after_trading_end(context):
    log.info(
        "PIT_DAY_END date=%s total_selected=%s total_value=%.2f"
        % (context.current_dt.strftime("%Y-%m-%d"), g.pit_signal_count, context.portfolio.total_value)
    )
'''
    generated = (
        _generated_header("Paste this file into a new JoinQuant native backtest strategy.")
        + _configured_sources(root, payload, parameters)
        + wrapper
    )
    return generated.encode("utf-8")


def _generated_strict_export(
    root: Path,
    payload: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> bytes:
    exporter = (root / "joinquant_strict_history_exporter.py").read_text(
        encoding="utf-8"
    )
    wrapper = r'''

STRICT_EXPORT_SCRIPT_VERSION = "2026-08-10.21-multipath"


def default_strict_export_month(today=None):
    today = today or date.today()
    mature_day = today - timedelta(days=45)
    return "%04d-%02d" % (mature_day.year, mature_day.month)


# 小白只需要改这一行；默认选择已经成熟到 D+10 的最近月份。
STRICT_EXPORT_MONTH = default_strict_export_month()
STRICT_EXPORT_INITIAL_CASH = float(
    SNAPSHOT_PARAMETERS["historical_replay"]["initial_cash_yuan"]
)


def run_complete_strict_export(month=None):
    month = str(month or STRICT_EXPORT_MONTH)
    engine = PointInTimeReplayEngine(
        SNAPSHOT_PARAMETERS,
        SNAPSHOT_MANIFEST,
        namespace=globals(),
        initial_cash=STRICT_EXPORT_INITIAL_CASH,
    )
    configure_strict_providers(
        engine.feature_provider,
        engine.portfolio_state_provider,
        engine.decision_observer,
    )
    identity = snapshot_export_config()
    dataset_id = "jq-pit-%s-%s" % (
        month.replace("-", ""), SNAPSHOT_MANIFEST["snapshot_id"][:12]
    )
    config = ExportConfig(
        dataset_id=dataset_id,
        month=month,
        output_root="jq_strict_exports",
        strategy_version=identity["strategy_version"],
        parameter_version=identity["parameter_version"],
        feature_schema_version=identity["feature_schema_version"],
        market_data_version=identity["market_data_version"],
        code_hash=identity["code_hash"],
        generator_hash=identity["generator_hash"],
        decision_times=DEFAULT_DECISION_TIMES,
        strict=True,
        export_daily_core=True,
        require_daily_features=False,
        forward_trade_days=10,
        security_batch_size=300,
        max_candidate_rows=100000,
        max_candidate_price_rows=2000000,
        max_archive_bytes=3000000000,
        overwrite=True,
    )
    result = export_month(
        config,
        candidate_builder=my_strict_candidate_builder,
    )
    print("STRICT_EXPORT_OK")
    print("月份:", month)
    print("候选行:", result["candidate_rows"])
    print("价格路径行:", result["candidate_price_rows"])
    print("导出包:", result["archive"])
    print("下一步：在聚宽左侧文件区找到上面的 ZIP，下载后拖到桌面的一键上传图标。")
    return result


if __name__ == "__main__" and str(sys.argv[0]).endswith("ipykernel_launcher.py"):
    STRICT_EXPORT_RESULT = run_complete_strict_export()
'''
    generated = (
        _generated_header("Paste this complete file into one JoinQuant Research cell.")
        + exporter
        + "\n\n"
        + _configured_sources(root, payload, parameters)
        + wrapper
    )
    return generated.encode("utf-8")


def _readme(payload: Mapping[str, Any]) -> bytes:
    text = f"""聚宽策略快照

快照 ID：{payload['snapshot_id']}
策略版本：{payload['strategy_version']}
参数版本：{payload['parameter_version']}

使用方法：
包内已有两个可以直接使用的完整脚本：
1. joinquant_native_backtest.py：粘贴到聚宽“策略研究/回测”并运行回测。
2. joinquant_strict_export.py：粘贴到聚宽“研究 Notebook”单个代码单元并运行。

严格导出脚本已内置时点特征、组合回放和候选生成器，不需要再手写 provider。
默认导出已成熟到 D+10 的最近月份；如需指定月份，只改 STRICT_EXPORT_MONTH。

反前视规则：
- 只读取决策时点及以前闭合的 5 分钟行情。
- 日线技术指标只读取决策日前的完整日线。
- 估值使用上一完整交易日，行业使用指定历史日期。
- 盘中观察池的个股新闻/LHB与线上同路径：不展开，分数固定为0，并记录状态。
- 因聚宽 CCTV 表缺少可证明的盘中发布时间，同日市场新闻不进入历史决策。
- 没有30根已完成日线的股票不伪造MA30，而是跳过并用后续候选补位。
- 原生回测在下一根5分钟才镜像订单，并按聚宽实际持仓和T+1可卖量夹紧卖单。

这个文件不会连接服务器，不包含 SSH 密钥、Token、Webhook、账户、持仓或数据库。
任一特征时间晚于决策时点、估值/行业/价格证据缺失或包哈希不一致时都会失败关闭。
"""
    return text.encode("utf-8")


def _zip_bytes(members: Mapping[str, bytes]) -> bytes:
    from io import BytesIO

    target = BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100644 & 0xFFFF) << 16
            archive.writestr(info, members[name])
    return target.getvalue()


def _package_bytes(root: Path, payload: Mapping[str, Any], parameters: Mapping[str, Any]) -> bytes:
    snapshot_json = (
        json.dumps(
            {"snapshot": payload, "parameters": parameters},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    content = {
        "strategy_snapshot.py": _generated_runtime(root, payload, parameters),
        "joinquant_native_backtest.py": _generated_native_backtest(
            root, payload, parameters
        ),
        "joinquant_strict_export.py": _generated_strict_export(
            root, payload, parameters
        ),
        "strategy_snapshot.json": snapshot_json,
        "README_聚宽使用.txt": _readme(payload),
    }
    manifest = {
        "schema_version": 1,
        "snapshot_id": payload["snapshot_id"],
        "members": {
            name: {"sha256": _sha256_bytes(value), "size": len(value)}
            for name, value in sorted(content.items())
        },
    }
    content["manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    serialized = b"".join(content[name] for name in sorted(content)).decode("utf-8")
    _assert_no_sensitive_environment_values(serialized)
    package = _zip_bytes(content)
    if len(package) > MAX_PACKAGE_BYTES:
        raise StrategySnapshotBuildError("SNAPSHOT_PACKAGE_TOO_LARGE")
    return package


def verify_snapshot_package(path: Path) -> dict[str, Any]:
    package = path.resolve()
    if not package.is_file() or package.suffix.lower() != ".zip":
        raise StrategySnapshotBuildError("SNAPSHOT_PACKAGE_REQUIRED")
    if package.stat().st_size > MAX_PACKAGE_BYTES:
        raise StrategySnapshotBuildError("SNAPSHOT_PACKAGE_TOO_LARGE")
    with zipfile.ZipFile(package, "r") as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or set(names) != PACKAGE_MEMBERS:
            raise StrategySnapshotBuildError("SNAPSHOT_MEMBER_SET_INVALID")
        for info in infos:
            member_path = Path(info.filename)
            mode = info.external_attr >> 16
            if member_path.is_absolute() or ".." in member_path.parts or stat.S_ISLNK(mode):
                raise StrategySnapshotBuildError("UNSAFE_SNAPSHOT_MEMBER")
            if info.file_size > MAX_PACKAGE_BYTES:
                raise StrategySnapshotBuildError("SNAPSHOT_MEMBER_TOO_LARGE")
            if info.compress_size == 0 and info.file_size > 0:
                raise StrategySnapshotBuildError("SNAPSHOT_COMPRESSION_RATIO_INVALID")
            if info.compress_size and info.file_size / info.compress_size > 200:
                raise StrategySnapshotBuildError("SNAPSHOT_COMPRESSION_RATIO_INVALID")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        expected_members = PACKAGE_MEMBERS.difference({"manifest.json"})
        manifest_members = manifest.get("members")
        if (
            manifest.get("schema_version") != 1
            or not isinstance(manifest_members, dict)
            or set(manifest_members) != expected_members
        ):
            raise StrategySnapshotBuildError("SNAPSHOT_MANIFEST_MEMBERS_INVALID")
        for name, expected in manifest_members.items():
            if not isinstance(expected, dict) or set(expected) != {"sha256", "size"}:
                raise StrategySnapshotBuildError("SNAPSHOT_MANIFEST_MEMBER_INVALID: " + name)
            content = archive.read(name)
            if len(content) != int(expected["size"]) or _sha256_bytes(content) != expected["sha256"]:
                raise StrategySnapshotBuildError("SNAPSHOT_MEMBER_HASH_MISMATCH: " + name)
        data = json.loads(archive.read("strategy_snapshot.json").decode("utf-8"))
        snapshot = data.get("snapshot")
        parameters = data.get("parameters")
        if not isinstance(snapshot, dict) or not isinstance(parameters, dict):
            raise StrategySnapshotBuildError("SNAPSHOT_METADATA_INVALID")
        identity = dict(snapshot)
        supplied_id = str(identity.pop("snapshot_id", ""))
        if not supplied_id or canonical_sha256(identity) != supplied_id:
            raise StrategySnapshotBuildError("SNAPSHOT_ID_MISMATCH")
        if manifest.get("snapshot_id") != supplied_id:
            raise StrategySnapshotBuildError("SNAPSHOT_MANIFEST_ID_MISMATCH")
        if canonical_sha256(parameters) != snapshot.get("parameter_sha256"):
            raise StrategySnapshotBuildError("SNAPSHOT_PARAMETER_HASH_MISMATCH")
    return {
        "accepted": True,
        "snapshot_id": supplied_id,
        "strategy_version": snapshot["strategy_version"],
        "parameter_version": snapshot["parameter_version"],
        "code_hash": snapshot["code_hash"],
        "package_sha256": _sha256_file(package),
        "package_size": package.stat().st_size,
    }


def build_snapshot(
    root: Path,
    *,
    archive_root: Path,
    latest_report: Path,
    verify_active: bool = True,
    service: str = "stock-analysis.service",
    env_file: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    archive_root = archive_root.resolve()
    latest_report = latest_report.resolve()
    cache_root = (root / "cache" / "backtest").resolve()
    output_root = (root / "output").resolve()
    if archive_root != cache_root / "strategy_snapshots":
        raise StrategySnapshotBuildError("SNAPSHOT_ARCHIVE_ROOT_INVALID")
    if latest_report != output_root / "strategy_snapshot_latest.json":
        raise StrategySnapshotBuildError("SNAPSHOT_REPORT_PATH_INVALID")
    evidence = None
    if verify_active:
        evidence = active_service_evidence(root, service=service, env_file=env_file)
    payload, parameters = _snapshot_payload(root)
    package_bytes = _package_bytes(root, payload, parameters)
    package_name = "strategy-snapshot-%s.zip" % payload["snapshot_id"][:16]
    archive_root.mkdir(parents=True, exist_ok=True)
    archive = archive_root / package_name
    existing = sorted(archive_root.glob("strategy-snapshot-*.zip"))
    if not archive.exists() and len(existing) >= MAX_SNAPSHOTS:
        raise StrategySnapshotBuildError("STRATEGY_SNAPSHOT_RETENTION_LIMIT_REACHED")
    idempotent = archive.exists()
    if idempotent:
        if archive.read_bytes() != package_bytes:
            raise StrategySnapshotBuildError("STRATEGY_SNAPSHOT_CONTENT_CONFLICT")
    else:
        with tempfile.NamedTemporaryFile(
            prefix=".strategy-snapshot-",
            suffix=".tmp",
            dir=archive_root,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(package_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)
    verified = verify_snapshot_package(archive)
    worktree = _strategy_worktree_status(root)
    result: dict[str, Any] = {
        "status": "success",
        **verified,
        "archive": str(archive),
        "idempotent": idempotent,
        "source_commit": payload["source_commit"],
        "strategy_worktree_clean": not bool(worktree),
        "strategy_worktree_changes": worktree[:20],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if evidence is not None:
        result["active_service"] = {
            "service": evidence.service,
            "active_state": evidence.active_state,
            "sub_state": evidence.sub_state,
            "main_pid": evidence.main_pid,
            "active_enter_timestamp": evidence.active_enter_timestamp,
        }
    _atomic_json(latest_report, result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build one active-strategy snapshot")
    build.add_argument("--root", default=str(Path(__file__).resolve().parent))
    build.add_argument("--archive-root", default="cache/backtest/strategy_snapshots")
    build.add_argument("--latest-report", default="output/strategy_snapshot_latest.json")
    build.add_argument("--service", default="stock-analysis.service")
    build.add_argument("--env-file", default="stock-analysis.env")
    build.add_argument("--skip-active-service-check", action="store_true")
    verify = sub.add_parser("verify", help="verify one downloaded snapshot")
    verify.add_argument("--package", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_snapshot_package(Path(args.package))
        else:
            root = Path(args.root).resolve()
            archive_root = Path(args.archive_root)
            if not archive_root.is_absolute():
                archive_root = root / archive_root
            latest_report = Path(args.latest_report)
            if not latest_report.is_absolute():
                latest_report = root / latest_report
            env_file = Path(args.env_file)
            if not env_file.is_absolute():
                env_file = root / env_file
            result = build_snapshot(
                root,
                archive_root=archive_root,
                latest_report=latest_report,
                verify_active=not args.skip_active_service_check,
                service=args.service,
                env_file=env_file,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
