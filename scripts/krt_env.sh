# source scripts/krt_env.sh   -- point badpcb at KiCadRoutingTools (the `badpcb droute` backend) and KiCad.
# Honours values already set; otherwise uses the documented defaults:
#   KRT_DIR    ~/KiCadRoutingTools                      (checkout holding py_router/route.py)
#   KRT_PY     $KRT_DIR/.venv/bin/python                (its own venv: numpy scipy shapely Pillow; NOT KiCad's python)
#   KICAD_CLI  kicad-cli (PATH, else the macOS app bundle)
export KRT_DIR="${KRT_DIR:-$HOME/KiCadRoutingTools}"
export KRT_PY="${KRT_PY:-$KRT_DIR/.venv/bin/python}"
if [ -z "${KICAD_CLI:-}" ]; then
  export KICAD_CLI="$(command -v kicad-cli || echo /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli)"
fi
# badpcb running from a source checkout: export PYTHONPATH=/path/to/badpcb   (not needed after `pip install -e .`)
krt() { local s="$1"; shift; "$KRT_PY" "$KRT_DIR/py_router/$s.py" "$@"; }   # krt route in.kicad_pcb out.kicad_pcb --nets ...
