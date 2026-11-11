# Case study: key_board_v2 (the start-right respin)

The same 58-key board as `../key_board`, redone with the PLAYBOOK workflow after an EE review of v1:
RP2354B (internal flash, no QSPI part), the Raspberry Pi RP2350 80-QFN minimal design as the reference block, U1 moved
so its front signals leave through the hole-free channel between switch rows, J1 kept out of U1's escape fans.

Files
- `badpcb.json`  project config with soft intents (blocks with `ref`, near/apart/edge/clear). The board is not included (too big).
- `WORKFLOW_LOG.md`  what each PLAYBOOK step found and decided, with numbers.

Result: 0 DRC errors, 0 unconnected (kicad-cli, fixed rules), nothing locked.

| | v1 | v2 |
|---|---|---|
| foreign block in a U1 fan | J1 on U1.N, U1.W | none |
| flash link | LONG, 15.6-21.1 mm | gone |
| block crossings | 105 | 40 |
| intent score | 102.6 | 0.0 |
| KRT time | ~40 min + hours of steering | ~59 min total, final chain 7 passes / 14 min |

Most of v2's routing time went into four routing-forced placement changes (each followed by a fresh chain rather than a patch):
support parts in the U1 front channel, a reference-pour experiment that made droute treat an outer layer as a plane, an ADC net rename, an EN pin move.
