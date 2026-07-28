import csv
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from backtest_engine import BacktestConfig, BacktestEngine, load_signal_rows, run_backtest
from execution_contracts import FeeSchedule


class BacktestEngineTest(unittest.TestCase):
    def test_reports_versioned_fee_components(self) -> None:
        fees = FeeSchedule(
            version="test-v1", effective_from="2026-01-01",
            buy_commission_rate=Decimal("0.0003"), sell_commission_rate=Decimal("0.0003"),
            minimum_commission_yuan=Decimal("5"), stamp_tax_rate=Decimal("0.0005"),
            transfer_fee_rate=Decimal("0.00001"), other_fee_rate=Decimal("0"),
            buy_slippage_rate=Decimal("0.001"), sell_slippage_rate=Decimal("0.001"),
        )
        result = BacktestEngine(BacktestConfig(initial_cash=10_000, fee_schedule=fees)).run(
            [{
                "date": "2026-01-02", "code": "600000", "action": "buy", "price": 10,
                "entry_price": 10, "position_pct": 10, "final_score": 90,
            }]
        )

        self.assertEqual(result.fee_schedule_version, "test-v1")
        self.assertEqual(result.trades[0]["price"], 10.0)
        self.assertEqual(result.trades[0]["fee"], 6.01)
        self.assertEqual(result.cash, 8_993.99)
        self.assertEqual(result.trades[0]["fee_components"], {
            "commission_yuan": 5.0,
            "stamp_tax_yuan": 0.0,
            "transfer_fee_yuan": 0.01,
            "other_fee_yuan": 0.0,
            "slippage_yuan": 1.0,
        })

    def test_buys_from_signal_position_and_marks_to_market(self) -> None:
        engine = BacktestEngine(
            BacktestConfig(initial_cash=100000, commission_rate=0, stamp_tax_rate=0, min_commission=0)
        )

        result = engine.run(
            [
                {
                    "date": "2026-01-02",
                    "code": "600000",
                    "name": "示例股",
                    "action": "buy",
                    "price": 10.0,
                    "entry_price": 10.0,
                    "position_pct": 10,
                    "final_score": 90,
                },
                {
                    "date": "2026-01-03",
                    "code": "600000",
                    "name": "示例股",
                    "action": "hold",
                    "price": 11.0,
                },
            ]
        )

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0]["action"], "buy")
        self.assertEqual(result.trades[0]["qty"], 1000)
        self.assertAlmostEqual(result.total_return_pct, 1.0)
        self.assertEqual(result.open_positions, 1)

    def test_t_plus_one_blocks_same_day_sell(self) -> None:
        engine = BacktestEngine(
            BacktestConfig(initial_cash=100000, commission_rate=0, stamp_tax_rate=0, min_commission=0)
        )

        result = engine.run(
            [
                {
                    "date": "2026-01-02",
                    "code": "600000",
                    "action": "buy",
                    "price": 10.0,
                    "entry_price": 10.0,
                    "position_pct": 10,
                    "final_score": 90,
                },
                {
                    "date": "2026-01-02",
                    "code": "600000",
                    "action": "sell",
                    "price": 9.5,
                },
                {
                    "date": "2026-01-03",
                    "code": "600000",
                    "action": "sell",
                    "price": 9.5,
                },
            ]
        )

        self.assertEqual([row["action"] for row in result.trades], ["buy", "sell"])
        self.assertEqual(result.trades[1]["date"], "2026-01-03")
        self.assertEqual(result.trades[1]["reason"], "sell_signal")
        self.assertEqual(result.open_positions, 0)

    def test_stop_loss_and_take_profit_sell_after_buy_day(self) -> None:
        engine = BacktestEngine(
            BacktestConfig(initial_cash=100000, commission_rate=0, stamp_tax_rate=0, min_commission=0)
        )

        result = engine.run(
            [
                {
                    "date": "2026-01-02",
                    "code": "600000",
                    "action": "buy",
                    "price": 10.0,
                    "entry_price": 10.0,
                    "stop_loss": 9.5,
                    "take_profit": 11.0,
                    "position_pct": 10,
                    "final_score": 90,
                },
                {
                    "date": "2026-01-03",
                    "code": "600000",
                    "action": "hold",
                    "price": 11.2,
                },
                {
                    "date": "2026-01-04",
                    "code": "600001",
                    "action": "buy",
                    "price": 10.0,
                    "entry_price": 10.0,
                    "stop_loss": 9.5,
                    "position_pct": 10,
                    "final_score": 90,
                },
                {
                    "date": "2026-01-05",
                    "code": "600001",
                    "action": "hold",
                    "price": 9.4,
                },
            ]
        )

        sell_reasons = [row["reason"] for row in result.trades if row["action"] == "sell"]
        self.assertEqual(sell_reasons, ["take_profit", "stop_loss"])
        self.assertEqual(result.win_trades, 1)
        self.assertEqual(result.loss_trades, 1)

    def test_loads_jsonl_samples_and_writes_report_and_trades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            signal_file = tmp_path / "samples.jsonl"
            report_file = tmp_path / "report.md"
            trades_file = tmp_path / "trades.csv"
            signal_file.write_text(
                "\n".join(
                    [
                        '{"signal": {"trade_date": "2026-01-02", "code": "600000", "name": "示例股", "action": "buy", "price": 10, "entry_price": 10, "position_pct": 10, "final_score": 90}}',
                        '{"signal": {"trade_date": "2026-01-03", "code": "600000", "action": "sell", "price": 11}}',
                    ]
                ),
                encoding="utf-8",
            )

            result = run_backtest(signal_file, report_file=report_file, trades_file=trades_file)

            self.assertEqual(len(result.trades), 2)
            self.assertIn("本地信号回测报告", report_file.read_text(encoding="utf-8"))
            with trades_file.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 2)

    def test_loads_joinquant_signal_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signals.json"
            path.write_text(
                '{"signals": [{"trade_date": "2026-01-02", "code": "600000", "action": "buy", "price": 10, "position_pct": 10}]}',
                encoding="utf-8",
            )

            rows = load_signal_rows(path)

            self.assertEqual(rows[0]["code"], "600000")


if __name__ == "__main__":
    unittest.main()
