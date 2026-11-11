"""Opt-in smoke test on any real board: BADPCB_TEST_BOARD=/path/to/board.kicad_pcb."""
import os
import unittest

from badpcb import cli, config

BOARD = os.environ.get("BADPCB_TEST_BOARD", "")


@unittest.skipUnless(BOARD and os.path.exists(BOARD), "set BADPCB_TEST_BOARD to a .kicad_pcb")
class BoardSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = cli.Ctx(cli.load_board(BOARD))

    def test_every_section_renders(self):
        for name, fn in cli.SECTIONS.items():
            out = fn(self.c, False)
            self.assertTrue(isinstance(out, list), name)

    def test_json_report(self):
        self.assertIn("metrics", cli.as_json(self.c))

    def test_audit_runs(self):
        from badpcb import audit
        st = audit.load_state(BOARD)
        self.assertTrue(audit.run(st) is not None)


if __name__ == "__main__":
    unittest.main()
