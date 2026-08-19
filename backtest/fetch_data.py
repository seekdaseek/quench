#!/usr/bin/env python3
"""
Pull real Binance USDT-M data for a backtest (run where Binance is reachable, e.g. the Mac):
  1m klines  -> data/{SYMBOL}_1m.csv            (timestamp,open,high,low,close,volume,... epoch seconds)
  fuel replay -> data/{SYMBOL}_fuel.json         (deterministic no-look-ahead snapshots at 5m cadence)

usage: python3 backtest/fetch_data.py --symbol SOLUSDT --days 14
"""
import argparse, importlib.util, json, os, sys, time
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("fuelmap_service", os.path.join(HERE, "..", "service", "fuelmap_service.py"))
fm = importlib.util.module_from_spec(spec); spec.loader.exec_module(fm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SOLUSDT")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--base", default="https://fapi.binance.com")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "data"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    end = int(time.time()) // 60 * 60
    start = end - a.days * 86400
    # 1m klines, paged 1500 at a time
    rows = []
    t = start * 1000
    while t < end * 1000:
        chunk = fm.fetch_klines(a.base, a.symbol, limit=1500, interval="1m", start_ms=t, end_ms=min(end * 1000, t + 1500 * 60_000))
        if not chunk:
            break
        rows += chunk
        t = int(chunk[-1][0]) + 60_000
        time.sleep(0.2)
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume", "close_time", "quote_asset_volume",
                                     "n_trades", "taker_buy_base_volume", "taker_buy_quote_volume", "ignore"])
    df = df.drop(columns=["close_time", "ignore"]).astype(float)
    df["timestamp"] = df["timestamp"] / 1000.0
    df = df.drop_duplicates("timestamp").sort_values("timestamp")
    csv_path = os.path.join(a.out, f"{a.symbol}_1m.csv")
    df.to_csv(csv_path, index=False)
    print(f"klines: {len(df)} rows -> {csv_path}")
    # fuel replay from OI history (Binance keeps ~30 days)
    bars = fm.fetch_range(a.base, a.symbol, start - 48 * 300, end)
    hist = fm.replay_history(bars, a.symbol)
    fuel_path = os.path.join(a.out, f"{a.symbol}_fuel.json")
    with open(fuel_path, "w") as fh:
        json.dump(hist, fh)
    print(f"fuel history: {len(hist)} snapshots from {len(bars)} 5m bars -> {fuel_path}")


if __name__ == "__main__":
    main()
