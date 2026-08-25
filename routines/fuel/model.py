"""
Pure fuel math — leverage-implied liquidation clusters and what they imply.

MOVED HERE FROM controllers/market_making/quench.py on Aug 25 2026, verbatim and unchanged,
after Federico's review on Botcamp: "keep the controller simple and just using the realtime data
that you need, and use routines to get more general data and use the agent to tune your controller
based on that external data".

Nothing in this file imports Hummingbot. It is plain Python, called by the routine, and it never
touches the trading loop. The controller no longer knows liquidation clusters exist.

Fuel snapshot JSON (produced by service/fuelmap_service.py):
{
  "symbol": "SOLUSDT", "ts": 1755270000, "ref_price": 75.46, "source": "binance_oi_tiers_v1",
  "clusters": [ {"price": 76.4, "side": "short", "notional": 3700000, "burned": false}, ... ],
  "cascade": {"liq_notional_60s": 120000, "baseline_60s": 25000, "dominant_side": "long"}
}
"side" is the side of the positions liquidated at that price: "short" clusters sit ABOVE price
(short liquidations = forced buys), "long" clusters sit BELOW.
"""
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------------------------------
# Pure fuel math (no framework dependency) — unit-tested in tests/test_fuel.py
# --------------------------------------------------------------------------------------------------

FUEL_OFF = "off"        # layer disabled by config
FUEL_ABSENT = "absent"  # never received a snapshot
FUEL_STALE = "stale"    # snapshot older than max age -> layer neutralised
FUEL_LIVE = "live"


@dataclass
class FuelSignal:
    state: str                    # off | absent | stale | live
    price_shift: float = 0.0      # fraction of reference price (already bounded)
    spread_multiplier: float = 1.0
    amount_multiplier: float = 1.0
    pull_buy: bool = False
    pull_sell: bool = False
    up_cluster: Optional[dict] = None    # nearest unburned "short" cluster above ref
    down_cluster: Optional[dict] = None  # nearest unburned "long" cluster below ref
    up_dist_natr: float = math.inf
    down_dist_natr: float = math.inf
    cascade_ratio: float = 0.0
    vol_dist: float = 0.0
    reason: str = ""

    def as_row(self) -> Dict[str, Any]:
        return {
            "fuel_state": self.state,
            "fuel_shift": self.price_shift,
            "fuel_spread_mult": self.spread_multiplier,
            "fuel_amount_mult": self.amount_multiplier,
            "pull_buy": 1 if self.pull_buy else 0,
            "pull_sell": 1 if self.pull_sell else 0,
            "up_dist_natr": self.up_dist_natr if math.isfinite(self.up_dist_natr) else -1.0,
            "down_dist_natr": self.down_dist_natr if math.isfinite(self.down_dist_natr) else -1.0,
            "cascade_ratio": self.cascade_ratio,
            "vol_dist": self.vol_dist,
        }


def neutral(state: str, reason: str = "") -> FuelSignal:
    return FuelSignal(state=state, reason=reason)


def snapshot_age(snapshot: Optional[dict], now: float) -> float:
    if not snapshot or "ts" not in snapshot:
        return math.inf
    try:
        return max(0.0, float(now) - float(snapshot["ts"]))
    except (TypeError, ValueError):
        return math.inf


def nearest_unburned(clusters: List[dict], ref_price: float, side: str, min_notional: float) -> Optional[dict]:
    """
    side="short": nearest cluster strictly ABOVE ref_price (short liquidations / squeeze fuel).
    side="long":  nearest cluster strictly BELOW ref_price (long liquidations / cascade fuel).
    Burned clusters and clusters below min_notional are ignored. Clusters on the wrong side of the
    price for their type are ignored too (a "short" cluster below price has already been crossed
    -> treated as burned even if the flag says otherwise).
    """
    best = None
    best_dist = math.inf
    for c in clusters or []:
        try:
            p = float(c["price"])
            notional = float(c.get("notional", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if c.get("burned") or c.get("side") != side or notional < min_notional:
            continue
        if side == "short" and p <= ref_price:
            continue
        if side == "long" and p >= ref_price:
            continue
        d = abs(p - ref_price)
        if d < best_dist:
            best_dist, best = d, c
    return best


def _size_norm(cluster: Optional[dict], reference_notional: float) -> float:
    if cluster is None or reference_notional <= 0:
        return 0.0
    return max(0.0, min(1.0, float(cluster.get("notional", 0.0)) / reference_notional))


def compute_fuel_signal(snapshot: Optional[dict],
                        ref_price: float,
                        vol_dist: float,
                        now: float,
                        *,
                        vol_shift: Optional[float] = None,
                        enabled: bool = True,
                        max_age_s: float = 90.0,
                        min_notional: float = 250_000.0,
                        reference_notional: float = 2_000_000.0,
                        lean_horizon_natr: float = 3.0,
                        max_lean_natr: float = 0.5,
                        fuse_natr: float = 1.5,
                        fuse_min_size: float = 0.5,
                        cascade_ratio_brake: float = 3.0,
                        cascade_spread_mult: float = 2.0,
                        cascade_amount_mult: float = 0.5,
                        require_attributed_cascade: bool = True) -> FuelSignal:
    """
    Turn a fuel snapshot into quoting adjustments. Pure and deterministic.

    vol_dist  — the unit distances to clusters are measured in, as a fraction of price. Use the
                volatility over the HORIZON price would need to reach a cluster (e.g. 1h realized
                vol), never the per-bar NATR: clusters sit 1-10% away and a 1m NATR is ~5 bps, so
                per-bar units put every cluster hundreds of units out and the layer never fires.
    vol_shift — the unit the price lean is expressed in (defaults to vol_dist). Use the QUOTE vol
                (per-bar NATR) so the lean stays small relative to the spread being quoted.
    """
    vol_shift = vol_dist if vol_shift is None else vol_shift
    if not enabled:
        return neutral(FUEL_OFF, "disabled")
    if not snapshot:
        return neutral(FUEL_ABSENT, "no snapshot")
    age = snapshot_age(snapshot, now)
    if age > max_age_s:
        return neutral(FUEL_STALE, f"age {age:.0f}s > {max_age_s:.0f}s")
    if not (ref_price and ref_price > 0) or not (vol_dist and vol_dist > 0) or not math.isfinite(vol_dist):
        return neutral(FUEL_LIVE, "no price/vol")

    natr_abs = ref_price * vol_dist  # distance unit in price terms
    clusters = snapshot.get("clusters") or []
    up = nearest_unburned(clusters, ref_price, "short", min_notional)
    down = nearest_unburned(clusters, ref_price, "long", min_notional)
    up_d = (float(up["price"]) - ref_price) / natr_abs if up else math.inf
    down_d = (ref_price - float(down["price"])) / natr_abs if down else math.inf
    up_s = _size_norm(up, reference_notional)
    down_s = _size_norm(down, reference_notional)

    # 1. magnet lean: pull of each side decays linearly to zero at lean_horizon_natr
    def lean(dist: float, size: float) -> float:
        if not math.isfinite(dist) or dist >= lean_horizon_natr:
            return 0.0
        return size * (1.0 - dist / lean_horizon_natr)

    net_lean = lean(up_d, up_s) - lean(down_d, down_s)  # +: toward the cluster above
    price_shift = max(-1.0, min(1.0, net_lean)) * max_lean_natr * vol_shift

    # 2. fuse pull
    pull_sell = bool(up and up_d < fuse_natr and up_s >= fuse_min_size)
    pull_buy = bool(down and down_d < fuse_natr and down_s >= fuse_min_size)

    # 3. cascade brake
    spread_mult, amount_mult, ratio = 1.0, 1.0, 0.0
    cascade = snapshot.get("cascade") or {}
    try:
        liq = float(cascade.get("liq_notional_60s", 0.0))
        base = float(cascade.get("baseline_60s", 0.0))
    except (TypeError, ValueError):
        liq, base = 0.0, 0.0
    if base > 0:
        ratio = liq / base
    dom = cascade.get("dominant_side")
    # A cascade you cannot attribute to a side is not cascade information. The OI-drop proxy always
    # reports dominant_side null - a 5m fall in open interest is closed positions of every kind - and
    # measured on 14 days of SOL it fired on 2.4% of snapshots and cost 11-23% of net PnL by widening
    # spreads and halving size for no reason. So the brake requires an attributed side by default.
    if ratio >= cascade_ratio_brake and (dom in ("long", "short") or not require_attributed_cascade):
        spread_mult = cascade_spread_mult
        amount_mult = cascade_amount_mult
        if dom == "long":     # longs being liquidated -> price falling -> don't buy the knife
            pull_buy = True
        elif dom == "short":  # shorts being liquidated -> price ripping -> don't sell into it
            pull_sell = True

    reasons = []
    if up:
        reasons.append(f"up {float(up['price']):.4g} @{up_d:.2f}natr size{up_s:.2f}")
    if down:
        reasons.append(f"down {float(down['price']):.4g} @{down_d:.2f}natr size{down_s:.2f}")
    if ratio:
        reasons.append(f"cascade x{ratio:.1f}")
    return FuelSignal(state=FUEL_LIVE, price_shift=price_shift, spread_multiplier=spread_mult,
                      amount_multiplier=amount_mult, pull_buy=pull_buy, pull_sell=pull_sell,
                      up_cluster=up, down_cluster=down, up_dist_natr=up_d, down_dist_natr=down_d,
                      cascade_ratio=ratio, vol_dist=vol_dist, reason="; ".join(reasons))


def latest_snapshot_at(history: List[dict], ts: float) -> Optional[dict]:
    """Backtest helper: the most recent snapshot whose ts <= candle ts (history sorted by ts)."""
    best = None
    for s in history:
        try:
            if float(s["ts"]) <= ts:
                best = s
            else:
                break
        except (KeyError, TypeError, ValueError):
            continue
    return best
