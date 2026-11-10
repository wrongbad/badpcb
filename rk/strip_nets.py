"""$KIPY -m rk.strip_nets src.kicad_pcb dst.kicad_pcb NET [NET ...]: delete all tracks and vias of the given nets (used by droute --strip).
$KIPY -m rk.strip_nets src.kicad_pcb dst.kicad_pcb --ids ids.json: delete the tracks/vias with these uuids (droute --repair)."""
import json
import sys

from rk import kipy_exit


def main(src, dst, nets, ids=None):
    import pcbnew as p
    b = p.LoadBoard(src)
    if ids is not None:
        dead = [t for t in b.GetTracks() if t.m_Uuid.AsString() in ids]
    else:
        dead = [t for t in b.GetTracks() if t.GetNetname() in nets]
    for t in dead:
        b.Remove(t)
    p.SaveBoard(dst, b)
    print("stripped", len(dead))


if __name__ == "__main__":
    try:
        if len(sys.argv) > 4 and sys.argv[3] == "--ids":
            main(sys.argv[1], sys.argv[2], set(), set(json.load(open(sys.argv[4]))))
        else:
            main(sys.argv[1], sys.argv[2], set(sys.argv[3:]))
    except BaseException:
        import traceback
        traceback.print_exc()
        kipy_exit.done(1)
    kipy_exit.done(0)
