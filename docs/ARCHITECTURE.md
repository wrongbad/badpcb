# rk — routing kernel for agent-driven PCB layout

Goal: an interface between an LLM agent and routing search. The agent makes the
topological decisions (assignments, rotations, corridors, layer plans). The kernel
does fast geometry and arithmetic, and **always explains why something is stuck**,
in text and matrices that use named handles. No images are needed.

Case study (see HISTORY.md, examples/key_board/): a 298×84 mm, 4-layer keyboard (GND and +3V3/+3V3A planes), 58 sensors,
an RP2350B (QFN-80, 0.4 mm) at U1, 8 EN nets × 8 SENSE/ADC nets. Freerouting ran for hours and left many nets unrouted.
Nothing in the package is specific to it: per-board facts live in `rk.json` (README, `rk/config.py`).

## Principles
1. **pcbnew only at the edges.** `extract.py` (run with `$KIPY`) dumps the board into
   `model.json`. `apply.py` (`$KIPY`) writes the kernel's results back to a `.kicad_pcb`.
   Everything else is pure Python 3 (stdlib + numpy if available), so it's fast and testable.
2. **Named handles everywhere.** Regions `R<n>`, cuts `C<n>`, conflict clusters `K<n>`, nets by
   name, parts by refdes. IDs are deterministic for the same geometry.
3. **Every call has a budget and returns diagnostics.** No opaque long runs. Each call takes
   < 10 s on key_board unless explicitly asked otherwise.
4. **Small, typed, reversible actions** on an immutable-ish state with snapshots.
5. **Text-first output.** Every report has a compact human/LLM text form and a JSON form.

## Layout
```
rk/
  config.py       rk.json discovery, state dir (.rk/), tool lookup ($KIPY, $KICAD_CLI, $KRT_DIR, $KRT_PY)
  extract.py      $KIPY: .kicad_pcb -> model.json
  apply.py        $KIPY: model.json/state + routes -> .kicad_pcb
  model.py        dataclasses: Board, Part, Pad, Net, Track, Via, Zone, Rules; load/save JSON
  swaps.py        swap-group declarations + legality checks (groups come from rk.json "swaps")
  geom.py         segment intersection, point-in-polygon, MST, etc.
  ratsnest.py     per-net connections (MST over pads); nets fully served by planes are excluded
  topo.py         region/cut segmentation, cut orderings, inversions, conflict matrix and clusters
  ascii_map.py    coarse labeled ASCII congestion map
  actions.py      action types + apply-to-state + legality
  evaluate.py     what-if: score candidate actions without committing (Δ metrics)
  state.py        State = model + applied actions + routes; snapshot/branch/revert/diff
  groute.py       PathFinder negotiated-congestion global router on a coarse multilayer grid;
                  history costs -> blockage explanations, thrash counts
  droute.py       detailed routing = scoped KiCadRoutingTools (KRT) passes + rk-vocabulary report (see Phase 4 notes)
  cli.py          `python -m rk <cmd>`: report, eval, act, groute, droute, snapshot, ...
  (tests/ at the repo root: synthetic unit tests; the board tests live with the board project)
```

## Core metrics (reported after every action as a Δ line)
crossings (ratsnest, same-layer-agnostic), cut overflow (sum and max), inversions,
estimated vias, wirelength, unrouted count, DRC errors (only after detailed routing).

## Swap freedoms of the case-study board (declared in its swaps json, examples/key_board/swaps.json)
- ADC channel permutation: SENSE0..7 ↔ GPIO40..47 (any permutation; firmware remaps).
- EN GPIO assignment: EN0..7 ↔ any free RP2350B GPIO (not just 32–39).
- Key→(region, en) assignment: any bijection where each key gets a unique (region, en) pair.
  Firmware remaps. Region size ≤ 8.
- RC filter parts per ADC line are interchangeable as sets.

## Phases
1. Foundation: extract, model, ratsnest, topo reports, ascii map, swaps, CLI `report`.
2. actions + state + evaluate (what-if).
3. groute (PathFinder) with explanations.
4. droute + apply + DRC loop.
5. Torture: a separate agent drives only the CLI to route key_board, and we log what it needed.

## Phase 4 notes (apply + droute on KiCadRoutingTools)
Decision: no own detailed router. KRT is located by $KRT_DIR / $KRT_PY (or rk.json `krt`), own venv; `source scripts/krt_env.sh`.
- `python3 -m rk apply SESSION out.kicad_pcb` (plan in py3, writer under $KIPY via `-m rk.apply`, PYTHONPATH set by rk; every $KIPY entry point exits through
  `kipy_exit.done()` = os._exit, because pcbnew segfaults at interpreter teardown). Writes `swaps_applied.md` beside the board.
- `python3 -m rk droute SESSION|BOARD [--nets ..|--region R8|--component ..] [--fanout U1] [--grid mm|auto] [--time S] [--preview]`;
  passes chain (.rk/routes/<session>/passNN.kicad_pcb, current.kicad_pcb). `--measure` prints the METRICS line of any board.
- Grading DRC always uses the ORIGINAL project rules (rk.json rules.project, else the input board's own .kicad_pro) (KRT lowers clearance/drill floors in the project it writes).
- KRT runs with `--escalation board` (never below the board's minimums); KRT treats every KiCad-locked track as unrippable and
  protects those nets, so locked lanes/stubs are hard obstacles for it.

## Phase 5 notes (rules, feasibility, locks, pad-level legality)
- **Rules live in the project, not the board.** a board-side script (key_board: `rules.py`) writes `.kicad_pro` (Default netclass 0.15/0.2, via 0.5/0.25, minimums) and `.kicad_dru`
  (clearance/track 0.10 inside the `U1_FINE` rule area = courtyard + 1 mm). `apply.copy_rules` puts both beside every board rk writes; DRC grades against them (`--rules-pro` overrides).
  
- `feas.py` / `rk report --section feasibility` (first section): per fine-pitch (< 0.65 mm) part and side: pitch, pad width, gap, max track between pads,
  straight-stub fit, 45 degree fan spacing vs track + clearance, via-in-pad, staggered via fan; verdict OK/FAIL with the limit. Also the min grid step per region,
  which `droute --grid auto` uses. It reads the effective rules (netclass + the .kicad_dru forms rules.py writes).
- `locks.py`: copper classes from rk.json `locks.classes` (conventionally `lane` / `mimic`) / `fanout` (other locked) / `free`; Track/Via carry `id` (KiCad uuid) and `cls`.
  Actions `unlock TARGET`, `lock TARGET` (class | net/glob | region | box), `trim-lanes`; `apply` writes the lock flags (plan keys `unlock`/`lock`) and deletes trimmed lane segments.
  Metrics: `Xcu=N(L locked)`. Report section `copper` splits crossings into locked/rippable and lists locked walls per net;
  droute marks KRT's blockers `[LOCKED lane xN]` and prints the straight-path blockage by locked copper.
- `legal.py`: pad-level legality for move/rot/flip/spots: new pad-to-pad (any part incl. through-hole), pad-to-foreign-track/via (lane copper = error, free/mimic copper = warning, apply rips it),
  holes and board edge. Only conflicts new relative to the current position count.

## Phase 6 notes (rk audit: placement linter)
`python3 -m rk audit <board|session> [--json] [--full] [--roles] [--groute] [--only DET,..]` (rk/audit.py). Detectors ESCAPE (fan-zone capacity + intruders
of fine-pitch sides), PLANE (supply pads with no plane under them), BOWTIE, ORIENT, TENSION, ROLES (inferred decap/filter/crystal/pull-up/ESD roles, `--roles`),
BUSORDER, COURTYARD (bbox model minus 0.09 mm bias, notched big-part corners ignored), DENSITY. Every fix is a compact action list with its evaluate.py delta
and legal.py verdict (read-only). `rk suggest S audit` returns the best fix per finding as cumulative eval rows (`act S @1`, `@2`, ...).

## Phase 7 notes (tools lane)
- `rk plan S [U1[,U2]] [--edge 1.5]` / `rk audit S --joint` (rk/joint.py): ONE placement plan for an IC's fan-zone intruders + role parts (decap, filter cap, crystal load, pull-up, esd) under a shared constraint set (outside all escape zones, courtyard >= edge mm from the board edge, role distance, legality against the plan so far, stubs move with the part). Most constrained first, relaxed second pass, prints before/after fan usage / role violations / evaluate delta and a pasteable `rk act` line. It cannot create space: parts with no legal spot are listed.
- `reshape NET add|sub X0,Y0,X1,Y1 [layer=In2.Cu]` (actions.py): plane-zone edit. `add` gives the box to NET (added to NET's zone, carved out of other zones on that layer; warns about pads of the carved plane that lose it), `sub` only removes it from NET's zone. Reversible (inverse = `reshape NET restore` with the old polygons), engine/evaluate see it through `board.zones` (plane stubs). `rk apply` writes zone outlines first, extra polygons become sibling zones (a saved zone holds one polygon), then refills. audit PLANE now emits `reshape ...; via ... pad=..` as a fix next to the part moves.
- `delvia NET X Y stub` (remove a via and the short stub track to its pad) is parseable text now.
- `rk droute S --repair [--radius 4]` (rk/repair.py): rips only the unlocked, non-plane copper within `radius` of both ends of every open connection (locked copper counted and named for `unlock`), adds the ripped nets to the KRT scope. Pass `mode` (plain/repair/strip) is recorded in passes.jsonl; the ping-pong warning ends with the next rung of the ladder (plain -> repair -> strip+fanout -> change placement/unlock).
- `rk groute S|board [...]` (rk/gscore.py): the PathFinder global router on a session's current state with its hints (corridor/layer/priority/freeze; side/reserve/near listed as ignored). `rk audit S --gscore [SEC]` / `rk suggest S audit --gscore [SEC]` re-rank each finding's top-3 fixes by a short groute run on the finding's region (score = overflow + 5 no-path + 0.1 history; shown as `{groute R8: score a -> b}`). Coarse: at the 1 mm default it separates fixes only where the locked-lane/cut capacity is the limit (U1's fan is invisible below ~0.25 mm cells, ~40 s per run; RK_GSCORE_CELL overrides).


## Project config and state (standalone rk)
- `rk.json` (cwd or parent, `--project`, `$RK_PROJECT`) holds board facts: rules project, mcu / fine-pitch parts, swap groups, lock classes + lane trim
  settings, plane hints, KRT location. No file: auto-detect fine-pitch parts, no groups, no classes.
- State (`sessions/`, `routes/`, `cache/`) lives in `<project dir>/.rk/` or `$RK_HOME`; session files store the board path relative to the project.
- `$KIPY` entry points (`-m rk.extract|apply|strip_nets`) are run by `config.kipy_run`, which puts the rk package on PYTHONPATH.
