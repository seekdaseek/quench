"""Regression for the Aug 15 finding: the cascade brake was the entire cost of the fuel layer. It
fired on the open-interest-drop proxy, which never names a liquidated side, so it degraded quotes
(spread x2, size x0.5) on 2.4% of snapshots and could never do the one thing a brake is for."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backtest"))
from harness import load_fuel_module  # noqa: E402

q = load_fuel_module()
NOW = 1_755_270_000.0


def snap(dominant, ratio=5.0):
    return {"symbol": "SOLUSDT", "ts": NOW, "ref_price": 75.0, "clusters": [],
            "cascade": {"liq_notional_60s": 1000 * ratio, "baseline_60s": 1000, "dominant_side": dominant}}


class Cascade(unittest.TestCase):
    def test_unattributed_cascade_does_not_brake_by_default(self):
        s = q.compute_fuel_signal(snap(None), 75.0, 0.0046, NOW, vol_shift=0.0006)
        self.assertEqual(s.spread_multiplier, 1.0)
        self.assertEqual(s.amount_multiplier, 1.0)
        self.assertFalse(s.pull_buy or s.pull_sell)
        self.assertAlmostEqual(s.cascade_ratio, 5.0)   # still reported, just not acted on

    def test_attributed_cascade_brakes_and_pulls_the_liquidated_side(self):
        s = q.compute_fuel_signal(snap("long"), 75.0, 0.0046, NOW, vol_shift=0.0006)
        self.assertEqual(s.spread_multiplier, 2.0)
        self.assertEqual(s.amount_multiplier, 0.5)
        self.assertTrue(s.pull_buy)
        self.assertFalse(s.pull_sell)
        short = q.compute_fuel_signal(snap("short"), 75.0, 0.0046, NOW, vol_shift=0.0006)
        self.assertTrue(short.pull_sell)
        self.assertFalse(short.pull_buy)

    def test_opt_out_restores_the_old_behaviour(self):
        s = q.compute_fuel_signal(snap(None), 75.0, 0.0046, NOW, vol_shift=0.0006,
                                  require_attributed_cascade=False)
        self.assertEqual(s.spread_multiplier, 2.0)
        self.assertFalse(s.pull_buy or s.pull_sell)   # still cannot pull: no side named

    def test_below_threshold_never_brakes(self):
        s = q.compute_fuel_signal(snap("long", ratio=1.5), 75.0, 0.0046, NOW, vol_shift=0.0006)
        self.assertEqual(s.spread_multiplier, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
