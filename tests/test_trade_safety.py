import unittest
import math

from trade_safety import MarketRegimeState, attainable_sell_target, tradability_reject_reason


class TradeSafetyTest(unittest.TestCase):
    def test_sell_target_is_limited_by_closeable_quantity(self):
        self.assertEqual(attainable_sell_target(1000, 0, 600), (400, "partial_sellable"))
        self.assertEqual(attainable_sell_target(1000, 500, 0), (None, "t_plus_one"))

    def test_market_regime_requires_confirmation_and_slower_recovery(self):
        state = MarketRegimeState("NORMAL", "", 0)
        state = state.advance("RISK_OFF")
        self.assertEqual(state.current, "NORMAL")
        state = state.advance("RISK_OFF")
        self.assertEqual(state.current, "RISK_OFF")
        state = state.advance("NORMAL").advance("NORMAL")
        self.assertEqual(state.current, "RISK_OFF")
        self.assertEqual(state.advance("NORMAL").current, "NORMAL")

    def test_tradability_rejects_unsafe_or_chased_buy(self):
        self.assertEqual(tradability_reject_reason({"is_st": True}), "buy_st")
        self.assertEqual(tradability_reject_reason({"paused": True}), "buy_suspended")
        self.assertEqual(tradability_reject_reason({"entry_price": 10, "price": 10.3, "atr14": 0.2}), "buy_chasing")
        self.assertEqual(tradability_reject_reason({"entry_price": 10, "price": 10.1, "atr14": 0.2, "amount": 1e8}), "")
        self.assertEqual(tradability_reject_reason({"paused": math.nan, "is_st": math.nan, "entry_price": 10, "price": 10}), "")

    def test_tradability_rejects_new_listing_and_stale_quote(self):
        self.assertEqual(tradability_reject_reason({"listing_days": 4}), "buy_special_listing_stage")
        self.assertEqual(tradability_reject_reason({"quote_age_sec": 121}), "buy_quote_stale")


if __name__ == "__main__":
    unittest.main()
