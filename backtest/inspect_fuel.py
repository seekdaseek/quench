#!/usr/bin/env python3
"""
Inspect a fuel history file and report what the fuel layer can possibly do on that tape — in seconds,
without running a backtest.

  python3 backtest/inspect_fuel.py data/SOLUSDT_fuel.json

Answers three questions the sweep can only answer slowly:
  1. how often is there an unburned cluster within reach (so the LEAN can act)
  2. how often is one inside the fuse window (so the FUSE can act)
  3. how often does the CASCADE BRAKE fire, and on what evidence
"""
import argparse
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402

q = harness.load_fuel_module()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fuel")
    ap.add_argument("--lean-horizon", type=float, default=3.0)
    ap.add_argument("--fuse", type=float, default=1.5)
    ap.add_argument("--brake", type=float, default=3.0)
    ap.add_argument("--min-notional", type=float, default=250_000)
    ap.add_argument("--reference-notional", type=float, default=2_000_000)
    ap.add_argument("--vol", type=float, default=0.0046,
                    help="horizon volatility as a fraction of price, the unit distances are measured in "
                         "(SOL 1h realized vol is ~0.45%%; only used for the distance histogram)")
    a = ap.parse_args()
    with open(a.fuel) as fh:
        hist = json.load(fh)
    print(f"{len(hist)} snapshots  {hist[0]['symbol']}  "
          f"cadence {(float(hist[1]['ts']) - float(hist[0]['ts'])):.0f}s")

    ups, downs, ratios, n_up, n_down, n_any = [], [], [], 0, 0, 0
    in_lean, in_fuse, braked, dom_none = 0, 0, 0, 0
    n_clusters, n_burned = 0, 0
    for s in hist:
        ref = float(s["ref_price"])
        unit = ref * a.vol
        cl = s.get("clusters") or []
        n_clusters += len(cl)
        n_burned += sum(1 for c in cl if c.get("burned"))
        up = q.nearest_unburned(cl, ref, "short", a.min_notional)
        dn = q.nearest_unburned(cl, ref, "long", a.min_notional)
        du = (float(up["price"]) - ref) / unit if up else None
        dd = (ref - float(dn["price"])) / unit if dn else None
        if du is not None:
            ups.append(du); n_up += 1
        if dd is not None:
            downs.append(dd); n_down += 1
        if du is not None or dd is not None:
            n_any += 1
        if (du is not None and du < a.lean_horizon) or (dd is not None and dd < a.lean_horizon):
            in_lean += 1
        if (du is not None and du < a.fuse) or (dd is not None and dd < a.fuse):
            in_fuse += 1
        c = s.get("cascade") or {}
        base = float(c.get("baseline_60s") or 0)
        liq = float(c.get("liq_notional_60s") or 0)
        r = liq / base if base > 0 else 0.0
        ratios.append(r)
        if r >= a.brake:
            braked += 1
            if not c.get("dominant_side"):
                dom_none += 1

    n = len(hist)
    def pct(x):
        return f"{x} ({x / n * 100:.1f}%)"

    print(f"\nclusters: {n_clusters} total across snapshots, {n_burned} burned "
          f"({n_burned / max(1, n_clusters) * 100:.0f}%)")
    print(f"snapshots with an unburned cluster above: {pct(n_up)}   below: {pct(n_down)}   either: {pct(n_any)}")
    for name, arr in (("above", ups), ("below", downs)):
        if arr:
            arr = sorted(arr)
            print(f"  distance {name} (horizon-vol units): p05 {arr[int(.05*len(arr))]:.2f}  "
                  f"p25 {arr[int(.25*len(arr))]:.2f}  median {statistics.median(arr):.2f}  "
                  f"p75 {arr[int(.75*len(arr))]:.2f}")
    print(f"\nLEAN can act (cluster within lean_horizon={a.lean_horizon}): {pct(in_lean)}")
    print(f"FUSE can act (cluster within fuse={a.fuse}):               {pct(in_fuse)}")
    src = (hist[0].get("cascade") or {}).get("source", "?")
    print(f"\nCASCADE BRAKE fires (ratio >= {a.brake}): {pct(braked)}   source: {src}")
    if braked:
        print(f"  of those, dominant_side is null in {dom_none} ({dom_none / braked * 100:.0f}%) "
              f"-> the brake widens spreads and halves size but can never pull a side")
    if ratios:
        rs = sorted(ratios)
        print(f"  ratio distribution: median {statistics.median(rs):.2f}  p90 {rs[int(.9*len(rs))]:.2f}  "
              f"max {rs[-1]:.2f}")
    if src == "oi_drop_proxy":
        print("  NOTE: this is the open-interest-drop proxy, not realized liquidations. A 5m drop in OI is "
              "closed positions of every kind. Wire LIQ_SQLITE/LIQ_SQL to a real liquidation table before "
              "trusting the brake, or raise cascade_ratio_brake so it never fires.")


if __name__ == "__main__":
    main()
