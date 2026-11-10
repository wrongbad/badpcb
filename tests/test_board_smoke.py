"""Opt-in smoke test on any real board: RK_TEST_BOARD=/path/to/board.kicad_pcb (needs $KIPY for extraction)."""
import os
import unittest

from rk import cli, config

BOARD = os.environ.get("RK_TEST_BOARD", "")


@unittest.skipUnless(BOARD and os.path.exists(BOARD) and config.kipy_available(), "set RK_TEST_BOARD to a .kicad_pcb (and have KiCad's python)")
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
        from rk import audit
        st = audit.load_state(BOARD)
        self.assertTrue(audit.run(st) is not None)


if __name__ == "__main__":
    unittest.main()
