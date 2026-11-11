"""Detailed routing through KiCadRoutingTools (KRT), translated into badpcb's vocabulary.

    python3 -m badpcb droute <session|board.kicad_pcb> [--nets N ...] [--component R ...] [--region R8|x0,y0,x1,y1]
                         [--preview] [--time S] [--out board] [--fresh] [--measure] [--extra "raw KRT route.py args"]

* a session is first written to a board (badpcb apply, cached in <badpcb home>/routes/<session>/applied.kicad_pcb);
  every pass then continues from <badpcb home>/routes/<session>/current.kicad_pcb, so small scoped passes chain naturally;
* a plain board is routed from itself, result written to --out (default <badpcb home>/routes/<name>/passN.kicad_pcb);
* the scope (nets / component / region) is handed to KRT's route.py (--nets / --component), signal layers only;
* KRT's log is parsed for failed nets and the nets that blocked them, the written board is re-extracted and
  measured with badpcb (unrouted conns, vias, length) and kicad-cli DRC; every still-unrouted connection of the scope
  is reported with region/cut handles (topo.py) and the blocking nets.
KRT runs with its own python ($KRT_PY, tool dir $KRT_DIR, see scripts/krt_env.sh); badpcb itself stays pure python3.
"""
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

from . import apply as AP
from . import config


def krt_dir():
    return config.current().krt_dir()


def krt_py():
    return config.current().krt_py()


def routes_dir():
    return config.current().routes_dir


def available():
    return os.path.exists(os.path.join(krt_dir(), "py_router", "route.py")) and os.path.exists(krt_py())


# --------------------------------------------------------------------------- measuring
def measure(board_path, with_drc=True, pro=None):
    """badpcb + kicad-cli view of a board file (the yardstick used for baseline, steered and per-pass numbers)."""
    from . import cli
    b = cli.load_board(board_path)
    c = cli.Ctx(b)
    m = c.T.metrics()
    unrouted_nets = sorted({x.net for x in c.rn.conns})
    plane_open = sorted(n for n, (s, u) in c.rn.plane_nets.items() if u)
    length = sum(((t.a[0] - t.b[0]) ** 2 + (t.a[1] - t.b[1]) ** 2) ** 0.5 for t in b.tracks)
    sig_nets = {pd.net for pd in b.pads() if pd.net}
    out = {"board": board_path, "nets": len(sig_nets), "unrouted_conns": m["unrouted"], "unrouted_nets": len(unrouted_nets),
           "unrouted_net_names": unrouted_nets, "plane_stub_pads": len(c.rn.stubs), "plane_nets_open": plane_open,
           "tracks": len(b.tracks), "vias": len(b.vias), "length_mm": round(length, 1)}
    if with_drc:
        base = os.path.join(routes_dir(), "drc")
        os.makedirs(base, exist_ok=True)
        d = AP.drc(board_path, os.path.join(base, os.path.basename(board_path) + ".json"), pro=pro or AP.default_rules_pro(board_path))
        s = AP.drc_summary(d)
        un = {}
        for u in d.get("unconnected_items", []):
            for it in u["items"][:1]:
                mm = re.search(r"\[([^\]]+)\]", it["description"])
                un[mm.group(1) if mm else "?"] = un.get(mm.group(1) if mm else "?", 0) + 1
        fine = config.current().fine_pitch_parts(b)
        nf = sum(1 for v in d["violations"] if v["severity"] == "error" and v["type"] != "drill_out_of_range"
                 and any(" of %s" % r in i["description"] for r in fine for i in v["items"]))
        out.update(fine_parts=fine, drc_fine_errors=nf, drc_errors=s["errors"], drc_by_type=s["by_type"], drc_unconnected=s["unconnected"],
                   drc_unconnected_nets=len(un), drc_unconnected_by_net=un)
    return out, c


def fmt_measure(m, label="DMETRICS"):
    s = "%s nets_unrouted=%d/%d unrouted_conns=%d vias=%d len=%.0fmm" % (label, m["unrouted_nets"], m["nets"], m["unrouted_conns"], m["vias"], m["length_mm"])
    if "drc_errors" in m:
        sig = {k: v for k, v in m["drc_by_type"].items() if k != "drill_out_of_range"}
        s += " drc_err=%d (excl. drill_range: %d, of which at %s: %d) unconnected=%d (%d nets)" % (
            m["drc_errors"], sum(sig.values()), ",".join(m.get("fine_parts") or []) or "fine-pitch parts", m.get("drc_fine_errors", 0), m["drc_unconnected"], m["drc_unconnected_nets"])
    return s


# --------------------------------------------------------------------------- scope
def region_nets(ctx, spec):
    T, b = ctx.T, ctx.board
    if spec in T.regions:
        bb = T.regions[spec].bbox
    else:
        try:
            bb = tuple(float(v) for v in spec.split(","))
            assert len(bb) == 4
        except Exception:
            raise SystemExit("unknown region '%s' (known: %s) or give x0,y0,x1,y1" % (spec, " ".join(T.regions)))
    plane = set(b.plane_nets())
    nets = {}
    for pd in b.pads():
        if pd.net and pd.net not in plane and bb[0] <= pd.pos[0] <= bb[2] and bb[1] <= pd.pos[1] <= bb[3]:
            nets[pd.net] = nets.get(pd.net, 0) + 1
    # only nets that still have an open connection with an end in the box
    open_nets = {x.net for x in ctx.rn.conns if any(bb[0] <= p[0] <= bb[2] and bb[1] <= p[1] <= bb[3] for p in (x.pa, x.pb))}
    return sorted(n for n in nets if n in open_nets), bb


def _glob(name):
    return name.replace("[", "[[]")


# --------------------------------------------------------------------------- KRT run + log parse
def progress_line(log_path):
    """Last '[n/m] Routing NET' / SUCCESS / FAILED line of the KRT log (what it is doing right now)."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 6000))
            tail = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return "no log yet"
    last = [l for l in tail if re.match(r"\[\d+/\d+", l) or "SUCCESS" in l or "FAILED" in l or "Escalat" in l or "Retry" in l]
    return (last[-1].strip() if last else (tail[-1].strip() if tail else "starting"))[:110]


def run_krt(src, dst, nets, comps, rules, time_cap, preview, extra, json_out, log_path, grid=None):
    cmd = [krt_py(), os.path.join(krt_dir(), "py_router", "route.py"), src, dst]
    if nets:
        cmd += ["--nets"] + [_glob(n) for n in nets]
    if comps:
        cmd += ["--component"] + comps
    cmd += ["--layers"] + rules["layers"] + ["--track-width", str(rules["w"]), "--clearance", str(rules["clr"]),
                                              "--via-size", str(rules["via"]), "--via-drill", str(rules["drill"]),
                                              "--json-out", json_out, "--no-bga-zones", "--escalation", "board"]
    if grid:
        cmd += ["--grid-step", str(grid)]
    if preview:
        cmd += ["--preview"]
    if extra:
        cmd += extra.split()
    t0 = time.time()
    with open(log_path, "w") as lf:
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=krt_dir(), start_new_session=True)
        timed_out = False
        every = float(os.environ.get("BADPCB_PROGRESS_S", "30"))
        while True:
            left = time_cap - (time.time() - t0)
            try:
                p.wait(timeout=max(0.1, min(every, left)))
                break
            except subprocess.TimeoutExpired:
                if time.time() - t0 >= time_cap:
                    timed_out = True
                    os.killpg(p.pid, signal.SIGKILL)       # whole process group: KRT forks workers
                    p.wait()
                    break
                print("  ... KRT %3.0fs/%ds: %s" % (time.time() - t0, time_cap, progress_line(log_path)), flush=True)
    return p.returncode, time.time() - t0, timed_out, cmd


def parse_log(text):
    """-> {'failed': {net: {'blockers': [(net, pct)], 'tries': n}}, 'routed': [...], 'summary': {...}}"""
    out = {"failed": {}, "routed": [], "summary": {}, "hints": []}
    cur = cur3 = None
    pend = {}
    in_block = False
    for ln in text.splitlines():
        m = re.match(r"\[(\d+)/(\d+)[^\]]*\] Routing (\S+)", ln)
        if m:
            cur, in_block = m.group(3), False
            continue
        if ln.strip().startswith("SUCCESS") and cur:
            out["routed"].append(cur)
        if "FAILED: Could not find route" in ln and cur:
            out["failed"].setdefault(cur, {"blockers": [], "tries": 0})["tries"] += 1
        if ln.strip().startswith("Top blockers:") and cur:
            in_block = True
            continue
        if in_block:
            mb = re.match(r"\s+\d+\. (\S+): (\d+) \(([\d.]+)%\)", ln)
            if mb and cur in out["failed"]:
                b = out["failed"][cur]["blockers"]
                if mb.group(1) not in [x[0] for x in b]:
                    b.append((mb.group(1), float(mb.group(3))))
            else:
                in_block = False
        mf = re.search(r"(\S+): Retry FAILED \(N=(\d+)\): still (\d+) failed edges", ln)
        if mf:
            out["failed"].setdefault(mf.group(1), {"blockers": [], "tries": 0})["retry"] = (int(mf.group(2)), int(mf.group(3)))
        mn = re.match(r"^(\S+) \(net \d+\):\s*$", ln)           # phase-3 per-net block of a multipoint net
        if mn:
            cur3 = mn.group(1)
        mnc = re.search(r"NOT CONNECTED \(so far\): (\S+) - (.*?) - may still", ln)
        if mnc:
            out["failed"].setdefault(mnc.group(1), {"blockers": [], "tries": 0})
            out["failed"][mnc.group(1)]["pads"] = out["failed"][mnc.group(1)].get("pads", []) + [mnc.group(2)]
            for nm in pend.pop(mnc.group(1), []):
                if nm not in [x[0] for x in out["failed"][mnc.group(1)]["blockers"]]:
                    out["failed"][mnc.group(1)]["blockers"].append((nm, 0.0))
        mpr = re.search(r"\(seam re-ask skipped: (\S+) is protected\)", ln)
        if mpr:
            out.setdefault("protected", set()).add(mpr.group(1))
        mp3 = re.search(r"Blocking obstacles: (.*)", ln)
        if mp3 and cur3:
            for nm in re.findall(r"([^\s,()]+)\(\d+ (?:track|via|pad)", mp3.group(1)):
                pend.setdefault(cur3, [])
                if nm != cur3 and nm not in pend[cur3]:
                    pend[cur3].append(nm)
        mp = re.search(r"Blocking obstacles: (.*)", ln)
        if mp and cur and cur in out["failed"]:
            for nm in re.findall(r"([^\s,()]+)\(\d+ (?:track|via|pad)", mp.group(1)):
                if nm not in [x[0] for x in out["failed"][cur]["blockers"]]:
                    out["failed"][cur]["blockers"].append((nm, 0.0))
        if "KiCad-locked copper" in ln or "PROTECTED net" in ln:
            out["hints"].append(ln.strip()[:240])
        if ln.startswith("JSON_SUMMARY_MIN:"):
            try:
                out["summary"] = json.loads(ln.split(":", 1)[1])
            except Exception:
                pass
    return out


# --------------------------------------------------------------------------- reporting in badpcb terms
def _lock_note(board, net):
    """'LOCKED lane x16' if the net has locked copper (KRT cannot rip it), '' otherwise."""
    from collections import Counter
    from . import locks
    if net in board.plane_nets():
        return ""                                     # plane nets always carry locked stubs/vias; they are never the wall
    c = Counter(locks.cls_of(t) for t in list(board.tracks) + list(board.vias) if t.net == net and t.locked)
    return ("[LOCKED %s]" % ",".join("%s x%d" % kv for kv in c.most_common(2))) if c else ""


def explain_open(ctx, scope_nets, parsed, limit=14):
    """Still-open connections of the scope with region/cut handles, the KRT blockers of their net (marked LOCKED when the
    blocking net owns locked copper) and the locked walls on the straight path."""
    from . import geom, locks
    T, rn, b = ctx.T, ctx.rn, ctx.board
    L = []
    scope = set(scope_nets) if scope_nets else None
    conns = [(i, c) for i, c in enumerate(rn.conns) if scope is None or c.net in scope]
    conns.sort(key=lambda ic: -ic[1].length)
    locked_seen = {}
    for i, c in conns[:limit]:
        ra, rb = T.region_at(c.pa).id, T.region_at(c.pb).id
        cuts = []
        for cid, cut in T.cuts.items():
            if i in T.cut_cross.get(cid, {}):
                nm = cut.label or "%s|%s" % (cut.a, cut.b)
                cuts.append("%s(%s dem %d/cap %d%s)" % (cid, nm, T.demand(cid), cut.cap_total, " OVERFLOW" if T.overflow(cid) else ""))
        bl = parsed["failed"].get(c.net, {}).get("blockers", [])
        L.append("  OPEN %-9s %s -> %s %5.1fmm  %s->%s  cuts: %s  blockers(KRT): %s" % (
            c.net, c.a, c.b, c.length, ra, rb, " ".join(cuts[:6]) or "none",
            " ".join("%s%s%s" % (n, " %.0f%%" % p if p else "", (" " + _lock_note(b, n)) if _lock_note(b, n) else "") for n, p in bl[:6]) or "-"))
        d = locks.describe_blockage(b, c)
        if d:
            L.append("       " + d)
            for n, _ in bl[:6]:
                if _lock_note(b, n):
                    locked_seen[n] = locked_seen.get(n, 0) + 1
    prot = sorted(n for n in (scope or ()) if n in parsed.get("protected", ()) or (n in parsed["failed"] and _lock_note(b, n)))
    if prot:
        L.append("  PROTECTED (net owns locked copper, so KRT never rips it nor re-routes it around other nets): %s -> `unlock <net>` / `unlock <class>`" %
                 " ".join("%s %s" % (n, _lock_note(b, n)) for n in prot[:10]))
    if locked_seen:
        L.append("  LOCKED blockers named by KRT: %s -> consider `unlock <net>` (or `unlock <class>`) in the session, then `badpcb apply`/droute again" %
                 " ".join("%s %s" % (n, _lock_note(b, n)) for n in sorted(locked_seen, key=lambda n: -locked_seen[n])[:8]))
    if len(conns) > limit:
        L.append("  ... %d more open connections in scope" % (len(conns) - limit))
    return L


# --------------------------------------------------------------------------- orchestration
def _pass_dir(name):
    d = os.path.join(routes_dir(), name)
    os.makedirs(d, exist_ok=True)
    return d


def main_args(a):
    if not available():
        raise SystemExit("KRT not installed: expected %s and %s (set $KRT_DIR / $KRT_PY, see scripts/krt_env.sh)" % (krt_dir(), krt_py()))
    os.makedirs(routes_dir(), exist_ok=True)
    from . import cli, state
    if a.measure_only:
        m, c = measure(a.target, pro=getattr(a, "rules_pro", None))
        print(fmt_measure(m))
        return
    is_board = a.target.endswith(".kicad_pcb") and os.path.exists(a.target)
    if not getattr(a, "rules_pro", None):        # grade against the ORIGINAL rules: the project's, else the input board's own (never a pass's lowered copy)
        a.rules_pro = AP.default_rules_pro(os.path.abspath(a.target) if is_board else state.State.load(a.target).board_path)
    if is_board:
        name = os.path.basename(a.target)[:-10]
        pd = _pass_dir(name)
        src = os.path.abspath(a.from_board or a.target)
    else:
        name = a.target
        pd = _pass_dir(name)
        applied = os.path.join(pd, "applied.kicad_pcb")
        cur = os.path.join(pd, "current.kicad_pcb")
        sp = state.session_path(name)
        meta = os.path.join(pd, "applied.meta.json")
        st_now = state.State.load(name)
        nlog = len(st_now.log)
        done_n = json.load(open(meta))["n"] if os.path.exists(meta) else None
        stale = a.fresh or not os.path.exists(applied) or os.path.getmtime(applied) < os.path.getmtime(sp)
        if stale and not a.fresh and done_n == nlog and os.path.exists(applied):
            stale = False                      # session file only re-saved (suggest/report store `last`); no new actions
        LOCKOPS = ("unlock", "lock", "trim", "add_tracks")
        if stale and not a.fresh and done_n is not None and nlog > done_n and os.path.exists(cur) and \
                all(d["op"] in LOCKOPS for e in st_now.log[done_n:] for d in e["acts"]):
            # only copper lock changes / trims since the last apply: patch the routed board instead of re-applying (keeps the passes)
            rep, nu, nl_, nt = AP.run_lock_delta(name, cur)
            print("session %s: %d new lock/trim step(s) applied to current.kicad_pcb in place (%d unlocked, %d locked, %d tracks trimmed); routed passes kept" % (
                name, nlog - done_n, nu, nl_, len([r for r in rep["removed"]]) if False else nt))
            json.dump({"n": nlog}, open(meta, "w"))
            os.utime(applied)
            stale = False
        if stale:
            print("applying session %s ..." % name)
            AP.run(name, applied, None, verify=False)
            json.dump({"n": nlog}, open(meta, "w"))
            if os.path.exists(cur):
                os.remove(cur)
        AP.copy_rules(applied, getattr(a, "rules_pro", None))
        src = os.path.abspath(a.from_board) if a.from_board else (cur if os.path.exists(cur) else applied)
    n_prev = max(len([f for f in os.listdir(pd) if re.match(r"pass\d+\.kicad_pcb$", f)]), len([f for f in os.listdir(pd) if re.match(r"pass\d+\.krt\.log$", f)]))
    k = n_prev + 1
    dst = os.path.abspath(a.out) if a.out else os.path.join(pd, "pass%d.kicad_pcb" % k)      # KRT runs with cwd=krt_dir()
    board = cli.load_board(src)
    ctx = cli.Ctx(board, rules_pro=getattr(a, "rules_pro", None))
    nets = []
    for pat in a.nets:                      # expand globs against the board's net names so the scope is explicit
        hit = [n for n in board.nets if fnmatch.fnmatchcase(n, pat)] or [pat]
        nets += [n for n in hit if n not in nets]
    note = []
    bb = None
    if a.region:
        rn_, bb = region_nets(ctx, a.region)
        nets += [n for n in rn_ if n not in nets]
        note.append("region %s box %.1f,%.1f..%.1f,%.1f -> %d nets with open connections" % (a.region, *bb, len(rn_)))
    if not nets and not a.component:
        nets = sorted({x.net for x in ctx.rn.conns})
        note.append("no scope given: all %d nets with open connections" % len(nets))
    sig = board.signal_layers()
    rl = board.rules
    if getattr(a, "repair", False):
        from . import repair as RP
        scope = set(nets) if (a.nets or a.region) else None
        rpl = RP.plan(board, ctx.rn.conns, a.radius, scope, board.plane_nets())
        print("  " + RP.describe(rpl, a.radius).replace("\n", "\n  "))
        if rpl["nets"]:
            nets = rpl["nets"]
        if rpl["ids"] and not a.preview:
            idf = os.path.join(pd, "pass%d.repair.ids.json" % k)
            json.dump(rpl["ids"], open(idf, "w"))
            rsrc = os.path.join(pd, "pass%d.repair.kicad_pcb" % k)
            from . import strip_nets
            strip_nets.strip(src, rsrc, ids=set(rpl["ids"]))
            if os.path.exists(rsrc):
                AP.copy_rules(rsrc, getattr(a, "rules_pro", None))
                src = rsrc
                board = cli.load_board(src)
                ctx = cli.Ctx(board, rules_pro=getattr(a, "rules_pro", None))
                m0 = ctx.T.metrics()
                print("  after the rip: unrouted conns %d" % m0["unrouted"])
        if rpl["ripped_nets"] and not a.fanout:
            from . import audit as AU
            fine = sorted({p.ref for p in board.parts.values() if any(q.net in rpl["ripped_nets"] for q in p.pads) and len(p.pads) >= 32})
            if fine:
                print("  note: ripped nets reach %s; their pin escapes are gone: add `--fanout %s` so they re-escape" % (", ".join(fine), fine[0]))
    rules = {"layers": sig, "w": rl.track_width, "clr": rl.clearance, "via": rl.via_dia, "drill": rl.via_drill}
    m0 = ctx.T.metrics()
    print("droute %s pass %d: %d nets%s%s, KRT on %s, cap %ds%s" % (
        name, k, len(nets), (" + component " + ",".join(a.component)) if a.component else "", "", "/".join(sig), a.time,
        "  PREVIEW (no board written)" if a.preview else ""))
    for n in note:
        print("  " + n)
    print("  before: unrouted conns %d, X=%d" % (m0["unrouted"], m0["crossings"]))
    if getattr(a, "strip", False):
        plane = set(board.plane_nets())
        ssrc = os.path.join(pd, "pass%d.strip.kicad_pcb" % k)
        P = {"tracks_del": [t for t in board.tracks if t.net in nets and t.net not in plane], "vias_del": [v for v in board.vias if v.net in nets and v.net not in plane]}
        from . import strip_nets
        strip_nets.strip(src, ssrc, nets=[n for n in nets if n not in plane])
        print("  strip: removed copper of %d scope nets (%d tracks, %d vias) so they escape together" % (len(nets), len(P["tracks_del"]), len(P["vias_del"])))
        if os.path.exists(ssrc):
            AP.copy_rules(ssrc, getattr(a, "rules_pro", None))
            src = ssrc
            board = cli.load_board(src)
            ctx = cli.Ctx(board, rules_pro=getattr(a, "rules_pro", None))
    if a.fanout:
        fsrc = os.path.join(pd, "pass%d.fanout.kicad_pcb" % k)
        fnets = [n for n in nets if any(p.startswith(a.fanout + ".") for x in ctx.rn.conns if x.net == n for p in (x.a, x.b))]
        if fnets:
            fclr, ftrk, fareas = ctx.eff.at(board, board.parts[a.fanout].bbox)      # local rule area (rules.py): the fan needs it
            fw = ftrk if fareas else rl.track_width
            fc = fclr if fareas else rl.clearance
            print("  fanout %s uses track %.2f / clearance %.2f%s" % (a.fanout, fw, fc, (" (rule area %s)" % ",".join(fareas)) if fareas else " (default rules)"))
            cmd = [krt_py(), os.path.join(krt_dir(), "py_router", "qfn_fanout.py"), src, "-c", a.fanout, "-o", fsrc, "--clearance", str(fc),
                   "--width", str(fw), "--escalation", "board", "--nets"] + [_glob(n) for n in fnets]
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=krt_dir())
            js = [ln for ln in r.stdout.splitlines() if ln.startswith("JSON_SUMMARY:")]
            info = json.loads(js[0].split(":", 1)[1]) if js else {}
            print("  fanout %s: %d/%d nets escaped%s" % (a.fanout, info.get("escaped", 0), info.get("requested", len(fnets)),
                                                      (", unescaped: " + " ".join(info.get("unescaped_nets", []))) if info.get("unescaped_nets") else ""))
            if os.path.exists(fsrc):
                AP.copy_rules(fsrc, getattr(a, "rules_pro", None))      # qfn_fanout lowers min track/clearance to its own 0.10 in the pro it writes -> KRT then
                src = fsrc                                              # "escalates" every failing net to 0.10 outside the rule area (found in Phase 5b)
    jo = os.path.join(pd, "pass%d.krt.json" % k)
    lg = os.path.join(pd, "pass%d.krt.log" % k)
    from . import feas
    eff = ctx.eff
    if a.grid not in (None, "auto"):
        grid, gwhy = float(a.grid), "--grid given"
    elif bb is not None:
        grid, gwhy = feas.grid_for_box(ctx, eff, bb)
    else:                                   # net/component scope: regions holding the open ends of the scope
        pts = [p for x in ctx.rn.conns if x.net in set(nets) for p in (x.pa, x.pb)]
        for ref in a.component or []:
            if ref in board.parts:
                pts.append(board.parts[ref].pos)
        if a.fanout and a.fanout in board.parts:
            pts.append(board.parts[a.fanout].pos)
        if pts:
            grid, gwhy = feas.grid_for_box(ctx, eff, (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))) \
                if len(pts) == 1 else min((feas.grid_for_box(ctx, eff, (p[0], p[1], p[0], p[1])) for p in pts), key=lambda t: t[0])
        else:
            grid, gwhy = 0.25, "no open ends"
    print("  grid %.3g mm (%s; badpcb feasibility; override with --grid)" % (grid, gwhy))
    rc, wall, timed_out, cmd = run_krt(src, dst, nets, a.component, rules, a.time, a.preview, a.extra, jo, lg, grid)
    text = open(lg, errors="replace").read()
    parsed = parse_log(text)
    s = parsed["summary"]
    print("  KRT: rc=%s wall %.0fs%s; routed %s failed %s vias %s (log %s)" % (
        rc, wall, " TIMED OUT (killed, no board written: narrow the scope or raise --time)" if timed_out else "",
        s.get("routed", len(parsed["routed"])), s.get("failed", len(parsed["failed"])), s.get("vias", "?"), config.current().short(lg)))
    if rc not in (0, 1) and not timed_out:
        print("  KRT ERROR (rc=%s), last log lines:\n    %s" % (rc, "\n    ".join(text.strip().splitlines()[-4:])))
    for h in parsed["hints"][:2]:
        print("  KRT hint: " + h)
    if timed_out or a.preview or not os.path.exists(dst):
        if parsed["failed"]:
            for net, f in list(parsed["failed"].items())[:12]:
                print("  FAILED %-9s blockers: %s" % (net, " ".join("%s %.0f%%" % x for x in f["blockers"][:6]) or "-"))
        return
    m, c2 = measure(dst, pro=getattr(a, "rules_pro", None))
    for line in explain_open(c2, nets, parsed):
        print(line)
    if is_board or a.out:
        AP.copy_rules(dst, getattr(a, "rules_pro", None))      # KRT lowered the rules in the pro it wrote; restore ours so the next pass starts from them
    if not is_board and not a.out:
        shutil.copyfile(dst, os.path.join(pd, "current.kicad_pcb"))
        AP.copy_rules(os.path.join(pd, "current.kicad_pcb"), getattr(a, "rules_pro", None))      # KRT lowers the rules in the .kicad_pro it writes; keep ours
    print("  wrote %s" % dst)
    print(fmt_measure(m, "METRICS(droute)") + " time=%.0fs" % wall)
    mode = "repair" if getattr(a, "repair", False) else "strip" if getattr(a, "strip", False) else "plain"
    try:
        recs = [json.loads(l) for l in open(os.path.join(pd, "passes.jsonl")) if l.strip()][-3:]
        hist = [r_["measure"]["unrouted_net_names"] for r_ in recs]
        cur = m["unrouted_net_names"]
        sets = [set(h) for h in hist] + [set(cur)]
        if len(sets) >= 3 and all(0 < len(x) <= 4 for x in sets[-3:]) and len({frozenset(x) for x in sets[-3:]}) > 1 and len(set().union(*sets[-3:])) <= 7:
            print("  WARNING ping-pong: the last %d passes each left 1-4 open nets but not the same ones (%s): the failure moves inside one group "
                  "(escape congestion at the same pins), re-running the same kind of pass will not converge." % (len(sets[-3:]), " | ".join(",".join(sorted(x)) for x in sets[-3:])))
            from . import repair as RP
            hh = [{"mode": r_.get("mode", "plain"), "names": r_["measure"]["unrouted_net_names"]} for r_ in recs] + [{"mode": mode, "names": cur}]
            print("  " + RP.strategy(hh, cur, a.fanout, name if not is_board else a.target))
            for ln_ in RP.escape_advice(name if not is_board else a.target, sorted(set().union(*sets[-3:])), None):
                print("  " + ln_)
    except Exception:
        pass
    rec = dict(measure=m, wall=wall, nets=len(nets), pass_=k, krt=s, mode=mode)
    with open(os.path.join(pd, "passes.jsonl"), "a") as fh:
        fh.write(json.dumps(rec, default=list) + "\n")
    return m
