# source scripts/krt_env.sh   -- point rk at KiCadRoutingTools (the `rk droute` backend) and KiCad.
# Honours values already set; otherwise uses the documented defaults:
#   KRT_DIR    ~/KiCadRoutingTools                      (checkout holding py_router/route.py)
#   KRT_PY     $KRT_DIR/.venv/bin/python                (its own venv: numpy scipy shapely Pillow; NOT KiCad's python)
#   KIPY       KiCad's bundled python (has pcbnew); macOS default below
#   KICAD_CLI  kicad-cli (PATH, else the macOS app bundle)
export KRT_DIR="${KRT_DIR:-$HOME/KiCadRoutingTools}"
export KRT_PY="${KRT_PY:-$KRT_DIR/.venv/bin/python}"
if [ -z "${KIPY:-}" ]; then
  for c in "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3" /usr/lib/kicad/bin/python3; do
    [ -x "$c" ] && export KIPY="$c" && break
  done
fi
if [ -z "${KICAD_CLI:-}" ]; then
  export KICAD_CLI="$(command -v kicad-cli || echo /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli)"
fi
# rk running from a source checkout: export PYTHONPATH=/path/to/rk   (not needed after `pip install -e .`)
krt() { local s="$1"; shift; "$KRT_PY" "$KRT_DIR/py_router/$s.py" "$@"; }   # krt route in.kicad_pcb out.kicad_pcb --nets ...
