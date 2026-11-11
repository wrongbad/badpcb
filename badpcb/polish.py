"""Polish pass: rip and reroute the N ugliest nets of a fully routed board, one at a time, keeping a result only when it is better.

    badpcb droute BOARD --polish N [--time S]

The accept/reject logic lives here and is independent of KRT and kicad-cli (hooks are injected, tests mock them); `droute.polish_main`
wires the real ones. For every candidate group (a net, plus its pair partner for `pair` styles) taken from the lint ranking, worst first:

  1. reroute  : copy of the current board, the group's copper stripped, the group routed alone (every other net frozen), written to a candidate
  2. frozen   : no other net's copper may have changed
  3. unrouted : open connections must not increase
  4. lint     : the group's summed lint score must drop by at least `min_gain`
  5. DRC      : errors and unconnected items must not increase (kicad-cli, run last because it is the slow check)
  accepted -> the candidate becomes the current board; rejected -> dropped (the reason is logged). Time-boxed: the loop stops when the budget is used.
"""
import time

MIN_GAIN = 0.5


class Env:
    """Hooks the loop needs. lint(path) -> badpcb.lint.Lint; measure(path, drc) -> {"unrouted_conns", ["drc_errors", "drc_unconnected"]};
    reroute(path, group, out_path, cap_s) -> bool; frozen(path_before, path_after, group) -> bool."""

    def __init__(self, lint, measure, reroute, frozen):
        self.lint, self.measure, self.reroute, self.frozen = lint, measure, reroute, frozen


def groups(L, n, partner=None, min_score=0.0):
    """The n worst groups of the lint `L`: [(score, [nets])]. Open nets are skipped (polish needs a fully routed net); a pair is one group."""
    out, seen = [], set()
    for nl in L.ranked():
        if nl.net in seen or nl.open or nl.score <= min_score:
            continue
        g = [nl.net]
        p = partner(nl.net) if partner else None
        if p and p in L.nets and not L.nets[p].open and p not in seen:
            g.append(p)
        seen.update(g)
        out.append((sum(L.nets[x].score for x in g), g))
        if len(out) >= n:
            break
    return out


def _brief(nl):
    m = nl.m
    return "vias %d jogs %d acute %d c90 %d split %.1f detour %.2f" % (m["vias"], m["jogs"], m["acute"], m["c90"], m["split_mm"] + 4 * m["split_x"], m["detour"])


def polish(src, n, env, workdir, log=print, budget_s=600.0, cap_s=120.0, min_gain=MIN_GAIN, partner=None, min_score=0.0, clock=time.time):
    """-> {"board": final path, "steps": [...], "before": total, "after": total}; see module docstring."""
    import os
    t0 = clock()
    cur = src
    L = env.lint(cur)
    m_cur = env.measure(cur, True)
    total0 = L.total
    todo = groups(L, n, partner, min_score)
    log("POLISH start: %d nets scored, total ugliness %.1f, unrouted %d, drc_err %s unconnected %s; %d group(s) to try: %s" % (
        len(L.nets), L.total, m_cur["unrouted_conns"], m_cur.get("drc_errors", "?"), m_cur.get("drc_unconnected", "?"), len(todo),
        " ".join("+".join(g) for _, g in todo) or "-"))
    steps = []
    for i, (sbefore, g) in enumerate(todo):
        left = budget_s - (clock() - t0)
        if left < 20:
            log("POLISH time box (%.0fs) used: %d group(s) not tried" % (budget_s, len(todo) - i))
            break
        cap = max(20.0, min(cap_s, left / max(1, len(todo) - i)))
        cand = os.path.join(workdir, "polish%d.kicad_pcb" % (i + 1))
        st = {"nets": g, "before": sbefore, "after": None, "accepted": False, "why": "", "t": 0.0}
        t1 = clock()
        before_m = {x: dict(L.nets[x].m) for x in g}
        handle = L.nets[g[0]].handle
        try:
            ok = env.reroute(cur, g, cand, cap)
        except Exception as ex:                                  # a crashed KRT run is a rejected candidate, not a crashed polish
            ok, st["why"] = False, "reroute error: %s" % ex
        if not ok:
            st["why"] = st["why"] or "reroute produced no board (KRT failed or timed out)"
        elif not env.frozen(cur, cand, g):
            st["why"] = "other nets' copper changed"
        else:
            m1 = env.measure(cand, False)
            if m1["unrouted_conns"] > m_cur["unrouted_conns"]:
                st["why"] = "unrouted %d -> %d" % (m_cur["unrouted_conns"], m1["unrouted_conns"])
            else:
                L1 = env.lint(cand)
                miss = [x for x in g if x not in L1.nets]
                st["after"] = sum(L1.nets[x].score for x in g if x in L1.nets) if not miss else None
                if miss:
                    st["why"] = "net %s lost its copper" % ",".join(miss)
                elif st["after"] > sbefore - min_gain:
                    st["why"] = "lint %.1f -> %.1f (no gain)" % (sbefore, st["after"])
                else:
                    m2 = env.measure(cand, True)
                    if m2.get("drc_errors", 0) > m_cur.get("drc_errors", 0) or m2.get("drc_unconnected", 0) > m_cur.get("drc_unconnected", 0):
                        st["why"] = "DRC errors %s -> %s, unconnected %s -> %s" % (m_cur.get("drc_errors"), m2.get("drc_errors"),
                                                                                  m_cur.get("drc_unconnected"), m2.get("drc_unconnected"))
                    else:
                        st["accepted"] = True
                        st["detail_before"] = {x: _brief(L.nets[x]) for x in g}
                        st["detail_after"] = {x: _brief(L1.nets[x]) for x in g}
                        cur, L, m_cur = cand, L1, m2
        st["t"] = clock() - t1
        steps.append(st)
        log("POLISH #%d %-14s %-8s score %5.1f -> %s  %s  %.0fs" % (
            i + 1, "+".join(g), handle, sbefore, ("%5.1f" % st["after"]) if st["after"] is not None else "  n/a",
            ("ACCEPT  [%s -> %s]" % (st["detail_before"][g[0]], st["detail_after"][g[0]])) if st["accepted"] else "reject: " + st["why"], st["t"]))
    res = {"board": cur, "steps": steps, "before": total0, "after": L.total, "metrics": m_cur, "wall": clock() - t0}
    log("POLISH done: %d of %d accepted, total ugliness %.1f -> %.1f, unrouted %d, drc_err %s unconnected %s, wall %.0fs" % (
        sum(1 for s in steps if s["accepted"]), len(steps), total0, L.total, m_cur["unrouted_conns"], m_cur.get("drc_errors", "?"),
        m_cur.get("drc_unconnected", "?"), res["wall"]))
    return res
