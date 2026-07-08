from pathlib import Path
import unittest


class JoinQuantLinuxScriptTest(unittest.TestCase):
    def test_run_script_is_the_single_linux_entrypoint(self) -> None:
        text = Path("run_ubuntu.sh").read_text(encoding="utf-8")

        self.assertIn('set_env "JOINQUANT_ENABLE" "1"', text)
        self.assertIn('set_env "PAPER_TRADE_ENABLE" "0"', text)
        self.assertIn('set_env "JOINQUANT_DRY_RUN" "false"', text)
        self.assertIn("stock-joinquant-signal.service", text)
        self.assertIn("stock-joinquant-sync.timer", text)
        self.assertIn("stock-joinquant-health.timer", text)
        self.assertIn("stock-ml-report.timer", text)
        self.assertIn("joinquant_signal_server.py", text)
        self.assertIn("joinquant_sync.py", text)
        self.assertIn("joinquant_health.py", text)
        self.assertIn("ml_dataset.py", text)
        self.assertIn("backtest_engine.py", text)
        self.assertIn("health)", text)
        self.assertIn("生成 JoinQuant 健康检查", text)
        self.assertIn("ml-report)", text)
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


if __name__ == "__main__":
    unittest.main()
