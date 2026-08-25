"""Regression for the Aug 15 sweep finding: gross edge per round trip was FLAT (~2.3 bps) at every
quoted spread from 1 to 5 NATR, because the triple barrier was a fixed distance from the ENTRY and
never scaled with how far from mid the quote sat. Barriers must scale with the level's spread."""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backtest"))
import harness  # noqa: E402

q = harness.load_quench_module()


def controller(**over):
    cfg = dict(id="bar", connector_name="bitget_perpetual", trading_pair="SOL-USDT",
               candles_connector="binance_perpetual", candles_trading_pair="SOL-USDT", interval="1m",
               total_amount_quote=Decimal("800"), buy_spreads="1,3", sell_spreads="1,3",
               fee_bps_per_side=Decimal("2.0"), tp_fee_multiple=Decimal("1.5"))
    cfg.update(over)
    c = q.QuenchController(q.QuenchControllerConfig(**cfg), market_data_provider=harness.OfflineDataProvider({}, {}),
                           actions_queue=None)
    c.processed_data = {"natr": 0.0006, "spread_multiplier": Decimal("0.0006"), "reference_price": Decimal("75")}
    return c


class Barriers(unittest.TestCase):
    def test_take_profit_scales_with_the_quoted_spread(self):
        c = controller(tp_spread_mult=Decimal("1.0"), sl_spread_mult=Decimal("2.0"))
        tp0, sl0 = c.barriers_for("buy_0")   # level 0 quoted 1 NATR out
        tp1, sl1 = c.barriers_for("buy_1")   # level 1 quoted 3 NATR out
        self.assertAlmostEqual(float(tp1 / tp0), 3.0, places=6)
        self.assertAlmostEqual(float(sl1 / sl0), 3.0, places=6)
        # level 1: 3 NATR = 18 bps target, 36 bps stop
        self.assertAlmostEqual(float(tp1), 3 * 0.0006, places=9)
        self.assertAlmostEqual(float(sl1), 6 * 0.0006, places=9)

    def test_take_profit_is_floored_above_the_round_trip_fee(self):
        c = controller(tp_spread_mult=Decimal("1.0"))
        tp0, _ = c.barriers_for("buy_0")     # 1 NATR = 6 bps, floor = 1.5 * 4 bps = 6 bps
        self.assertGreaterEqual(float(tp0), 1.5 * 2 * 2.0 / 1e4 - 1e-12)
        tight = controller(tp_spread_mult=Decimal("0.2"))
        tp, _ = tight.barriers_for("buy_0")  # 1.2 bps target would lose to a 4 bps round trip
        self.assertAlmostEqual(float(tp), 6e-4 * 0.0 + 1.5 * 2 * 2.0 / 1e4, places=9)

    def test_stop_is_never_tighter_than_the_target(self):
        c = controller(tp_spread_mult=Decimal("2.0"), sl_spread_mult=Decimal("0.5"))
        tp, sl = c.barriers_for("sell_1")
        self.assertGreaterEqual(float(sl), float(tp))

    def test_fixed_natr_mode_still_available(self):
        c = controller(tp_spread_mult=Decimal("0"), sl_spread_mult=Decimal("0"),
                       tp_natr=Decimal("2.0"), sl_natr=Decimal("4.0"), tp_fee_multiple=Decimal("0"))
        tp, sl = c.barriers_for("buy_1")
        self.assertAlmostEqual(float(tp), 2 * 0.0006, places=9)
        self.assertAlmostEqual(float(sl), 4 * 0.0006, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
