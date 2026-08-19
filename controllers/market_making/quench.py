"""
quench — a liquidation-aware perpetual market maker (Hummingbot Strategy V2 controller).

Baseline: two-sided quoting with spreads in units of NATR (volatility-adaptive), inventory-aware
reference price, per-fill triple-barrier risk (TP/SL/time in NATR units).

Fuel layer (the part that is ours): a "fuel map" of leverage-implied liquidation clusters above and
below price, published by a small collector service (see service/fuelmap_service.py). Doctrine:

  1. MAGNET LEAN   — price tends to walk toward the largest UNBURNED cluster; lean the reference
                     price toward it, bounded by max_lean_natr * NATR.
  2. FUSE PULL     — inside fuse_natr of a large cluster ABOVE (short liquidations => squeeze up),
                     pull our SELL quotes; inside fuse_natr of a large cluster BELOW (long
                     liquidations => cascade down), pull our BUY quotes. Never be the counterparty
                     of a cascade.
  3. CASCADE BRAKE — when realized liquidation flow spikes vs baseline, widen spreads and cut size,
                     and pull the side being liquidated.
  4. BURNED = SPENT — a cluster the tape has traded through is gone; the lean flips off.
  5. FAIL-SAFE     — feed stale or absent => fuel layer OFF, plain vol-adaptive PMM. Never act on
                     stale fuel. Status line always says FUEL: live | stale | absent | off.

Fuel snapshot JSON (served by the collector):
{
  "symbol": "SOLUSDT", "ts": 1755270000, "ref_price": 75.46, "source": "binance_oi_tiers_v1",
  "clusters": [ {"price": 76.4, "side": "short", "notional": 3700000, "burned": false}, ... ],
  "cascade": {"liq_notional_60s": 120000, "baseline_60s": 25000, "dominant_side": "long"}
}
"side" is the side of the positions that get liquidated at that price:
  "short" clusters sit ABOVE price (short liquidations = forced buys), "long" clusters sit BELOW.
"""
import json
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401
from pydantic import ConfigDict, Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.candles_feed.candles_base import CandlesBase
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction, StopExecutorAction

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


# --------------------------------------------------------------------------------------------------
# Controller
# --------------------------------------------------------------------------------------------------

class QuenchControllerConfig(MarketMakingControllerConfigBase):
    # validate defaults so the derived fields (candles_connector, fuel_symbol, amounts_pct) resolve even when a
    # field is omitted from the YAML — the base classes assume every field is present in the file.
    model_config = ConfigDict(arbitrary_types_allowed=True, validate_assignment=True, title=None, extra="forbid",
                              validate_default=True)
    controller_name: str = "quench"
    # spreads are in units of NATR (like pmm_dynamic)
    buy_spreads: List[float] = Field(
        default="1,2",
        json_schema_extra={"prompt": "Buy spreads in NATR units (e.g. '1,2'): ", "prompt_on_new": True, "is_updatable": True})
    sell_spreads: List[float] = Field(
        default="1,2",
        json_schema_extra={"prompt": "Sell spreads in NATR units (e.g. '1,2'): ", "prompt_on_new": True, "is_updatable": True})
    candles_connector: str = Field(
        default=None,
        json_schema_extra={"prompt": "Candles connector (blank = same as connector): ", "prompt_on_new": True})
    candles_trading_pair: str = Field(
        default=None,
        json_schema_extra={"prompt": "Candles trading pair (blank = same as trading pair): ", "prompt_on_new": True})
    interval: str = Field(default="1m", json_schema_extra={"prompt": "Candle interval: ", "prompt_on_new": True})
    natr_length: int = Field(default=14, json_schema_extra={"prompt": "NATR length: ", "prompt_on_new": True})
    min_spread_pct: Decimal = Field(
        default=Decimal("0.0005"),
        json_schema_extra={"prompt": "Floor for one NATR unit as a fraction of price (e.g. 0.0005 = 5 bps): ", "prompt_on_new": True})
    # risk per fill, in NATR units (used only when the *_spread_mult fields below are 0)
    tp_natr: Decimal = Field(default=Decimal("1.0"), json_schema_extra={"prompt": "Take profit in NATR units: ", "prompt_on_new": True})
    sl_natr: Decimal = Field(default=Decimal("3.0"), json_schema_extra={"prompt": "Stop loss in NATR units: ", "prompt_on_new": True})
    # Barriers as a MULTIPLE OF THE QUOTED SPREAD of the level that filled. This is what makes a wider
    # quote actually pay: a fill at mid - s must exit near mid + s to capture 2s. With a fixed tp the
    # quoted spread is never harvested and gross edge per round trip is flat in s (measured Aug 15:
    # ~2.3 bps at every spread from 1 to 5 NATR). 1.0 = exit back at mid, 2.0 = exit at the opposite quote.
    tp_spread_mult: Decimal = Field(
        default=Decimal("1.0"),
        json_schema_extra={"prompt": "Take profit as a multiple of the filled level's spread (0 = use tp_natr): ",
                           "prompt_on_new": True, "is_updatable": True})
    sl_spread_mult: Decimal = Field(
        default=Decimal("2.0"),
        json_schema_extra={"prompt": "Stop loss as a multiple of the filled level's spread (0 = use sl_natr): ",
                           "prompt_on_new": True, "is_updatable": True})
    # a take profit under the round-trip fee is a guaranteed loss however often it hits
    fee_bps_per_side: Decimal = Field(default=Decimal("2.0"), json_schema_extra={"prompt": "Maker fee in bps per side: "})
    tp_fee_multiple: Decimal = Field(default=Decimal("1.5"), json_schema_extra={"prompt": "Floor the take profit at this multiple of the round-trip fee: "})
    # inventory skew: shift reference by -inventory_skew_natr * NATR * (net_base / max_base)
    inventory_skew_natr: Decimal = Field(default=Decimal("0.5"), json_schema_extra={"prompt": "Inventory skew in NATR units at full inventory: ", "prompt_on_new": True})
    max_inventory_quote: Decimal = Field(default=Decimal("0"), json_schema_extra={"prompt": "Inventory considered 'full' in quote (0 = total_amount_quote): ", "prompt_on_new": True})
    # fuel layer
    fuel_enabled: bool = Field(default=True, json_schema_extra={"prompt": "Enable the liquidation-fuel layer (True/False): ", "prompt_on_new": True})
    fuel_url: str = Field(
        default="",
        json_schema_extra={"prompt": "Fuel snapshot URL ({symbol} is replaced, e.g. https://host/fuel/{symbol}.json): ", "prompt_on_new": True})
    fuel_symbol: str = Field(default="", json_schema_extra={"prompt": "Symbol key for the fuel feed (blank = trading pair without dash): ", "prompt_on_new": True})
    fuel_refresh_seconds: int = Field(default=10)
    fuel_max_age_seconds: int = Field(default=90)
    fuel_horizon_minutes: int = Field(
        default=60,
        json_schema_extra={"prompt": "Horizon in minutes for the fuel distance unit (realized vol over this window): "})
    vol_window: int = Field(default=60, json_schema_extra={"prompt": "Rolling window (bars) for realized volatility: "})
    fuel_min_notional: Decimal = Field(default=Decimal("250000"))
    fuel_reference_notional: Decimal = Field(default=Decimal("2000000"))
    lean_horizon_natr: Decimal = Field(default=Decimal("3.0"))
    max_lean_natr: Decimal = Field(default=Decimal("0.5"))
    fuse_natr: Decimal = Field(default=Decimal("1.5"))   # calibrated Aug 15 off measured p05 cluster distance (1.36-1.64)
    fuse_min_size: Decimal = Field(default=Decimal("0.5"))
    cascade_ratio_brake: Decimal = Field(default=Decimal("3.0"))
    cascade_spread_mult: Decimal = Field(default=Decimal("2.0"))
    cascade_amount_mult: Decimal = Field(default=Decimal("0.5"))
    require_attributed_cascade: bool = Field(
        default=True,
        json_schema_extra={"prompt": "Only brake when the cascade names a liquidated side (True unless you feed "
                                     "real liquidation data): "})
    # backtest-only: path to a JSON list of fuel snapshots (sorted by ts). Ignored when blank.
    fuel_history_path: str = Field(default="")
    # backtest-only: staleness applied during replay. 0 = a snapshot stays valid until the next one
    # (step function). Set >0 to replay the live max-age rule against a densely recorded history.
    fuel_history_max_age_seconds: int = Field(default=0)

    @field_validator("candles_connector", mode="before")
    @classmethod
    def set_candles_connector(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("connector_name")
        return v

    @field_validator("candles_trading_pair", mode="before")
    @classmethod
    def set_candles_trading_pair(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("trading_pair")
        return v

    @field_validator("fuel_symbol", mode="before")
    @classmethod
    def set_fuel_symbol(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            tp = validation_info.data.get("trading_pair") or ""
            return tp.replace("-", "")
        return v

    def fuel_kwargs(self) -> dict:
        return dict(
            enabled=self.fuel_enabled,
            max_age_s=float(self.fuel_max_age_seconds),
            min_notional=float(self.fuel_min_notional),
            reference_notional=float(self.fuel_reference_notional),
            lean_horizon_natr=float(self.lean_horizon_natr),
            max_lean_natr=float(self.max_lean_natr),
            fuse_natr=float(self.fuse_natr),
            fuse_min_size=float(self.fuse_min_size),
            cascade_ratio_brake=float(self.cascade_ratio_brake),
            cascade_spread_mult=float(self.cascade_spread_mult),
            cascade_amount_mult=float(self.cascade_amount_mult),
            require_attributed_cascade=self.require_attributed_cascade,
        )


class QuenchController(MarketMakingControllerBase):
    """
    Liquidation-aware market maker. See module docstring for the doctrine.
    """

    def __init__(self, config: QuenchControllerConfig, *args, **kwargs):
        self.config = config
        self.max_records = max(config.natr_length, config.vol_window) + 100
        interval_s = CandlesBase.interval_to_seconds[config.interval]
        self.horizon_bars = max(1.0, config.fuel_horizon_minutes * 60.0 / interval_s)
        self._fuel_snapshot: Optional[dict] = None
        self._fuel_fetched_at: float = 0.0
        self._fuel_last_error: str = ""
        self._fuel_history: List[dict] = []
        self._fuel_signal: FuelSignal = neutral(FUEL_OFF if not config.fuel_enabled else FUEL_ABSENT)
        if config.fuel_history_path:
            with open(config.fuel_history_path) as fh:
                hist = json.load(fh)
            self._fuel_history = sorted(hist, key=lambda s: float(s["ts"]))
        super().__init__(config, *args, **kwargs)

    # ------------------------------------------------------------------ candles
    def get_candles_config(self) -> List[CandlesConfig]:
        return [CandlesConfig(connector=self.config.candles_connector,
                              trading_pair=self.config.candles_trading_pair,
                              interval=self.config.interval,
                              max_records=self.max_records)]

    # ------------------------------------------------------------------ fuel feed (live)
    def _fuel_url(self) -> str:
        return self.config.fuel_url.replace("{symbol}", self.config.fuel_symbol)

    async def _maybe_refresh_fuel(self, now: float):
        if not self.config.fuel_enabled or not self.config.fuel_url or self._fuel_history:
            return
        if now - self._fuel_fetched_at < self.config.fuel_refresh_seconds:
            return
        self._fuel_fetched_at = now
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=3)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._fuel_url()) as resp:
                    if resp.status != 200:
                        self._fuel_last_error = f"HTTP {resp.status}"
                        return
                    data = await resp.json(content_type=None)
            if isinstance(data, dict) and "clusters" in data and "ts" in data:
                self._fuel_snapshot = data
                self._fuel_last_error = ""
            else:
                self._fuel_last_error = "malformed snapshot"
        except Exception as e:  # keep the last good snapshot; staleness will neutralise it
            self._fuel_last_error = f"{type(e).__name__}: {e}"[:120]

    def _snapshot_for(self, ts: float) -> Optional[dict]:
        if self._fuel_history:
            return latest_snapshot_at(self._fuel_history, ts)
        return self._fuel_snapshot

    # ------------------------------------------------------------------ inventory
    def _inventory_shift(self, natr: float) -> float:
        """Shift as a fraction of price: negative when long (lower quotes to sell), positive when short."""
        if natr <= 0 or not math.isfinite(natr):
            return 0.0
        max_quote = float(self.config.max_inventory_quote) if self.config.max_inventory_quote > 0 else float(self.config.total_amount_quote)
        if max_quote <= 0:
            return 0.0
        ref = float(self.processed_data.get("reference_price", 0) or 0)
        if ref <= 0:
            return 0.0
        net_base = float(self.get_current_base_position())
        frac = max(-1.0, min(1.0, net_base * ref / max_quote))
        return -frac * float(self.config.inventory_skew_natr) * natr

    # ------------------------------------------------------------------ core
    async def update_processed_data(self):
        now = float(self.market_data_provider.time())
        await self._maybe_refresh_fuel(now)
        candles = self.market_data_provider.get_candles_df(connector_name=self.config.candles_connector,
                                                           trading_pair=self.config.candles_trading_pair,
                                                           interval=self.config.interval,
                                                           max_records=self.max_records)
        candles = candles.copy()
        natr = ta.natr(candles["high"], candles["low"], candles["close"], length=self.config.natr_length) / 100
        floor = float(self.config.min_spread_pct)
        natr = natr.fillna(floor).clip(lower=floor)
        candles["natr"] = natr
        # distance unit: realized volatility over fuel_horizon_minutes, from log returns of the same feed.
        # sigma_bar * sqrt(bars_in_horizon). This is the move price would have to make to reach a cluster,
        # not the size of one bar — that distinction is what makes the fuse able to fire at all.
        logret = np.log(candles["close"].astype(float)).diff()
        sigma_bar = logret.rolling(self.config.vol_window, min_periods=max(5, self.config.vol_window // 4)).std()
        sigma_h = (sigma_bar * math.sqrt(self.horizon_bars)).fillna(floor).clip(lower=floor)
        candles["sigma_h"] = sigma_h
        kwargs = self.config.fuel_kwargs()
        if self._fuel_history:
            kwargs["max_age_s"] = (float(self.config.fuel_history_max_age_seconds)
                                   if self.config.fuel_history_max_age_seconds > 0 else math.inf)

        # per-candle fuel signal (history in backtest, current snapshot live -> only last row is used live)
        rows = []
        for ts, close, n, sh in zip(candles["timestamp"].astype(float), candles["close"].astype(float),
                                    candles["natr"].astype(float), candles["sigma_h"].astype(float)):
            snap = self._snapshot_for(ts)
            sig = compute_fuel_signal(snap, close, sh, ts if self._fuel_history else now, vol_shift=n, **kwargs)
            rows.append(sig.as_row())
        fuel_df = pd.DataFrame(rows, index=candles.index)
        candles = pd.concat([candles, fuel_df], axis=1)

        last_close = float(candles["close"].iloc[-1])
        last_natr = float(candles["natr"].iloc[-1])
        last_sigma_h = float(candles["sigma_h"].iloc[-1])
        last_snap = self._snapshot_for(float(candles["timestamp"].iloc[-1]))
        self._fuel_signal = compute_fuel_signal(last_snap, last_close, last_sigma_h,
                                                float(candles["timestamp"].iloc[-1]) if self._fuel_history else now,
                                                vol_shift=last_natr, **kwargs)

        # reference price = close * (1 + fuel lean + inventory skew); spread multiplier = natr * cascade brake
        candles["reference_price"] = candles["close"] * (1 + candles["fuel_shift"])
        candles["spread_multiplier"] = candles["natr"] * candles["fuel_spread_mult"]
        self.processed_data.update({
            "reference_price": Decimal(str(candles["reference_price"].iloc[-1])),
            "spread_multiplier": Decimal(str(candles["spread_multiplier"].iloc[-1])),
            "natr": last_natr,
            "sigma_h": last_sigma_h,
            "pull_buy": bool(candles["pull_buy"].iloc[-1]),
            "pull_sell": bool(candles["pull_sell"].iloc[-1]),
            "fuel_amount_mult": float(candles["fuel_amount_mult"].iloc[-1]),
            "fuel_state": self._fuel_signal.state,
            "features": candles,
        })
        inv = self._inventory_shift(last_natr)
        if inv:
            self.processed_data["reference_price"] = Decimal(str(float(self.processed_data["reference_price"]) * (1 + inv)))
        self.processed_data["inventory_shift"] = inv

    def get_levels_to_execute(self) -> List[str]:
        levels = super().get_levels_to_execute()
        if self.processed_data.get("pull_buy"):
            levels = [lv for lv in levels if not lv.startswith("buy")]
        if self.processed_data.get("pull_sell"):
            levels = [lv for lv in levels if not lv.startswith("sell")]
        return levels

    def executors_to_early_stop(self) -> List[ExecutorAction]:
        """Cancel resting (unfilled) quotes on a pulled side. Filled positions keep their barriers."""
        pulled = []
        if self.processed_data.get("pull_buy"):
            pulled.append(TradeType.BUY)
        if self.processed_data.get("pull_sell"):
            pulled.append(TradeType.SELL)
        if not pulled:
            return []
        to_stop = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_active and not x.is_trading and x.side in pulled
            and str(x.custom_info.get("level_id", "")).split("_")[0] in ("buy", "sell"))
        return [StopExecutorAction(controller_id=self.config.id, executor_id=e.id) for e in to_stop]

    def get_price_and_amount(self, level_id: str) -> Tuple[Decimal, Decimal]:
        price, amount = super().get_price_and_amount(level_id)
        mult = Decimal(str(self.processed_data.get("fuel_amount_mult", 1.0)))
        return price, amount * mult

    def level_spread_pct(self, level_id: str) -> Decimal:
        """The distance from the reference price this level is quoted at, as a fraction of price."""
        level = self.get_level_from_level_id(level_id)
        trade_type = self.get_trade_type_from_level_id(level_id)
        spreads = self.config.buy_spreads if trade_type == TradeType.BUY else self.config.sell_spreads
        spread_units = Decimal(str(spreads[min(level, len(spreads) - 1)]))
        return spread_units * Decimal(str(self.processed_data.get("spread_multiplier", self.config.min_spread_pct)))

    def barriers_for(self, level_id: str) -> Tuple[Decimal, Decimal]:
        """(take_profit, stop_loss) as fractions of the entry price."""
        natr = Decimal(str(self.processed_data.get("natr", float(self.config.min_spread_pct))))
        spread = self.level_spread_pct(level_id)
        tp = self.config.tp_spread_mult * spread if self.config.tp_spread_mult > 0 else self.config.tp_natr * natr
        sl = self.config.sl_spread_mult * spread if self.config.sl_spread_mult > 0 else self.config.sl_natr * natr
        floor = self.config.tp_fee_multiple * 2 * self.config.fee_bps_per_side / Decimal("10000")
        tp = max(tp, floor)
        sl = max(sl, tp)  # a stop tighter than the target is a guaranteed negative expectancy
        return tp, sl

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        trade_type = self.get_trade_type_from_level_id(level_id)
        take_profit, stop_loss = self.barriers_for(level_id)
        tb = TripleBarrierConfig(
            stop_loss=stop_loss,
            take_profit=take_profit,
            time_limit=self.config.time_limit,
            trailing_stop=self.config.trailing_stop,
            open_order_type=self.config.triple_barrier_config.open_order_type,
            take_profit_order_type=self.config.take_profit_order_type,
            stop_loss_order_type=self.config.triple_barrier_config.stop_loss_order_type,
            time_limit_order_type=self.config.triple_barrier_config.time_limit_order_type,
        )
        return PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            level_id=level_id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            entry_price=price,
            amount=amount,
            triple_barrier_config=tb,
            leverage=self.config.leverage,
            side=trade_type,
        )

    # ------------------------------------------------------------------ status
    def to_format_status(self) -> List[str]:
        s = self._fuel_signal
        lines = [
            f"quench {self.config.connector_name} {self.config.trading_pair} | ref {self.processed_data.get('reference_price')} "
            f"| natr {self.processed_data.get('natr', 0):.4%} | sigma_h {self.processed_data.get('sigma_h', 0):.4%} | inv shift {self.processed_data.get('inventory_shift', 0):+.5f}",
            f"FUEL: {s.state.upper()}  shift {s.price_shift:+.5f}  spread x{s.spread_multiplier:.2f}  amount x{s.amount_multiplier:.2f}"
            f"  pull_buy={s.pull_buy} pull_sell={s.pull_sell}",
        ]
        if s.reason:
            lines.append(f"  {s.reason}")
        if self._fuel_last_error:
            lines.append(f"  feed error: {self._fuel_last_error}")
        return lines

    def get_custom_info(self) -> dict:
        s = self._fuel_signal
        return {"fuel_state": s.state, "pull_buy": s.pull_buy, "pull_sell": s.pull_sell,
                "cascade_ratio": round(s.cascade_ratio, 2), "up_dist_natr": None if not math.isfinite(s.up_dist_natr) else round(s.up_dist_natr, 2),
                "down_dist_natr": None if not math.isfinite(s.down_dist_natr) else round(s.down_dist_natr, 2)}
