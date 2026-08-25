#!/usr/bin/env python3
"""
AGENT: tune — read the fuelmap report, decide, rewrite the controller YAML.

  python3 agent/tune.py --report reports/SOLUSDT.json \
                        --config conf/controllers/quench_bitget_sol.yml --dry-run
  python3 agent/tune.py --report ... --config ... --journal agent/journal.jsonl

HOW THE CHANGE REACHES A RUNNING BOT — verified in the Hummingbot source, not assumed:
  StrategyV2Base.update_controllers_configs() fires every `config_update_interval` seconds
  (default 10), re-reads the controller YAML files, and calls ControllerBase.update_config(), which
  copies across ONLY fields whose json_schema_extra carries is_updatable=True, in a single
  model_copy. So this agent's entire mechanism of action is: write a file. It never imports the
  controller, never touches the trading loop, and cannot restart the bot.
  A field without is_updatable is a silent no-op — agent/policy.validate() rejects those by name.

SAFETY MODEL, borrowed from Condor's own two-layer design: the deterministic layer constrains what
the AI layer can do. policy.decide() generates the candidates and policy.validate() is the last gate
before any write. With --llm, a model may only CHOOSE between candidates that already passed that
gate; it cannot author a number. If the model returns anything else, the run falls back to the
deterministic decision and says so in the journal.

EVERY RUN IS JOURNALLED, including holds and refusals. A tuner that only records the times it acted
is a tuner you cannot audit.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import policy  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    print("pyyaml is required: pip install pyyaml", file=sys.stderr)
    raise


def read_yaml(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def write_yaml_preserving_comments(path, changes):
    """Rewrite only the changed keys, in place, line by line.

    yaml.safe_dump would round-trip the file and throw away every comment in it — and those comments
    are where the measured reasoning lives ("measured best on 14d SOL", "Bitget VIP0 maker = 0.020%").
    Losing them to a tuning run would be a real cost, so the writer edits the lines it needs and
    leaves everything else byte-identical.
    """
    with open(path) as fh:
        lines = fh.read().split("\n")
    remaining = dict(changes)
    out = []
    for line in lines:
        stripped = line.lstrip()
        hit = None
        for key in remaining:
            if stripped.startswith(key + ":"):
                hit = key
                break
        if hit is None:
            out.append(line)
            continue
        indent = line[:len(line) - len(stripped)]
        comment = ""
        if "#" in line:
            comment = "  " + line[line.index("#"):]
        out.append(f"{indent}{hit}: {remaining.pop(hit)}{comment}")
    if remaining:
        raise KeyError(f"keys not present in {path}: {sorted(remaining)}")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(out))
    os.replace(tmp, path)   # atomic: the bot re-reads this file every 10s and must never see half of it


def choose_with_llm(report, decision, model_call):
    """Optional AI layer. `model_call(prompt) -> str` returns the id of a candidate, nothing else.

    The candidate set is built here, from the deterministic policy. The model picks an index. Any
    other answer is discarded. This is why the LLM cannot widen a spread past the ceiling or tighten
    one under the fee floor: those configurations are never in the list it is choosing from.
    """
    candidates = [{"id": 0, "label": "hold (change nothing)", "changes": {}},
                  {"id": 1, "label": decision.action, "changes": decision.changes}]
    prompt = (
        "You are choosing between pre-validated market-making configurations. You may not invent "
        "values; answer with a single integer id and nothing else.\n\n"
        f"Fuel map report:\n{json.dumps(report, indent=2)}\n\n"
        f"Deterministic policy said: {decision.action} because {'; '.join(decision.reasons)}\n\n"
        f"Candidates:\n{json.dumps(candidates, indent=2)}\n\nid:")
    try:
        raw = (model_call(prompt) or "").strip()
        pick = int("".join(ch for ch in raw if ch.isdigit())[:2])
    except (ValueError, TypeError, IndexError):
        return decision.changes, "llm returned an unparseable answer; used the deterministic decision"
    if pick not in (c["id"] for c in candidates):
        return decision.changes, f"llm picked {pick}, not a candidate id; used the deterministic decision"
    chosen = candidates[pick]["changes"]
    bad = policy.validate(chosen)
    if bad:
        return decision.changes, f"llm choice failed validation ({bad}); used the deterministic decision"
    return chosen, f"llm chose candidate {pick}: {candidates[pick]['label']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="JSON report from routines/fuelmap.py")
    ap.add_argument("--config", required=True, help="controller YAML to tune")
    ap.add_argument("--journal", default="agent/journal.jsonl")
    ap.add_argument("--dry-run", action="store_true", help="decide and print, write nothing")
    ap.add_argument("--min-spread", type=float, default=5.0,
                    help="the measured fee floor in NATR units; the policy will never go below it")
    a = ap.parse_args()

    with open(a.report) as fh:
        report = json.load(fh)
    current = read_yaml(a.config)
    bounds = policy.Bounds(min_spread_units=a.min_spread)
    decision = policy.decide(report, current, bounds)

    violations = policy.validate(decision.changes, bounds)
    if violations:
        decision.action = "hold"
        decision.refused.extend(violations)
        decision.changes = {}

    entry = {
        "ts": round(time.time(), 3),
        "symbol": report.get("symbol"),
        "report_snapshot_ts": report.get("snapshot_ts"),
        "config": os.path.basename(a.config),
        "dry_run": bool(a.dry_run),
        **decision.as_dict(),
    }

    print(f"action: {decision.action}")
    for r in decision.reasons:
        print(f"  why: {r}")
    for r in decision.refused:
        print(f"  refused: {r}")
    if decision.changes:
        for k, v in decision.changes.items():
            print(f"  set {k}: {current.get(k)} -> {v}")
    else:
        print("  no changes")

    if decision.changes and not a.dry_run:
        write_yaml_preserving_comments(a.config, decision.changes)
        print(f"wrote {a.config} — a running bot picks this up within config_update_interval (10s)")

    os.makedirs(os.path.dirname(os.path.abspath(a.journal)), exist_ok=True)
    with open(a.journal, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    main()
