"""Delete copper from a board (pure python, no KiCad):

python3 -m badpcb.strip_nets src.kicad_pcb dst.kicad_pcb NET [NET ...]: delete all tracks and vias of the given nets (droute --strip).
python3 -m badpcb.strip_nets src.kicad_pcb dst.kicad_pcb --ids ids.json: delete the tracks/vias with these uuids (droute --repair).
"""
import json
import sys

from .kicad_pcb import Board


def strip(src, dst, nets=(), ids=None):
    """Write `dst` = `src` without the selected tracks/vias; returns how many were removed."""
    b = Board.load(src)
    nets = set(nets)
    dead = [t for t in b.items if (t.uuid in ids if ids is not None else t.net in nets)]
    for t in dead:
        t.delete()
    b.save(dst)
    return len(dead)


def main(src, dst, nets, ids=None):
    print("stripped", strip(src, dst, nets, ids))


if __name__ == "__main__":
    if len(sys.argv) > 4 and sys.argv[3] == "--ids":
        main(sys.argv[1], sys.argv[2], set(), set(json.load(open(sys.argv[4]))))
    else:
        main(sys.argv[1], sys.argv[2], set(sys.argv[3:]))
