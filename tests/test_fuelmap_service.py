import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backtest"))
spec = importlib.util.spec_from_file_location("fuelmap_service", os.path.join(HERE, "..", "service", "fuelmap_service.py"))
fm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fm)
from harness import load_fuel_module  # noqa: E402

q = load_fuel_module()

T0 = 1_755_200_000


class Model(unittest.TestCase):
    def test_oi_increase_projects_to_tier_liq_prices_both_sides(self):
        st = fm.FuelState("SOLUSDT", bucket_bps=5, decay_hours=0, tiers={10: 0.5, 50: 0.5})
        st.ingest_bar(T0, close=100.0, high=100.5, low=99.5, oi_usd=1_000_000, taker_buy_ratio=0.5)
        st.ingest_bar(T0 + 300, close=100.0, high=100.2, low=99.8, oi_usd=1_400_000, taker_buy_ratio=0.5)  # +400k
        cl = st.clusters(ref_price=100.0, now=T0 + 300, top_n=20)
        above = sorted([c for c in cl if c["side"] == "short"], key=lambda c: c["price"])
        below = sorted([c for c in cl if c["side"] == "long"], key=lambda c: c["price"])
        # 10x: 110 / 90 ; 50x: 102 / 98 ; each tier 200k split 50/50 -> 100k per side per tier
        self.assertEqual(len(above), 2)
        self.assertEqual(len(below), 2)
        self.assertAlmostEqual(above[0]["price"], 102.0, delta=0.06)
        self.assertAlmostEqual(above[1]["price"], 110.0, delta=0.06)
        self.assertAlmostEqual(below[1]["price"], 98.0, delta=0.06)
        self.assertAlmostEqual(below[0]["price"], 90.0, delta=0.06)
        for c in cl:
            self.assertAlmostEqual(c["notional"], 100_000, delta=1)
            self.assertFalse(c["burned"])

    def test_bar_trading_through_a_level_burns_it_and_restack_rearms(self):
        st = fm.FuelState("SOLUSDT", bucket_bps=5, decay_hours=0, tiers={50: 1.0})
        st.ingest_bar(T0, 100.0, 100.5, 99.5, 1_000_000, 0.5)
        st.ingest_bar(T0 + 300, 100.0, 100.2, 99.8, 1_200_000, 0.5)   # short cluster at 102, long at 98
        st.ingest_bar(T0 + 600, 101.0, 102.3, 100.9, 1_200_000, 0.5)  # bar high crosses 102 -> burned
        cl = st.clusters(ref_price=101.0, now=T0 + 600, top_n=20)
        shorts = [c for c in cl if c["side"] == "short"]
        self.assertEqual(len(shorts), 1)
        self.assertTrue(shorts[0]["burned"])
        # controller ignores burned clusters
        snap = fm.build_snapshot(st, 101.0, T0 + 600)
        sig = q.compute_fuel_signal(snap, 101.0, 0.004, T0 + 600, min_notional=1)
        self.assertIsNone(sig.up_cluster)
        # new OI stacked on that level re-arms it as fresh
        st.ingest_bar(T0 + 900, 100.0, 100.1, 99.9, 1_400_000, 0.5)  # 50x from 100 -> 102 again
        cl2 = st.clusters(ref_price=100.0, now=T0 + 900, top_n=20)
        shorts2 = [c for c in cl2 if c["side"] == "short" and abs(c["price"] - 102) < 0.1]
        self.assertEqual(len(shorts2), 1)
        self.assertFalse(shorts2[0]["burned"])
        self.assertAlmostEqual(shorts2[0]["notional"], 100_000, delta=1)  # only the fresh stack, old burned notional dropped

    def test_oi_decrease_shrinks_live_buckets_pro_rata_and_feeds_cascade_proxy(self):
        st = fm.FuelState("SOLUSDT", bucket_bps=5, decay_hours=0, tiers={50: 1.0})
        st.ingest_bar(T0, 100.0, 100.5, 99.5, 1_000_000, 0.5)
        st.ingest_bar(T0 + 300, 100.0, 100.2, 99.8, 1_400_000, 0.5)  # +400k -> 200k/side
        st.ingest_bar(T0 + 600, 100.0, 100.2, 99.8, 1_200_000, 0.5)  # -200k of 400k live -> halve
        cl = st.clusters(100.0, T0 + 600, top_n=20)
        for c in cl:
            self.assertAlmostEqual(c["notional"], 100_000, delta=1)
        casc = st.oi_drop_cascade()
        self.assertEqual(casc["liq_notional_60s"], 200_000)
        self.assertEqual(casc["source"], "oi_drop_proxy")

    def test_decay_reduces_effective_notional(self):
        st = fm.FuelState("SOLUSDT", bucket_bps=5, decay_hours=1.0, tiers={50: 1.0})
        st.ingest_bar(T0, 100.0, 100.5, 99.5, 1_000_000, 0.5)
        st.ingest_bar(T0 + 300, 100.0, 100.2, 99.8, 1_200_000, 0.5)
        fresh = st.clusters(100.0, T0 + 300, top_n=20)[0]["notional"]
        later = st.clusters(100.0, T0 + 300 + 3600, top_n=20)[0]["notional"]
        self.assertAlmostEqual(later / fresh, 2.718281828 ** -1, places=3)

    def test_snapshot_schema_is_what_the_controller_expects(self):
        st = fm.FuelState("SOLUSDT", tiers={25: 1.0})
        st.ingest_bar(T0, 75.0, 75.3, 74.7, 5_000_000, 0.6)
        st.ingest_bar(T0 + 300, 75.0, 75.2, 74.8, 7_000_000, 0.6)
        snap = fm.build_snapshot(st, 75.0, T0 + 300, top_n=5)
        for k in ("symbol", "ts", "ref_price", "clusters", "cascade", "source", "model"):
            self.assertIn(k, snap)
        self.assertEqual(snap["ts"], T0 + 300)
        sig = q.compute_fuel_signal(snap, 75.0, 0.004, T0 + 300, min_notional=1, reference_notional=1e6, fuse_natr=0.0)
        self.assertEqual(sig.state, q.FUEL_LIVE)
        self.assertIsNotNone(sig.up_cluster)
        self.assertIsNotNone(sig.down_cluster)
        # taker-buy 0.6 -> long share 0.6 -> the long cluster below carries 60% of the tier notional
        self.assertGreater(sig.down_cluster["notional"], sig.up_cluster["notional"])
        # ts far in the past -> controller says STALE, so a dead collector can never steer quotes
        self.assertEqual(q.compute_fuel_signal(snap, 75.0, 0.004, T0 + 300 + 10_000).state, q.FUEL_STALE)

    def test_bars_from_binance_joins_on_open_time_and_sorts(self):
        klines = [[(T0 + 300) * 1000, "0", "101", "99", "100.5", "1000", 0, "0", 0, "600", "0", "0"],
                  [T0 * 1000, "0", "100", "98", "99.5", "2000", 0, "0", 0, "1000", "0", "0"]]
        oi = [{"timestamp": (T0 + 300) * 1000, "sumOpenInterestValue": "1500000"},
              {"timestamp": T0 * 1000, "sumOpenInterestValue": "1400000"},
              {"timestamp": (T0 + 600) * 1000, "sumOpenInterestValue": "1600000"}]  # no kline -> dropped
        bars = fm.bars_from_binance(oi, klines)
        self.assertEqual([b["ts"] for b in bars], [T0, T0 + 300])
        self.assertAlmostEqual(bars[1]["taker_buy_ratio"], 0.6)
        self.assertEqual(bars[1]["oi_usd"], 1_500_000)

    def test_parse_tiers_normalises(self):
        t = fm.parse_tiers("10:1,20:3")
        self.assertAlmostEqual(t[10], 0.25)
        self.assertAlmostEqual(t[20], 0.75)


if __name__ == "__main__":
    unittest.main(verbosity=2)
