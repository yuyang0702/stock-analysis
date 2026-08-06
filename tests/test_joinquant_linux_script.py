from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from trading_store import SCHEMA_VERSION, TradingStore


class JoinQuantLinuxScriptTest(unittest.TestCase):
    def run_ledger_check(
        self, db_path: Path, *, schema_version: int = SCHEMA_VERSION
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            app_dir = Path(temp_dir)
            for name in (
                "run_ubuntu.sh",
                "config.py",
                "trading_store.py",
                "ledger_check.py",
                "execution_contracts.py",
                "notification_outbox.py",
                "pre_trade_check.py",
                "position_sizing.py",
            ):
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
            if schema_version != SCHEMA_VERSION:
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
        self.assertIn('set_env "PAPER_TRADE_STAMP_TAX_RATE" "0.0005"', text)
        self.assertIn('set_env "JOINQUANT_DRY_RUN" "false"', text)
        self.assertIn("ledger-check", text)
        self.assertIn('set_env "RISK_MODE" "observe"', text)
        self.assertIn('set_env "MAX_TOTAL_POSITION_PCT" "95"', text)
        self.assertIn('set_env "ACCOUNT_SNAPSHOT_MAX_AGE_SEC" "300"', text)
        self.assertIn('set_env "JOINQUANT_PORTFOLIO_RISK_ENABLE" "1"', text)
        self.assertIn('set_env "JOINQUANT_TRADABILITY_FILTER_ENABLE" "1"', text)
        self.assertIn('set_env "JOINQUANT_REGIME_CONFIRM_ENABLE" "1"', text)
        self.assertIn('set_env "JOINQUANT_EXIT_COOLDOWN_ENABLE" "1"', text)
        self.assertIn('set_env "JOINQUANT_LAYERED_EXIT_ENABLE" "1"', text)
        self.assertIn('mkdir -p "${APP_DIR}/cache/trading"', text)
        self.assertIn("stock-joinquant-signal.service", text)
        self.assertIn("stock-joinquant-sync.timer", text)
        self.assertIn("stock-joinquant-health.timer", text)
        self.assertIn("stock-notify-retry.timer", text)
        self.assertIn("stock-ml-report.timer", text)
        self.assertIn("stock-sector-context.timer", text)
        self.assertIn("stock-trading-backup.timer", text)
        self.assertIn("stock-trading-backup-drill.timer", text)
        self.assertIn("joinquant_signal_server.py", text)
        self.assertIn("joinquant_sync.py", text)
        self.assertIn("joinquant_health.py", text)
        self.assertIn("notification_worker.py --once", text)
        self.assertNotIn("notify_retry.py", text)
        self.assertIn("Description=Deliver transactional WeCom notifications", text)
        self.assertIn("ml_dataset.py", text)
        self.assertIn("--sector-context-only", text)
        self.assertIn("backtest_engine.py", text)
        self.assertIn("trading_backup.py", text)
        self.assertIn("trading_backup.py backup", text)
        self.assertIn("trading_backup.py drill", text)
        self.assertIn('set_env "TRADING_BACKUP_DIR" "/opt/stock-analysis-backups"', text)
        self.assertIn("OnCalendar=*-*-* 16:30:00 Asia/Shanghai", text)
        self.assertIn("OnCalendar=Sun *-01,04,07,10-01..07 03:30:00 Asia/Shanghai", text)
        self.assertIn("health)", text)
        self.assertIn("notify-retry)", text)
        self.assertIn("notify-status)", text)
        self.assertIn("notify-legacy-audit)", text)
        self.assertIn("notify-compact-dry-run)", text)
        self.assertIn("notify-compact-apply)", text)
        self.assertIn("notify-resolve-write-failure)", text)
        self.assertNotIn("strategy-compare-weekly)", text)
        self.assertNotIn("strategy_compare_report.py --notify --weekly", text)
        self.assertNotIn('SYNC_TOKEN   = ${token}', text)
        self.assertIn('token="$(env_value JOINQUANT_SYNC_TOKEN "")"', text)
        self.assertIn('webhook="$(env_value WECOM_WEBHOOK_URL "")"', text)
        for field in (
            "pending=", "leased=", "sent=", "dead=", "cancelled=",
            "gaps=", "high_gaps=", "dead_detail_rows=", "dead_detail_bytes=",
            "high_dead=", "tombstones=", "write_failure_marker=",
            "write_failure_requires_manual_resolution=",
        ):
            self.assertIn(field, text)
        self.assertIn("生成 JoinQuant 健康检查", text)
        self.assertIn("ml-report)", text)
        self.assertIn("sector-context)", text)
        self.assertIn("backtest)", text)
        self.assertIn("historical-backtest)", text)
        self.assertIn("historical-backtest-validate)", text)
        self.assertNotIn("stock-historical-backtest.timer", text)
        self.assertIn("运行本地信号回测", text)
        self.assertIn("install)", text)
        self.assertIn("DRY_RUN      = False", text)
        self.assertIn("show_menu()", text)
        self.assertIn("menu_loop()", text)
        self.assertIn("A股策略服务器菜单", text)
        self.assertIn("请输入序号", text)
        self.assertIn("[[ $# -eq 0 && -t 0 ]]", text)
        for command in (
            "trading-status", "reconcile", "unlock", "stop-buy", "resume-buy",
            "kill-switch-on", "kill-switch-off",
        ):
            self.assertIn(f"{command})", text)
        self.assertIn("交易控制与自动对账", text)
        self.assertIn("执行完整对账", text)
        self.assertIn("交易解锁向导", text)

    def test_ml_maintenance_commands_and_automation_are_safe(self) -> None:
        text = Path("run_ubuntu.sh").read_text(encoding="utf-8")

        for command in (
            "ml-labels",
            "ml-train",
            "ml-model-status",
            "ml-backup",
            "ml-restore-check",
            "ml-retention-dry-run",
            "ml-retention-apply",
        ):
            self.assertIn(f"{command})", text)

        self.assertIn("ml_maintenance.py", text)
        self.assertIn("stock-ml-labels.timer", text)
        self.assertIn("stock-ml-train.timer", text)
        self.assertIn("stock-ml-backup.timer", text)
        self.assertIn("stock-history-backup.timer", text)
        self.assertIn(
            "ml_maintenance.py labels --allow-unconfigured",
            text,
        )
        self.assertIn(
            "ml_maintenance.py train --allow-unconfigured",
            text,
        )
        self.assertIn("ml_maintenance.py backup --kind ml", text)
        self.assertIn("ml_maintenance.py backup --kind history", text)
        self.assertIn("OnCalendar=Mon..Fri *-*-* 16:10:00 Asia/Shanghai", text)
        self.assertIn("OnCalendar=Fri *-*-* 18:00:00 Asia/Shanghai", text)
        self.assertGreaterEqual(
            text.count("OnCalendar=*-*-* 19:00:00 Asia/Shanghai"), 2
        )
        self.assertIn(
            'set_env_default "ML_BACKUP_DIR" "/opt/stock-analysis-backups/ml"',
            text,
        )
        self.assertIn(
            'set_env_default "HISTORY_BACKUP_DIR" "/opt/stock-analysis-backups/history"',
            text,
        )
        self.assertNotIn("ml_admin.py approve", text)
        self.assertNotIn("ml_admin.py activate", text)
        self.assertNotIn("ml_maintenance.py approve", text)
        self.assertNotIn("ml_maintenance.py activate", text)
        self.assertNotIn(
            "ExecStart=${py} ${APP_DIR}/ml_maintenance.py retention-apply",
            text,
        )

    def test_old_linux_entrypoints_are_removed(self) -> None:
        self.assertFalse(Path("install_ubuntu.sh").exists())
        self.assertFalse(Path("start_linux_all.sh").exists())
        self.assertFalse(Path("start_joinquant_linux.sh").exists())

    def test_ledger_check_routes_and_probes_current_schema_database(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "trading.db"
            result = self.run_ledger_check(db_path)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(
                f"schema_version={SCHEMA_VERSION} health=ok writable_probe=ok", result.stdout
            )

    def test_ledger_check_rejects_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            result = self.run_ledger_check(
                Path(temp_dir) / "trading.db", schema_version=SCHEMA_VERSION + 1
            )

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
