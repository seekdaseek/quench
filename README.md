# quench

A volatility-adaptive perpetual market maker for Hummingbot (Strategy V2), plus a routine that reads
the liquidation landscape and an agent that tunes the maker from it. Built for the Botcamp Agent
Builders Cup (Series 1).

Three layers, and the separation is the design:

```
controllers/  quotes.   Candles and its own position. No network, no LLM, tick rate.
routines/     reports.  Builds the liquidation fuel map and says what it shows. Plain Python, slow.
agent/        decides.  Reads the report, rewrites the controller's config inside hard bounds.
```

The controller is deliberately dull. It quotes two sides at spreads measured in NATR units, skews the
reference price against inventory, and sets each fill's take-profit and stop as a multiple of the
spread that fill was quoted at — which is the change that made a wide quote pay at all (see the
barrier fix below). That is the whole file, 233 lines, and it knows nothing about liquidations.

Everything about liquidations moved outward on 25 Aug 2026, after a review by Federico of the
Hummingbot Foundation on the Botcamp submission: *"keep the controller simple and just using the
realtime data that you need, and use routines to get more general data and use the agent to tune your
controller based on that external data."*

**The measurements below are why that was the right call, and they predate the review.** With the
cascade brake disabled, the fuel layer inside the controller produced results identical to the cent
with it switched on and off across 14 days of SOL-USDT — same 53 fills, same +$3.68, same 5.734 bps.
The magnet lean touched 3,071 of 20,142 rows and changed nothing. The fuse fired zero times. The
reason is in the map's own numbers: unburned clusters sit a median 5.57 volatility units above price
and 2.63 below, and only ~36% of snapshots carry one at all. A quote resting 30 bps from mid cannot
react to something five volatility units away.

So the liquidation map is real information on a slow clock. It was never tick-rate information. It
now lives where its clock belongs.

### How the agent reaches a running bot

It writes a file. `StrategyV2Base.update_controllers_configs()` re-reads the controller YAML every
`config_update_interval` seconds (default 10) and calls `ControllerBase.update_config()`, which
applies only the fields whose `json_schema_extra` carries `is_updatable`. The agent never imports the
controller, never touches the trading loop, and cannot restart the bot. A field without that flag is
a silent no-op, so `agent/policy.validate()` rejects those by name.

### What the agent cannot do

Quote under the measured fee floor. Gross edge per round trip on this tape was **2.42 bps at spreads
of 4,8** against a **4.0 bps round trip** at Bitget VIP0 — that configuration loses however often it
fills. 5,10 measured 5.73. The floor is 5 NATR units and nothing crosses it, whatever the report says
and whatever a model picks. An optional LLM layer may only *choose between* candidates the
deterministic policy already generated and validated; it cannot author a number, and anything it
returns that is not a candidate id falls back to the deterministic decision and is recorded as such
in the journal.

Full agent description in `agent/AGENT.md`.

## Layout

```
controllers/market_making/quench.py    the controller. Quoting and risk only, 233 lines
routines/fuelmap.py                    ROUTINE: snapshot -> report, ranked against its own history
routines/fuel/model.py                 the cluster math, unchanged, moved out of the controller
service/fuelmap_service.py             collector (Binance USDT-M OI + klines -> clusters JSON)
agent/policy.py                        AGENT: the decision, its bounds, and the last validation gate
agent/tune.py                          AGENT: report -> controller YAML, journalled every run
agent/AGENT.md                         what the agent observes, decides, and cannot do
backtest/harness.py                    offline harness around Hummingbot's REAL V2 backtesting engine
backtest/run_backtest.py               one config end to end on a tape
backtest/sweep.py                      parameter grid; gross bps vs fees, per-trade t-stat, walk-forward halves
backtest/inspect_fuel.py               what the fuel map can possibly do on a tape, in seconds
backtest/fetch_data.py                 pull real 1m klines + a deterministic no-look-ahead fuel replay
conf/controllers/quench_{bitget,gate}_sol.yml, conf/scripts/v2_quench.yml
tests/                                 52 tests, all offline
```

> **The sections below are the build log, and they describe the SINGLE-FILE controller as it was
> before 25 Aug 2026.** Every number in them is real and was measured on real tape; they are kept
> because they are the evidence for the split, not a description of the code as it stands. Where they
> say "the fuel layer", "the magnet lean", "the fuse" or "the cascade brake", read: components that
> used to live inside the controller and now live in `routines/` and `agent/`.

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

## What is verified (hummingbot==20260729 from PyPI, Python 3.12)

- `python3 -m unittest discover -s tests` — **52 tests**, five consecutive clean runs: cluster math,
  fuel-map model, replay no-look-ahead, YAML configs load, the barrier rules, the routine, the agent
  policy, and the controller running **inside Hummingbot's real `BacktestingEngineBase`** with both
  sides quoted and every entry price bracketing the reference of its own candle.
- **The split is behaviour-neutral on real tape.** Before and after, on the same 14 days of SOL-USDT:
  53 fills, $12.16 gross, $8.48 fees, +$3.68 net, 5.734 bps, dd −$1.38, t 2.4, halves +2.28 / +1.40.
  Identical to the cent.
- Three guard tests were each verified by deliberately breaking the thing they guard and watching the
  test fail: the controller cannot regain a fuel layer, the runners cannot pass a rejected field, and
  every lever the agent writes must carry `is_updatable`.
- The agent's containment was verified by attack: `4,8` is refused at the fee floor, a non-updatable
  field is refused by name, and an LLM told to answer `{"buy_spreads":"1,2"}` had its answer discarded
  in favour of the deterministic decision.

Synthetic-tape PnL carries no information and is never quoted here as a result.

## What is NOT yet verified

- Live connector run on Bitget / Gate (paper or funded). The controller only uses framework
  primitives (`PositionExecutorConfig`, `TripleBarrierConfig`) so nothing exotic is required, but it
  has not been started against a live connector yet.
- The fuel service against Binance from the VPS (network + geo). It writes nothing on error, so a
  dead collector can never steer quotes.
- 🔴 **The agent's tilt.** The fee floor and the cascade rule come from measurements on a real tape.
  The tilt does not. The identical idea applied per-tick did nothing at all over 14 days, and the
  argument for retrying it on a slow clock is that the map's own distances say it is slow information.
  **That is a reason to test it, not evidence that it works.** The test is a sweep with the tilt on
  and off against a real tape, and it has not been run.
- Whether the agent's changes help or hurt over a live session. Nothing here claims they do.

## Run it

```bash
pip install hummingbot==20260729            # PyPI wheel exists for macOS arm64 + Linux x86_64, Python 3.12
python3 -m unittest discover -s tests       # 52 tests, ~14s, no network
python3 backtest/fetch_data.py --symbol SOLUSDT --days 14        # needs Binance reachable
python3 backtest/sweep.py --csv data/SOLUSDT_1m.csv \
  --spreads 5,10 6,12 --tp-mult 1.0 --sl-mult 3.0 --refresh 300 --json sweep.json

# the routine, then the agent
python3 routines/fuelmap.py --snapshot data/SOLUSDT_fuel.json --out reports/SOLUSDT.json
python3 agent/tune.py --report reports/SOLUSDT.json \
  --config conf/controllers/quench_bitget_sol.yml --dry-run    # drop --dry-run to actually write
```

Live: copy `controllers/market_making/quench.py` into your Hummingbot instance's
`controllers/market_making/`, the YAML into `conf/controllers/`, then
`start --script v2_with_controllers.py --conf conf/scripts/v2_quench.yml`. The controller runs alone
and needs nothing else. Point `agent/tune.py` at that same YAML on a cron and the bot picks up its
changes within 10 seconds — no restart, and stopping the agent simply leaves the last settings in
place.

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
gross edge clears the fee, and nothing else matters until it does. Then `tp_spread_mult`,
`sl_spread_mult`, `time_limit`, `inventory_skew_natr`.

The agent's own bounds live in `agent/policy.Bounds`: `min_spread_units` (the fee floor, 5.0),
`max_spread_units`, `max_tilt_pct`, `close_cluster_pctile`, `cascade_size_cut`. Raising the floor is
always safe; lowering it is how you lose money, and the measured reason is in the docstring.

MIT.
