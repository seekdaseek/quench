#!/usr/bin/env python3
"""
fuelmap_service — publishes liquidation "fuel map" snapshots for the quench controller.

Model (stated plainly — this is an estimate, not observed liquidation orders):
  Every 5m bar where USDT-M open interest INCREASES, the increase is treated as new leverage opened
  near that bar's price. It is split across leverage tiers (default 10x/25x/50x/100x) and across
  long/short by the bar's taker-buy ratio, and projected to liquidation prices P*(1 -/+ 1/L). Those
  notionals accumulate in price buckets. When OI decreases, buckets shrink pro-rata (positions
  closed). When the tape trades THROUGH a bucket (bar high/low crosses it) the bucket is BURNED —
  the leverage that sat there is gone. Snapshots list the largest unburned buckets above ("short"
  clusters) and below ("long" clusters) the current price.

Cascade metric: realized liquidations if a liquidation table is configured (LIQ_SQLITE + LIQ_SQL),
otherwise an OI-drop proxy (notional of OI removed in the last 5m bar vs its rolling baseline).

I/O: Binance USDT-M public REST (no key). Output: OUT_DIR/{symbol}.json rewritten atomically every
INTERVAL seconds, and optionally served over HTTP on SERVE_PORT.

Env:
  SYMBOLS=SOLUSDT,BTCUSDT,ETHUSDT   OUT_DIR=./out   INTERVAL=20   SERVE_PORT=0 (0 = don't serve)
  BINANCE_FAPI=https://fapi.binance.com   TIERS=10:0.3,25:0.3,50:0.25,100:0.15
  BUCKET_BPS=5   DECAY_HOURS=12   TOP_N=12   OI_LIMIT=500
  LIQ_SQLITE=/path/liq.db   LIQ_SQL="SELECT ts, notional, side FROM liq WHERE symbol=? AND ts>=?"
  HISTORY_DIR=./history  (optional: append every snapshot to {symbol}.history.jsonl for replays)
"""
import json
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

DEFAULT_TIERS = {10: 0.30, 25: 0.30, 50: 0.25, 100: 0.15}


def parse_tiers(spec: str) -> Dict[int, float]:
    if not spec:
        return dict(DEFAULT_TIERS)
    out = {}
    for part in spec.split(","):
        lev, w = part.split(":")
        out[int(lev)] = float(w)
    total = sum(out.values())
    return {k: v / total for k, v in out.items()}


# --------------------------------------------------------------------------------------------------
# Pure model
# --------------------------------------------------------------------------------------------------

@dataclass
class Bucket:
    price: float
    side: str            # "long" (below entry) or "short" (above entry)
    notional: float
    born_ts: float
    burned: bool = False


@dataclass
class FuelState:
    symbol: str
    bucket_bps: float = 5.0
    decay_hours: float = 12.0
    tiers: Dict[int, float] = field(default_factory=lambda: dict(DEFAULT_TIERS))
    buckets: Dict[Tuple[str, int], Bucket] = field(default_factory=dict)
    last_oi_usd: Optional[float] = None
    last_bar_ts: Optional[float] = None
    oi_drop_history: List[float] = field(default_factory=list)  # notional removed per bar (proxy for forced closes)

    # ---- helpers
    def _bucket_key(self, price: float, side: str) -> Tuple[str, int]:
        # bucket by log-price so bins are proportional (bucket_bps wide at any price level)
        idx = int(math.floor(math.log(price) / (self.bucket_bps / 10_000.0)))
        return (side, idx)

    def _bucket_price(self, key: Tuple[str, int]) -> float:
        return math.exp((key[1] + 0.5) * (self.bucket_bps / 10_000.0))

    def ingest_bar(self, ts: float, close: float, high: float, low: float, oi_usd: float,
                   taker_buy_ratio: float = 0.5):
        """Feed one 5m bar (in chronological order)."""
        if self.last_bar_ts is not None and ts <= self.last_bar_ts:
            return
        # 1. burn anything the bar traded through
        for key, b in self.buckets.items():
            if not b.burned and low <= b.price <= high:
                b.burned = True
        # 2. OI change
        if self.last_oi_usd is not None:
            delta = oi_usd - self.last_oi_usd
            if delta > 0:
                long_share = min(1.0, max(0.0, taker_buy_ratio))
                for lev, w in self.tiers.items():
                    notional = delta * w
                    lp = close * (1.0 - 1.0 / lev)
                    sp = close * (1.0 + 1.0 / lev)
                    self._add(lp, "long", notional * long_share, ts)
                    self._add(sp, "short", notional * (1.0 - long_share), ts)
                self.oi_drop_history.append(0.0)
            elif delta < 0:
                removed = -delta
                self.oi_drop_history.append(removed)
                self._shrink_pro_rata(removed)
            else:
                self.oi_drop_history.append(0.0)
            self.oi_drop_history = self.oi_drop_history[-288:]  # 24h of 5m bars
        self.last_oi_usd = oi_usd
        self.last_bar_ts = ts
        # 3. time decay + housekeeping
        self._decay(ts)

    def _add(self, price: float, side: str, notional: float, ts: float):
        if notional <= 0 or price <= 0:
            return
        key = self._bucket_key(price, side)
        b = self.buckets.get(key)
        if b is None:
            self.buckets[key] = Bucket(price=self._bucket_price(key), side=side, notional=notional, born_ts=ts)
        else:
            if b.burned:
                # new leverage stacked on a burned level re-arms it (fresh positions)
                b.burned = False
                b.born_ts = ts
                b.notional = 0.0
            b.notional += notional

    def _shrink_pro_rata(self, removed: float):
        live = [b for b in self.buckets.values() if not b.burned]
        total = sum(b.notional for b in live)
        if total <= 0:
            return
        f = max(0.0, 1.0 - removed / total)
        for b in live:
            b.notional *= f

    def _decay(self, now: float):
        if self.decay_hours <= 0:
            return
        tau = self.decay_hours * 3600.0
        dead = []
        for key, b in self.buckets.items():
            age = max(0.0, now - b.born_ts)
            # exponential decay applied incrementally per ingest via a snapshot factor would double-count;
            # instead store notional as "born notional" and compute the effective value at read time.
            if b.burned and age > tau:
                dead.append(key)
        for k in dead:
            del self.buckets[k]

    def effective(self, b: Bucket, now: float) -> float:
        if self.decay_hours <= 0:
            return b.notional
        return b.notional * math.exp(-max(0.0, now - b.born_ts) / (self.decay_hours * 3600.0))

    def clusters(self, ref_price: float, now: float, top_n: int = 12, min_notional: float = 0.0) -> List[dict]:
        above, below = [], []
        for b in self.buckets.values():
            eff = self.effective(b, now)
            if eff < min_notional:
                continue
            item = {"price": round(b.price, 6), "side": b.side, "notional": round(eff, 2), "burned": bool(b.burned),
                    "age_s": int(max(0.0, now - b.born_ts))}
            if b.side == "short" and b.price > ref_price:
                above.append(item)
            elif b.side == "long" and b.price < ref_price:
                below.append(item)
        above.sort(key=lambda x: -x["notional"])
        below.sort(key=lambda x: -x["notional"])
        return above[:top_n] + below[:top_n]

    def oi_drop_cascade(self) -> dict:
        hist = self.oi_drop_history
        if len(hist) < 2:
            return {"liq_notional_60s": 0.0, "baseline_60s": 0.0, "dominant_side": None, "source": "oi_drop_proxy"}
        last = hist[-1]
        base_vals = [v for v in hist[:-1] if v > 0]
        baseline = (sum(base_vals) / len(base_vals)) if base_vals else 0.0
        return {"liq_notional_60s": round(last, 2), "baseline_60s": round(baseline, 2), "dominant_side": None,
                "source": "oi_drop_proxy"}


def build_snapshot(state: FuelState, ref_price: float, now: float, cascade: Optional[dict] = None,
                   top_n: int = 12, min_notional: float = 0.0, source: str = "binance_oi_tiers_v1") -> dict:
    return {
        "symbol": state.symbol,
        "ts": int(now),
        "ref_price": ref_price,
        "source": source,
        "model": {"tiers": {str(k): v for k, v in state.tiers.items()}, "bucket_bps": state.bucket_bps,
                  "decay_hours": state.decay_hours},
        "clusters": state.clusters(ref_price, now, top_n=top_n, min_notional=min_notional),
        "cascade": cascade if cascade is not None else state.oi_drop_cascade(),
    }


# --------------------------------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------------------------------

def _get_json(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": "quench-fuelmap/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_oi_hist(base: str, symbol: str, limit: int = 500, period: str = "5m",
                  start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> List[dict]:
    params = {"symbol": symbol, "period": period, "limit": min(limit, 500)}
    if start_ms:
        params["startTime"] = int(start_ms)
    if end_ms:
        params["endTime"] = int(end_ms)
    return _get_json(f"{base}/futures/data/openInterestHist?{urllib.parse.urlencode(params)}")


def fetch_klines(base: str, symbol: str, limit: int = 500, interval: str = "5m",
                 start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> List[list]:
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1500)}
    if start_ms:
        params["startTime"] = int(start_ms)
    if end_ms:
        params["endTime"] = int(end_ms)
    return _get_json(f"{base}/fapi/v1/klines?{urllib.parse.urlencode(params)}")


def fetch_range(base: str, symbol: str, start_s: int, end_s: int, period: str = "5m") -> List[dict]:
    """Page OI history + klines over [start_s, end_s] (Binance caps OI history at ~30 days) and join to bars."""
    step_s = 300 if period == "5m" else 60
    oi_rows, kl_rows = [], []
    t = start_s * 1000
    while t < end_s * 1000:
        chunk_end = min(end_s * 1000, t + 500 * step_s * 1000)
        oi_rows += fetch_oi_hist(base, symbol, 500, period, start_ms=t, end_ms=chunk_end)
        kl_rows += fetch_klines(base, symbol, 500, period, start_ms=t, end_ms=chunk_end)
        t = chunk_end + 1
        time.sleep(0.25)
    return bars_from_binance(oi_rows, kl_rows)


def replay_history(bars: List[dict], symbol: str, tiers: Optional[Dict[int, float]] = None, bucket_bps: float = 5.0,
                   decay_hours: float = 12.0, top_n: int = 12, warmup_bars: int = 48) -> List[dict]:
    """
    Deterministic reconstruction of the fuel map through time from historical bars: the snapshot at bar i uses
    only bars <= i (no look-ahead). Emits one snapshot per bar after warmup, ts = bar open time, ref = bar close.
    """
    st = FuelState(symbol=symbol, bucket_bps=bucket_bps, decay_hours=decay_hours, tiers=tiers or dict(DEFAULT_TIERS))
    out = []
    for i, b in enumerate(bars):
        st.ingest_bar(b["ts"], b["close"], b["high"], b["low"], b["oi_usd"], b.get("taker_buy_ratio", 0.5))
        if i >= warmup_bars:
            out.append(build_snapshot(st, b["close"], b["ts"], top_n=top_n, source="binance_oi_tiers_v1_replay"))
    return out


def fetch_mark_price(base: str, symbol: str) -> float:
    q = urllib.parse.urlencode({"symbol": symbol})
    d = _get_json(f"{base}/fapi/v1/premiumIndex?{q}")
    return float(d["markPrice"])


def bars_from_binance(oi_rows: List[dict], klines: List[list]) -> List[dict]:
    """Join OI history (per 5m, timestamp = bar open ms) with klines on the bar timestamp."""
    by_ts = {}
    for k in klines:
        # [openTime, open, high, low, close, volume, closeTime, quoteVol, trades, takerBuyBase, takerBuyQuote, ignore]
        open_ts = int(k[0]) // 1000
        vol = float(k[5])
        tb = float(k[9])
        by_ts[open_ts] = {"ts": open_ts, "high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
                          "taker_buy_ratio": (tb / vol) if vol > 0 else 0.5}
    out = []
    for r in oi_rows:
        ts = int(r["timestamp"]) // 1000
        bar = by_ts.get(ts)
        if bar is None:
            continue
        out.append({**bar, "oi_usd": float(r["sumOpenInterestValue"])})
    out.sort(key=lambda b: b["ts"])
    return out


def liq_cascade_from_sqlite(path: str, sql: str, symbol: str, now: float) -> Optional[dict]:
    """
    Realized-liquidation cascade metric from a local table. sql must accept (symbol, since_ts) and return rows
    of (ts_seconds, notional_usd, side) where side is 'long' or 'short' (the liquidated side).
    """
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(sql, (symbol, now - 3600)).fetchall()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:160], "source": "liq_sqlite"}
    last60 = [r for r in rows if float(r[0]) >= now - 60]
    liq60 = sum(float(r[1]) for r in last60)
    hour = sum(float(r[1]) for r in rows)
    baseline = hour / 60.0  # per-minute average over the last hour
    long_n = sum(float(r[1]) for r in last60 if str(r[2]).lower().startswith("l"))
    short_n = liq60 - long_n
    dom = None
    if liq60 > 0:
        dom = "long" if long_n >= short_n else "short"
    return {"liq_notional_60s": round(liq60, 2), "baseline_60s": round(baseline, 2), "dominant_side": dom,
            "source": "liq_sqlite"}


def atomic_write(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, path)


class _Handler(BaseHTTPRequestHandler):
    out_dir = "."

    def do_GET(self):  # noqa: N802
        name = os.path.basename(self.path.split("?")[0])
        if not name.endswith(".json"):
            name = name + ".json"
        p = os.path.join(self.out_dir, name)
        if not os.path.isfile(p):
            self.send_response(404)
            self.end_headers()
            return
        body = open(p, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        return


def serve(out_dir: str, port: int):
    _Handler.out_dir = out_dir
    srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def main():
    symbols = [s.strip().upper() for s in os.environ.get("SYMBOLS", "SOLUSDT,BTCUSDT,ETHUSDT").split(",") if s.strip()]
    out_dir = os.environ.get("OUT_DIR", "./out")
    interval = float(os.environ.get("INTERVAL", "20"))
    base = os.environ.get("BINANCE_FAPI", "https://fapi.binance.com")
    tiers = parse_tiers(os.environ.get("TIERS", ""))
    bucket_bps = float(os.environ.get("BUCKET_BPS", "5"))
    decay_hours = float(os.environ.get("DECAY_HOURS", "12"))
    top_n = int(os.environ.get("TOP_N", "12"))
    oi_limit = int(os.environ.get("OI_LIMIT", "500"))
    port = int(os.environ.get("SERVE_PORT", "0"))
    liq_db = os.environ.get("LIQ_SQLITE", "")
    liq_sql = os.environ.get("LIQ_SQL", "")
    history_dir = os.environ.get("HISTORY_DIR", "")
    os.makedirs(out_dir, exist_ok=True)
    if history_dir:
        os.makedirs(history_dir, exist_ok=True)
    if port:
        serve(out_dir, port)
        print(f"[fuelmap] serving {out_dir} on :{port}", flush=True)
    states = {s: FuelState(symbol=s, bucket_bps=bucket_bps, decay_hours=decay_hours, tiers=tiers) for s in symbols}
    print(f"[fuelmap] symbols={symbols} interval={interval}s tiers={tiers} bucket={bucket_bps}bps decay={decay_hours}h", flush=True)
    while True:
        t0 = time.time()
        for sym in symbols:
            st = states[sym]
            try:
                oi = fetch_oi_hist(base, sym, limit=oi_limit)
                kl = fetch_klines(base, sym, limit=oi_limit)
                for bar in bars_from_binance(oi, kl):
                    st.ingest_bar(bar["ts"], bar["close"], bar["high"], bar["low"], bar["oi_usd"], bar["taker_buy_ratio"])
                ref = fetch_mark_price(base, sym)
                now = time.time()
                cascade = None
                if liq_db and liq_sql:
                    cascade = liq_cascade_from_sqlite(liq_db, liq_sql, sym, now)
                snap = build_snapshot(st, ref, now, cascade=cascade, top_n=top_n)
                atomic_write(os.path.join(out_dir, f"{sym}.json"), snap)
                if history_dir:
                    with open(os.path.join(history_dir, f"{sym}.history.jsonl"), "a") as fh:
                        fh.write(json.dumps(snap, separators=(",", ":")) + "\n")
                n_live = sum(1 for b in st.buckets.values() if not b.burned)
                print(f"[fuelmap] {sym} ref={ref:.4f} buckets={len(st.buckets)} live={n_live} clusters={len(snap['clusters'])} "
                      f"cascade={snap['cascade'].get('liq_notional_60s')}/{snap['cascade'].get('baseline_60s')}", flush=True)
            except Exception as e:  # noqa: BLE001
                # write nothing new: consumers see the old ts and go STALE by themselves — never publish a fake map
                print(f"[fuelmap] {sym} ERROR {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(max(1.0, interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
