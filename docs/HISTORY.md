# History: how badpcb got here (case study: the key_board PCB)

The board: 298x84 mm, 4 layers (In1 GND, In2 +3V3/+3V3A), 58 sensors on B.Cu, an RP2350B (QFN-80, 0.4 mm pitch) at U1, 8 EN lines x 8 ADC lines.
Freerouting ran for hours and left many nets open. badpcb was built so an LLM agent could steer routing with text and named handles instead.
Numbers below are that board's; the lessons are general.

## Phases
1. **Foundation**: pcbnew only at the edges (extract/apply under `$KIPY`), pure-Python model, ratsnest MST per net, region/cut topology with
   named handles (R8, C12, K1), conflict matrix and clusters, ASCII map, swap-group legality, `report`.
2. **Actions + what-if**: typed reversible actions (move/rot/flip/swap/assign/via...), sessions with undo/snapshot/branch/revert, `eval` (delta metrics
   without committing; incremental result == from-scratch result is a test).
3. **Global router**: PathFinder negotiated congestion on a coarse multilayer grid; history costs turn into "who blocks whom" explanations.
4. **Detailed routing**: no own router; scoped KiCadRoutingTools passes translated back into badpcb vocabulary; DRC always graded against the ORIGINAL rules
   (KRT writes a lowered .kicad_pro; qfn_fanout does too, which made the next pass escalate to 0.10 mm everywhere: 161 DRC errors until droute restored the rules).
5. **Rules, feasibility, locks, pad-level legality**: the fan-out was infeasible under the default rules (stubs 0.283 mm apart, needing 0.35): a local
   0.10/0.10 rule area fixed it, no placement change could have. Locked scripted lanes made every net that owned one "protected" for KRT: unlocking
   lanes took 65 open connections to 6, more than any placement work.
6. **Audit**: placement linter (ESCAPE, PLANE, BOWTIE, ORIENT, TENSION, ROLES, BUSORDER, COURTYARD, DENSITY), each finding with a fix list, its
   evaluate delta and a legality verdict.
7. **Tools lane**: joint placement plans (`badpcb plan`), plane-zone edits (`reshape`), incremental repair (`droute --repair`), the ping-pong ladder
   (plain -> repair -> strip+fanout -> change the board), groute scoring of audit fixes, drag-with-move, progress output.

## The steered run (21 passes)
Start: 224 open connections. Fan-out at 0.10 mm: 65. Unlock lanes: 6. Targeted passes: 3, then 0 signal connections open at pass 16; the plane finalize then
ripped three escapes and the run ping-ponged (ADC1/ADC2/+1V1 on the same four pins) to a best board with 1 open connection and 1 pre-existing courtyard error.
The board finish closed it: nudge the one courtyard offender, move the series resistor that sat on the blocked pin's fan (the audit's ESCAPE finding named it),
pull the ADC filter capacitor from 11.3 to 6.6 mm of its pin, strip and re-escape the bundle. Final: 0 unrouted, DRC 0, no firmware pin swap needed.

## Lessons
- Feasibility before geometry: check rules against pitch first; spend seconds on diagnosis, minutes on routing.
- Fix one cause at a time. Applying every audit fix at once made things worse (65 open both ways).
- Locked copper is a wall for the router: name it in every failure report and offer `unlock`.
- Measure every step with one yardstick (`droute --measure`); for placement look at the open connections near the IC under study, not the board total.
- Cycle detection matters: three passes of the same kind that move the failure around mean change the board, not the pass.
- Time boxes (pass <= 10 min), kill the whole process group on timeout, flush progress when piped.
- Every `$KIPY` entry point must exit through `kipy_exit.done()` (pcbnew segfaults at interpreter teardown).
