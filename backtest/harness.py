"""
Offline harness around Hummingbot's real BacktestingEngineBase.

- OfflineDataProvider: BacktestingDataProvider whose candles and trading rules are injected, so no
  exchange connection is needed (sandbox-safe). Real candles can be loaded from a CSV with columns
  timestamp,open,high,low,close,volume (epoch seconds), or synthesised.
- MarketMakingBacktesting: the per-row processed_data update the base engine leaves abstract.
"""
import asyncio
import importlib.util
import math
import os
import sys
from decimal import Decimal
from typing import Dict, Optional

import numpy as np
import pandas as pd

from hummingbot.connector.trading_rule import TradingRule
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.backtesting.backtesting_data_provider import BacktestingDataProvider
from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER_PATH = os.path.join(REPO_ROOT, "controllers", "market_making", "quench.py")


def load_quench_module():
    """Import controllers/market_making/quench.py as module 'quench' regardless of sys.path."""
    if "quench" in sys.modules:
        return sys.modules["quench"]
    spec = importlib.util.spec_from_file_location("quench", CONTROLLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["quench"] = mod
    spec.loader.exec_module(mod)
    return mod


class OfflineDataProvider(BacktestingDataProvider):
    def __init__(self, candles: Dict[str, pd.DataFrame], trading_rules: Dict[str, Dict[str, TradingRule]]):
        super().__init__(connectors={})
        self.candles_feeds = dict(candles)          # key: f"{connector}_{pair}_{interval}"
        self.trading_rules = trading_rules

    async def initialize_trading_rules(self, connector_name: str):
        # injected; never touch the network
        if connector_name not in self.trading_rules:
            raise KeyError(f"no injected trading rules for {connector_name}")

    def initialize_rate_sources(self, connector_pairs):
        # MarketMakingControllerBase calls this in __init__ and the base implementation spins up a real
        # connector to poll last traded prices. Offline that logs an error per run and depends on the
        # network for nothing: the backtest prices come from the injected candles.
        return None

    async def get_candles_feed(self, config: CandlesConfig):
        key = self._generate_candle_feed_key(config)
        if key not in self.candles_feeds:
            raise KeyError(f"no injected candles for {key}")
        return self.candles_feeds[key]


class MarketMakingBacktesting(BacktestingEngineBase):
    """Row-level update: mirrors what the controller writes to processed_data live."""

    async def update_processed_data(self, row: pd.Series):
        pdict = self.controller.processed_data
        pdict["reference_price"] = Decimal(str(row["reference_price"]))
        pdict["spread_multiplier"] = Decimal(str(row["spread_multiplier"]))
        for k in ("pull_buy", "pull_sell"):
            if k in row:
                pdict[k] = bool(row[k])
        if "fuel_amount_mult" in row:
            pdict["fuel_amount_mult"] = float(row["fuel_amount_mult"])
        if "natr" in row:
            pdict["natr"] = float(row["natr"])


def default_trading_rule(pair: str) -> TradingRule:
    return TradingRule(trading_pair=pair, min_order_size=Decimal("0.1"), max_order_size=Decimal("1000000"),
                       min_price_increment=Decimal("0.001"), min_base_amount_increment=Decimal("0.1"),
                       min_quote_amount_increment=Decimal("0.0001"), min_notional_size=Decimal("5"))


def synthetic_candles(start_ts: int, n: int, interval_s: int = 60, p0: float = 75.0, vol: float = 0.0015,
                      seed: int = 7, drift_events: Optional[list] = None) -> pd.DataFrame:
    """
    Random-walk 1m candles. drift_events: list of (start_idx, end_idx, drift_per_bar) to inject
    directional moves (e.g. a squeeze into a cluster).
    """
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, vol, n)
    for (a, b, d) in (drift_events or []):
        rets[a:b] += d
    close = p0 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[p0], close[:-1]])
    wick = np.abs(rng.normal(0, vol / 2, n)) * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    volume = rng.uniform(500, 5000, n)
    ts = start_ts + np.arange(n) * interval_s
    return pd.DataFrame({"timestamp": ts.astype(float), "open": open_, "high": high, "low": low,
                         "close": close, "volume": volume, "quote_asset_volume": volume * close,
                         "n_trades": rng.integers(50, 500, n), "taker_buy_base_volume": volume / 2,
                         "taker_buy_quote_volume": volume * close / 2})


def load_candles_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    need = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing columns {sorted(missing)}")
    df = df.sort_values("timestamp").reset_index(drop=True)
    if df["timestamp"].iloc[-1] > 1e12:  # ms -> s
        df["timestamp"] = df["timestamp"] / 1000.0
    df["timestamp"] = df["timestamp"].astype(float)
    return df


def run(config, candles: pd.DataFrame, start: Optional[int] = None, end: Optional[int] = None,
        resolution: str = "1m", trade_cost: float = 0.0002):
    """Run the real V2 backtesting engine on the quench controller with injected candles."""
    interval = config.interval
    keys = {
        f"{config.connector_name}_{config.trading_pair}_{resolution}": candles,
        f"{config.candles_connector}_{config.candles_trading_pair}_{interval}": candles,
    }
    rules = {config.connector_name: {config.trading_pair: default_trading_rule(config.trading_pair)}}
    provider = OfflineDataProvider(keys, rules)
    engine = MarketMakingBacktesting()
    engine.backtesting_data_provider = provider
    warmup = (config.natr_length + 5) * 60
    start = int(candles["timestamp"].iloc[0] + warmup) if start is None else start
    end = int(candles["timestamp"].iloc[-1]) if end is None else end
    return asyncio.get_event_loop().run_until_complete(
        engine.run_backtesting(controller_config=config, start=start, end=end,
                               backtesting_resolution=resolution, trade_cost=trade_cost))


def summarize(result: dict) -> dict:
    r = result["results"]
    keys = ["net_pnl", "net_pnl_quote", "total_executors", "total_executors_with_position", "total_volume",
            "total_long", "total_short", "accuracy", "max_drawdown_usd", "max_drawdown_pct", "sharpe_ratio",
            "profit_factor", "win_signals", "loss_signals"]
    out = {k: r.get(k) for k in keys if k in r}
    out["executors"] = len(result["executors"])
    return out


FUEL_MODEL_PATH = os.path.join(REPO_ROOT, "routines", "fuel", "model.py")


def load_fuel_module():
    """Import routines/fuel/model.py as module 'fuelmodel'.

    The pure fuel math moved out of the controller on Aug 25 2026 (see the controller docstring).
    It imports no Hummingbot and no framework — the tests that cover it are plain unit tests now.
    """
    if "fuelmodel" in sys.modules:
        return sys.modules["fuelmodel"]
    spec = importlib.util.spec_from_file_location("fuelmodel", FUEL_MODEL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fuelmodel"] = mod
    spec.loader.exec_module(mod)
    return mod
