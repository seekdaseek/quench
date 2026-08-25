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
    )
    cfg.update(over)
    return q.QuenchControllerConfig(**cfg)


class ConfigTests(unittest.TestCase):
    def test_config_defaults_and_symbol_derivation(self):
        c = base_config()
        self.assertEqual(c.controller_name, "quench")
        self.assertEqual(c.controller_type, "market_making")
        self.assertEqual(c.candles_connector, "binance_perpetual")
        self.assertEqual(c.buy_spreads, [1.0, 2.0])
        c2 = q.QuenchControllerConfig(id="x", connector_name="gate_io_perpetual", trading_pair="SOL-USDT",
                                      total_amount_quote=Decimal("100"))
        self.assertEqual(c2.candles_connector, "gate_io_perpetual")  # falls back to the connector


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

    def test_controller_has_no_fuel_layer_and_no_network(self):
        """The split is the point: this file must stay quoting-only.

        Aug 25 2026 — the fuel layer moved to routines/ + agent/. Two engine tests that asserted the
        controller leaned and pulled on liquidation clusters were deleted with it; their subject is
        gone. The math they exercised is still covered, at the routine level, by test_fuel.py (10),
        test_units.py (3) and test_cascade.py (4). This test is what stops it creeping back in.
        """
        src = open(harness.CONTROLLER_PATH).read()
        body = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
        body = body.split('"""', 2)[-1]          # drop the module docstring, which discusses the split
        for banned in ("aiohttp", "compute_fuel_signal", "pull_buy", "pull_sell", "fuel_url", "cluster"):
            self.assertNotIn(banned, body, f"{banned!r} is back in the controller")
        cfg = base_config()
        for gone in ("fuel_enabled", "fuel_url", "fuse_natr", "cascade_ratio_brake", "max_lean_natr"):
            self.assertFalse(hasattr(cfg, gone), f"{gone} is back on the config")

    def test_agent_levers_are_all_framework_updatable(self):
        """Every parameter the agent writes must carry is_updatable, or the running bot ignores it.

        StrategyV2Base.update_controllers_configs() re-reads the YAML every config_update_interval
        seconds and calls ControllerBase.update_config(), which copies across ONLY the fields with
        is_updatable set. A lever without the flag is a silent no-op.
        """
        fields = q.QuenchControllerConfig.model_fields
        for lever in ("buy_spreads", "sell_spreads", "tp_spread_mult", "sl_spread_mult",
                      "inventory_skew_natr", "total_amount_quote", "buy_amounts_pct",
                      "sell_amounts_pct", "executor_refresh_time"):
            self.assertIn(lever, fields, f"{lever} is not a config field")
            extra = fields[lever].json_schema_extra or {}
            self.assertTrue(extra.get("is_updatable", False), f"{lever} is not is_updatable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
