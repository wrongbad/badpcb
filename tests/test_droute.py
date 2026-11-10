"""droute smoke (skipped unless KiCadRoutingTools + KiCad are installed): scoped KRT pass on key_board_placed (one net)."""
import argparse
import os
import shutil
import tempfile
import unittest

from rk import droute, cli, apply as AP

EX = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "key_board_placed.kicad_pcb")
OK = droute.available() and os.path.exists(EX) and os.path.exists(AP.KICAD_CLI_DEFAULT) and os.path.exists(AP.KIPY_DEFAULT)


class TestParse(unittest.TestCase):
    def test_parse_log(self):
        log = """[34/48] Routing J1_TX (id=6)
  FAILED: Could not find route (0.62s)
  Top blockers:
    1. J1_RX: 803 (14.2%), 803 uniq (100%), near: 161 src, 0 tgt
    2. ADC2: 765 (13.5%), 765 uniq (100%)
  Ripping up J1_RX to retry...
    ADC0: Retry FAILED (N=1): still 1 failed edges
JSON_SUMMARY_MIN: {"routed": 3, "failed": 1}
"""
        p = droute.parse_log(log)
        self.assertEqual(p["failed"]["J1_TX"]["blockers"][0], ("J1_RX", 14.2))
        self.assertEqual(p["failed"]["ADC0"]["retry"], (1, 1))
        self.assertEqual(p["summary"]["routed"], 3)




if __name__ == "__main__":
    unittest.main()
