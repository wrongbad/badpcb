"""Add plane-stub vias to a routed board WITHOUT touching signal copper: `python3 -m badpcb.stubs in.kicad_pcb out.kicad_pcb`.

For every plane-net island that no via serves (badpcb `plane_stubs`) the nearest legal via spot is searched (inside the net's plane zone,
clear of ALL existing copper, pads and holes, stub segment clear too) and a short stub + via is written. If no spot exists the
island is listed (the only case where a signal would have to move)."""
import json
import math
import os
import subprocess
import sys

from . import actions as A, config, cli, locks


def plan_stubs(board_path, r_max=2.5):
    b = cli.load_board(board_path)
    c = cli.Ctx(b)
    w = A.World(b)
    rl = b.rules
    P = {"session": "stubs", "base_board": board_path, "parts": [], "pads": [], "vias_add": [], "tracks_add": [], "vias_del": [], "tracks_del": [],
         "hints": {}, "unlock": [], "lock": [], "clearance": rl.clearance,
         "protect": locks.protected_ids(b)}
    failed = []
    for I in c.rn.stubs:
        pd = A._pad_by_id(b, I.pads[0])
        lay = A._stub_layer(w, pd)
        done = False
        for ri in range(5, int(r_max * 10) + 1):
            r = ri / 10.0
            for ang in range(0, 360, 10):
                pt = (round(pd.pos[0] + r * math.cos(math.radians(ang)), 2), round(pd.pos[1] + r * math.sin(math.radians(ang)), 2))
                if not A._inside_board(b, (pt[0] - .25, pt[1] - .25, pt[0] + .25, pt[1] + .25), rl.edge_clearance):
                    continue
                if not A._in_zone(b, I.net, pt):
                    continue
                if A._clearance_issues(w, I.net, pt, rl.via_dia / 2):
                    continue
                if A._clearance_issues(w, I.net, None, 0, layers=[lay], seg=(pd.pos, pt), w=0.15):
                    continue
                P["vias_add"].append({"pos": list(pt), "net": I.net, "dia": rl.via_dia, "drill": rl.via_drill, "layers": [b.layers[0], b.layers[-1]]})
                P["tracks_add"].append({"a": list(pd.pos), "b": list(pt), "w": 0.15, "layer": lay, "net": I.net})
                # make later searches see it
                from .model import Via, Track
                b.vias.append(Via(pt, I.net, rl.via_dia, rl.via_drill, False, [b.layers[0], b.layers[-1]]))
                b.tracks.append(Track(tuple(pd.pos), pt, 0.15, lay, I.net, False))
                done = True
                break
            if done:
                break
        if not done:
            failed.append("%s %s" % (I.net, "/".join(I.pads)))
    return P, failed


def main(src, dst):
    P, failed = plan_stubs(os.path.abspath(src))
    pj = dst.rsplit(".", 1)[0] + ".stubs.plan.json"
    rj = dst.rsplit(".", 1)[0] + ".stubs.rep.json"
    json.dump(P, open(pj, "w"))
    r = config.kipy_run(["-m", "badpcb.apply", "--write", pj, os.path.abspath(src), os.path.abspath(dst), rj], cwd=config.current().root)
    print("plane stubs: %d vias added, %d islands without a legal spot%s" % (len(P["vias_add"]), len(failed), (": " + "; ".join(failed)) if failed else ""))
    if not os.path.exists(dst):
        print(r.stderr[-1500:])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
