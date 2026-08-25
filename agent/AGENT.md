# quench tuner

An agent that watches where liquidation clusters sit and adjusts a market maker's quotes. It never
places an order. Its entire mechanism of action is rewriting one YAML file.

## Observe

One input: the fuel map. Leverage-implied liquidation clusters above and below price, built by
`service/fuelmap_service.py` from Binance open-interest history and klines, published as a JSON
snapshot per symbol.

## Orient

`routines/fuelmap.py` turns a snapshot plus its history into a report. The report does not say "there
is a cluster 1.4 units away". It says where that reading sits in the distribution of every reading on
this tape, because a raw distance is not interpretable on its own. That mistake has already been made
once here and it cost the whole first version of this strategy: a fuse threshold of 1.5 volatility
units was chosen without checking that clusters actually sit a median 5.57 units away, so it fired
zero times in fifty backtests.

The report also marks whether a cascade reading is attributed to a side. An unattributed spike in
liquidation flow is ordinary position closing. Acting on it was measured as the entire cost of the
previous design.

## Decide

`agent/policy.py`. Three outcomes and nothing else.

Hold. The default. Nothing unusual, or the map is stale, or clusters are close on both sides at once.

Tilt. One side carries an unusually close unburned cluster, so that side's quotes widen and the other
side is left alone. Bounded at forty percent, scaled by how unusual the reading is.

Derisk. An attributed cascade is running at three times baseline, so total size is halved. No tilt,
because during a cascade the direction is the least reliable thing on the screen.

An optional model layer may choose between the candidates the policy generated. It cannot write a
number. Anything it returns that is not a candidate id is discarded and the deterministic decision
stands, which is recorded in the journal.

## Act

`agent/tune.py` writes the changed keys into the controller YAML, in place, leaving every comment
byte-identical. Hummingbot re-reads that file every ten seconds and applies the fields flagged
updatable, live, without restarting the bot.

Every run is journalled, including the ones that changed nothing and the ones that refused.

## What it cannot do

Quote under the measured fee floor. On fourteen days of SOL-USDT the gross edge per round trip was
2.42 basis points at spreads of four and eight, against a four basis point round trip at Bitget. That
configuration loses however often it fills. Five and ten measured 5.73. The floor is five and the
policy cannot cross it, whatever the report says and whatever a model picks.

Write a setting the running bot would ignore. A field without the updatable flag is a silent no-op,
which is worse than an error, so the validator rejects those by name.

Restart the bot, place an order, cancel an order, or touch the trading loop.

## Honest status

The fee floor and the cascade rule come from measurements on a real tape. The tilt does not. The same
idea applied per tick did nothing at all, and the argument for retrying it on a slow clock is that
the map's own numbers say it is slow information. That is a reason to test it, not evidence that it
works. It has not been tested yet. The test is a sweep against a real tape with the tilt on and off.
