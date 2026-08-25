import os, sys, unittest, yaml
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backtest"))
import harness
q = harness.load_quench_module()

class Configs(unittest.TestCase):
    def test_yaml_configs_load(self):
        for name, conn in (("quench_bitget_sol.yml", "bitget_perpetual"), ("quench_gate_sol.yml", "gate_io_perpetual")):
            with open(os.path.join(HERE, "..", "conf", "controllers", name)) as fh:
                d = yaml.safe_load(fh)
            c = q.QuenchControllerConfig(**d)
            self.assertEqual(c.connector_name, conn)
            self.assertEqual(c.controller_name, "quench")
            
            self.assertTrue(c.skip_rebalance)
if __name__ == "__main__":
    unittest.main()
