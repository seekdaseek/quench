"""
quench — a volatility-adaptive perpetual market maker (Hummingbot Strategy V2 controller).

WHAT THIS FILE DOES, AND NOTHING ELSE:
  - quotes two sides at spreads measured in NATR units (volatility-adaptive)
  - skews the reference price against inventory
  - sets per-fill triple barriers as a MULTIPLE OF THE FILLED LEVEL'S OWN SPREAD, floored at a
    multiple of the round-trip fee

It reads candles and its own positions. That is all the realtime data it needs. It makes no network
calls of its own, holds no external state, and knows nothing about liquidations.

THE SPLIT (Aug 25 2026, after Federico's review on Botcamp: "keep the controller simple and just
using the realtime data that you need, and use routines to get more general data and use the agent
to tune your controller based on that external data"):

  controller (this file)  quotes. fast, deterministic, no LLM, no external I/O
  routines/               builds the liquidation fuel map and reports on it. plain Python, slow
  agent/                  reads the report and rewrites this controller's YAML

The agent tunes only fields Hummingbot itself marks is_updatable. StrategyV2Base.
update_controllers_configs() re-reads the controller YAML every config_update_interval seconds
(default 10) and calls ControllerBase.update_config(), which applies exactly those fields via
model_copy without interrupting the bot. The agent never touches this code path at runtime — it
writes a file and the framework does the rest.

WHY THE FUEL LAYER LEFT THIS FILE, measured not assumed (Aug 15, 14 days SOL-USDT 1m, 20,161
candles): with the cascade brake disabled, fuel ON and fuel OFF produced identical results to the
cent — same 53 fills, same +$3.68 net, same 5.734 bps gross. The magnet lean touched 3,071 of
20,142 rows and changed nothing. The fuse fired zero times. Measured cluster distances explain it:
median 5.57 horizon-vol units above and 2.63 below, far outside a quote sitting ~30 bps from mid.
The map is real information on a slow clock. It was never tick-rate information, and this file runs
at tick rate.
"""
import math
from decimal import Decimal
from typing import List, Tuple

import pandas_ta as ta  # noqa: F401
from pydantic import ConfigDict, Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig


class QuenchControllerConfig(MarketMakingControllerConfigBase):
    # validate defaults so the derived fields (candles_connector, amounts_pct) resolve even when a
    # field is omitted from the YAML — the base classes assume every field is present in the file.
    model_config = ConfigDict(arbitrary_types_allowed=True, validate_assignment=True, title=None, extra="forbid",
                              validate_default=True)
    controller_name: str = "quench"
    # spreads in NATR units. is_updatable — the agent's widest lever, and it can move one side
    # without the other, which is how a side gets backed off now that pull_buy/pull_sell are gone.
    buy_spreads: List[float] = Field(
        default="5,10",
        json_schema_extra={"prompt": "Buy spreads in NATR units (e.g. '5,10'): ", "prompt_on_new": True, "is_updatable": True})
    sell_spreads: List[float] = Field(
        default="5,10",
        json_schema_extra={"prompt": "Sell spreads in NATR units (e.g. '5,10'): ", "prompt_on_new": True, "is_updatable": True})
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
        default=Decimal("3.0"),
        json_schema_extra={"prompt": "Stop loss as a multiple of the filled level's spread (0 = use sl_natr): ",
                           "prompt_on_new": True, "is_updatable": True})
    # a take profit under the round-trip fee is a guaranteed loss however often it hits
    fee_bps_per_side: Decimal = Field(default=Decimal("2.0"), json_schema_extra={"prompt": "Maker fee in bps per side: "})
    tp_fee_multiple: Decimal = Field(default=Decimal("1.5"), json_schema_extra={"prompt": "Floor the take profit at this multiple of the round-trip fee: "})
    # inventory skew: shift reference by -inventory_skew_natr * NATR * (net_base / max_base)
    inventory_skew_natr: Decimal = Field(
        default=Decimal("0.5"),
        json_schema_extra={"prompt": "Inventory skew in NATR units at full inventory: ",
                           "prompt_on_new": True, "is_updatable": True})
    max_inventory_quote: Decimal = Field(default=Decimal("0"), json_schema_extra={"prompt": "Inventory considered 'full' in quote (0 = total_amount_quote): ", "prompt_on_new": True})

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


class QuenchController(MarketMakingControllerBase):
    """Volatility-adaptive market maker. Quotes and risk only — see the module docstring."""

    def __init__(self, config: QuenchControllerConfig, *args, **kwargs):
        self.config = config
        self.max_records = config.natr_length + 100
        super().__init__(config, *args, **kwargs)

    # ------------------------------------------------------------------ candles
    def get_candles_config(self) -> List[CandlesConfig]:
        return [CandlesConfig(connector=self.config.candles_connector,
                              trading_pair=self.config.candles_trading_pair,
                              interval=self.config.interval,
                              max_records=self.max_records)]

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
        candles = self.market_data_provider.get_candles_df(connector_name=self.config.candles_connector,
                                                           trading_pair=self.config.candles_trading_pair,
                                                           interval=self.config.interval,
                                                           max_records=self.max_records)
        candles = candles.copy()
        natr = ta.natr(candles["high"], candles["low"], candles["close"], length=self.config.natr_length) / 100
        floor = float(self.config.min_spread_pct)
        natr = natr.fillna(floor).clip(lower=floor)
        candles["natr"] = natr

        last_natr = float(candles["natr"].iloc[-1])
        candles["reference_price"] = candles["close"]
        candles["spread_multiplier"] = candles["natr"]
        self.processed_data.update({
            "reference_price": Decimal(str(candles["reference_price"].iloc[-1])),
            "spread_multiplier": Decimal(str(candles["spread_multiplier"].iloc[-1])),
            "natr": last_natr,
            "features": candles,
        })
        inv = self._inventory_shift(last_natr)
        if inv:
            self.processed_data["reference_price"] = Decimal(str(float(self.processed_data["reference_price"]) * (1 + inv)))
        self.processed_data["inventory_shift"] = inv

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
        return [
            f"quench {self.config.connector_name} {self.config.trading_pair} "
            f"| ref {self.processed_data.get('reference_price')} "
            f"| natr {self.processed_data.get('natr', 0):.4%} "
            f"| inv shift {self.processed_data.get('inventory_shift', 0):+.5f}",
            f"spreads buy {self.config.buy_spreads} sell {self.config.sell_spreads} "
            f"| tpX {self.config.tp_spread_mult} slX {self.config.sl_spread_mult} "
            f"| size {self.config.total_amount_quote}",
        ]

    def get_custom_info(self) -> dict:
        return {"natr": round(float(self.processed_data.get("natr", 0.0)), 6),
                "inventory_shift": round(float(self.processed_data.get("inventory_shift", 0.0)), 6),
                "buy_spreads": list(self.config.buy_spreads),
                "sell_spreads": list(self.config.sell_spreads)}
