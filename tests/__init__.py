"""Synthetic badpcb tests: no board, KiCad or KiCadRoutingTools needed (board smoke tests are opt-in via BADPCB_TEST_BOARD)."""
import os
import tempfile

_home = tempfile.mkdtemp(prefix="rk_test_home_")
os.environ["BADPCB_HOME"] = _home                       # never write .badpcb/ into the checkout
os.environ.pop("BADPCB_PROJECT", None)

from badpcb import config  # noqa: E402

config.use({})                                      # defaults: no swap groups, no lock classes, auto-detected fine-pitch parts
