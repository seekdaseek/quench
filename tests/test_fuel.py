import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backtest"))
from harness import load_fuel_module  # noqa: E402

q = load_fuel_module()

NOW = 1_755_270_000.0
REF = 75.0
NATR = 0.004  # 0.4% -> one NATR = 0.30 in price


def snap(clusters, ts=NOW, cascade=None):
    return {"symbol": "SOLUSDT", "ts": ts, "ref_price": REF, "clusters": clusters,
            "cascade": cascade or {"liq_notional_60s": 0, "baseline_60s": 10000, "dominant_side": None}}


class NearestUnburned(unittest.TestCase):
    def test_picks_nearest_correct_side_and_ignores_burned_small_wrongside(self):
        clusters = [
            {"price": 76.4, "side": "short", "notional": 3.7e6},
            {"price": 76.0, "side": "short", "notional": 2.5e6},            # nearer above
            {"price": 75.6, "side": "short", "notional": 5e6, "burned": True},  # burned -> ignored
            {"price": 75.8, "side": "short", "notional": 100},              # too small
            {"price": 74.5, "side": "long", "notional": 3.0e6},
            {"price": 74.0, "side": "long", "notional": 2.0e6},
            {"price": 75.5, "side": "long", "notional": 9e6},               # a 'long' cluster ABOVE price = already crossed
        ]
        up = q.nearest_unburned(clusters, REF, "short", 250_000)
        down = q.nearest_unburned(clusters, REF, "long", 250_000)
        self.assertEqual(up["price"], 76.0)
        self.assertEqual(down["price"], 74.5)

    def test_none_when_nothing_qualifies(self):
        self.assertIsNone(q.nearest_unburned([], REF, "short", 1))
        self.assertIsNone(q.nearest_unburned([{"price": 70, "side": "short", "notional": 1e9}], REF, "short", 1))
        self.assertIsNone(q.nearest_unburned([{"price": "x", "side": "short", "notional": 1e9}], REF, "short", 1))


class Signal(unittest.TestCase):
    def test_off_absent_stale_are_neutral(self):
        off = q.compute_fuel_signal(snap([]), REF, NATR, NOW, enabled=False)
        self.assertEqual(off.state, q.FUEL_OFF)
        absent = q.compute_fuel_signal(None, REF, NATR, NOW)
        self.assertEqual(absent.state, q.FUEL_ABSENT)
        stale = q.compute_fuel_signal(snap([{"price": 75.1, "side": "short", "notional": 9e6}], ts=NOW - 1000),
                                      REF, NATR, NOW, max_age_s=90)
        self.assertEqual(stale.state, q.FUEL_STALE)
        for s in (off, absent, stale):
            self.assertEqual(s.price_shift, 0.0)
            self.assertFalse(s.pull_buy or s.pull_sell)
            self.assertEqual(s.spread_multiplier, 1.0)

    def test_magnet_lean_toward_big_close_cluster_above_and_bounded(self):
        # cluster 1 NATR above, full size -> lean up = 1 - 1/3 = 0.667 * max_lean(0.5 natr)
        s = q.compute_fuel_signal(snap([{"price": REF * (1 + NATR), "side": "short", "notional": 2e6}]),
                                  REF, NATR, NOW, reference_notional=2e6, lean_horizon_natr=3.0, max_lean_natr=0.5)
        self.assertEqual(s.state, q.FUEL_LIVE)
        self.assertAlmostEqual(s.price_shift, (1 - 1 / 3) * 0.5 * NATR, places=9)
        # never more than max_lean_natr * natr even with absurd size very close
        s2 = q.compute_fuel_signal(snap([{"price": REF * (1 + 0.01 * NATR), "side": "short", "notional": 1e12}]),
                                   REF, NATR, NOW, max_lean_natr=0.5, fuse_natr=0.0)
        self.assertLessEqual(abs(s2.price_shift), 0.5 * NATR + 1e-12)

    def test_lean_cancels_when_symmetric(self):
        s = q.compute_fuel_signal(snap([{"price": REF * (1 + NATR), "side": "short", "notional": 2e6},
                                        {"price": REF * (1 - NATR), "side": "long", "notional": 2e6}]),
                                  REF, NATR, NOW, reference_notional=2e6)
        self.assertAlmostEqual(s.price_shift, 0.0, places=12)

    def test_fuse_pulls_sell_near_cluster_above_and_buy_near_cluster_below(self):
        up = q.compute_fuel_signal(snap([{"price": REF * (1 + 0.2 * NATR), "side": "short", "notional": 2e6}]),
                                   REF, NATR, NOW, fuse_natr=0.35, fuse_min_size=0.5, reference_notional=2e6)
        self.assertTrue(up.pull_sell)
        self.assertFalse(up.pull_buy)
        down = q.compute_fuel_signal(snap([{"price": REF * (1 - 0.2 * NATR), "side": "long", "notional": 2e6}]),
                                     REF, NATR, NOW, fuse_natr=0.35, fuse_min_size=0.5, reference_notional=2e6)
        self.assertTrue(down.pull_buy)
        self.assertFalse(down.pull_sell)
        # too small a cluster does not trigger the fuse
        small = q.compute_fuel_signal(snap([{"price": REF * (1 + 0.2 * NATR), "side": "short", "notional": 3e5}]),
                                      REF, NATR, NOW, fuse_natr=0.35, fuse_min_size=0.5, reference_notional=2e6)
        self.assertFalse(small.pull_sell)
        # far cluster does not trigger the fuse
        far = q.compute_fuel_signal(snap([{"price": REF * (1 + 2 * NATR), "side": "short", "notional": 5e6}]),
                                    REF, NATR, NOW, fuse_natr=0.35)
        self.assertFalse(far.pull_sell)

    def test_burned_cluster_flips_lean_off(self):
        live = q.compute_fuel_signal(snap([{"price": REF * (1 + NATR), "side": "short", "notional": 2e6}]), REF, NATR, NOW)
        burned = q.compute_fuel_signal(snap([{"price": REF * (1 + NATR), "side": "short", "notional": 2e6, "burned": True}]), REF, NATR, NOW)
        self.assertGreater(live.price_shift, 0)
        self.assertEqual(burned.price_shift, 0.0)
        self.assertIsNone(burned.up_cluster)

    def test_cascade_brake_widens_cuts_and_pulls_liquidated_side(self):
        s = q.compute_fuel_signal(snap([], cascade={"liq_notional_60s": 120000, "baseline_60s": 25000, "dominant_side": "long"}),
                                  REF, NATR, NOW, cascade_ratio_brake=3.0, cascade_spread_mult=2.0, cascade_amount_mult=0.5)
        self.assertAlmostEqual(s.cascade_ratio, 4.8)
        self.assertEqual(s.spread_multiplier, 2.0)
        self.assertEqual(s.amount_multiplier, 0.5)
        self.assertTrue(s.pull_buy)
        self.assertFalse(s.pull_sell)
        calm = q.compute_fuel_signal(snap([], cascade={"liq_notional_60s": 30000, "baseline_60s": 25000, "dominant_side": "long"}),
                                     REF, NATR, NOW)
        self.assertEqual(calm.spread_multiplier, 1.0)
        self.assertFalse(calm.pull_buy)

    def test_bad_inputs_are_neutral_not_exceptions(self):
        s = q.compute_fuel_signal(snap([{"price": None, "side": "short", "notional": "big"}]), REF, NATR, NOW)
        self.assertEqual(s.state, q.FUEL_LIVE)
        self.assertEqual(s.price_shift, 0.0)
        s2 = q.compute_fuel_signal(snap([]), REF, float("nan"), NOW)
        self.assertEqual(s2.price_shift, 0.0)
        row = s.as_row()
        self.assertEqual(row["up_dist_natr"], -1.0)
        self.assertTrue(all(math.isfinite(v) for k, v in row.items() if isinstance(v, float)))


class History(unittest.TestCase):
    def test_latest_snapshot_at(self):
        hist = [{"ts": 10, "clusters": []}, {"ts": 20, "clusters": []}, {"ts": 30, "clusters": []}]
        self.assertIsNone(q.latest_snapshot_at(hist, 5))
        self.assertEqual(q.latest_snapshot_at(hist, 10)["ts"], 10)
        self.assertEqual(q.latest_snapshot_at(hist, 25)["ts"], 20)
        self.assertEqual(q.latest_snapshot_at(hist, 99)["ts"], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class StaleSemantics(unittest.TestCase):
    """Moved out of test_backtest.py Aug 25 2026: this is pure math, not controller behaviour."""

    def test_stale_snapshot_neutralises_the_layer(self):
        sig = q.compute_fuel_signal({"ts": 0, "clusters": [{"price": 1, "side": "short", "notional": 1e9}]},
                                    75.0, 0.004, 10_000.0, max_age_s=90)
        self.assertEqual(sig.state, q.FUEL_STALE)


if __name__ == "__main__":
    unittest.main()
