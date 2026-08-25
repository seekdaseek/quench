"""The two backtest runners had NO test coverage, and the Aug 25 split broke both silently.

35 green tests said nothing about them: they are argparse scripts that build a config and shell out,
and nothing imported them. `run_backtest.py` and `sweep.py` both passed `fuel_enabled=` into a config
whose model_config is extra="forbid", so both raised ValidationError on the first line of real work
while the suite stayed green.

These are smoke tests, not measurements. They assert the runners EXIT ZERO and print the columns they
are supposed to print, on a tiny synthetic tape. Nothing here says anything about whether the strategy
makes money — the tape is noise with no edge in it, and a handful of fills on two simulated days is
not evidence of anything. Real numbers come from backtest/fetch_data.py on the Mac against a real
tape.
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PY = sys.executable


def run(args, **kw):
    return subprocess.run([PY] + args, cwd=REPO, capture_output=True, text=True, timeout=300, **kw)


class Runners(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(REPO, "backtest"))
        import harness
        cls.tape = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        harness.synthetic_candles(1_755_200_000, 800, seed=7).to_csv(cls.tape, index=False)

    @classmethod
    def tearDownClass(cls):
        try:
            os.unlink(cls.tape)
        except OSError:
            pass

    def test_run_backtest_exits_zero_and_reports(self):
        r = run(["backtest/run_backtest.py", "--synthetic", "--spreads", "5,10"])
        self.assertEqual(r.returncode, 0, f"run_backtest.py failed:\n{r.stdout}\n{r.stderr}")
        for expected in ("== quench ==", "net_pnl_quote", "total_volume", "executors"):
            self.assertIn(expected, r.stdout, f"missing {expected!r} from the report")

    def test_run_backtest_says_fuel_is_no_longer_its_job(self):
        """--fuel used to switch the layer on. It must not silently do nothing."""
        r = run(["backtest/run_backtest.py", "--synthetic", "--fuel", "/nonexistent.json"])
        self.assertEqual(r.returncode, 0, f"run_backtest.py failed:\n{r.stdout}\n{r.stderr}")
        self.assertIn("ignored", r.stdout)
        self.assertIn("routines/fuel", r.stdout)

    def test_sweep_exits_zero_and_prints_the_gross_bps_column(self):
        out = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        try:
            r = run(["backtest/sweep.py", "--csv", self.tape, "--spreads", "5,10",
                     "--tp-mult", "1.0", "--sl-mult", "3.0", "--refresh", "300", "--json", out])
            self.assertEqual(r.returncode, 0, f"sweep.py failed:\n{r.stdout}\n{r.stderr}")
            # gross_bps vs the fee is the whole point of the sweep — it must survive the split
            self.assertIn("gross_bps", r.stdout)
            self.assertIn("breaks even at a maker fee", r.stdout)
            self.assertTrue(os.path.getsize(out) > 0, "sweep wrote no json")
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass

    def test_no_runner_passes_a_field_the_config_rejects(self):
        """The exact failure mode of the split: extra="forbid" turns a stale kwarg into a hard error."""
        import harness
        q = harness.load_quench_module()
        fields = set(q.QuenchControllerConfig.model_fields)
        dead = ("fuel_enabled", "fuel_url", "fuel_history_path", "fuse_natr", "max_lean_natr",
                "cascade_ratio_brake", "fuel_horizon_minutes", "vol_window", "require_attributed_cascade")
        for name in dead:
            self.assertNotIn(name, fields, f"{name} is back on the config")
        for script in ("backtest/run_backtest.py", "backtest/sweep.py"):
            src = open(os.path.join(REPO, script)).read()
            code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
            for name in dead:
                self.assertNotIn(f"{name}=", code, f"{script} still passes {name}=")


if __name__ == "__main__":
    unittest.main()
