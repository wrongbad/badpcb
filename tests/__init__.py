"""Synthetic rk tests: no board, KiCad or KiCadRoutingTools needed (board smoke tests are opt-in via RK_TEST_BOARD)."""
import os
import tempfile

_home = tempfile.mkdtemp(prefix="rk_test_home_")
os.environ["RK_HOME"] = _home                       # never write .rk/ into the checkout
os.environ.pop("RK_PROJECT", None)

from rk import config  # noqa: E402

config.use({})                                      # defaults: no swap groups, no lock classes, auto-detected fine-pitch parts
