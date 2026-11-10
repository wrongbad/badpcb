# badpcb: an agent-facing PCB routing kernel

badpcb sits between an LLM (or a human) and PCB routing search. The agent makes the topological decisions (placement, pin assignment, corridors, which copper to unlock);
the kernel does the fast geometry and arithmetic and **always says why something is stuck**. No images are needed.

The idea
- **Named handles everywhere**: regions `R8`, cuts `C12`, conflict clusters `K1`, nets by name, parts by refdes. IDs are deterministic for the same geometry.
- **Text and matrix views**: `badpcb report` prints the crossing matrix, cut demand/capacity, an ASCII congestion map, feasibility of the design rules against fine-pitch footprints, lock classes and plane service. Every report has a JSON form.
- **What-if before commit**: small typed, reversible actions (`move`, `rot`, `flip`, `swap`, `assign`, `via`, `reshape`, `unlock`, hints...) in sessions with undo/snapshot/branch/diff. `badpcb eval` scores candidates (delta of crossings, overflow, wirelength...) without applying them.
- **Explanations**: `badpcb audit` (placement linter with ranked, legality-checked fixes), `badpcb plan` (joint placement of an IC's fan-zone intruders and role parts), global router (`badpcb groute`, PathFinder with blockage history) and `badpcb droute` (scoped KiCadRoutingTools passes reported as open connections with region/cut handles and the locked copper that blocks them).
- **pcbnew only at the edges**: extraction and board writing run under KiCad's python; everything else is stdlib Python.

## Requirements
- Python 3.9+ (stdlib only; `numpy` optional).
- KiCad 9 or 10. Its bundled python (has `pcbnew`) is used as `$KIPY` for extract/apply; `kicad-cli` for DRC.
- Optional, for `badpcb droute`: [KiCadRoutingTools](https://github.com/) (KRT) plus its own venv (numpy, scipy, shapely, Pillow; not KiCad's python).

## Install
```
pip install -e .            # console script `badpcb`
# or from a checkout, no install:
export PYTHONPATH=/path/to/badpcb-checkout      # then: python3 -m badpcb ...
source scripts/krt_env.sh                   # KRT_DIR / KRT_PY / KIPY / KICAD_CLI defaults
```

## Environment variables
| var | meaning | default |
|---|---|---|
| `KIPY` | KiCad's python (pcbnew) | first existing of the macOS app bundle, `/usr/lib/kicad/bin/python3` |
| `KICAD_CLI` | kicad-cli | `kicad-cli` on PATH, else the macOS app bundle |
| `KRT_DIR` / `KRT_PY` | KiCadRoutingTools checkout / its python | `badpcb.json` `krt`, else `~/KiCadRoutingTools` and `~/KiCadRoutingTools/.venv/bin/python` |
| `BADPCB_PROJECT` | badpcb.json (or its directory) | badpcb.json in the cwd or a parent |
| `BADPCB_HOME` | state directory | `<project dir>/.badpcb/` |
| `BADPCB_NO_CACHE`, `BADPCB_GSCORE_CELL`, `BADPCB_PROGRESS_S` | disable groute cache / groute-score cell mm / droute progress seconds | |

State never goes into the package: `.badpcb/sessions`, `.badpcb/routes` (droute passes), `.badpcb/cache` (extracted models).

## Project config: `badpcb.json`
Found in the cwd (or a parent), via `--project PATH`, or `$BADPCB_PROJECT`. All keys optional; relative paths are relative to the file. Without one badpcb auto-detects fine-pitch parts and has no swap groups or lock classes.
```json
{
  "board": "my_board.kicad_pcb",
  "rules": {"project": "my_board.kicad_pro"},
  "mcu": "U1",
  "fine_pitch_parts": ["U1"],
  "swaps": "swaps.json",
  "locks": {"classes": {"lane": "lanes.json", "mimic": "mimic.json:copied"},
            "trim": {"nets": "^(EN|SENSE)\\d+$", "key_parts": "^U1\\d\\d$", "start_part": "U1"}},
  "planes": {"assume": ["GND", "+3V3"]},
  "krt": {"dir": "../tools/KiCadRoutingTools", "python": "../tools/krt-venv/bin/python"}
}
```
- `rules.project`: the `.kicad_pro` (+ sibling `.kicad_dru`) DRC is graded against and copied beside every board badpcb writes (default: the input board's own).
- `mcu`, `fine_pitch_parts`: the IC pin-swap groups apply to / parts whose fan-out matters (default: auto-detect pitch < 0.65 mm).
- `swaps`: file or inline dict `{"mcu", "pin_names": {pin: name}, "groups": [...]}`; group kinds `pin_permutation`, `pin_assignment`, `part_sets`, `key_assignment` (needs `net_regex`). See `examples/key_board/swaps.json`.
- `locks.classes`: JSON files of track/via uuids per copper class (`file` = list, `file:key` = `d[key]`). `lane` is the class `apply` protects and `trim-lanes` cuts.
- `planes.assume`: nets treated as plane nets on boards with no zones yet.
`badpcb project` prints what was resolved.

## Quickstart
```
cd my_board_dir                          # holds badpcb.json
badpcb report my_board.kicad_pcb             # feasibility, summary, clusters, copper, cuts, planes, mcu, map
badpcb session new s1 my_board.kicad_pcb
badpcb audit s1                              # placement findings with fixes
badpcb eval s1 'move R20 153.4 1.1' 'rot C27 90'
badpcb act s1 'move R20 153.4 1.1'
badpcb groute s1 --region R8 --time 20
badpcb apply s1 out.kicad_pcb                # writes the board, verifies metrics + DRC
badpcb droute s1 --region R8 --fanout U1     # needs KiCadRoutingTools
badpcb droute out.kicad_pcb --measure        # METRICS line (badpcb + DRC)
```

## Command reference
`report` `session new|ls|rm` `act` `eval` `suggest` `audit` `plan` `groute` `undo` `snapshot` `revert` `branch` `switch` `diff` `log` `spots` `show` `apply` `droute` `project`. `badpcb <cmd> -h` lists options; action syntax is in the docstring of `badpcb/actions.py`.

## Typical agent loop
`report -> feasibility -> audit -> small fix -> groute score -> scoped droute -> repair -> audit`. Full version: [docs/PLAYBOOK.md](docs/PLAYBOOK.md). Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Case study: [docs/HISTORY.md](docs/HISTORY.md), [examples/key_board](examples/key_board).

## Using badpcb from a board project
Keep the board repo free of kernel code: put `badpcb.json` (+ swaps and lock files) next to the board, `export PYTHONPATH=/path/to/badpcb` (or `pip install -e`), run from the board directory. State lands in the board's `.badpcb/`. Board-specific tests go in the board project and run against the shared package (`PYTHONPATH=../badpcb python3 -m unittest discover -s rk_board_tests -t .`).

## Tests
`python3 -m unittest discover -s tests -t .` (synthetic, no KiCad needed). `tests/test_board_smoke.py` is skipped unless `BADPCB_TEST_BOARD=/path/board.kicad_pcb` (needs `$KIPY`).

## TODO / known board-flavoured remnants
- Docstring and help examples use the case study's names (U1, R8, ADC0, EN3); they are examples only.
- The lock class names `lane` (protected by `apply`, trimmed by `trim-lanes`) and `mimic` are conventions baked into the code.
- Audit/ROLES heuristics (decap, filter, crystal, pull-up inference) are generic but tuned on one board.

## License

Mozilla Public License 2.0 (see `LICENSE`). Use badpcb for anything, including commercial work; designs you make with it are yours. If you distribute modified badpcb files, share those changes under MPL-2.0. Contributions are welcome, see `CONTRIBUTING.md`.
