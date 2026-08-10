from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class StrategySnapshotScriptTest(unittest.TestCase):
    def test_script_and_launcher_contain_no_server_or_key_material(self) -> None:
        root = Path(__file__).parents[1]
        text = (root / "scripts" / "strategy_snapshot_download.ps1").read_text(encoding="utf-8-sig")
        launcher = (root / "一键生成聚宽策略快照.cmd").read_text(encoding="utf-8-sig")
        self.assertNotIn("150.158.", text)
        self.assertNotIn("codex_temp_", text)
        self.assertIn("strict_history_upload_config.json", text)
        self.assertIn("strategy_snapshot_builder.py build", text)
        self.assertIn("strategy_snapshot_builder.py", text)
        self.assertIn("聚宽策略快照.py", text)
        self.assertIn("聚宽原生回测策略.py", text)
        self.assertIn("聚宽严格历史导出.py", text)
        self.assertIn("joinquant_native_backtest.py", text)
        self.assertIn("joinquant_strict_export.py", text)
        self.assertIn(".strategy_snapshot_latest.downloading.zip", text)
        self.assertIn("strategy_snapshot_download.ps1", launcher)
        self.assertTrue(all(ord(character) < 128 for character in launcher))
        self.assertNotIn("chcp", launcher.lower())

    @unittest.skipUnless(shutil.which("powershell.exe"), "requires Windows PowerShell")
    def test_validate_only_checks_local_entry_without_connecting(self) -> None:
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            key = base / "test_key"
            key.write_text("not-a-real-key", encoding="utf-8")
            config = base / "connection.json"
            config.write_text(json.dumps({
                "ssh_target": "user@example.com",
                "identity_file": str(key),
                "remote_project": "/opt/stock-analysis",
            }), encoding="utf-8")
            completed = subprocess.run(
                [
                    "powershell.exe", "-NoLogo", "-NoProfile",
                    "-ExecutionPolicy", "Bypass", "-File",
                    str(root / "scripts" / "strategy_snapshot_download.ps1"),
                    "-ValidateOnly", "-ConnectionConfig", str(config),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("没有连接服务器", completed.stdout)


if __name__ == "__main__":
    unittest.main()
