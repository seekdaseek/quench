# quench — a liquidation-aware perpetual market maker

Hummingbot V2 Controller · Bitget USDT-M perpetuals · SOL-USDT

## What it does

quench quotes both sides of a perpetual order book at spreads measured in units of realised
volatility, and it treats the liquidation map of the market as a first-class input to where it is
willing to be filled.

Two ideas carry the whole strategy.

The first is that a market maker's exit has to scale with its entry. A fill five volatility units
below mid has to target its way back toward mid, not a fixed take profit, or the spread it quoted is
never harvested. This sounds obvious written down. It was wrong in my first version and it cost the
strategy its entire edge, which I only found because gross profit per round trip came back flat at
every spread width I tested. In quench the triple barrier is set as a multiple of the spread of the
level that actually filled, floored above the round-trip fee, and the stop is never tighter than the
target.

The second is that leverage sitting above and below price is information about where you do not want
to be quoting. A collector reads open-interest changes off the perpetual tape every five minutes,
treats each increase as fresh leverage opened near that bar's price, splits it across leverage tiers
and across long and short by the bar's taker-buy ratio, and projects it to the prices where those
positions would be liquidated. Clusters shrink when open interest falls, decay with age, and are
marked spent the moment the tape trades through them. The agent will not put a sell quote in front of
unspent short-liquidation fuel above it, and will not buy into long-liquidation fuel below it.

Distances to those clusters are measured in volatility over an hour, not per-bar volatility. That
distinction is not cosmetic: clusters sit one to ten percent away while a one-minute range on SOL is
about six basis points, so in per-bar units every cluster is a hundred units away and the logic can
never fire. It did not fire, for fifty backtest runs, until I fixed the unit.

If the fuel feed is stale or missing the layer switches itself off and quench is a plain
volatility-scaled market maker. It never acts on stale data, and the status line always says which
state it is in.

## What the numbers actually say

Fourteen days of one-minute SOL data, run inside Hummingbot's own V2 backtesting engine, 800 dollars
of capital, Bitget's real VIP0 maker fee of 2 basis points a side.

Quoting one volatility unit wide loses money. Gross edge is 2.3 basis points per round trip against a
4 basis point round-trip fee, so 1,919 fills turn 177 dollars of gross into a 129 dollar loss. No
amount of signal work fixes that; the quote is simply too tight to pay for itself.

Quoting five and ten volatility units wide, with barriers scaled to the spread, earns 5.7 basis
points gross per round trip. Fifty-three fills, net positive, maximum drawdown 1.38 dollars, a
per-trade t-statistic of 2.4 with both halves of the sample independently positive. A four-level
variant at five, seven, nine and eleven units gives 71 fills, a t-statistic of 2.66 and a smaller
drawdown, at slightly lower net because the same capital splits four ways.

The same configuration on BTC returns a t-statistic of 2.36 but with the second half of the sample
flat, and on ETH it does not work at all. I set the acceptance bar before running it — significance
plus both halves positive on two of three symbols — and this clears it on one. So the honest position
is that quench has a measured edge on SOL and an unproven one elsewhere.

The liquidation layer, as first written, cost eleven to twenty-three percent of net profit. I isolated
each component rather than guessing which one, and the culprit was the cascade brake firing on an
open-interest-drop proxy that could never name which side was being liquidated. A fall in open
interest is closed positions of every kind. The brake now requires an attributed liquidated side,
which only real liquidation data provides, and is inert until that data is wired in. The magnet lean
turned out to be measurably inert on this tape and stays behind a parameter rather than a claim.

I would rather submit that paragraph than a tuned curve.

## What is not verified

No live run on a funded account yet. The backtest simulator fills every touch, which flatters any
maker and flatters a tight quote most of all; real queue position will make fill quality worse than
these numbers. The fuel collector has been run against Binance's public API but not yet under a
long-lived process. Fifty-three fills is a small sample however good the t-statistic looks.

## Running it

```
pip install hummingbot==20260729
python3 -m unittest discover -s tests          # 34 offline tests, no network
python3 backtest/fetch_data.py --symbol SOLUSDT --days 14
python3 backtest/run_backtest.py --csv data/SOLUSDT_1m.csv --fuel data/SOLUSDT_fuel.json
python3 backtest/sweep.py --csv data/SOLUSDT_1m.csv --fuel data/SOLUSDT_fuel.json \
  --spreads 4,8 5,10 6,12 --tp-mult 1.0 --sl-mult 3.0 --refresh 300
```

Copy `controllers/market_making/quench.py` into a Hummingbot instance, the YAML into
`conf/controllers/`, and start with `v2_with_controllers.py`. The fuel service runs anywhere with
outbound HTTPS and publishes one JSON file per symbol.

## Configuration that produced the measured result

Spreads 5 and 10 volatility units a side, order refresh 300 seconds, take profit at 1.0 times the
filled level's spread, stop at 3.0 times, 15 minute time limit, 5x leverage, fuse at 1.5 horizon
volatility units, cascade brake requiring an attributed side, maker fee assumption 2.0 basis points.

The parameters worth tuning first are the spreads and the refresh interval together, because a wide
quote needs time to fill and neither number means anything alone.

## Files

`controllers/market_making/quench.py` is the controller, single file, no dependencies beyond
Hummingbot. `service/fuelmap_service.py` is the fuel collector. `backtest/` holds an offline harness
around Hummingbot's real backtesting engine, a parameter sweep that reports gross basis points per
round trip and per-trade significance, and a fuel inspector that reports what the liquidation layer
can possibly do on a given tape before you spend twenty minutes finding out. `tests/` is 34 offline
tests, several of which exist specifically to stop the bugs described above from coming back.

MIT licensed. Built by ochinimus, Chisinau.
