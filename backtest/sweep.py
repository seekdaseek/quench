#!/usr/bin/env python3
"""
Sweep quench parameters on one tape and print a table.

The column that decides everything is gross_bps: net PnL plus the fees the simulator charged
(cum_fees_quote = 2 * trade_cost * filled_amount_quote), divided by filled volume, in bps. That is the
edge before fees. A configuration only survives if gross_bps > 2 * maker_fee_bps of the venue.

  python3 backtest/sweep.py --csv data/SOLUSDT_1m.csv --fuel data/SOLUSDT_fuel.json
  python3 backtest/sweep.py --csv ... --fuel ... --tail-days 7 --spreads 1,2 3,6 5,10
"""
import argparse
import itertools
import json
import math
import os
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402

q = harness.load_quench_module()


def one_run(candles, spreads, tpm, slm, tl, refresh, trade_cost, connector, pair, quote,
            leverage, fee_bps):
    kw = dict(id="sweep", connector_name=connector, trading_pair=pair,
              candles_connector="binance_perpetual", candles_trading_pair=pair, interval="1m",
              total_amount_quote=Decimal(str(quote)), leverage=leverage,
              buy_spreads=spreads, sell_spreads=spreads,
              executor_refresh_time=refresh, cooldown_time=15, time_limit=tl,
              tp_spread_mult=Decimal(str(tpm)), sl_spread_mult=Decimal(str(slm)), natr_length=14,
              fee_bps_per_side=Decimal(str(fee_bps)), skip_rebalance=True)
    cfg = q.QuenchControllerConfig(**kw)
    res = harness.run(cfg, candles, trade_cost=trade_cost)
    r = res["results"]
    stats = trade_stats(res["executors"], candles)
    vol = float(r.get("total_volume", 0.0) or 0.0)
    net = float(r.get("net_pnl_quote", 0.0) or 0.0)
    fees = 2.0 * trade_cost * vol
    fills = int(r.get("total_executors_with_position", 0) or 0)
    return {
        "spreads": spreads, "tpm": tpm, "slm": slm, "tl": tl, "refresh": refresh,
        "cost_bps": trade_cost * 1e4,
        "fills": fills, "volume": round(vol, 0), "net": round(net, 2), "fees": round(fees, 2),
        "gross": round(net + fees, 2),
        "gross_bps": round((net + fees) / vol * 1e4, 3) if vol else 0.0,
        "net_bps": round(net / vol * 1e4, 3) if vol else 0.0,
        "dd": round(float(r.get("max_drawdown_usd", 0) or 0), 2),
        "acc": round(float(r.get("accuracy", 0) or 0), 3),
        **stats,
    }


def trade_stats(executors, candles):
    """
    Per-trade significance and a walk-forward split, both free from the run we already did.

    A positive net over a handful of fills is not evidence. t is the mean gross edge divided by its
    standard error: below ~2 the result is indistinguishable from luck no matter how good the total
    looks. h1/h2 split the tape in half — an edge that lives entirely in one half is a regime, not
    an edge.
    """
    import numpy as np
    rows = [(float(e.timestamp), float(e.net_pnl_quote), float(e.cum_fees_quote), float(e.filled_amount_quote))
            for e in executors if float(e.filled_amount_quote) > 0]
    if len(rows) < 2:
        return {"n": len(rows), "t": None, "sd_bps": None, "h1": None, "h2": None}
    ts = np.array([r[0] for r in rows])
    gross_bps = np.array([(r[1] + r[2]) / r[3] * 1e4 for r in rows])
    net_q = np.array([r[1] for r in rows])
    sd = float(gross_bps.std(ddof=1))
    t = float(gross_bps.mean() / (sd / math.sqrt(len(rows)))) if sd > 0 else None
    mid = float(candles["timestamp"].iloc[len(candles) // 2])
    return {"n": len(rows), "t": round(t, 2) if t is not None else None, "sd_bps": round(sd, 1),
            "h1": round(float(net_q[ts < mid].sum()), 2), "h2": round(float(net_q[ts >= mid].sum()), 2)}


def _pct(feats, col, p):
    if col not in feats:
        return None
    v = feats[col]
    v = v[v > 0]
    return round(float(v.quantile(p / 100.0)), 3) if len(v) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--fuel")
    ap.add_argument("--tail-days", type=float, default=0, help="use only the last N days of the CSV")
    ap.add_argument("--spreads", nargs="+", default=["1,2", "2,4", "3,6", "5,10"])
    ap.add_argument("--tp-mult", nargs="+", type=float, default=[1.0, 2.0],
                    help="take profit as a multiple of the filled level's spread")
    ap.add_argument("--sl-mult", nargs="+", type=float, default=[2.0, 3.0],
                    help="stop loss as a multiple of the filled level's spread")
    ap.add_argument("--fee-bps", type=float, default=2.0, help="maker fee per side, used for the tp floor")
    ap.add_argument("--tl", nargs="+", type=int, default=[900])
    ap.add_argument("--refresh", nargs="+", type=int, default=[60],
                    help="executor_refresh_time: a wide quote needs longer to fill, sweep it with the spreads")
    ap.add_argument("--costs", nargs="+", type=float, default=[0.0002])
    ap.add_argument("--connector", default="bitget_perpetual")
    ap.add_argument("--pair", default="SOL-USDT")
    ap.add_argument("--quote", type=float, default=800)
    ap.add_argument("--leverage", type=int, default=5)
    ap.add_argument("--json", default="sweep.json")
    a = ap.parse_args()

    candles = harness.load_candles_csv(a.csv)
    if a.tail_days:
        cutoff = candles["timestamp"].iloc[-1] - a.tail_days * 86400
        candles = candles[candles["timestamp"] >= cutoff].reset_index(drop=True)
    print(f"tape: {len(candles)} candles  {time.strftime('%Y-%m-%d %H:%M', time.gmtime(candles['timestamp'].iloc[0]))}"
          f" -> {time.strftime('%Y-%m-%d %H:%M', time.gmtime(candles['timestamp'].iloc[-1]))} UTC")
    grid = list(itertools.product(a.spreads, a.tp_mult, a.sl_mult, a.tl, a.refresh, a.costs))
    print(f"{len(grid)} runs\n")
    hdr = (f"{'spreads':>8} {'tpX':>4} {'slX':>4} {'tl':>5} {'refr':>5} {'cost':>5} {'fills':>6} "
           f"{'gross$':>9} {'fees$':>8} {'net$':>9} {'gross_bps':>9} {'dd$':>8} {'t':>6} {'h1$':>8} {'h2$':>8}")
    print(hdr)
    print("-" * len(hdr))
    rows = []
    for spreads, tpm, slm, tl, refresh, cost in grid:
        try:
            r = one_run(candles, spreads, tpm, slm, tl, refresh, cost, a.connector, a.pair,
                        a.quote, a.leverage, a.fee_bps)
        except Exception as e:  # noqa: BLE001
            print(f"{spreads:>8} {tpm:>4} {slm:>4} {tl:>5} {refresh:>5} {cost*1e4:>5}  ERROR {type(e).__name__}: {e}")
            continue
        rows.append(r)
        print(f"{r['spreads']:>8} {r['tpm']:>4} {r['slm']:>4} {r['tl']:>5} {r['refresh']:>5} {r['cost_bps']:>5.1f} "
              f"{r['fills']:>6} {r['gross']:>9} {r['fees']:>8} {r['net']:>9} {r['gross_bps']:>9} {r['dd']:>8} "
              f"{str(r['t']):>6} {str(r['h1']):>8} {str(r['h2']):>8}", flush=True)
    rows.sort(key=lambda x: -x["net"])
    print("\nbest by net:")
    for r in rows[:5]:
        print(f"  spreads={r['spreads']} tpX={r['tpm']} slX={r['slm']} "
              f"net=${r['net']} gross_bps={r['gross_bps']} fills={r['fills']} t={r['t']} h1=${r['h1']} h2=${r['h2']} dd=${r['dd']}")
    solid = [r for r in rows if r["t"] and r["t"] >= 2 and r["h1"] and r["h2"] and r["h1"] > 0 and r["h2"] > 0]
    print(f"\nconfigs with t>=2 AND both halves positive: {len(solid)}"
          + ("" if solid else "  <- nothing here is distinguishable from luck yet"))
    for r in solid[:5]:
        print(f"  spreads={r['spreads']} tpX={r['tpm']} slX={r['slm']} net=${r['net']} t={r['t']} n={r['n']}")
    if rows:
        best_gross = max(rows, key=lambda x: x["gross_bps"])
        print(f"\nhighest gross edge: spreads={best_gross['spreads']} tpX={best_gross['tpm']} -> {best_gross['gross_bps']} bps "
              f"per round trip; breaks even at a maker fee of {best_gross['gross_bps']/2:.2f} bps per side")
    with open(a.json, "w") as fh:
        json.dump(rows, fh, indent=2)
    print(f"wrote {a.json}")


if __name__ == "__main__":
    main()
