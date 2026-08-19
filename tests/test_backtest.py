import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backtest"))
import harness  # noqa: E402

q = harness.load_quench_module()

START = 1_755_200_000  # arbitrary epoch seconds, minute aligned
N = 400               # 400 one-minute candles


def base_config(**over):
    cfg = dict(
        id="quench-test",
        connector_name="bitget_perpetual",
        trading_pair="SOL-USDT",
        candles_connector="binance_perpetual",
        candles_trading_pair="SOL-USDT",
        interval="1m",
        total_amount_quote=Decimal("800"),
        leverage=5,
        buy_spreads="1,2",
        sell_spreads="1,2",
        executor_refresh_time=60,
        cooldown_time=15,
        tp_natr=Decimal("1.0"),
        sl_natr=Decimal("3.0"),
        time_limit=900,
        natr_length=14,
        fuel_enabled=False,
    )
    cfg.update(over)
    return q.QuenchControllerConfig(**cfg)


class ConfigTests(unittest.TestCase):
    def test_config_defaults_and_symbol_derivation(self):
        c = base_config()
        self.assertEqual(c.controller_name, "quench")
        self.assertEqual(c.controller_type, "market_making")
        self.assertEqual(c.fuel_symbol, "SOLUSDT")
        self.assertEqual(c.candles_connector, "binance_perpetual")
        self.assertEqual(c.buy_spreads, [1.0, 2.0])
        c2 = q.QuenchControllerConfig(id="x", connector_name="gate_io_perpetual", trading_pair="SOL-USDT",
                                      total_amount_quote=Decimal("100"))
        self.assertEqual(c2.candles_connector, "gate_io_perpetual")  # falls back to the connector
        self.assertEqual(c2.fuel_symbol, "SOLUSDT")


class BacktestTests(unittest.TestCase):
    def test_baseline_pmm_runs_in_real_engine(self):
        candles = harness.synthetic_candles(START, N, seed=1)
        cfg = base_config()
        res = harness.run(cfg, candles)
        summ = harness.summarize(res)
        self.assertGreater(summ["executors"], 0, "no executors created")
        # both sides quoted
        sides = {e.side for e in res["executors"]}
        self.assertEqual(sides, {q.TradeType.BUY, q.TradeType.SELL})
        # every executor's entry price brackets the reference of its creation candle
        feats = res["processed_data"]["features"]
        for e in res["executors"][:50]:
            row = feats.loc[feats["timestamp"] == e.timestamp]
            if row.empty:
                continue
            ref = float(row["reference_price"].iloc[0])
            ep = float(e.config.entry_price)
            if e.side == q.TradeType.BUY:
                self.assertLess(ep, ref)
            else:
                self.assertGreater(ep, ref)
        self.assertEqual(res["processed_data"]["fuel_state"], q.FUEL_OFF)

    def test_fuel_layer_pulls_sells_inside_fuse_and_leans_up(self):
        # price drifts up from bar 150 to 260 into a big short-liquidation cluster parked above
        candles = harness.synthetic_candles(START, N, seed=3, vol=0.0008, drift_events=[(150, 350, 0.00015)])
        p150 = float(candles["close"].iloc[150])
        cluster_price = p150 * 1.02  # ~2% above where the drift starts; the drift covers ~3% over 200 bars
        history = [
            {"symbol": "SOLUSDT", "ts": START, "ref_price": p150,
             "clusters": [{"price": cluster_price, "side": "short", "notional": 4e6, "burned": False}],
             "cascade": {"liq_notional_60s": 0, "baseline_60s": 20000, "dominant_side": None}},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(history, fh)
            path = fh.name
        cfg = base_config(fuel_enabled=True, fuel_history_path=path, fuel_reference_notional=Decimal("2000000"),
                          fuse_natr=Decimal("0.35"), lean_horizon_natr=Decimal("3.0"), max_lean_natr=Decimal("0.5"),
                          fuel_horizon_minutes=60, vol_window=60)
        res = harness.run(cfg, candles)
        feats = res["processed_data"]["features"]
        # the fuel layer was live throughout (history snapshot present, never stale in backtest mode)
        self.assertTrue((feats["fuel_state"] == q.FUEL_LIVE).all())
        # somewhere the tape got inside the fuse window of the cluster -> pull_sell rows exist
        fuse_rows = feats[feats["pull_sell"] == 1]
        self.assertGreater(len(fuse_rows), 0, "fuse never engaged; drift did not reach the cluster")
        fuse_ts = set(fuse_rows["timestamp"].astype(float))
        sells_in_fuse = [e for e in res["executors"] if e.side == q.TradeType.SELL and float(e.timestamp) in fuse_ts]
        buys_in_fuse = [e for e in res["executors"] if e.side == q.TradeType.BUY and float(e.timestamp) in fuse_ts]
        self.assertEqual(len(sells_in_fuse), 0, "sell quotes were placed inside the fuse window")
        self.assertGreater(len(buys_in_fuse), 0, "buys should keep quoting inside the fuse window")
        # lean is toward the cluster (up) while it is unburned and within horizon
        near = feats[(feats["up_dist_natr"] > 0) & (feats["up_dist_natr"] < 3)]
        self.assertGreater(len(near), 0)
        self.assertTrue((near["fuel_shift"] > 0).all())
        self.assertLessEqual(float(near["fuel_shift"].max()), 0.5 * float(near["natr"].max()) + 1e-12)
        # the distance unit must be the HORIZON vol, materially larger than one bar's NATR
        self.assertGreater(float(feats["vol_dist"].median()), 3 * float(feats["natr"].median()))
        # once price is above the cluster it is 'crossed' => treated as burned => no lean, no pull
        above = feats[feats["close"] > cluster_price]
        if len(above):
            self.assertTrue((above["fuel_shift"] == 0).all())
            self.assertTrue((above["pull_sell"] == 0).all())
        os.unlink(path)

    def test_stale_history_neutralises_layer_live_mode_semantics(self):
        # In live mode the age is measured against wall time; simulate by calling compute directly through the
        # controller's helper with a stale snapshot.
        sig = q.compute_fuel_signal({"ts": 0, "clusters": [{"price": 1, "side": "short", "notional": 1e9}]},
                                    75.0, 0.004, 10_000.0, max_age_s=90)
        self.assertEqual(sig.state, q.FUEL_STALE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
