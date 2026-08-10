from __future__ import annotations

import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.test_strict_history_ingest import _build_package


class StrictHistoryUploadScriptTest(unittest.TestCase):
    def test_upload_script_contains_no_server_or_key_material(self) -> None:
        root = Path(__file__).parents[1]
        text = (root / "scripts" / "strict_history_upload.ps1").read_text(encoding="utf-8")
        launcher = (root / "一键上传严格历史数据.cmd").read_text(encoding="utf-8")

        self.assertNotIn("150.158.", text)
        self.assertNotIn("codex_temp_", text)
        self.assertIn("strict_history_upload_config.json", text)
        self.assertIn("joinquant_strict_history_exporter.py", text)
        self.assertIn("strict_history_ingest.py", text)
        self.assertIn("sha256sum -c", text)
        self.assertIn('[Guid]::NewGuid().ToString("N")', text)
        self.assertIn('$remoteStem = "$remoteInbox/$datasetId-$packageSha256-$uploadRunId"', text)
        self.assertNotIn(
            '$remoteReady = "$remoteInbox/$datasetId-$packageSha256.zip"',
            text,
        )
        self.assertIn("%~1", launcher)

    @unittest.skipUnless(
        shutil.which("powershell.exe"),
        "PowerShell validation is available only on Windows test hosts",
    )
    def test_validate_only_accepts_a_real_contract_package(self) -> None:
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            package = _build_package(Path(directory))
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(root / "scripts" / "strict_history_upload.ps1"),
                    "-PackagePath",
                    str(package),
                    "-ValidateOnly",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("strict-upload-test-v1", completed.stdout)


if __name__ == "__main__":
    unittest.main()
