import importlib.util, os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("fuelmap_service", os.path.join(HERE, "..", "service", "fuelmap_service.py"))
fm = importlib.util.module_from_spec(spec); spec.loader.exec_module(fm)

class Replay(unittest.TestCase):
    def test_replay_has_no_lookahead_and_emits_after_warmup(self):
        T0 = 1_755_200_000
        bars = []
        oi = 1_000_000.0
        p = 100.0
        for i in range(60):
            oi += 20_000 if i % 3 else -10_000
            p *= 1.0005 if i % 2 else 0.9997
            bars.append({"ts": T0 + i * 300, "close": p, "high": p * 1.001, "low": p * 0.999, "oi_usd": oi, "taker_buy_ratio": 0.5})
        hist = fm.replay_history(bars, "SOLUSDT", tiers={50: 1.0}, warmup_bars=10)
        self.assertEqual(len(hist), 50)
        self.assertEqual(hist[0]["ts"], bars[10]["ts"])
        # snapshot i must equal a fresh model fed only bars[:i+1]  (no look-ahead)
        st = fm.FuelState("SOLUSDT", tiers={50: 1.0})
        for b in bars[:31]:
            st.ingest_bar(b["ts"], b["close"], b["high"], b["low"], b["oi_usd"], b["taker_buy_ratio"])
        direct = fm.build_snapshot(st, bars[30]["close"], bars[30]["ts"], source="binance_oi_tiers_v1_replay")
        self.assertEqual(direct["clusters"], hist[20]["clusters"])

if __name__ == "__main__":
    unittest.main()
