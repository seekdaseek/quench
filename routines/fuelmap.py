#!/usr/bin/env python3
"""
ROUTINE: fuelmap — read the liquidation fuel map and write a report.

Federico's definition, from The Bot Pod ep. 6: "Routines are just Python programs — very simple
Python programs — that do something and generate a report." That is exactly this file. It runs on a
slow clock, touches no trading loop, and its only output is a report on disk.

  python3 routines/fuelmap.py --snapshot data/SOLUSDT_fuel.json --out reports/SOLUSDT.json
  python3 routines/fuelmap.py --snapshot https://data.ochinimus.app/fuel/SOLUSDT.json --out ...

Input is one fuel snapshot, or a history file (a JSON list) in which case the LAST snapshot is the
current one and the rest supply the distribution the current reading is judged against. That
distinction matters: "the nearest cluster is 1.4 vol units away" means nothing until you know the
median is 5.6.

WHAT THE REPORT IS FOR: the agent reads it and decides whether to tilt the controller's quotes.
Nothing here decides anything. A routine reports; an agent decides; the controller quotes.

WHAT IT WILL NOT DO: invent a reading. Every field is either measured off the snapshot or absent.
A stale snapshot produces state="stale" and no tilt advice, because a fuel map from an hour ago is
not information about now — the same fail-safe the controller used to carry, kept at the layer that
now owns it.
"""
import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routines.fuel import model as fuel  # noqa: E402

STATE_LIVE = "live"
STATE_STALE = "stale"
STATE_EMPTY = "empty"


def load(src: str):
    """Return (snapshots, current). Accepts a path or an http(s) URL, one snapshot or a list."""
    if src.startswith("http://") or src.startswith("https://"):
        with urllib.request.urlopen(src, timeout=10) as r:
            data = json.load(r)
    else:
        with open(src) as fh:
            data = json.load(fh)
    if isinstance(data, dict):
        return [data], data
    if not data:
        raise ValueError("empty history")
    hist = sorted(data, key=lambda s: float(s["ts"]))
    return hist, hist[-1]


def distances(snapshots, min_notional, vol):
    """Distances to the nearest unburned cluster each side, in horizon-vol units, across the history.

    This is the backdrop. A single reading is not interpretable without it — the whole reason the
    per-tick fuse never fired is that 1.5 vol units was chosen without looking at where clusters
    actually sit (measured median: 5.57 up, 2.63 down).
    """
    ups, downs = [], []
    n_clusters = n_burned = 0
    for s in snapshots:
        ref = float(s["ref_price"])
        unit = ref * vol
        cl = s.get("clusters") or []
        n_clusters += len(cl)
        n_burned += sum(1 for c in cl if c.get("burned"))
        up = fuel.nearest_unburned(cl, ref, "short", min_notional)
        dn = fuel.nearest_unburned(cl, ref, "long", min_notional)
        if up:
            ups.append((float(up["price"]) - ref) / unit)
        if dn:
            downs.append((ref - float(dn["price"])) / unit)
    return ups, downs, n_clusters, n_burned


def pct(values, p):
    if not values:
        return None
    return round(float(statistics.quantiles(sorted(values), n=100)[min(p, 99) - 1]) if len(values) > 1
                 else values[0], 3)


def build_report(src, min_notional=250_000.0, vol=0.0046, max_age_s=900.0, now=None):
    snapshots, cur = load(src)
    now = float(now if now is not None else time.time())
    age = now - float(cur["ts"])
    ref = float(cur["ref_price"])
    unit = ref * vol
    cl = cur.get("clusters") or []

    rep = {
        "routine": "fuelmap",
        "version": 1,
        "symbol": cur.get("symbol"),
        "generated_at": round(now, 3),
        "snapshot_ts": float(cur["ts"]),
        "snapshot_age_s": round(age, 1),
        "ref_price": ref,
        "vol_unit_pct": round(vol * 100, 4),
        "snapshots_in_history": len(snapshots),
    }

    if age > max_age_s:
        rep["state"] = STATE_STALE
        rep["note"] = (f"snapshot is {age:.0f}s old, older than max_age {max_age_s:.0f}s. "
                       "A stale map is not information about now. No tilt advice.")
        return rep
    if not cl:
        rep["state"] = STATE_EMPTY
        rep["note"] = "snapshot carries no clusters. No tilt advice."
        return rep

    rep["state"] = STATE_LIVE
    up = fuel.nearest_unburned(cl, ref, "short", min_notional)
    dn = fuel.nearest_unburned(cl, ref, "long", min_notional)
    rep["nearest_above"] = None if not up else {
        "price": float(up["price"]), "notional": float(up["notional"]),
        "distance_vol_units": round((float(up["price"]) - ref) / unit, 3)}
    rep["nearest_below"] = None if not dn else {
        "price": float(dn["price"]), "notional": float(dn["notional"]),
        "distance_vol_units": round((ref - float(dn["price"])) / unit, 3)}

    ups, downs, n_clusters, n_burned = distances(snapshots, min_notional, vol)
    rep["backdrop"] = {
        "clusters_seen": n_clusters,
        "burned_pct": round(100.0 * n_burned / n_clusters, 1) if n_clusters else None,
        "snapshots_with_cluster_above_pct": round(100.0 * len(ups) / len(snapshots), 1),
        "snapshots_with_cluster_below_pct": round(100.0 * len(downs) / len(snapshots), 1),
        "above_p05": pct(ups, 5), "above_p50": pct(ups, 50),
        "below_p05": pct(downs, 5), "below_p50": pct(downs, 50),
    }

    casc = cur.get("cascade") or {}
    base = float(casc.get("baseline_60s") or 0)
    liq = float(casc.get("liq_notional_60s") or 0)
    rep["cascade"] = {
        "ratio": round(liq / base, 2) if base > 0 else None,
        "dominant_side": casc.get("dominant_side"),
        # An unattributed cascade is not cascade information. Measured Aug 15: the open-interest-drop
        # proxy reports dominant_side null 100% of the time, and acting on it was the entire cost of
        # the old fuel layer. The agent must be able to see that distinction, so it is in the report.
        "attributed": bool(casc.get("dominant_side")),
    }

    # How unusual is today's reading? This, not the raw distance, is what an agent should act on.
    def rank(x, dist):
        if x is None or not dist:
            return None
        return round(100.0 * sum(1 for d in dist if d < x) / len(dist), 1)

    rep["percentile_of_current"] = {
        "above": rank(rep["nearest_above"]["distance_vol_units"] if rep["nearest_above"] else None, ups),
        "below": rank(rep["nearest_below"]["distance_vol_units"] if rep["nearest_below"] else None, downs),
    }
    return rep


def to_text(rep) -> str:
    L = [f"fuelmap {rep.get('symbol')}  state={rep['state']}  age={rep['snapshot_age_s']}s  "
         f"ref={rep.get('ref_price')}  history={rep['snapshots_in_history']} snapshots"]
    if rep["state"] != STATE_LIVE:
        L.append("  " + rep.get("note", ""))
        return "\n".join(L)
    for side, key in (("above", "nearest_above"), ("below", "nearest_below")):
        c = rep.get(key)
        if not c:
            L.append(f"  nearest unburned {side}: none")
        else:
            p = rep["percentile_of_current"][side]
            L.append(f"  nearest unburned {side}: {c['distance_vol_units']} vol units, "
                     f"${c['notional']:,.0f}  (closer than usual: {p}th pct)")
    b = rep["backdrop"]
    L.append(f"  backdrop: {b['clusters_seen']} clusters, {b['burned_pct']}% burned, "
             f"median distance {b['above_p50']} up / {b['below_p50']} down")
    c = rep["cascade"]
    L.append(f"  cascade: ratio {c['ratio']}  side {c['dominant_side']}  attributed={c['attributed']}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True, help="path or http(s) URL to a fuel snapshot or history")
    ap.add_argument("--out", help="write the JSON report here")
    ap.add_argument("--min-notional", type=float, default=250_000)
    ap.add_argument("--vol", type=float, default=0.0046,
                    help="horizon volatility as a fraction of price, the unit distances are measured in")
    ap.add_argument("--max-age", type=float, default=900)
    a = ap.parse_args()
    rep = build_report(a.snapshot, min_notional=a.min_notional, vol=a.vol, max_age_s=a.max_age)
    print(to_text(rep))
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        tmp = a.out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(rep, fh, indent=2)
        os.replace(tmp, a.out)   # a reader never sees a half-written report
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
