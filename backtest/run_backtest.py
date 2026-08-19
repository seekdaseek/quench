#!/usr/bin/env python3
"""
Run the quench controller through Hummingbot's real V2 backtesting engine, offline.

  python3 backtest/run_backtest.py --synthetic                          # smoke run, no data needed
  python3 backtest/run_backtest.py --csv data/SOLUSDT_1m.csv --fuel data/SOLUSDT_fuel.json
  python3 backtest/run_backtest.py --csv data/SOLUSDT_1m.csv           # same tape, fuel layer OFF (the control)

Prints the engine's own summary for both. Compare fuel ON vs OFF on the same tape — that is the claim.
"""
import argparse, os, sys, json
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402

q = harness.load_quench_module()


def build_config(a, fuel_on: bool):
    kw = dict(
        id="quench-bt",
        connector_name=a.connector, trading_pair=a.pair,
        candles_connector="binance_perpetual", candles_trading_pair=a.pair, interval="1m",
        total_amount_quote=Decimal(str(a.quote)), leverage=a.leverage,
        buy_spreads=a.spreads, sell_spreads=a.spreads,
        executor_refresh_time=a.refresh, cooldown_time=15, time_limit=a.time_limit,
        tp_natr=Decimal(str(a.tp)), sl_natr=Decimal(str(a.sl)), natr_length=14,
        fuel_enabled=fuel_on,
    )
    if fuel_on and a.fuel:
        kw["fuel_history_path"] = a.fuel
    return q.QuenchControllerConfig(**kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv"); ap.add_argument("--fuel"); ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--connector", default="bitget_perpetual"); ap.add_argument("--pair", default="SOL-USDT")
    ap.add_argument("--quote", type=float, default=800); ap.add_argument("--leverage", type=int, default=5)
    ap.add_argument("--spreads", default="1,2"); ap.add_argument("--refresh", type=int, default=60)
    ap.add_argument("--time-limit", type=int, default=900); ap.add_argument("--tp", type=float, default=1.0)
    ap.add_argument("--sl", type=float, default=3.0); ap.add_argument("--trade-cost", type=float, default=0.0002)
    ap.add_argument("--json", help="write full summaries here")
    a = ap.parse_args()
    if a.synthetic:
        candles = harness.synthetic_candles(1_755_200_000, 600, seed=11)
    elif a.csv:
        candles = harness.load_candles_csv(a.csv)
    else:
        ap.error("--csv or --synthetic")
    out = {}
    for label, fuel_on in (("fuel_off", False), ("fuel_on", True)):
        if fuel_on and not a.fuel and not a.synthetic:
            print("fuel_on skipped: no --fuel history given"); continue
        cfg = build_config(a, fuel_on)
        res = harness.run(cfg, candles, trade_cost=a.trade_cost)
        summ = harness.summarize(res)
        feats = res["processed_data"]["features"]
        summ["fuel_states"] = feats["fuel_state"].value_counts().to_dict() if "fuel_state" in feats else {}
        summ["rows_pull_buy"] = int(feats["pull_buy"].sum()) if "pull_buy" in feats else 0
        summ["rows_pull_sell"] = int(feats["pull_sell"].sum()) if "pull_sell" in feats else 0
        out[label] = summ
        print(f"\n== {label} ==")
        for k, v in summ.items():
            print(f"  {k}: {v}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
