# Contributing to badpcb

Thanks for helping! badpcb is licensed under MPL-2.0. By contributing, you agree that your contribution is licensed under it too.

## Sign your commits (DCO)
We use the [Developer Certificate of Origin](https://developercertificate.org/) instead of a CLA. Sign off every commit:

    git commit -s

This adds `Signed-off-by: Your Name <you@example.com>`, certifying that you have the right to submit the change.

## Before opening a PR
- Run the tests: `python3 -m unittest discover -s tests -t .` (no board needed). With `kicad-cli` installed the KiCad-reopens test runs too, and `BADPCB_TEST_BOARD=/path/to/board.kicad_pcb` adds the real-board smoke test. badpcb itself never imports `pcbnew`.
- Keep the package board-agnostic: board-specific values belong in a project `badpcb.json`, not in code.
- Keep CLI output compact and text-first: badpcb's main user is an LLM agent reading it.
- New detectors or actions need a small synthetic test (see `tests/`).
- Lessons from real boards (what badpcb missed, what confused the driving agent) are very welcome as issues, even without a fix.
