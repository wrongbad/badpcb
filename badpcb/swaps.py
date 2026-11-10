"""Swap freedoms (declared in the project config, badpcb.json "swaps"), legality checks, and the MCU pin-order view.

    cfg  = load_config()                       # badpcb.json swaps (inline or file)
    gs   = groups(board, cfg)                  # {'adc': Group, 'en': Group, 'rc': Group}
    check(board, cfg, 'adc', {'ADC0': '52', 'ADC1': '49', ...}) -> [] if legal else [errors]
    b2   = apply(board, cfg, 'adc', assignment)            # new Board with the MCU pad nets rewritten
    pin_cycle(board, mcu)                                  # pins in clockwise order with nets
    view = pin_vs_arrival(board, rn, topo, gs['en'])        # pin order vs where each net arrives from
    suggest(board, rn, gs['en'])                            # monotone pin assignment for the arrivals

Angles: atan2(dy, dx) in the board's +Y-down frame, i.e. increasing = clockwise on screen.
"Arrival" of a net = the far end of the first ratsnest connection leaving its MCU pad's island
(a lane end, a passive, a connector pad). The ordering inversions are between clockwise pin order
and clockwise arrival order, minimised over cyclic shifts.
"""
import copy
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Dict, List

from . import geom

from . import config


def load_config(path=None, board=None):
    """Swap config of the project (badpcb.json "swaps": inline dict or file; defaults: no groups, no pin names), or of `path`.
    With `board`: when the configured MCU is not on that board, the first auto-detected fine-pitch part stands in (groups dropped)."""
    if path:
        with open(path) as fh:
            cfg = json.load(fh)
        cfg.setdefault("mcu", "")
        cfg.setdefault("pin_names", {})
        cfg.setdefault("groups", [])
    else:
        cfg = config.current().swaps()
    if board is not None and cfg["mcu"] not in board.parts:
        fp = config.current().fine_pitch_parts(board)
        cfg = dict(cfg, mcu=fp[0] if fp else "", groups=[])
    return cfg


@dataclass
class Group:
    id: str
    kind: str
    desc: str
    nets: List[str]
    pins: List[str]          # legal pin numbers (for pin groups)
    current: Dict[str, str]  # net -> pin number now


def _pins_by_net(board, ref):
    out = {}
    for pd in board.parts[ref].pads:
        if pd.net:
            out.setdefault(pd.net, []).append(pd.num)
    return out


def groups(board, cfg) -> Dict[str, Group]:
    ref = cfg["mcu"]
    names = cfg["pin_names"]
    by_net = _pins_by_net(board, ref)
    out = {}
    for g in cfg["groups"]:
        # pins held by nets of *other* groups are not free for this one (its own nets' pins stay legal after a move)
        used = {pd.num for pd in board.parts[ref].pads if pd.net and pd.net not in g.get("nets", [])}
        pins = []
        if "pin_names" in g:
            want = set(g["pin_names"])
            pins = [n for n, nm in names.items() if nm in want]
        if g.get("also_free_pins_named"):
            rx = re.compile(g["also_free_pins_named"])
            pins += [n for n, nm in names.items() if rx.match(nm) and n not in used and n not in pins]
        pins = sorted(set(pins), key=int)
        cur = {n: by_net[n][0] for n in g.get("nets", []) if n in by_net}
        out[g["id"]] = Group(g["id"], g["kind"], g["desc"], g.get("nets", []), pins, cur)
    return out


def check(board, cfg, gid, assign):
    """Legality of a net->pin assignment for a pin group. Returns a list of error strings."""
    G = groups(board, cfg)[gid]
    if G.kind not in ("pin_permutation", "pin_assignment"):
        return ["group %s is not a pin group" % gid]
    err = []
    if set(assign) != set(G.nets):
        err.append("assignment must cover exactly %s" % ",".join(G.nets))
    pins = list(assign.values())
    if len(set(pins)) != len(pins):
        err.append("two nets on the same pin")
    for n, p in assign.items():
        if p not in G.pins:
            err.append("%s -> pin %s (%s) not allowed" % (n, p, cfg["pin_names"].get(p, "?")))
    if G.kind == "pin_permutation" and set(pins) != set(G.current.values()):
        err.append("permutation must use exactly the current pins %s" % ",".join(sorted(G.current.values(), key=int)))
    # pins that are not currently this group's must be unconnected
    ref = cfg["mcu"]
    occupied = {pd.num: pd.net for pd in board.parts[ref].pads if pd.net}
    for n, p in assign.items():
        if p in occupied and occupied[p] not in G.nets:
            err.append("pin %s already carries %s" % (p, occupied[p]))
    return err


def apply(board, cfg, gid, assign):
    """Board copy with MCU pad nets rewritten per `assign` (raises ValueError if illegal)."""
    err = check(board, cfg, gid, assign)
    if err:
        raise ValueError("; ".join(err))
    G = groups(board, cfg)[gid]
    b = copy.deepcopy(board)
    ref = cfg["mcu"]
    old = {p: n for n, p in G.current.items()}
    for pd in b.parts[ref].pads:
        if pd.num in old:
            pd.net = ""
    new = {p: n for n, p in assign.items()}
    for pd in b.parts[ref].pads:
        if pd.num in new:
            pd.net = new[pd.num]
    return b


def check_key_assignment(cfg, assign):
    """assign: {key_id: (region, en)}. Unique pairs, region sizes <= max, en < n_en, all keys present."""
    g = [x for x in cfg["groups"] if x["id"] == "keys"][0]
    err = []
    if len(assign) != g["n_keys"]:
        err.append("expected %d keys, got %d" % (g["n_keys"], len(assign)))
    pairs = list(assign.values())
    if len(set(pairs)) != len(pairs):
        err.append("duplicate (region, en) pair")
    sizes = {}
    for r, e in pairs:
        sizes[r] = sizes.get(r, 0) + 1
        if not (0 <= r < g["n_regions"] and 0 <= e < g["n_en"]):
            err.append("(%s,%s) out of range" % (r, e))
    err += ["region %s has %d keys (> %d)" % (r, n, g["max_region_size"]) for r, n in sorted(sizes.items()) if n > g["max_region_size"]]
    return err


# ----------------------------------------------------------------------------- pin order
def _ang(c, p):
    return math.atan2(p[1] - c[1], p[0] - c[0])


def pin_cycle(board, ref, cfg=None):
    """[(pin_num, name, net, angle)] for the signal pads of `ref`, clockwise on screen."""
    part = board.parts[ref]
    names = (cfg or {}).get("pin_names", {})
    seen, out = set(), []
    for pd in part.pads:
        if pd.num in seen or not pd.num or max(pd.size) > 2.0:   # skip exposed pad
            continue
        seen.add(pd.num)
        out.append((pd.num, names.get(pd.num, ""), pd.net, _ang(part.pos, pd.pos)))
    out.sort(key=lambda t: t[3])
    k = min(range(len(out)), key=lambda i: int(out[i][0]) if out[i][0].isdigit() else 10 ** 6) if out else 0
    return out[k:] + out[:k]      # start at the lowest pin number


def _side(part, p):
    a = math.degrees(_ang(part.pos, p))
    return "E" if -45 <= a < 45 else "S" if 45 <= a < 135 else "N" if -135 <= a < -45 else "W"


def arrivals(board, rn, ref, nets, reach=8.0):
    """net -> (far_point, label): where the net heads once it leaves the immediate neighbourhood of
    its `ref` pad. Walks the net's ratsnest tree from the pad's island, hopping (longest edge first)
    until an attach point is >= `reach` mm from the pad (so a decoupling cap right at the pin is
    passed through); the last far end is the arrival."""
    pos = {}
    for n in nets:
        isl = [I for I in rn.islands.get(n, []) if any(p.startswith(ref + ".") for p in I.pads)]
        if not isl:
            continue
        pad = [pd for pd in board.parts[ref].pads if pd.net == n][0].pos
        cur, seen, last = isl[0].idx, {isl[0].idx}, None
        while True:
            opts = []
            for c in rn.conns:
                if c.net != n:
                    continue
                if c.ia == cur and c.ib not in seen:
                    opts.append((c.ib, c.pb, c.b))
                elif c.ib == cur and c.ia not in seen:
                    opts.append((c.ia, c.pa, c.a))
            if not opts:
                break
            nxt = max(opts, key=lambda o: geom.dist(o[1], pad))
            seen.add(nxt[0])
            cur, last = nxt[0], (nxt[1], nxt[2])
            if geom.dist(nxt[1], pad) >= reach:
                break
        if last:
            pos[n] = last
    return pos


def pin_vs_arrival(board, rn, group, cfg, ref=None):
    ref = ref or cfg["mcu"]
    part = board.parts[ref]
    by_net = _pins_by_net(board, ref)
    pins = [(n, by_net[n][0]) for n in group.nets if n in by_net]
    pang = {n: _ang(part.pos, [pd for pd in part.pads if pd.num == p][0].pos) for n, p in pins}
    arr = arrivals(board, rn, ref, [n for n, _ in pins])
    aang = {n: _ang(part.pos, arr[n][0]) for n in arr}
    P = sorted(pang, key=lambda n: pang[n])
    A = sorted(aang, key=lambda n: aang[n])
    best = (10 ** 9, 0)
    for s in range(len(A) or 1):
        inv = len(geom.inversions(P, A[s:] + A[:s]))
        best = min(best, (inv, s))
    As = A[best[1]:] + A[:best[1]]
    pairs = geom.inversions(P, As)
    pin_side = {n: _side(part, [pd for pd in part.pads if pd.num == p][0].pos) for n, p in pins}
    arr_side = {n: _side(part, arr[n][0]) for n in arr}
    return {"group": group.id, "pin_order": [(n, dict(pins)[n], pin_side[n]) for n in P],
            "arrival_order": [(n, arr_side[n], arr[n][1]) for n in As], "inversions": best[0], "pairs": pairs,
            "over_package": [n for n in P if n in arr_side and arr_side[n] != pin_side[n]]}


def suggest(board, rn, group, cfg, ref=None):
    """Monotone (order-preserving) net->pin assignment over the group's legal pins that minimises the
    angular distance between each net's pin and its arrival direction. Returns dict with the
    assignment, nets that move, and the angular cost before/after."""
    ref = ref or cfg["mcu"]
    part = board.parts[ref]
    arr = arrivals(board, rn, ref, group.nets)
    nets = sorted(arr, key=lambda n: _ang(part.pos, arr[n][0]))
    pos = {pd.num: pd.pos for pd in part.pads}
    pins = sorted(group.pins, key=lambda p: _ang(part.pos, pos[p]))
    def d(n, p):
        x = abs(_ang(part.pos, arr[n][0]) - _ang(part.pos, pos[p])) % (2 * math.pi)
        return min(x, 2 * math.pi - x)
    best = (float("inf"), None)
    for s in range(len(nets)):
        N = nets[s:] + nets[:s]
        for t in range(len(pins)):
            Pp = pins[t:] + pins[:t]
            INF = float("inf")
            m = len(Pp)
            dp = [[INF] * (m + 1) for _ in range(len(N) + 1)]
            for j in range(m + 1):
                dp[0][j] = 0.0
            for i in range(1, len(N) + 1):
                for j in range(1, m + 1):
                    dp[i][j] = min(dp[i][j - 1], dp[i - 1][j - 1] + d(N[i - 1], Pp[j - 1]) + (0.002 if group.current.get(N[i - 1]) != Pp[j - 1] else 0.0))
            if dp[len(N)][m] < best[0]:
                # backtrack
                i, j, asg = len(N), m, {}
                while i > 0 and j > 0:
                    if dp[i][j] == dp[i][j - 1]:
                        j -= 1
                    else:
                        asg[N[i - 1]] = Pp[j - 1]
                        i -= 1
                        j -= 1
                best = (dp[len(N)][m], asg)
    asg = best[1] or {}
    if group.kind == "pin_permutation" and set(asg.values()) != set(group.current.values()):
        asg = {}
    cost_now = sum(d(n, p) for n, p in group.current.items() if n in arr)
    return {"assignment": asg, "moves": {n: (group.current.get(n), p) for n, p in asg.items() if group.current.get(n) != p},
            "cost_before_deg": round(math.degrees(cost_now), 0), "cost_after_deg": round(math.degrees(best[0]), 0)}
