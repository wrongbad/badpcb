# Case study: key_board

Config for the 298x84 mm keyboard board the kernel was developed on (RP2350B at U1, 58 sensors, EN/ADC lines). See `../../docs/HISTORY.md`.

Files
- `badpcb.json`  the project config. Copy it next to `key_board_placed.kicad_pcb` (the board is not included: too big) and adjust the paths.
- `swaps.json`  swap groups: `adc` (permute the 8 ADC lines over GPIO40..47), `en` (EN0..7 on any free GPIO), `rc` (RC part sets), `keys` (key -> region/EN assignment, `net_regex` marks the EN nets), plus `pin_names` for the 80 pins.

The config shows every board-specific knob: `mcu`, `fine_pitch_parts`, `rules.project`, `locks.classes` (`lanes.json`: scripted EN/SENSE trunk copper, `mimic.json:copied`: reference copper), `locks.trim` (which lane nets `trim-lanes` may cut, which refdes count as keys, where trunks start), `krt`.
Not included, produced by the board's own scripts: `lanes.json`, `mimic.json`, `key_board_placed.kicad_pro/.kicad_dru`.

In the author's tree this is `synth/key_board_smarter/` (`badpcb.json`, state in `.badpcb/`, board tests in `rk_board_tests/`):
`cd key_board_smarter; source ../env.sh; export PYTHONPATH=../badpcb; python3 -m badpcb report key_board_placed.kicad_pcb`
