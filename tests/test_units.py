"""Regression for the Aug 15 real-tape finding: distances measured in per-bar NATR put every
liquidation cluster hundreds of units away, so the fuse could never fire. Distances must be measured
in horizon volatility."""
import math, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backtest"))
from harness import load_fuel_module  # noqa: E402

q = load_fuel_module()
NOW = 1_755_270_000.0
REF = 75.0
NATR_1M = 0.0006      # measured order of magnitude for SOL 1m: ~6 bps
SIGMA_1H = 0.0046     # NATR_1M * sqrt(60) ~ 46 bps


def snap(price):
    return {"symbol": "SOLUSDT", "ts": NOW, "ref_price": REF,
            "clusters": [{"price": price, "side": "short", "notional": 4e6, "burned": False}],
            "cascade": {"liq_notional_60s": 0, "baseline_60s": 1, "dominant_side": None}}


class Units(unittest.TestCase):
    def test_per_bar_natr_puts_a_1pct_cluster_out_of_reach(self):
        s = q.compute_fuel_signal(snap(REF * 1.01), REF, NATR_1M, NOW, reference_notional=4e6)
        self.assertGreater(s.up_dist_natr, 15)      # ~16 NATR away
        self.assertFalse(s.pull_sell)
        self.assertEqual(s.price_shift, 0.0)        # beyond lean horizon -> layer inert

    def test_horizon_vol_brings_the_same_cluster_into_range(self):
        s = q.compute_fuel_signal(snap(REF * 1.01), REF, SIGMA_1H, NOW, vol_shift=NATR_1M, reference_notional=4e6)
        self.assertLess(s.up_dist_natr, 3)          # ~2.2 sigma_h
        self.assertGreater(s.price_shift, 0)
        # and the lean stays small relative to the quoted spread, because it is scaled by NATR not sigma_h
        self.assertLessEqual(s.price_shift, 0.5 * NATR_1M + 1e-12)

    def test_fuse_fires_when_price_closes_in(self):
        far = q.compute_fuel_signal(snap(REF * 1.03), REF, SIGMA_1H, NOW, vol_shift=NATR_1M, reference_notional=4e6)
        near = q.compute_fuel_signal(snap(REF * 1.0012), REF, SIGMA_1H, NOW, vol_shift=NATR_1M, reference_notional=4e6)
        self.assertFalse(far.pull_sell)
        self.assertTrue(near.pull_sell)             # 0.26 sigma_h < fuse


if __name__ == "__main__":
    unittest.main()
