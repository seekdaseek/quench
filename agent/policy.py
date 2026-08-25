"""
AGENT POLICY — turn a routine's report into controller settings.

This is the deciding layer. It reads the fuelmap report and produces a bounded, explained change to
the controller's YAML. It is deterministic by default; an LLM may CHOOSE among the candidates this
file generates, but can never author a number of its own (see agent/tune.py --llm).

THE ONE RULE THAT IS NOT NEGOTIABLE — THE FEE FLOOR
  Measured Aug 15 on 14 days of SOL-USDT 1m, gross edge per round trip by quoted spread:
      4,8  -> 2.42 bps      5,10 -> 5.73 bps      6,12 -> 6.06 bps      8,16 -> 7.07 bps
  A configuration only makes money if gross_bps > 2 x maker_fee_bps. At Bitget VIP0 (2.0 bps/side)
  the round trip is 4.0 bps, so 4,8 loses by construction however often it fills. The policy will
  NOT propose spreads below `min_spread_units`, ever, whatever the report says. An agent that can
  quote itself into a guaranteed loss is not an agent, it is a liability.

WHAT THE TILT IS, AND WHAT IT IS NOT
  When the fuel map shows an unusually close unburned cluster on one side, the policy widens THAT
  side and shifts size to the other. That is all. It is a slow, bounded, reversible lean expressed
  in settings the framework already updates live.

  🔴 STATE THIS IN ANY WRITE-UP: the tilt is a HYPOTHESIS, not a measured win. What IS measured is
  that the same idea applied per-tick did nothing — with the brake off, fuel ON and fuel OFF were
  identical to the cent over 14 days. The argument for retrying it here is that the map's own
  numbers say it is slow information (median cluster distance 5.57 vol units up, 2.63 down; only
  ~27% of snapshots carry a near cluster at all), and a quote sitting 30 bps from mid cannot react
  to something 5 vol units away. Slow information belongs on a slow clock. That is a reason to test
  it, not evidence that it works. It is testable with backtest/sweep.py and has not been tested yet.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Bounds:
    """Everything the policy is allowed to do. Nothing outside this is reachable."""
    min_spread_units: float = 5.0        # the fee floor, measured — see the module docstring
    max_spread_units: float = 16.0       # beyond this the tape shows too few fills to mean anything
    max_tilt_pct: float = 0.40           # widen a side by at most 40%
    min_size_pct: float = 0.50           # never cut total size below half
    close_cluster_pctile: float = 20.0   # "unusually close" = inside the 20th percentile of history
    cascade_size_cut: float = 0.50       # size multiplier when an ATTRIBUTED cascade is running
    fee_bps_per_side: float = 2.0


@dataclass
class Decision:
    action: str                                   # "hold" | "tilt" | "derisk"
    reasons: List[str] = field(default_factory=list)
    changes: Dict[str, Any] = field(default_factory=dict)   # field -> new value, YAML-ready
    refused: List[str] = field(default_factory=list)        # things considered and rejected, with why

    def as_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "reasons": self.reasons, "changes": self.changes,
                "refused": self.refused}


def _spreads(value) -> List[float]:
    if isinstance(value, str):
        return [float(x) for x in value.split(",") if x.strip()]
    return [float(x) for x in value]


def _fmt(spreads: List[float]) -> str:
    return ",".join(f"{s:g}" for s in spreads)


def decide(report: Dict[str, Any], current: Dict[str, Any], bounds: Bounds = None) -> Decision:
    """Deterministic policy. `current` is the controller YAML as a dict.

    Returns a Decision whose `changes` are safe to write. Every path either changes something and
    says why, or holds and says why — there is no silent no-op.
    """
    b = bounds or Bounds()
    d = Decision(action="hold")

    state = report.get("state")
    if state != "live":
        d.reasons.append(f"fuelmap state is {state!r}, not live. Holding current settings — a map "
                         f"we cannot trust is not a reason to move quotes.")
        return d

    buy = _spreads(current.get("buy_spreads", "5,10"))
    sell = _spreads(current.get("sell_spreads", "5,10"))
    base_size = float(current.get("total_amount_quote", 800))

    # ---- 1. attributed cascade: cut size, do not tilt ----
    casc = report.get("cascade") or {}
    if casc.get("attributed") and (casc.get("ratio") or 0) >= 3.0:
        side = casc.get("dominant_side")
        new_size = round(base_size * b.cascade_size_cut, 2)
        d.action = "derisk"
        d.reasons.append(f"attributed cascade running at {casc['ratio']}x baseline, "
                         f"{side} positions being liquidated. Cutting size to {new_size}.")
        d.changes["total_amount_quote"] = new_size
        return d
    if (casc.get("ratio") or 0) >= 3.0 and not casc.get("attributed"):
        d.refused.append(f"cascade ratio {casc['ratio']}x but dominant_side is null. An unattributed "
                         f"spike is ordinary position closing, not a cascade — measured Aug 15 as the "
                         f"entire cost of the old fuel layer. Ignored.")

    # ---- 2. tilt away from an unusually close cluster ----
    pctiles = report.get("percentile_of_current") or {}
    above_p, below_p = pctiles.get("above"), pctiles.get("below")
    close_above = above_p is not None and above_p <= b.close_cluster_pctile
    close_below = below_p is not None and below_p <= b.close_cluster_pctile

    if close_above and close_below:
        d.reasons.append(f"clusters unusually close on BOTH sides ({above_p}th / {below_p}th pct). "
                         f"A two-sided tilt is no tilt. Holding.")
        return d
    if not close_above and not close_below:
        def where(p):
            return "no cluster" if p is None else f"{p}th pct"
        d.reasons.append(f"nearest clusters are ordinary for this tape — above: {where(above_p)}, "
                         f"below: {where(below_p)}. Holding.")
        return d

    side = "sell" if close_above else "buy"
    p = above_p if close_above else below_p
    # tilt scales with how unusual the reading is, capped
    strength = min(1.0, (b.close_cluster_pctile - p) / b.close_cluster_pctile)
    factor = 1.0 + b.max_tilt_pct * strength

    target = sell if side == "sell" else buy
    widened = [min(b.max_spread_units, round(s * factor, 2)) for s in target]
    if widened == target:
        d.reasons.append(f"tilt would hit the {b.max_spread_units} spread ceiling and change nothing. Holding.")
        return d

    d.action = "tilt"
    d.changes[f"{side}_spreads"] = _fmt(widened)
    d.reasons.append(
        f"unburned cluster {'above' if close_above else 'below'} at the {p}th percentile of its own "
        f"history — closer than usual. Widening the {side} side {target} -> {widened} "
        f"({(factor - 1) * 100:.0f}%), leaving the other side alone.")

    # the floor applies to the result, not just the input
    floor_hit = [s for s in widened if s < b.min_spread_units]
    if floor_hit:
        d.changes.pop(f"{side}_spreads")
        d.action = "hold"
        d.reasons.append(f"REFUSED: result {widened} contains spreads under the measured fee floor "
                         f"{b.min_spread_units}.")
        return d
    return d


def validate(changes: Dict[str, Any], bounds: Bounds = None) -> List[str]:
    """Last gate before anything is written. Returns a list of violations; empty means safe.

    Called on the deterministic path AND on anything an LLM selects, so the LLM cannot route around
    the floor by picking a candidate that was never generated here.
    """
    b = bounds or Bounds()
    bad = []
    for key in ("buy_spreads", "sell_spreads"):
        if key in changes:
            try:
                vals = _spreads(changes[key])
            except (TypeError, ValueError):
                bad.append(f"{key}: {changes[key]!r} is not a spread list")
                continue
            if not vals:
                bad.append(f"{key}: empty")
            for s in vals:
                if s < b.min_spread_units:
                    bad.append(f"{key}: {s} is under the measured fee floor {b.min_spread_units} "
                               f"(gross edge there does not clear a {2 * b.fee_bps_per_side} bps round trip)")
                if s > b.max_spread_units:
                    bad.append(f"{key}: {s} is over the ceiling {b.max_spread_units}")
    if "total_amount_quote" in changes:
        try:
            float(changes["total_amount_quote"])
        except (TypeError, ValueError):
            bad.append("total_amount_quote is not a number")
    allowed = {"buy_spreads", "sell_spreads", "buy_amounts_pct", "sell_amounts_pct",
               "total_amount_quote", "executor_refresh_time", "cooldown_time",
               "tp_spread_mult", "sl_spread_mult", "inventory_skew_natr", "manual_kill_switch"}
    for k in changes:
        if k not in allowed:
            bad.append(f"{k} is not an is_updatable field — writing it would need a bot restart, "
                       f"which the agent is not allowed to cause")
    return bad
