from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import strategy_snapshot_builder as builder


class StrategySnapshotBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parents[1]

    def test_package_is_deterministic_safe_and_python36_compatible(self) -> None:
        payload, parameters = builder._snapshot_payload(self.root)
        first = builder._package_bytes(self.root, payload, parameters)
        second = builder._package_bytes(self.root, payload, parameters)
        self.assertEqual(first, second)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.zip"
            path.write_bytes(first)
            verified = builder.verify_snapshot_package(path)
            self.assertEqual(verified["snapshot_id"], payload["snapshot_id"])
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), builder.PACKAGE_MEMBERS)
                source = archive.read("strategy_snapshot.py").decode("utf-8")
                ast.parse(source, feature_version=(3, 6))
                native = archive.read("joinquant_native_backtest.py").decode("utf-8")
                strict = archive.read("joinquant_strict_export.py").decode("utf-8")
                ast.parse(native, feature_version=(3, 6))
                ast.parse(strict, feature_version=(3, 6))
                data = json.loads(archive.read("strategy_snapshot.json"))
        self.assertIn("def initialize(context):", native)
        self.assertIn("avoid_future_data", native)
        self.assertIn('position = positions[security] if security in positions else None', native)
        self.assertIn('closeable_qty', native)
        self.assertIn("def run_complete_strict_export", strict)
        self.assertIn("configure_strict_providers", strict)
        self.assertIn("neutral_no_intraday_timestamp", strict)
        self.assertEqual(data["parameters"], parameters)
        self.assertNotIn("150.158.", first.decode("utf-8", errors="ignore"))
        self.assertNotIn("codex_temp_", first.decode("utf-8", errors="ignore"))

    def test_secret_environment_value_is_never_packaged(self) -> None:
        sentinel = "never-package-this-secret-7a418c"
        with patch.dict(os.environ, {"SNAPSHOT_TEST_TOKEN": sentinel}):
            payload, parameters = builder._snapshot_payload(self.root)
            package = builder._package_bytes(self.root, payload, parameters)
        self.assertNotIn(sentinel.encode("utf-8"), package)

    def test_tampered_or_unsafe_package_is_rejected(self) -> None:
        payload, parameters = builder._snapshot_payload(self.root)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.zip"
            path.write_bytes(builder._package_bytes(self.root, payload, parameters))
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("../escape.txt", "bad")
            with self.assertRaisesRegex(ValueError, "MEMBER_SET_INVALID"):
                builder.verify_snapshot_package(path)

    def test_manifest_must_hash_every_payload_member(self) -> None:
        payload, parameters = builder._snapshot_payload(self.root)
        package = builder._package_bytes(self.root, payload, parameters)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.zip"
            source = Path(directory) / "original.zip"
            source.write_bytes(package)
            with zipfile.ZipFile(source, "r") as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            manifest = json.loads(members["manifest.json"].decode("utf-8"))
            manifest["members"].pop("README_聚宽使用.txt")
            members["manifest.json"] = (
                json.dumps(manifest, ensure_ascii=False) + "\n"
            ).encode("utf-8")
            with zipfile.ZipFile(path, "w") as archive:
                for name, value in members.items():
                    archive.writestr(name, value)
            with self.assertRaisesRegex(ValueError, "MANIFEST_MEMBERS_INVALID"):
                builder.verify_snapshot_package(path)

    def test_effective_parameters_are_an_explicit_non_secret_allowlist(self) -> None:
        _, parameters = builder._snapshot_payload(self.root)
        self.assertEqual(
            set(parameters["ml"]),
            set(builder.build_safe_strategy_parameters(builder.app_config)["ml"]),
        )
        serialized = json.dumps(parameters, ensure_ascii=False).lower()
        for token in ("webhook", "token", "password", "private_key", "ssh_target"):
            self.assertNotIn(token, serialized)


if __name__ == "__main__":
    unittest.main()
