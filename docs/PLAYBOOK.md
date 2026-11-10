# badpcb playbook: the agent loop

Distilled from a 21-pass steered routing session (1 open connection left), the board finish (the last connection closed after the
diagnosis was acted on) and tool experiments; see HISTORY.md. Run everything from the board project directory (where `badpcb.json` is)
after `source scripts/krt_env.sh` (and `export PYTHONPATH=<badpcb checkout>` unless installed). Names such as U1, R20, ADC2 below are the case study's. Routing passes take minutes, decisions take seconds:
**spend the seconds first, route second.**

## The loop

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
   C24 C27 R20"), PLANE = supply pads over the wrong plane, ROLES = decaps/filter caps too far, COURTYARD = DRC errors.
4. **Fix small, one cause at a time.** What closed the last connection (board lane B3): move the *one* part sitting on the
   blocked pin's fan (R20, a series resistor, not the cap), pull the filter cap toward its pin (C26 11.3 -> 6.6 mm), then re-escape
   the bundle. Applying every audit fix at once made things worse (ESCAPE pushed caps out, ROLES pulled them back, parts ended on the
   board edge, plane stubs were stranded; 65 open conns both ways).
   * `badpcb eval S 'move R20 149.8 1.2' ...` what-if table (nothing applied), `badpcb spots S R26 157 14` legal places,
     `badpcb act S '<action>; <action>'` applies atomically (`@N` applies row N of the last eval/suggest table).
   * Several parts that compete for the same room: `badpcb plan S U1 [--minimal] [--edge 1.5]` computes them JOINTLY (outside the fan,
     off the board edge, role distance kept, plane stubs follow) and prints one pasteable `badpcb act` line. `--minimal` moves only the
     parts that block a stub plus their filter caps. Parts it lists as "NO LEGAL SPOT" need a manual decision.
   * Supply pads over the wrong plane (PLANE ERR): either move the part (fix `move ...`) or `reshape +3V3A add X0,Y0,X1,Y1
     layer=In2.Cu` (the audit offers both; the reshape fix lists how many pads of the other plane it strands). Count the stranded pads
     before choosing; the board lane left a split alone because the box would strand 11 pads.
   * Pin assignments: `badpcb suggest S adc|en|U1` ranks swaps; remember a swap is a firmware change (`swaps_applied.md`).
5. **Cheap routability check.** `badpcb groute S --region R8 --time 20` (global router on the session's current placement + its
   hints: corridor/layer/priority/freeze). `badpcb audit S --gscore 60` / `badpcb suggest S audit --gscore 60` rank each finding's fixes
   by a groute run. It is coarse (1 mm cells do not see the U1 fan; 0.25 mm sees it but takes ~40 s), so treat "score gets worse" as a
   veto and "same" as no information.
6. **Apply + scoped routing.** `badpcb apply S out.kicad_pcb` checks that the written board has the session's metrics and no new DRC errors.
   Route with `badpcb droute S --region R8 --fanout U1` (first pass; `--nets`, `--component` narrow the scope; always <= 10 min, kill the
   process group on timeout). Chain passes (`current.kicad_pcb`); lock/trim-only `act`s patch the routed board in place.
7. **Read the failure, then pick the next rung.** droute prints OPEN connections with region/cut handles and the blockers marked
   `[LOCKED lane xN]`. Locked copper makes a net PROTECTED: KRT never rips it nor re-routes around it. `badpcb act S 'unlock EN4' ...`
   (or `unlock lanes`, `unlock mimic`) before blaming the router. torture2: 65 -> 6 open conns came from unlocking lanes, not from placement.
8. **Repair, don't restart.** Few open conns on a routed board: `badpcb droute S --repair [--radius 4] [--fanout U1]` rips only the
   unlocked copper within `radius` of the open ends and re-routes that group together. If the failure moves between the same few nets
   (ping-pong warning) droute prints the next rung of the ladder: plain pass -> `--repair --nets <group>` -> `--strip --fanout U1
   --nets <group>` (re-escape the whole bundle together; this is what closed ADC2) -> change the board (placement via `badpcb plan`, pin swap,
   unlock) because another pass will not converge.
9. **Plane finalize last.** Plane nets (`--nets GND +3V3 +3V3A`) go through KRT's finalize and can rip signal escapes; run them
   before the final signal repair, check `badpcb report <board> --section planes` (0 unserved islands) and `python -m badpcb.stubs` only for stub vias
   with all signal copper fixed.
10. **Finish.** `kicad-cli pcb drc --refill-zones` against the project rules (not the lowered ones KRT writes; droute restores ours),
    DRC errors 0, unconnected 0, write `swaps_applied.md`.

## Rules of thumb

* Measure every step with one yardstick: `badpcb droute <board> --measure` -> `METRICS(droute) unrouted_conns=.. drc_err=.. unconnected=..`.
  For placement work look at the open conns of nets with a pad on the IC under study, not the board total (60 of 65 open conns in the
  U1 test were far-away locked SENSE/EN lanes that no U1 placement can move).
* Never land a part within ~1.5 mm of the board edge unless there is no alternative (`badpcb plan --edge`); never move a part without
  its plane-via stub (the joint plan does this: `delvia ... stub; move; via ... pad=`).
* `nudge` leaves attached copper where it was (apply rips it); for a sub-0.1 mm courtyard fix stretch the track ends instead
  (board lane B2).
* Time boxes: a routing pass <= 10 min, a decision step <= a few seconds. If you have run the same kind of pass three times, change the kind.
