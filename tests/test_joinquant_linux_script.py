from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from trading_store import TradingStore


class JoinQuantLinuxScriptTest(unittest.TestCase):
    def run_ledger_check(self, db_path: Path, *, schema_version: int = 1) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            app_dir = Path(temp_dir)
            for name in ("run_ubuntu.sh", "config.py", "trading_store.py"):
                shutil.copy2(name, app_dir / name)
            venv_python = app_dir / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text(
                f"#!/bin/sh\nexec '{Path(sys.executable).as_posix()}' \"$@\"\n",
                encoding="utf-8",
            )
            venv_python.chmod(0o755)
            (app_dir / "stock-analysis.env").write_text(
                f"TRADING_DB_FILE={db_path.as_posix()}\nRISK_MODE=observe\n",
                encoding="utf-8",
            )
            if schema_version != 1:
                with sqlite3.connect(db_path) as conn:
                    conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                    conn.execute("INSERT INTO schema_migrations VALUES (?, datetime('now'))", (schema_version,))
            git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
            bash = str(git_bash) if git_bash.exists() else (shutil.which("bash") or "bash")
            env = os.environ.copy()
            if git_bash.exists():
                env["PATH"] = os.pathsep.join(
                    [r"C:\Program Files\Git\usr\bin", r"C:\Program Files\Git\bin", env.get("PATH", "")]
                )
            return subprocess.run(
                [bash, "run_ubuntu.sh", "ledger-check"],
                cwd=app_dir,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

    def test_run_script_is_the_single_linux_entrypoint(self) -> None:
        text = Path("run_ubuntu.sh").read_text(encoding="utf-8")

        self.assertIn('set_env "JOINQUANT_ENABLE" "1"', text)
        self.assertIn('set_env "PAPER_TRADE_ENABLE" "0"', text)
        self.assertIn('set_env "JOINQUANT_DRY_RUN" "false"', text)
        self.assertIn("ledger-check", text)
        self.assertIn('set_env "RISK_MODE" "observe"', text)
        self.assertIn('set_env "MAX_TOTAL_POSITION_PCT" "95"', text)
        self.assertIn('set_env "ACCOUNT_SNAPSHOT_MAX_AGE_SEC" "300"', text)
        self.assertIn('mkdir -p "${APP_DIR}/cache/trading"', text)
        self.assertIn("stock-joinquant-signal.service", text)
        self.assertIn("stock-joinquant-sync.timer", text)
        self.assertIn("stock-joinquant-health.timer", text)
        self.assertIn("stock-notify-retry.timer", text)
        self.assertIn("stock-ml-report.timer", text)
        self.assertIn("stock-sector-context.timer", text)
        self.assertIn("joinquant_signal_server.py", text)
        self.assertIn("joinquant_sync.py", text)
        self.assertIn("joinquant_health.py", text)
        self.assertIn("notify_retry.py", text)
        self.assertIn("ml_dataset.py", text)
        self.assertIn("--sector-context-only", text)
        self.assertIn("backtest_engine.py", text)
        self.assertIn("health)", text)
        self.assertIn("notify-retry)", text)
        self.assertIn("生成 JoinQuant 健康检查", text)
        self.assertIn("ml-report)", text)
        self.assertIn("sector-context)", text)
        self.assertIn("backtest)", text)
        self.assertIn("运行本地信号回测", text)
        self.assertIn("install)", text)
        self.assertIn("DRY_RUN      = False", text)
        self.assertIn("show_menu()", text)
        self.assertIn("menu_loop()", text)
        self.assertIn("A股策略服务器菜单", text)
        self.assertIn("请输入序号", text)
        self.assertIn("[[ $# -eq 0 && -t 0 ]]", text)

    def test_old_linux_entrypoints_are_removed(self) -> None:
        self.assertFalse(Path("install_ubuntu.sh").exists())
        self.assertFalse(Path("start_linux_all.sh").exists())
        self.assertFalse(Path("start_joinquant_linux.sh").exists())

    def test_ledger_check_routes_and_probes_version_one_database(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "trading.db"
            result = self.run_ledger_check(db_path)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("schema_version=1 health=ok writable_probe=ok", result.stdout)

    def test_ledger_check_rejects_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            result = self.run_ledger_check(Path(temp_dir) / "trading.db", schema_version=2)

            self.assertNotEqual(0, result.returncode)

    def test_ledger_check_preserves_existing_system_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "trading.db"
            store = TradingStore(db_path)
            store.initialize()
            with store.transaction() as conn:
                store.set_system_state(conn, "ledger_check_probe", "keep", "existing")

            result = self.run_ledger_check(db_path)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("keep", store.get_system_state("ledger_check_probe"))


if __name__ == "__main__":
    unittest.main()
