# badpcb: an agent-facing PCB routing kernel

badpcb sits between an LLM (or a human) and PCB routing search. The agent makes the topological decisions (placement, pin assignment, corridors, which copper to unlock);
the kernel does the fast geometry and arithmetic and **always says why something is stuck**. No images are needed.

The idea
- **Named handles everywhere**: regions `R8`, cuts `C12`, conflict clusters `K1`, nets by name, parts by refdes. IDs are deterministic for the same geometry.
- **Text and matrix views**: `badpcb report` prints the crossing matrix, cut demand/capacity, an ASCII congestion map, feasibility of the design rules against fine-pitch footprints, lock classes and plane service. Every report has a JSON form.
- **What-if before commit**: small typed, reversible actions (`move`, `rot`, `flip`, `swap`, `assign`, `via`, `reshape`, `unlock`, hints...) in sessions with undo/snapshot/branch/diff. `badpcb eval` scores candidates (delta of crossings, overflow, wirelength...) without applying them.
- **Intent and floorplan**: `intent` in `badpcb.json` (soft near/apart/edge/clear/block constraints and reference layouts, scored as `intent=`), `badpcb floorplan` (blocks, fill, block-level crossings, what faces each fine-pitch IC side, long critical links).
- **Explanations**: `badpcb audit` (placement linter with ranked, legality-checked fixes; its header names the board file, its mtime and its zone count so a stale board is obvious; detectors ESCAPE, CHANNEL, PLANE, ROLES, INTENT, ...), `badpcb plan` (joint placement of an IC's fan-zone intruders and role parts), global router (`badpcb groute`, PathFinder with blockage history) and `badpcb droute` (scoped KiCadRoutingTools passes reported as open connections with region/cut handles and the locked copper that blocks them).
- **Net styles, route lint, polish**: `net_styles` in `badpcb.json` (soft per-net via penalty / detour budget / avoid / pair + skew / reference plane / priority, by name pattern or KiCad netclass), `badpcb styles` (resolved style per net), `badpcb lint` (per-net ugliness table: detour, vias, jogs, corners, direction breaks, plane splits, pair skew, escape-zone intrusions; weighted score, handle and reason per net), `droute --priority` (constrained nets first) and `droute --polish N` (rip and reroute the N worst nets, keep a result only if lint improves and open conns / DRC do not get worse).
- **No KiCad in the loop**: badpcb reads and edits `.kicad_pcb` files itself (pure stdlib S-expression editor: lossless, only the touched tokens change). KiCad is only the sign-off tool (`kicad-cli`: DRC, zone refill, fab output).

## Requirements
- Python 3.9+ (stdlib only; `numpy` optional).
- KiCad 8, 9 or 10 board files (tested on 10). `kicad-cli` is needed only for DRC / zone refill (`badpcb apply` verification); no `pcbnew`, no KiCad python.
- Optional, for `badpcb droute`: [KiCadRoutingTools](https://github.com/) (KRT) plus its own venv (numpy, scipy, shapely, Pillow; not KiCad's python).

## Install
```
pip install -e .            # console script `badpcb`
# or from a checkout, no install:
export PYTHONPATH=/path/to/badpcb-checkout      # then: python3 -m badpcb ...
source scripts/krt_env.sh                   # KRT_DIR / KRT_PY / KICAD_CLI defaults
```

## Environment variables
| var | meaning | default |
|---|---|---|
| `KICAD_CLI` | kicad-cli | `kicad-cli` on PATH, else the macOS app bundle |
| `KRT_DIR` / `KRT_PY` | KiCadRoutingTools checkout / its python | `badpcb.json` `krt`, else `~/KiCadRoutingTools` and `~/KiCadRoutingTools/.venv/bin/python` |
| `BADPCB_PROJECT` | badpcb.json (or its directory) | badpcb.json in the cwd or a parent |
| `BADPCB_HOME` | state directory (sessions, work files, cache) | `<project dir>/.badpcb/` |
| `BADPCB_NO_CACHE`, `BADPCB_GSCORE_CELL`, `BADPCB_PROGRESS_S` | disable groute cache / groute-score cell mm / droute progress seconds | |

State never goes into the package: `.badpcb/sessions`, `.badpcb/cache` (extracted models) and **`.badpcb/work/`**, the nested work directory for every intermediate artifact:
```
.badpcb/work/droute/<session|board>/passN.kicad_pcb, passN.krt.log/.json, passN.strip|repair|fanout.kicad_pcb, current.kicad_pcb, passes.jsonl
.badpcb/work/apply/<out board stem>/plan.json, apply_report.json, drc_in.json, drc.json
.badpcb/work/stubs/<out board stem>/   .badpcb/work/drc/   .badpcb/work/extract/
```
Only what you ask for lands elsewhere: the board named in `apply S out.kicad_pcb` / `droute --out` / `stubs in out` (with its `.kicad_pro`/`.kicad_dru`) and `swaps_applied.md` beside it. The directory is configurable (`"work_dir": "build/work"` in `badpcb.json`, relative to the project) so the project top level keeps only the final board, the config, notes and `fab/`.

## Project config: `badpcb.json`
Found in the cwd (or a parent), via `--project PATH`, or `$BADPCB_PROJECT`. All keys optional; relative paths are relative to the file. Without one badpcb auto-detects fine-pitch parts and has no swap groups or lock classes.
```json
{
  "board": "my_board.kicad_pcb",
  "rules": {"project": "my_board.kicad_pro"},
  "mcu": "U1",
  "fine_pitch_parts": ["U1"],
  "swaps": "swaps.json",
  "locks": {"classes": {"lane": {"file": "lanes.json", "protect": true, "trim": true},
                        "mimic": {"file": "mimic.json", "key": "copied"}},
            "trim": {"nets": "^(EN|SENSE)\\d+$", "key_parts": "^U1\\d\\d$", "start_part": "U1"}},
  "audit": {"decap_bypass_mm": 4.0},
  "planes": {"assume": ["GND", "+3V3"], "min_coverage": 0.30},
  "work_dir": ".badpcb/work",
  "intent": {"blocks": {"vreg": {"parts": ["L1", "C15", "C17"], "max_span_mm": 10}},
             "near": [["decaps:U1", "U1", 3]], "clear": [{"zone": "escape:U1", "except": ["decaps:U1"]}], "edge": [["J1", "back", 2]]},
  "krt": {"dir": "../tools/KiCadRoutingTools", "python": "../tools/krt-venv/bin/python"}
}
```
- `rules.project`: the `.kicad_pro` (+ sibling `.kicad_dru`) DRC is graded against and copied beside every board badpcb writes (default: the input board's own).
- `mcu`, `fine_pitch_parts`: the IC pin-swap groups apply to / parts whose fan-out matters (default: auto-detect pitch < 0.65 mm).
- `swaps`: file or inline dict `{"mcu", "pin_names": {pin: name}, "groups": [...]}`; group kinds `pin_permutation`, `pin_assignment`, `part_sets`, `key_assignment` (needs `net_regex`). See `examples/pcb_v2/swaps.json`.
- `locks.classes`: copper lock classes, none by default. Each is `"name": "file[:key]"` (JSON list of track/via uuids, or `d[key]`) or `{"file", "key", "protect", "trim"}`. `protect`: `apply` keeps the copper unless a changed pad shorts or breaks it, and `legal`/`stubs` treat it as hard copper. `trim`: eligible for `trim [CLASS]` (alias `trim-lanes`; all trim classes when no CLASS); it may be a dict that overrides `locks.trim` (`nets`, `key_parts`, `start_part`, `band_depth`) for that class. Output labels use the class name.
- `audit`: overrides for the ROLES / escape-zone thresholds (decap, filter-cap, crystal, pull-up distances, slack factors, minimum IC pins, fan-zone depth, joint-placement limits). Defaults and meanings: `DEFAULTS` in `badpcb/audit.py`; unknown keys are rejected. `badpcb audit --roles` and `badpcb project` print the effective values.
- `planes.assume`: nets treated as plane nets on boards with no zones yet. `planes.min_coverage` (default 0.30): a layer is a **plane layer** only when its netted zones (outlines, so unfilled zones count) cover more than this fraction of the board outline; a few-mm2 outer-layer pour (regulator LX / +1V1) does not make the layer a plane, so droute keeps routing on it. `report --section summary` and every `droute` pass print which zones made each decision (`0` = any netted zone, the old behaviour).
- `work_dir`: nested work directory for intermediates (default `<state dir>/work`, see above).
- `intent`: soft design intent, an inline object or the path of a JSON file; see **Design intent** below.
`badpcb project` prints what was resolved (`no lock classes` when there is no `locks` block; lock files that do not exist are flagged, not fatal).

## Design intent (`intent`)
What the placement should look like, as **soft penalties only**: no vetoes, no rigid locks. A broken intent just costs and is reported;
breaking a reference on purpose is allowed, it has to be visible. The sum of all weighted penalties is the `intent` metric: `intent=N` in the
session METRICS line (only when intent is declared; without intent every METRICS line is exactly as before), a term of every `dscore`
(`eval`, `suggest`, audit fix ranking, `plan`), and `0` means every intent is met. Selectors in all entries: a refdes, a glob (`C1*`, `U1??`),
comma lists, or a role `decaps:U1` (supply caps <= 1.5 uF whose nearest IC pin on their net is on U1), `bulk:U1`, `caps:U1`, `passives:U1`.
Distances are courtyard-box gaps (box to box, or box to a pad point); every entry takes an optional `weight` (last list element, or `"weight"`).
```json
"intent": {
  "refs": {"mini": {"pcb": "../ref/minimal/board.kicad_pcb", "anchor": "U1"}},
  "blocks": {"vreg":  {"parts": ["L1", "C15", "C16", "C17", "R3"], "max_span_mm": 10, "anchor": "U1",
                       "ref": {"pcb": "../ref/minimal/board.kicad_pcb", "anchor": "U1", "map": {"C15": "C6"}}},
             "flash": {"parts": ["U2"], "ref": "mini"}},
  "near":  [["decaps:U1", "U1", 3], ["Y1", "U1.20", 6, 2.0]],
  "apart": [["J1", "U1", 8]],
  "edge":  [["J1", "back", 2], ["J2", "any"]],
  "clear": [{"zone": "escape:U1", "except": ["decaps:U1"], "weight": 3}, {"zone": [10, 5, 40, 20]}]
}
```
| entry | meaning | penalty (x weight) |
|---|---|---|
| `blocks: {name: {parts, max_span_mm?, anchor?, ref?}}` | parts that belong together (also the floorplan's declared blocks) | span: `0.5` per mm of centre-bbox diagonal over `max_span_mm`; reference drift: `0.3` per mm above 1 mm (below) |
| `near: [A, B[.PAD], mm]` | each part of A within `mm` (gap) of part B or its pad | per mm of excess (default weight 1) |
| `apart: [A, B, mm]` | at least `mm` between every A and B | per mm of shortfall (1) |
| `edge: [REF, front\|back\|left\|right\|any, max_mm?]` | courtyard within `max_mm` (default 3) of that board edge; front = +Y (bottom), back = top | per mm over (0.5) |
| `clear: {zone, except?}` | nothing in the zone but the excepted parts; `zone` = `escape:U1[:N]` (the fan zones of an IC, optionally one side) or a box `[x0, y0, x1, y1]` | per intruder `1 + min(overlap mm2, 25) / 5` (2) |
- **Reference blocks**: `ref` = a reference `.kicad_pcb` path or a name from `refs`, `{pcb, anchor, map}` (`map`: `{part here: part there}` for parts whose
  refdes differ; the anchor is an IC present in both boards, may be outside the block), or a captured arrangement (below). Alignment is the best
  rigid fit (rotation, translation, mirror when the board sides differ) of the anchor's pads by pad number, so the reference may be on the other
  board side or turned. **Drift** = RMS of the members' offsets from where the reference puts them + `0.25 mm` per member turned by 90 degrees
  (mod 180); a drift above 1 mm costs `0.3 x weight` per mm and is listed in the audit and the floorplan.
- In a session: `badpcb act S 'intent add {"near": ["C5", "U1.5", 3]}'` (same JSON as the config sections, singular or plural), `'intent rm near2 block:vreg'`
  (ids as shown by `badpcb show S`: `near1`, `edge2`, `clear1`, `block:vreg`), `'intent capture vreg'` (the block's reference := the current positions of its parts
  relative to the anchor). All are logged, undoable and part of `snapshot`/`branch` like hints. Entries from `badpcb.json` can be removed the same way.
- `badpcb audit S --only INTENT`: one finding per violated entry (WARN; ERR once the weighted penalty reaches `audit.intent_err`, default 8), with
  compact fixes and their delta; unresolved selectors are NOTEs. `badpcb plan` adds 0.5 x the intent penalty of a candidate spot to its cost.
- `examples/pcb_v2/badpcb.json` has an intent for the case-study board (blocks mcu / vreg / flash / xtal from the RP2350 minimal design).

## Net styles, route lint and polish
```json
"net_styles": {
  "usb_d":   {"match": ["USB_D*"], "pair": "USB_DP/USB_DN", "skew_mm": 0.5, "via_penalty": 8, "keep_ref_plane": true, "priority": true},
  "vreg_lx": {"match": ["VREG_LX"], "via_penalty": 6, "detour_max": 1.3, "avoid": ["XIN", "XOUT", "ADC*"], "priority": true},
  "power":   {"class": "Power", "via_penalty": 2}
}
```
Keys: `match` (fnmatch globs / exact names), `class` (a KiCad netclass; read from the board's `.kicad_pro`: `net_settings.netclass_assignments` and `netclass_patterns`), `via_penalty` (extra via cost, mm of path like groute's `--via-cost`),
`detour_max` (routed / direct length the net should stay under), `avoid` (net globs to keep away from), `pair` (`"P/N"`), `skew_mm`, `keep_ref_plane`, `priority`. A net gets a style by name **or** netclass; when several styles match they **stack** and the stricter value per key wins
(max `via_penalty`, min `detour_max` / `skew_mm`, union of `avoid` / pairs, OR of the flags). Everything is soft: penalties and ranking, never a veto. No `net_styles` = nothing changes.
- `badpcb styles BOARD [--json]`: the resolved style per net (and styles that matched nothing, and the nets the priority pass will take).
- **groute** uses them per net: via cost = base + `via_penalty`, a tight `detour_max` weights path length up, `avoid` is a soft proximity cost around the other nets' copper (mutual).
- `badpcb lint BOARD [--nets GLOB ..] [--top N] [--all] [--json]` (also `report --section lint`): for every routed signal net (plane nets, stitching vias and single-pad nets excluded) detour ratio, signal vias, jogs (segment < 3 x track width between two bends), bends per 10 mm, acute and 90 degree corners, layer-direction breaks (each layer's dominant direction is inferred from its copper),
  tracks over a split or gap of the adjacent reference plane (outer layers vs the zone outlines of the plane layer next to them), pair skew / gap variation / uncoupled share, copper inside another fine-pitch part's escape zone, copper close to `avoid` nets. A weighted score per net (`DEFAULTS` in `badpcb/lint.py`, `"lint": {"weights": {...}}` in `badpcb.json`; the style scales it:
  `via_penalty` -> via weight, `keep_ref_plane` -> splits x3, `priority` -> x1.5), a table sorted worst first with the reason and a handle (`C12` / `U1-in` / `R8`), the board total, and a verdict: few bad nets -> candidates for polish, many -> ESCALATE (placement problem).
- `badpcb droute BOARD --priority [--clean-board] [--strip]`: constrained-first pass for the nets with `priority`, `via_penalty >= 8` or a `pair`. KRT has one via cost per run (no per-net option, `route.py --via-cost`), so the pass routes those nets alone with the strictest via cost (75 + 10 x via_penalty), `--turn-cost 1500` and `--keep-away` bands for `avoid`
  (KRT also has `route_diff.py` for differential pairs; not wired in). A normal pass raises the via cost only if every net of its scope has a `via_penalty`.
- `badpcb droute BOARD --polish N [--time S] [--out BOARD]`: on a routed board rip and reroute the N worst nets of the lint (a pair together), one at a time, rest frozen (`--keep-input-copper`, KRT's pre-existing-rip disabled); a candidate is kept only if open connections, DRC errors and unconnected items do not increase, no other net's copper changed and the net's lint score improved. `--time` is the total budget. Logged per net with before/after.

## Quickstart
```
cd my_board_dir                          # holds badpcb.json
badpcb report my_board.kicad_pcb             # feasibility, summary, clusters, copper, cuts, planes, mcu, map
badpcb session new s1 my_board.kicad_pcb
badpcb floorplan s1                          # blocks, connectors, what faces the MCU fan, long links
badpcb audit s1                              # placement findings with fixes (incl. INTENT)
badpcb eval s1 'move R20 153.4 1.1' 'rot C27 90'
badpcb act s1 'move R20 153.4 1.1'
badpcb groute s1 --region R8 --time 20
badpcb apply s1 out.kicad_pcb                # writes the board, verifies metrics + DRC
badpcb droute s1 --region R8 --fanout U1     # needs KiCadRoutingTools
badpcb droute out.kicad_pcb --measure        # METRICS line (badpcb + DRC)
```

## Command reference
`report` `lint` `styles` `session new|ls|rm` `act` `eval` `suggest` `audit` `plan` `floorplan` `groute` `undo` `snapshot` `revert` `branch` `switch` `diff` `log` `spots` `show` `apply` `droute` `project`. `badpcb <cmd> -h` lists options; action syntax is in the docstring of `badpcb/actions.py`.

## Typical agent loop
Get the start right, then route: `0 simplify the architecture -> 1 reference first (intents + reference blocks) -> 2 floorplan (blocks before parts) -> 3 draft placement inside the blocks -> 4 gate (audit + intent) -> 5 route in scoped passes -> 6 escalate upward (re-floorplan) instead of patching`.
Steps 4-5 are the inner loop `report -> feasibility -> audit -> small fix -> groute score -> scoped droute -> repair -> audit`; more than ~2 repair passes in a region, or the ping-pong warning, means going back to step 2/3. Full version: [docs/PLAYBOOK.md](docs/PLAYBOOK.md). Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Case study: [docs/HISTORY.md](docs/HISTORY.md), [examples/pcb_v2](examples/pcb_v2) (steered routing of the first board), [examples/pcb_v4](examples/pcb_v4) (the start-right respin, with example `net_styles`).

## Using badpcb from a board project
Keep the board repo free of kernel code: put `badpcb.json` (+ swaps and lock files) next to the board, `export PYTHONPATH=/path/to/badpcb` (or `pip install -e`), run from the board directory. State lands in the board's `.badpcb/`. Board-specific tests go in the board project and run against the shared package (`PYTHONPATH=../badpcb python3 -m unittest discover -s rk_board_tests -t .`).

## Tests
`python3 -m unittest discover -s tests -t .` (synthetic, no KiCad needed; the parser/writer tests use the small boards in `tests/fixtures/`, and one test is skipped unless `kicad-cli` is installed). `tests/test_board_smoke.py` is skipped unless `BADPCB_TEST_BOARD=/path/board.kicad_pcb`.

## License

Mozilla Public License 2.0 (see `LICENSE`). Use badpcb for anything, including commercial work; designs you make with it are yours. If you distribute modified badpcb files, share those changes under MPL-2.0. Contributions are welcome, see `CONTRIBUTING.md`.
