"""Tests for the routine -> agent chain added by the Aug 25 2026 split.

The controller no longer decides anything about liquidations. These tests cover what took over that
job: a routine that reports, and an agent that decides inside hard bounds.

The bar for the agent is not "does it improve PnL" — nothing here claims that, and the tilt is an
untested hypothesis (see agent/policy.py's docstring). The bar is that it CANNOT do the two things
that would cost real money: quote under the measured fee floor, and write a field the running bot
would silently ignore.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from agent import policy  # noqa: E402
from routines import fuelmap  # noqa: E402

NOW = 1_756_000_000.0


def snap(clusters, ts=NOW, ref=180.0, cascade=None):
    return {"symbol": "SOLUSDT", "ts": ts, "ref_price": ref, "clusters": clusters,
            "cascade": cascade or {"liq_notional_60s": 0, "baseline_60s": 20000, "dominant_side": None}}


def history(n=60, ref=180.0, far=0.03, ts0=NOW - 60 * 300):
    """n snapshots whose clusters all sit ~far away, so 'close' means something."""
    out = []
    for i in range(n):
        out.append(snap([{"price": ref * (1 + far), "side": "short", "notional": 3e6, "burned": False},
                         {"price": ref * (1 - far), "side": "long", "notional": 3e6, "burned": False}],
                        ts=ts0 + i * 300, ref=ref))
    return out


def write(obj):
    p = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False).name
    with open(p, "w") as fh:
        json.dump(obj, fh)
    return p


CONFIG = {"buy_spreads": "5,10", "sell_spreads": "5,10", "total_amount_quote": 800}


class Routine(unittest.TestCase):
    def test_stale_snapshot_gives_no_advice(self):
        """A map from an hour ago is not information about now — same fail-safe the controller had."""
        p = write(snap([{"price": 185.0, "side": "short", "notional": 3e6, "burned": False}]))
        rep = fuelmap.build_report(p, now=NOW + 5000, max_age_s=900)
        self.assertEqual(rep["state"], "stale")
        self.assertNotIn("nearest_above", rep)
        os.unlink(p)

    def test_empty_snapshot_is_empty_not_zero(self):
        p = write(snap([]))
        rep = fuelmap.build_report(p, now=NOW)
        self.assertEqual(rep["state"], "empty")
        os.unlink(p)

    def test_current_reading_is_ranked_against_its_own_history(self):
        """The raw distance means nothing; the percentile is what an agent can act on."""
        h = history(far=0.03)
        h.append(snap([{"price": 180.0 * 1.004, "side": "short", "notional": 4e6, "burned": False}],
                      ts=NOW))
        p = write(h)
        rep = fuelmap.build_report(p, now=NOW + 10)
        self.assertEqual(rep["state"], "live")
        self.assertIsNotNone(rep["nearest_above"])
        self.assertLess(rep["percentile_of_current"]["above"], 20.0,
                        "a cluster 7x closer than every other snapshot should rank near the floor")
        os.unlink(p)

    def test_burned_clusters_are_not_counted_as_reachable(self):
        p = write(snap([{"price": 185.0, "side": "short", "notional": 5e6, "burned": True}]))
        rep = fuelmap.build_report(p, now=NOW)
        self.assertIsNone(rep["nearest_above"])
        os.unlink(p)


class Policy(unittest.TestCase):
    def test_holds_when_nothing_is_unusual(self):
        h = history(far=0.03)
        h.append(snap([{"price": 180 * 1.03, "side": "short", "notional": 3e6, "burned": False}], ts=NOW))
        p = write(h)
        d = policy.decide(fuelmap.build_report(p, now=NOW + 10), CONFIG)
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.changes, {})
        os.unlink(p)

    def test_tilts_only_the_side_the_cluster_is_on(self):
        h = history(far=0.03)
        h.append(snap([{"price": 180 * 1.004, "side": "short", "notional": 4e6, "burned": False}], ts=NOW))
        p = write(h)
        d = policy.decide(fuelmap.build_report(p, now=NOW + 10), CONFIG)
        self.assertEqual(d.action, "tilt")
        self.assertIn("sell_spreads", d.changes, "a cluster ABOVE should widen the SELL side")
        self.assertNotIn("buy_spreads", d.changes, "the other side must be left alone")
        widened = [float(x) for x in d.changes["sell_spreads"].split(",")]
        self.assertGreater(widened[0], 5.0)
        self.assertLessEqual(widened[0], 5.0 * 1.4 + 1e-9, "tilt must respect max_tilt_pct")
        os.unlink(p)

    def test_never_proposes_a_spread_under_the_measured_fee_floor(self):
        """4,8 measured 2.42 bps gross against a 4.0 bps round trip. It loses by construction."""
        for bad in ({"buy_spreads": "4,8"}, {"sell_spreads": "1,2"}, {"buy_spreads": "5,3"}):
            self.assertTrue(policy.validate(bad), f"{bad} should have been rejected")
        self.assertEqual(policy.validate({"sell_spreads": "6,12"}), [])

    def test_refuses_fields_the_running_bot_would_ignore(self):
        """A field without is_updatable is a silent no-op — worse than an error."""
        bad = policy.validate({"natr_length": 20})
        self.assertTrue(bad)
        self.assertIn("is_updatable", bad[0])
        self.assertEqual(policy.validate({"total_amount_quote": 400}), [])

    def test_unattributed_cascade_is_ignored_and_says_so(self):
        """Measured Aug 15: acting on the OI-drop proxy was the entire cost of the old fuel layer."""
        h = history(far=0.03)
        h.append(snap([{"price": 180 * 1.03, "side": "short", "notional": 3e6, "burned": False}], ts=NOW,
                      cascade={"liq_notional_60s": 200000, "baseline_60s": 20000, "dominant_side": None}))
        p = write(h)
        d = policy.decide(fuelmap.build_report(p, now=NOW + 10), CONFIG)
        self.assertNotEqual(d.action, "derisk")
        self.assertTrue(any("dominant_side is null" in r for r in d.refused))
        os.unlink(p)

    def test_attributed_cascade_cuts_size_and_does_not_tilt(self):
        h = history(far=0.03)
        h.append(snap([{"price": 180 * 1.004, "side": "short", "notional": 4e6, "burned": False}], ts=NOW,
                      cascade={"liq_notional_60s": 200000, "baseline_60s": 20000, "dominant_side": "long"}))
        p = write(h)
        d = policy.decide(fuelmap.build_report(p, now=NOW + 10), CONFIG)
        self.assertEqual(d.action, "derisk")
        self.assertEqual(d.changes, {"total_amount_quote": 400.0})
        os.unlink(p)

    def test_a_map_it_cannot_trust_never_moves_quotes(self):
        for state in ("stale", "empty"):
            d = policy.decide({"state": state}, CONFIG)
            self.assertEqual(d.action, "hold")
            self.assertEqual(d.changes, {})


class Runner(unittest.TestCase):
    def test_tune_writes_the_yaml_and_keeps_every_comment(self):
        """yaml.safe_dump would erase the measured reasoning in the comments. It must not."""
        src = os.path.join(REPO, "conf", "controllers", "quench_bitget_sol.yml")
        cfg = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False).name
        with open(src) as a, open(cfg, "w") as b:
            original = a.read()
            b.write(original)
        h = history(far=0.03)
        h.append(snap([{"price": 180 * 1.004, "side": "short", "notional": 4e6, "burned": False}], ts=time.time()))
        hp = write(h)
        rep = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        journal = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
        try:
            r = subprocess.run([sys.executable, "routines/fuelmap.py", "--snapshot", hp, "--out", rep],
                               cwd=REPO, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = subprocess.run([sys.executable, "agent/tune.py", "--report", rep, "--config", cfg,
                                "--journal", journal], cwd=REPO, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("action: tilt", r.stdout)
            after = open(cfg).read()
            self.assertNotEqual(after, original, "nothing was written")
            for comment in ("# --- quench", "Bitget VIP0 USDT-M maker = 0.020%",
                            "THIS is what makes a wide quote pay"):
                self.assertIn(comment, after, f"comment lost: {comment!r}")
            # and the file still loads as the real config
            sys.path.insert(0, os.path.join(REPO, "backtest"))
            import harness
            import yaml
            q = harness.load_quench_module()
            q.QuenchControllerConfig(**yaml.safe_load(after))
            entry = json.loads(open(journal).read().strip().split("\n")[-1])
            self.assertEqual(entry["action"], "tilt")
            self.assertTrue(entry["reasons"])
        finally:
            for f in (cfg, hp, rep, journal):
                try:
                    os.unlink(f)
                except OSError:
                    pass

    def test_dry_run_writes_nothing_but_still_journals(self):
        src = os.path.join(REPO, "conf", "controllers", "quench_bitget_sol.yml")
        cfg = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False).name
        with open(src) as a, open(cfg, "w") as b:
            original = a.read()
            b.write(original)
        h = history(far=0.03)
        h.append(snap([{"price": 180 * 1.004, "side": "short", "notional": 4e6, "burned": False}], ts=time.time()))
        hp = write(h)
        rep = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        journal = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
        try:
            subprocess.run([sys.executable, "routines/fuelmap.py", "--snapshot", hp, "--out", rep],
                           cwd=REPO, capture_output=True, text=True, timeout=120, check=True)
            r = subprocess.run([sys.executable, "agent/tune.py", "--report", rep, "--config", cfg,
                                "--journal", journal, "--dry-run"],
                               cwd=REPO, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(open(cfg).read(), original, "--dry-run wrote to the config")
            entry = json.loads(open(journal).read().strip().split("\n")[-1])
            self.assertTrue(entry["dry_run"])
        finally:
            for f in (cfg, hp, rep, journal):
                try:
                    os.unlink(f)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
