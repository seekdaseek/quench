# quench

A liquidation-aware perpetual market maker for Hummingbot (Strategy V2 controller), built for the
Botcamp Agent Builders Cup (Series 1).

Most market makers see the order book. quench also sees the *fuel*: where leverage sits above and
below price and would be force-liquidated if the tape got there. It quotes two-sided, volatility-
adaptive spreads like a normal maker, and then does four things a normal maker does not:

1. **Magnet lean** — leans the reference price toward the largest *unburned* liquidation cluster
   within reach (bounded to `max_lean_natr` × NATR). Price tends to walk toward unspent fuel.
2. **Fuse pull** — inside `fuse_natr` × NATR of a large cluster it pulls the quotes that would be run
   over: no selling into a short squeeze, no buying into a long cascade.
3. **Cascade brake** — when realized liquidation flow spikes vs its baseline it widens spreads,
   halves size and pulls the side being liquidated.
4. **Burned = spent** — a cluster the tape has traded through is gone; the lean flips off. Re-buying
   after the magnet is swept is the classic mistake; the controller cannot make it.

Fail-safe first: if the fuel feed is stale (> `fuel_max_age_seconds`) or absent, the layer switches
itself off and the controller is a plain NATR-spread PMM. It never acts on stale fuel. The status
line always says `FUEL: LIVE | STALE | ABSENT | OFF`.

## Layout

```
controllers/market_making/quench.py    the controller (single file, drop into a Hummingbot instance)
service/fuelmap_service.py             fuel-map collector (Binance USDT-M OI + klines -> clusters JSON)
backtest/harness.py                    offline harness around Hummingbot's REAL V2 backtesting engine
backtest/run_backtest.py               fuel ON vs OFF on the same tape
backtest/sweep.py                      parameter grid; gross bps vs fees, per-trade t-stat, walk-forward halves
backtest/inspect_fuel.py               what the fuel layer can possibly do on a tape, in seconds
backtest/fetch_data.py                 pull real 1m klines + a deterministic no-look-ahead fuel replay
conf/controllers/quench_{bitget,gate}_sol.yml, conf/scripts/v2_quench.yml
tests/                                 34 tests, all offline
```

## First real-tape result (SOL-USDT, 14 days of 1m Binance candles, Aug 1-15 2026)

Run as configured (spreads 1,2 NATR; tp 1 / sl 3 NATR; 15m time limit; $800; trade_cost 2 bps = Bitget
VIP0 USDT-M maker), 1,919 filled positions on $767,612 of volume:

| | fuel OFF | fuel ON |
|---|---|---|
| net | -$129.53 | -$123.90 |
| fees (2 x cost x volume) | $307.04 | $302.25 |
| **gross edge** | **+$177.51 = +2.31 bps / round trip** | +2.37 bps |
| pull_buy / pull_sell rows | 0 / 0 | 0 / 0 |

Then a 32-configuration sweep across spreads (1,2 / 2,4 / 3,6 / 5,10 NATR) x refresh (60/300) x
take-profit x fuel on/off, same tape. **Every one of the 32 lost, and the reason is visible in one
column: `gross_bps` is flat at ~2.0-2.4 no matter how wide the quote.**

| quoted | fills | gross_bps | gross per NATR quoted |
|---|---|---|---|
| 1 NATR | 1,919 | 2.31 | 2.31 |
| 2 NATR | 372 | 2.39 | 1.20 |
| 3 NATR | 95 | 1.85 | 0.62 |
| 5 NATR | 13 | 0.01 | 0.00 |

If the edge were spread capture, gross would scale with the quoted spread. It is flat, because the
triple barrier was a fixed distance from the **entry**: a fill at mid - 3 NATR took profit at
entry + 1 NATR, so the three NATR of spread was never harvested — only the fill selection changed.
Fixed: `tp_spread_mult` / `sl_spread_mult` set the barriers as multiples of the
filled level's own spread (1.0 = exit back at mid, 2.0 = exit at the opposite quote), with the take
profit floored above the round-trip fee (`fee_bps_per_side` x `tp_fee_multiple`). Pinned by
`tests/test_barriers.py`.

Three findings, all acted on:

1. **The barriers did not scale with the quote** (above) — the fix that had to land before any tuning meant anything.
2. **The maker edge is real but under the fee.** Gross is +2.31 bps per round trip against a 4 bps
   round-trip maker fee. The strategy is not broken; it is quoting too tight to pay for itself. Fix
   direction: wider spreads (and a longer refresh so wide quotes get filled), or a venue with a maker
   fee under ~1.15 bps. `backtest/sweep.py` reports `gross_bps` for every configuration so the
   breakeven fee is read off directly rather than assumed.
3. **The fuel layer was inert — and that was a units bug, now fixed.** Distances to clusters were
   measured in per-bar NATR (~6 bps on SOL 1m) while liquidation clusters sit 1-10% away, i.e. 15-150
   units out; `fuse_natr` and `lean_horizon_natr` could never be reached, so the fuse fired zero times
   in 14 days. Distances are now measured in **realized volatility over `fuel_horizon_minutes`**
   (default 60m, from log returns of the same feed), while the lean magnitude stays in per-bar NATR so
   it remains small relative to the quoted spread. Pinned by `tests/test_units.py`.

### After the barrier fix (18-config sweep, same tape)

Barriers scaled to the quoted spread turn the wide quotes positive for the first time:

| spreads | fills | gross_bps | net | breakeven maker fee |
|---|---|---|---|---|
| 2,4 | ~800 | 1.8 - 2.2 | -$58 to -$70 | 0.9 - 1.1 bps |
| 3,6 | ~290 | 1.9 - 2.6 | -$16 to -$23 | 1.0 - 1.3 bps |
| **5,10** | **48 - 53** | **5.2 - 5.6** | **+$2.32 to +$3.26** | **2.6 - 2.8 bps** |

5,10 clears Bitget VIP0 (2.0 bps maker) with room. Two caveats that decide whether it is real:

- **51 fills is not a sample.** `sweep.py` now reports a per-trade t-statistic and a walk-forward
  half-split, both computed free from the same run. A config is only interesting at t >= 2 with both
  halves positive.
- **The simulator fills every touch.** A real limit order at the top of a 5-NATR-wide quote has queue
  position and gets picked off; live fill quality will be worse than backtested.

`fuse_natr` recalibrated to 1.5 from the measured cluster-distance p05 on real tape (1.64 up / 1.36
down in horizon-vol units) — the old 0.35 default was unreachable, which is why the fuse fired zero
times in every run before this one.

### Cross-symbol check, and what it falsified

Same 14-day window, BTC and ETH fetched fresh, decision rule set before the run (t >= 2 with both
halves positive on at least two of three symbols):

| | best config | fills | gross_bps | net | t | halves |
|---|---|---|---|---|---|---|
| SOL | 5,10 | 53 | 5.73 | +$3.68 | 2.4 | +2.28 / +1.40 |
| BTC | 5,10 | 28 | 5.73 | +$1.94 | 2.36 | +2.01 / **-0.06** |
| ETH | 5,10 | 59 | 3.63 | **-$0.87** | 1.07 | -1.61 / +0.74 |

**One of three passes. The rule says not proven**, and the rule stands.

Two things it did establish. The barrier fix is real: gross edge now rises monotonically with the
quoted spread on SOL (2.4 -> 5.7 -> 6.1 -> 7.1 bps at 4,8 / 5,10 / 6,12 / 8,16), which is what spread
capture is supposed to look like and was flat before. And the fill rate is the binding constraint —
53 fills in 14 days is about 8 fills inside a 48-hour race.

**The fuel layer costs money where it can be measured.** Paired runs, same tape, fuel the only
difference, `pull_*` zero throughout so the magnet lean is the only active component:

| | fuel OFF | fuel ON | cost of the layer |
|---|---|---|---|
| SOL 5,10 | +$3.68 | +$3.26 | -$0.42 (-11%) |
| SOL 6,12 | +$2.22 | +$1.70 | -$0.52 (-23%) |

Then the lean was gridded at 0 / +0.5 / -0.5 and **all three came back identical to the cent** (51
fills, +$3.26, gross 5.595) — so on this tape the magnet lean never acts at all, and the cost of the
fuel layer comes from somewhere else. The remaining candidate is the **cascade brake**, which widens
spreads and halves size whenever a 5-minute drop in open interest exceeds three times its own
baseline. That trigger is the OI-drop proxy, not realized liquidations: a fall in open interest is
closed positions of every kind, most of them nothing to do with a cascade. `inspect_fuel.py` reports,
in seconds and without a backtest, how often each component can act on a given tape; `sweep.py
--brake 999` disables the brake to isolate it.

That is the project's own thesis failing its first honest test. `sweep.py --max-lean 0 0.5 -0.5` now
grids it: zero disables the lean and keeps only the fuse and cascade brake; a negative value inverts
it, quoting *away* from the nearest unburned cluster instead of toward it. The magnet argument is a
directional trader's argument, and a maker may want the opposite sign. Settle it with the flag.

### The cascade brake was the entire cost, and the venue is the entire result

With `--brake 999` the fuel-on and fuel-off runs come back **identical to the cent** (53 fills,
+$3.68, gross 5.734). So the whole cost of the fuel layer was the cascade brake, firing on 2.4% of
snapshots off an open-interest-drop proxy that reports a null `dominant_side` 100% of the time — it
could widen spreads and halve size, and could never do the one thing a brake is for. The brake now
requires an attributed liquidated side (`require_attributed_cascade`, default true), which only real
liquidation data provides. Pinned by `tests/test_cascade.py`.

`inspect_fuel.py` also explains why the lean and fuse are near-inert on this tape: only 36% of
snapshots carry any unburned cluster, and the median distance to one above price is 5.6 horizon-vol
units. The lean can act on 20.7% of snapshots, the fuse on 5.1%. An OI-implied cluster map is too
sparse and too far away to steer a maker quoting 30 bps from mid.

**The fee is the dominant term in this strategy — larger than any signal in it.** The same measured
gross edge, priced against each sponsor venue's actual maker fee:

| config | fills | volume | Bitget 2.0 bp | Hyperliquid 1.5 bp | XRPL ~0 |
|---|---|---|---|---|---|
| 1,2 refresh 300 | 2,555 | $1,022,000 | -$161.26 | -$59.06 | **+$247.54** |
| 5,10 refresh 300 | 53 | $21,200 | +$3.68 | +$5.80 | +$12.16 |

The XRPL native order book charges no percentage fee at all — placement, cancellation and execution
each cost one network transaction (~0.00001 XRP). Hummingbot ships an XRPL connector in the wheel
(pure Python, 100+ markets). At zero fee the tight-spread configuration is the best one instead of
the worst, on both P&L and volume.

Two caveats that keep this a hypothesis: the 2.3-2.4 bps gross edge was measured on SOL-USDT Binance
candles and does not automatically exist on XRP-RLUSD, and the simulator fills every touch — an
assumption that is most wrong exactly where this configuration makes the most money.

## What is verified (sandbox, hummingbot==20260729 from PyPI, Python 3.12)

- `python3 -m unittest discover -s tests` — 34 tests pass: fuel math, fuel-map model, replay
  no-look-ahead, YAML configs load, and the controller running **inside Hummingbot's real
  `BacktestingEngineBase`** on synthetic tape (baseline quotes both sides bracketing the reference;
  with a squeeze cluster parked above, sell quotes are pulled inside the fuse window, buys keep
  quoting, the lean is upward and bounded, and a crossed cluster is treated as burned).
- `python3 backtest/run_backtest.py --synthetic` runs end to end (688 executors on a 10h synthetic
  tape; fuel ON with no history behaves identically to OFF — the fail-safe).

Synthetic results carry no PnL information. The claim to test on real tape is *fuel ON vs OFF on the
same candles*, via `fetch_data.py` + `run_backtest.py`.

## What is NOT yet verified

- Live connector run on Bitget / Gate (paper or funded). The controller only uses framework
  primitives (`PositionExecutorConfig`, `TripleBarrierConfig`, `StopExecutorAction`) so nothing exotic
  is required, but it has not been started against a live connector yet.
- The fuel service against Binance from the VPS (network + geo). It writes nothing on error, so a
  dead collector can never steer quotes.
- Real-data backtest numbers.

## Run it

```bash
pip install hummingbot==20260729            # PyPI wheel exists for macOS arm64 + Linux x86_64, Python 3.12
python3 -m unittest discover -s tests       # 34 tests, ~5s, no network
python3 backtest/run_backtest.py --synthetic
python3 backtest/fetch_data.py --symbol SOLUSDT --days 14        # needs Binance reachable
python3 backtest/run_backtest.py --csv data/SOLUSDT_1m.csv --fuel data/SOLUSDT_fuel.json --json out.json
python3 backtest/sweep.py --csv data/SOLUSDT_1m.csv --fuel data/SOLUSDT_fuel.json \
  --spreads 2,4 3,6 5,10 --tp-mult 1.0 1.5 2.0 --sl-mult 2.0 3.0 --refresh 300 --json sweep.json
```

Live: copy `controllers/market_making/quench.py` into your Hummingbot instance's `controllers/market_making/`,
the YAML into `conf/controllers/`, then `start --script v2_with_controllers.py --conf conf/scripts/v2_quench.yml`.
Set `fuel_url` to your collector (or `fuel_enabled: false` to run it as a plain PMM).

Fuel service:
```bash
SYMBOLS=SOLUSDT,BTCUSDT,ETHUSDT OUT_DIR=/opt/quench/out SERVE_PORT=3018 HISTORY_DIR=/opt/quench/history \
  python3 service/fuelmap_service.py
curl -s localhost:3018/SOLUSDT.json | python3 -m json.tool | head
```

## The model, stated plainly

The fuel map is an *estimate*: each 5-minute increase in USDT-M open interest is treated as fresh
leverage opened near that bar's price, split across leverage tiers (default 10x/25x/50x/100x) and across
long/short by the bar's taker-buy ratio, and projected to liquidation prices `P·(1 ∓ 1/L)`. Buckets
shrink pro-rata when OI falls, decay with age, and burn when the tape trades through them. It is the
same class of model as public liquidation heatmaps, and it is wrong in the same ways (tier weights are
assumptions). What it gets right is the thing that matters for a maker: *which side's fuel is still
unspent right now, and how close it is in units of current volatility.*

Cascade metric: realized liquidations if you point it at a liquidation table (`LIQ_SQLITE`, `LIQ_SQL`),
otherwise an OI-drop proxy.

## Parameters worth tuning on real tape

`buy_spreads/sell_spreads` (NATR units) and `executor_refresh_time` together — these decide whether
gross edge clears the fee, and nothing else matters until it does. Then `tp_natr`, `sl_natr`,
`time_limit`, `fuel_horizon_minutes`, `fuse_natr`, `lean_horizon_natr`, `max_lean_natr`,
`fuel_reference_notional`, `cascade_ratio_brake`.

MIT.
