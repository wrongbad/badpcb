# badpcb playbook: the agent workflow

Distilled from a 21-pass steered routing session (1 open connection left), the board finish, tool experiments (HISTORY.md) and the
post-mortem of the key board (RP2350B QFN-80 + 58 Hall sensors). That post-mortem found that routing struggled mostly because of
**three bad starting conditions, not weak tools**: an external QSPI flash where an RP2354 (internal flash) would have removed it, a
reference layout (regulator, flash, crystal) that was not the starting point, and a connector (J1) sitting in the MCU's escape path.
Every later phase patched symptoms. So: **get the start right, then route**. Routing passes take minutes, decisions take seconds.

Run everything from the board project directory (where `badpcb.json` is) after `source scripts/krt_env.sh` (and
`export PYTHONPATH=<badpcb checkout>` unless installed). Names such as U1, R20, ADC2 below are the case study's.

**Keep the project top level readable.** It holds only: the final board (+ its `.kicad_pro` / `.kicad_dru`), `badpcb.json`, the notes and logs you write
for humans (NOTES.md, WORKFLOW_LOG.md), and `fab/`. Everything else is an intermediate and goes in the nested work directory, which badpcb already uses by default
(`.badpcb/work/<kind>/<id>/`: droute pass boards, strip/repair/fanout copies, KRT logs, apply plans and reports, stub plans, DRC json; relocate with `"work_dir"` in `badpcb.json`).
Rules for the agent: never pass `--out`/`apply`/`stubs` targets next to the final board for experiments (write them to `.badpcb/work/<run>/` or the scratch area),
put your own helper scripts and chain logs in a `work/` or `scripts/` subfolder, not beside the board, and copy only the pass you accept to the final name.

```
0 simplify -> 1 reference first -> 2 floorplan -> 3 draft placement -> 4 gate -> 5 route (scoped passes) -> 6 escalate upward, never patch
```

Intents and reference layouts are **soft**: penalty weights only, no vetoes, no locks. Breaking a reference is allowed and often
necessary; it just has to be visible (`intent=` in METRICS, audit INTENT findings, `floorplan`).

## 0. Simplify the architecture (before the schematic is frozen)
Every support circuit you delete is a routing problem you never have. Prefer integrated parts: an MCU variant with internal flash
(RP2354 instead of RP2350 + QSPI flash + its decaps), integrated regulators / DC-DC modules, modules with antenna or crystal. Check the
datasheet for the variant's pin compatibility and its own requirements before the schematic is frozen; after freeze this is a respin.
Ask for each support block: "what does it cost in parts, length-critical nets and fan-zone space, and can a part number remove it?"

## 1. Reference first
For each complex or multi-pin part (MCU, regulator, flash, USB, radio, ADC): find its **hardware design guide and reference layout**
(vendor "minimal" board, app note, datasheet layout section). Write down:
* its requirements as **intents** in `badpcb.json` (`intent`): decaps within N mm of their pins (`near`), connectors on the board
  edge (`edge`), nothing but decaps in the fan zone (`clear` `escape:U1` except `decaps:U1`), crystal next to the oscillator pins,
  digital switching away from analog (`apart`);
* its critical subcircuits as **reference blocks**: `blocks: {vreg: {parts: [...], ref: {pcb: <reference .kicad_pcb>, anchor: "U1", map:
  {C17: C7}}}}`, or place them once and `intent capture <block>` to snapshot the arrangement. The reference arrangement is aligned on the
  anchor's pads (rotation, mirror, translation), so a layout on the other board side or turned 90 degrees still compares.

Weights say how much you care (default 1 per mm; `clear` 2 per intruder; reference drift 0.3 per mm RMS above 1 mm). Add or retire
intents as you learn, in a session: `badpcb act S 'intent add {"near": ["C5", "U1.5", 3]}'`, `'intent rm near2'`, `'intent capture vreg'`
(all undoable). `badpcb show S` lists them.

## 2. Floorplan: blocks before parts
`badpcb floorplan <board|S>` (works on rough placement; `--json` for tools). It groups parts into blocks (declared first, then
automatic: connectors alone, big regular arrays as one block, each IC with its passives) and prints, in about 40 lines: an ASCII block
map; per block bbox, area, parts, fill; block-to-block net counts and the block-level ratsnest crossings; connectors and their edge
distance; **what faces each fine-pitch IC side** (`FOREIGN IN FAN: J1` is the key-board failure in one line); long critical links
(flash / crystal / other ICs far from their MCU pins); regulator arrangement.
Fix the floorplan **now**, with block-level moves, before any part-level work:
* connectors on the board edge and not in front of a fan side; no foreign block in a fine-pitch fan zone;
* the blocks with the most nets between them adjacent; block-level crossings low (reorder blocks, rotate/mirror the MCU);
* critical blocks (flash, crystal, regulator) next to the pins they serve, in the reference orientation.

## 3. Draft placement inside the blocks
Follow the references (`badpcb audit S --only INTENT` shows the drift per block with the move that restores it). Do not **crowd** what
must not be crowded: fine-pitch escape sides (`audit --only ESCAPE`, `badpcb plan S U1`), switching loops, analog. Do not **spread**
what must be close: decaps at their pins, crystal and load caps, flash, the regulator loop. `badpcb plan S U1` places a whole IC's
support jointly and now weighs intent too; `suggest S audit` ranks fixes with `intent` in the delta.

## 4. Gate: audit + intent clean enough before routing
Run `badpcb report S --section summary` (METRICS with `intent=`), `badpcb audit S` (check its header: board path, mtime, zone count, so you know it read the board you think), `badpcb floorplan S`.
Also look at the **escape corridors**: `badpcb audit S --only CHANNEL` finds the hole/keepout-free channel each fine-pitch side has to leave through and lists the support parts (pull-downs, RC filters,
anything that is not the IC's own reference block) sitting in it. ESCAPE only sees the 2-3 mm fan zone and ROLES only distances, so these were all "legal" and still cost three failed chains on the v2 keyboard.
Move them (pull-downs may sit anywhere on their net) or declare them (`blocks` / `clear.except`) and run `badpcb groute S --region <channel region> --time 20` as a cheap corridor score before routing.
Go to routing when: no ERR finding in ESCAPE / CHANNEL / PLANE / ROLES / INTENT / COURTYARD, WARN INTENT findings are consciously accepted (relax or remove the intent
instead of ignoring it), floorplan shows no foreign block in a fan zone, no LONG link. Breaking a reference on purpose is fine: say so
by `intent rm` / a looser weight, so the next audit does not nag. This gate is the inner loop below.

## 5. Route in scoped passes
Loop details in the next section. Order of scope: first the fine-pitch escape (`--fanout U1`), then the critical blocks, then buses, plane
nets last. Measure every pass with one yardstick (`badpcb droute <board> --measure`).

## 6. Escalate upward, do not patch
If a region needs **more than about 2 repair passes**, or the **ping-pong warning** fires (the failure just moves between the same few
nets, or, keyed on the cut handle, the same cut such as `U1 ring S` keeps failing while the net names rotate), stop routing and go back to **step 2/3**: re-floorplan the block (rotate or move the MCU, move the connector, split the bus) or
to **step 0/1** if the cause is the architecture or a reference you skipped. Another pass will not converge. Ask: which intent / which
block made this region impossible, and why did the gate not catch it? Add that as an intent so it is caught next time.

---

## The inner loop (steps 4-5)

```
report -> feasibility -> audit -> fix (small!) -> groute score -> scoped droute -> repair -> (back to audit)
```

1. **Look.** `badpcb report <board|S>` (summary, clusters, copper, cuts, planes, u1). Note open conns, locked tracks, which nets are
   plane nets. A session (`badpcb session new S board.kicad_pcb`) keeps every action reversible (`undo`, `snapshot`, `branch`, `revert`).
2. **Feasibility before geometry.** `badpcb report <board> --section mcu` / the feasibility lines of droute: fan stubs need
   `track + clearance <= pitch gap`. If the rules (`.kicad_pro`/`.kicad_dru`) do not allow the fan, fix the rules first
   (U1_FINE rule area 0.10/0.10 in torture2). No placement change fixes a rule problem.
3. **Audit.** `badpcb audit S` (add `--roles` for inferred decap/filter-cap roles, `--full` for every finding, `--json` for tools).
   Read ERR findings top to bottom. ESCAPE = what blocks the fine-pitch fan (the right diagnosis in torture2: "U1.N blocked by
   C24 C27 R20"), CHANNEL = support parts in the corridor the fan leaves through, PLANE = supply pads over the wrong plane (judged by pad copper extent and stub+via reach against zone outlines; unfilled zones count), ROLES = decaps/filter caps too far, INTENT = declared intents and reference
   drift, COURTYARD = DRC errors.
4. **Fix small, one cause at a time.** What closed the last connection (board lane B3): move the *one* part sitting on the
   blocked pin's fan (R20, a series resistor, not the cap), pull the filter cap toward its pin (C26 11.3 -> 6.6 mm), then re-escape
   the bundle. Applying every audit fix at once made things worse (ESCAPE pushed caps out, ROLES pulled them back, parts ended on the
   board edge, plane stubs were stranded; 65 open conns both ways).
   * `badpcb eval S 'move R20 149.8 1.2' ...` what-if table (nothing applied), `badpcb spots S R26 157 14` legal places,
     `badpcb act S '<action>; <action>'` applies atomically (`@N` applies row N of the last eval/suggest table).
   * Several parts that compete for the same room: `badpcb plan S U1 [--minimal] [--edge 1.5]` computes them JOINTLY (outside the fan,
     off the board edge, role distance kept, plane stubs follow, intents weighed) and prints one pasteable `badpcb act` line. `--minimal`
     moves only the parts that block a stub plus their filter caps. Parts it lists as "NO LEGAL SPOT" need a manual decision.
   * Supply pads over the wrong plane (PLANE ERR): either move the part (fix `move ...`) or `reshape +3V3A add X0,Y0,X1,Y1
     layer=In2.Cu` (the audit offers both; the reshape fix lists how many pads of the other plane it strands). Count the stranded pads
     before choosing; the board lane left a split alone because the box would strand 11 pads.
   * Pin assignments: `badpcb suggest S adc|en|U1` ranks swaps; remember a swap is a firmware change (`swaps_applied.md`).
5. **Cheap routability check.** `badpcb groute S --region R8 --time 20` (global router on the session's current placement + its
   hints: corridor/layer/priority/freeze). `badpcb audit S --gscore 60` / `badpcb suggest S audit --gscore 60` rank each finding's fixes
   by a groute run. It is coarse (1 mm cells do not see the U1 fan; 0.25 mm sees it but takes ~40 s), so treat "score gets worse" as a
   veto and "same" as no information.
6. **Apply + scoped routing.** `badpcb apply S out.kicad_pcb` checks that the written board has the session's metrics and no new DRC errors. The writer edits the board file in place (no KiCad needed) and drops stale zone fills; DRC refills them (`--refill-zones`).
   Route with `badpcb droute S --region R8 --fanout U1` (first pass; `--nets`, `--component` narrow the scope; always <= 10 min, kill the
   process group on timeout). Chain passes (`current.kicad_pcb`); lock/trim-only `act`s patch the routed board in place.
7. **Read the failure, then pick the next rung.** droute prints OPEN connections with region/cut handles and the blockers marked
   `[LOCKED lane xN]`. Locked copper makes a net PROTECTED: KRT never rips it nor re-routes around it. `badpcb act S 'unlock EN4' ...`
   (or `unlock lanes`, `unlock mimic`) before blaming the router. torture2: 65 -> 6 open conns came from unlocking lanes, not from placement.
8. **Repair, don't restart.** Few open conns on a routed board: `badpcb droute S --repair [--radius 4] [--fanout U1]` rips only the
   unlocked copper within `radius` of the open ends and re-routes that group together. If the failure moves between the same few nets
   (ping-pong warning) droute prints the next rung of the ladder (and `--repair` prints `repair scope WIDENED ...` when it reaches beyond the nets/region you asked for): plain pass -> `--repair --nets <group>` -> `--strip --fanout U1
   --nets <group>` (re-escape the whole bundle together; this is what closed ADC2) -> **change the board** (step 6 above: floorplan,
   placement via `badpcb plan`, pin swap, unlock) because another pass will not converge.
9. **Plane finalize last.** Plane nets (`--nets GND +3V3 +3V3A`) go through KRT's finalize and can rip signal escapes; run them
   before the final signal repair, check `badpcb report <board> --section planes` (0 unserved islands) and `python -m badpcb.stubs` only for stub vias
   with all signal copper fixed.
10. **Finish.** `kicad-cli pcb drc --refill-zones` against the project rules (not the lowered ones KRT writes; droute restores ours),
    DRC errors 0, unconnected 0, write `swaps_applied.md`.

## Rules of thumb

* Reference copper: copy the vendor's small outer-layer pours (regulator LX, +1V1) if you like; a layer counts as a plane layer only when its netted zones cover > 30 % of the board
  (`planes.min_coverage`), and `report --section summary` / `droute` print which zones decided. The floorplan's `regulator` line measures only loop caps
  (declared `vreg` block, LX/VREG/FB nets, the output cap nearest the inductor); DVDD-style rail decaps are listed as "not counted".
* Measure every step with one yardstick: `badpcb droute <board> --measure` -> `METRICS(droute) unrouted_conns=.. drc_err=.. unconnected=..`.
  For placement work look at the open conns of nets with a pad on the IC under study, not the board total (60 of 65 open conns in the
  U1 test were far-away locked SENSE/EN lanes that no U1 placement can move).
* Never land a part within ~1.5 mm of the board edge unless there is no alternative (`badpcb plan --edge`); never move a part without
  its plane-via stub (the joint plan does this: `delvia ... stub; move; via ... pad=`).
* `nudge` leaves attached copper where it was (apply rips it); for a sub-0.1 mm courtyard fix stretch the track ends instead
  (board lane B2).
* Time boxes: a routing pass <= 10 min, a decision step <= a few seconds. If you have run the same kind of pass three times, change the kind.
* An intent that nobody can satisfy is a wrong intent: relax its weight or `intent rm` it. An intent that is violated and ignored hides
  the real ones.
