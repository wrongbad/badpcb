# badpcb — routing kernel for agent-driven PCB layout

Goal: an interface between an LLM agent and routing search. The agent makes the
topological decisions (assignments, rotations, corridors, layer plans). The kernel
does fast geometry and arithmetic, and **always explains why something is stuck**,
in text and matrices that use named handles. No images are needed.

Case study (see HISTORY.md, examples/key_board/): a 298×84 mm, 4-layer keyboard (GND and +3V3/+3V3A planes), 58 sensors,
an RP2350B (QFN-80, 0.4 mm) at U1, 8 EN nets × 8 SENSE/ADC nets. Freerouting ran for hours and left many nets unrouted.
Nothing in the package is specific to it: per-board facts live in `badpcb.json` (README, `badpcb/config.py`).

## Principles
1. **No KiCad in the loop.** `sexpr.py` + `kicad_pcb.py` read and edit `.kicad_pcb` text directly
   (lazy lossless S-expression document; edits are text patches, so untouched nodes stay byte-identical and
   diffs stay small). `extract.py` dumps the board into `model.json`; `apply.py` writes the kernel's results
   back to a `.kicad_pcb`. Everything is pure Python 3 (stdlib + numpy if available), so it's fast and testable.
   KiCad (`kicad-cli`) is only the sign-off tool: DRC, zone refill, fab output.
2. **Named handles everywhere.** Regions `R<n>`, cuts `C<n>`, conflict clusters `K<n>`, nets by
   name, parts by refdes. IDs are deterministic for the same geometry.
3. **Every call has a budget and returns diagnostics.** No opaque long runs. Each call takes
   < 10 s on key_board unless explicitly asked otherwise.
4. **Small, typed, reversible actions** on an immutable-ish state with snapshots.
5. **Text-first output.** Every report has a compact human/LLM text form and a JSON form.

## Layout
```
badpcb/
  config.py       badpcb.json discovery, state dir (.badpcb/), tool lookup ($KICAD_CLI, $KRT_DIR, $KRT_PY)
  sexpr.py        lossless lazy S-expression document, patch edits, KiCad-style formatting
  kicad_pcb.py    .kicad_pcb reader/editor: footprints (pose, flip), pads, tracks, vias, zones, net formats (named/numbered)
  extract.py      .kicad_pcb -> model.json
  apply.py        model.json/state + routes -> .kicad_pcb (plan + in-process writer)
  strip_nets.py   delete tracks/vias by net or uuid
  model.py        dataclasses: Board, Part, Pad, Net, Track, Via, Zone, Rules; load/save JSON
  swaps.py        swap-group declarations + legality checks (groups come from badpcb.json "swaps")
  geom.py         segment intersection, point-in-polygon, MST, etc.
  ratsnest.py     per-net connections (MST over pads); nets fully served by planes are excluded
  topo.py         region/cut segmentation, cut orderings, inversions, conflict matrix and clusters
  ascii_map.py    coarse labeled ASCII congestion map
  actions.py      action types + apply-to-state + legality
  evaluate.py     what-if: score candidate actions without committing (Δ metrics)
  state.py        State = model + applied actions + routes; snapshot/branch/revert/diff
  intent.py       design intent (soft penalties): selectors/roles, near/apart/edge/clear/block, reference drift; `penalty(world)` feeds engine metrics
  floorplan.py    `badpcb floorplan`: blocks (declared + automatic), block nets/crossings, connectors, escape-facing, long links
  styles.py       net_styles (badpcb.json): parse, KiCad netclass reading (.kicad_pro), resolution + stacking per net, `badpcb styles`
  lint.py         route lint: per-net ugliness metrics + weighted score + table (`badpcb lint`, `report --section lint`)
  polish.py       `droute --polish N`: accept/reject loop (hooks injected; droute.polish_env wires KRT, kicad-cli and lint)
  groute.py       PathFinder negotiated-congestion global router on a coarse multilayer grid;
                  history costs -> blockage explanations, thrash counts
  droute.py       detailed routing = scoped KiCadRoutingTools (KRT) passes + badpcb-vocabulary report (see Phase 4 notes)
  cli.py          `python -m badpcb <cmd>`: report, eval, act, groute, droute, snapshot, ...
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
Decision: no own detailed router. KRT is located by $KRT_DIR / $KRT_PY (or badpcb.json `krt`), own venv; `source scripts/krt_env.sh`.
- `python3 -m badpcb apply SESSION out.kicad_pcb` (plan and writer both in-process python3; the writer patches the board text, see `kicad_pcb.py`).
  Stored zone fills are dropped when anything changed (KiCad / `kicad-cli pcb drc --refill-zones` refill them). Writes `swaps_applied.md` beside the board.
- `python3 -m badpcb droute SESSION|BOARD [--nets ..|--region R8|--component ..] [--fanout U1] [--grid mm|auto] [--time S] [--preview]`;
  passes chain (.badpcb/work/droute/<session>/passN.kicad_pcb, current.kicad_pcb). `--measure` prints the METRICS line of any board.
- Grading DRC always uses the ORIGINAL project rules (badpcb.json rules.project, else the input board's own .kicad_pro) (KRT lowers clearance/drill floors in the project it writes).
- KRT runs with `--escalation board` (never below the board's minimums); KRT treats every KiCad-locked track as unrippable and
  protects those nets, so locked lanes/stubs are hard obstacles for it.

## Phase 5 notes (rules, feasibility, locks, pad-level legality)
- **Rules live in the project, not the board.** a board-side script (key_board: `rules.py`) writes `.kicad_pro` (Default netclass 0.15/0.2, via 0.5/0.25, minimums) and `.kicad_dru`
  (clearance/track 0.10 inside the `U1_FINE` rule area = courtyard + 1 mm). `apply.copy_rules` puts both beside every board badpcb writes; DRC grades against them (`--rules-pro` overrides).
  
- `feas.py` / `badpcb report --section feasibility` (first section): per fine-pitch (< 0.65 mm) part and side: pitch, pad width, gap, max track between pads,
  straight-stub fit, 45 degree fan spacing vs track + clearance, via-in-pad, staggered via fan; verdict OK/FAIL with the limit. Also the min grid step per region,
  which `droute --grid auto` uses. It reads the effective rules (netclass + the .kicad_dru forms rules.py writes).
- `locks.py`: copper classes from badpcb.json `locks.classes` (conventionally `lane` / `mimic`) / `fanout` (other locked) / `free`; Track/Via carry `id` (KiCad uuid) and `cls`.
  Actions `unlock TARGET`, `lock TARGET` (class | net/glob | region | box), `trim-lanes`; `apply` writes the lock flags (plan keys `unlock`/`lock`) and deletes trimmed lane segments.
  Metrics: `Xcu=N(L locked)`. Report section `copper` splits crossings into locked/rippable and lists locked walls per net;
  droute marks KRT's blockers `[LOCKED lane xN]` and prints the straight-path blockage by locked copper.
- `legal.py`: pad-level legality for move/rot/flip/spots: new pad-to-pad (any part incl. through-hole), pad-to-foreign-track/via (lane copper = error, free/mimic copper = warning, apply rips it),
  holes and board edge. Only conflicts new relative to the current position count.

## Phase 6 notes (badpcb audit: placement linter)
`python3 -m badpcb audit <board|session> [--json] [--full] [--roles] [--groute] [--only DET,..]` (badpcb/audit.py). Detectors ESCAPE (fan-zone capacity + intruders
of fine-pitch sides), PLANE (supply pads with no plane under them), BOWTIE, ORIENT, TENSION, ROLES (inferred decap/filter/crystal/pull-up/ESD roles, `--roles`),
BUSORDER, COURTYARD (bbox model minus 0.09 mm bias, notched big-part corners ignored), DENSITY. Every fix is a compact action list with its evaluate.py delta
and legal.py verdict (read-only). `badpcb suggest S audit` returns the best fix per finding as cumulative eval rows (`act S @1`, `@2`, ...).

## Phase 7 notes (tools lane)
- `badpcb plan S [U1[,U2]] [--edge 1.5]` / `badpcb audit S --joint` (badpcb/joint.py): ONE placement plan for an IC's fan-zone intruders + role parts (decap, filter cap, crystal load, pull-up, esd) under a shared constraint set (outside all escape zones, courtyard >= edge mm from the board edge, role distance, legality against the plan so far, stubs move with the part). Most constrained first, relaxed second pass, prints before/after fan usage / role violations / evaluate delta and a pasteable `badpcb act` line. It cannot create space: parts with no legal spot are listed.
- `reshape NET add|sub X0,Y0,X1,Y1 [layer=In2.Cu]` (actions.py): plane-zone edit. `add` gives the box to NET (added to NET's zone, carved out of other zones on that layer; warns about pads of the carved plane that lose it), `sub` only removes it from NET's zone. Reversible (inverse = `reshape NET restore` with the old polygons), engine/evaluate see it through `board.zones` (plane stubs). `badpcb apply` writes zone outlines first, extra polygons become sibling zones (a saved zone holds one polygon), then refills. audit PLANE now emits `reshape ...; via ... pad=..` as a fix next to the part moves.
- `delvia NET X Y stub` (remove a via and the short stub track to its pad) is parseable text now.
- `badpcb droute S --repair [--radius 4]` (badpcb/repair.py): rips only the unlocked, non-plane copper within `radius` of both ends of every open connection (locked copper counted and named for `unlock`), adds the ripped nets to the KRT scope. Pass `mode` (plain/repair/strip) is recorded in passes.jsonl; the ping-pong warning ends with the next rung of the ladder (plain -> repair -> strip+fanout -> change placement/unlock).
- `badpcb groute S|board [...]` (badpcb/gscore.py): the PathFinder global router on a session's current state with its hints (corridor/layer/priority/freeze; side/reserve/near listed as ignored). `badpcb audit S --gscore [SEC]` / `badpcb suggest S audit --gscore [SEC]` re-rank each finding's top-3 fixes by a short groute run on the finding's region (score = overflow + 5 no-path + 0.1 history; shown as `{groute R8: score a -> b}`). Coarse: at the 1 mm default it separates fixes only where the locked-lane/cut capacity is the limit (U1's fan is invisible below ~0.25 mm cells, ~40 s per run; BADPCB_GSCORE_CELL overrides).

## Phase 8 notes (design intent and floorplan)
- `intent` (badpcb.json, parsed in `config.py`: `parse_intent`, `intent_entries`, ids `near1`, `clear1`, `block:NAME`) lives in `World.intent` (+ `intent_ver`),
  so it is part of the session like hints: actions `intent_add` / `intent_rm` / `intent_capture` (compact `intent add JSON`, `intent rm ID`, `intent capture BLOCK`)
  are logged, replayed and have exact inverses. Everything is soft: weights only, no checks in `actions.check` (it only warns about selectors that match nothing).
- Scoring: `intent.penalty(world)` is a pure function of the part poses (compiled selectors + roles cached per `intent_ver`; escape rectangles per IC pose), so the
  engine's incremental metrics equal a fresh recompute (tests). `Engine.metrics()` adds `intent` (+ `intent_n`); `METRIC_KEYS`/`WEIGHTS` carry it with weight 1,
  `fmt_metrics` prints `intent=` only when something is declared. `joint.py` adds `0.5 x intent.part_cost()` (entries involving that part, at the candidate courtyard) to a spot's cost.
- Reference blocks: the anchor's pads (by pad number) are fitted rigidly (rotation + translation, mirror when the board sides differ) onto the reference's;
  member offsets from the mapped reference positions give the RMS drift. References come from a `.kicad_pcb` (extracted and cached like any board) or a capture
  (stored in the entry, so it survives in the session log).
- Audit `INTENT` (audit.py `detect_intent`): one finding per violated entry; fixes come from a ring search for the nearest legal spot where the entry stops penalising
  the part without making the part's other entries worse (`Ctx.ring_spot`); fix ranking uses `dscore` (which includes `intent`) and requires a negative intent delta.
- `floorplan.py`: declared blocks, then connectors, regular arrays (>= 12 identical footprints, even spacing; co-located arrays merged), IC seeds, passives attached
  by shared signal net / nearest supply pin, leftovers by net connectivity. Block-level ratsnest = MST per signal net over the net's pad centroids in each block.
  Escape-facing reuses `audit.escape_zones(.., any_pitch=True)` and extends each zone to a `--depth` corridor.

## Project config and state (standalone badpcb)
- `badpcb.json` (cwd or parent, `--project`, `$BADPCB_PROJECT`) holds board facts: rules project, mcu / fine-pitch parts, swap groups, lock classes + lane trim
  settings, plane hints, KRT location. No file: auto-detect fine-pitch parts, no groups, no classes.
- State (`sessions/`, `work/`, `cache/`) lives in `<project dir>/.badpcb/` or `$BADPCB_HOME`; session files store the board path relative to the project.
- **Work directory**: every intermediate (droute pass boards and KRT logs, strip/repair/fanout copies, apply plan + report + DRC json, stub plans, extract dumps) is written under `config.work_dir(kind, id)` = `<state dir>/work/<kind>/<id>/` (`"work_dir"` in badpcb.json relocates it). Commands write only the final board they were asked for (+ its rules files, `swaps_applied.md`) outside it, so the board folder stays readable.
- `-m badpcb.extract|apply --write|strip_nets` are plain python3 entry points, also called in-process (no subprocess, no `$KIPY`).
- Flip/rotate emulate pcbnew (Flip about the origin + SetOrientation): the footprint keeps the requested angle, local y of all children is negated,
  pad angles are negated relative to the footprint, F./B. layers swap, `(justify mirror)` toggles. Verified against pcbnew 10 for 0/90/180/270.

## Phase 9 notes (start-right respin fixes)
- `audit` header: board path, mtime (age) and zone count (`board_line`), also in the JSON; a board without zones says so.
- **CHANNEL** (`audit.detect_channel`): per fine-pitch side the corridor its fan leaves through = from the pad row outward `channel_depth_mm` (shortened toward where the nets go),
  laterally between the nearest drilled holes / through pads / keepouts on each side of the pad row (at most `channel_widen_mm` past the outermost pads). Support passives on the
  IC's board side inside it that are not in the IC's reference block (decap/regulator/crystal roles, members of intent blocks containing the IC, `clear` exceptions) are findings; severity from the share of the
  corridor width they occupy. ESCAPE only looks 2-3 mm deep, ROLES only at distances, so on the v2 keyboard 8 RC parts in an 8.1 mm hole-to-hole channel passed both. Tunables: `audit.channel_*`.
- **PLANE** judges a pad by its copper extent and the stub + via reach into the right zone (zone outlines; fills are never read), not by the pad centre.
- **plane_layers** (`model.Board.plane_layer_info`): coverage of the board outline by netted zone outlines > `planes.min_coverage`; `plane_zones()` / `plane_nets()` only count zones on such layers.
- **floorplan regulator check** (`_regulators`): loop caps only (declared `vreg`/regulator block, LX/VREG/FB nets, nearest cap on each output rail + any within `REG_CAP_MM`); other rail caps are listed as `rail decaps not counted`.
- **droute**: `--repair` prints `repair scope WIDENED ...` when its nets/ends go beyond the request; passes record the cuts their open connections cross (`passes.jsonl` `cuts`),
  and `repair.cut_cycle` fires the ping-pong escalation when the same cut handle ('U1 ring S') keeps failing while the net names rotate.

## Phase 10 notes (net styles, route lint, constrained-first, polish)
- **styles.py**: `net_styles` is validated in `config.Project.net_styles()` (unknown keys rejected). A net is styled by name pattern or KiCad netclass (`net_settings.netclass_assignments` + `netclass_patterns` of the board's own `.kicad_pro`); stacking = stricter value per key
  (max via_penalty, min detour_max / skew_mm, union avoid / pairs, OR flags). Soft only: nothing in styles is a veto.
- **groute**: `GRouter(styles={net: NetStyle})` (`run()` loads badpcb.json's by default). Per net: via cost `via_cost + via_penalty`; length weight `1 + 2 (1.5 - detour_max) / 1.5` when `detour_max < 1.5`; `avoid` = extra cost `AVOID_COST x cell` on cells within one cell of the avoided nets' current copper (mutual).
  With no styles the three per-net tables are empty and the search is the global-cost one, bit for bit (tests compare routes).
- **lint.py**: reads only the board model (tracks, vias, pads, zone outlines, escape zones from `audit.escape_zones`); never fills. Bends = track-end nodes of degree 2 not on a pad / via; jogs = short segments between two bends; direction breaks vs the layer's dominant direction (H/V ratio > 1.5);
  split / gap: samples every 0.5 mm along outer-layer tracks, zone under the sample = highest-priority zone covering it on the adjacent plane layer. Handles from `groute.Names` (cut > ring > region) at the worst feature. ESCALATE when >= 40 % (and >= 8) of the nets are bad.
- **droute**: `style_krt_args` turns styles into KRT flags (`--via-cost` 75 + 10 x penalty, `--keep-away AGG:VICTIM:1.0`, priority: `--turn-cost 1500`). KRT capabilities used: `--via-cost`, `--turn-cost`, `--keep-away`, `--keep-input-copper`, env `KICAD_RIP_PREEXISTING=0` / `KICAD_FINALIZE_RIP=0`; not wired: `route_diff.py` (pairs),
  `--length-match-group`, `--layer-costs`. `--priority` scopes the pass to the constrained nets (`--clean-board`: work copy without other signal copper); `--polish N` = `polish.polish` with the real hooks. `route_layers(board)` replaces `signal_layers()` for KRT when outer GND pours make every layer a "plane layer".
