"""Project configuration and environment: the only place that knows where things live.

badpcb is board-agnostic. Everything that is specific to one board lives in a per-board project file, `badpcb.json`, found in the
current directory (or a parent), via `--project PATH|DIR`, or via $BADPCB_PROJECT. Without one, badpcb runs with defaults
(fine-pitch parts auto-detected, no swap groups, no lock classes, state under ./.badpcb/).

badpcb.json (every key optional; relative paths are relative to the directory holding badpcb.json):

  {
    "board":  "my_board.kicad_pcb",               # default board (first of a list); info only, commands take explicit paths
    "rules":  {"project": "my_board.kicad_pro"},  # .kicad_pro (+ sibling .kicad_dru) DRC is graded against (default: the board's own)
    "mcu":    "U1",                               # main IC of interest (pin-swap groups, plan/audit default, grid choice)
    "fine_pitch_parts": ["U1"],                   # parts whose fan-out matters; default: auto (pad pitch < 0.65 mm, >= 16 pads)
    "swaps":  "swaps.json" | {inline dict},       # swap groups: {"mcu", "pin_names", "groups":[...]}  (see examples/key_board)
    "locks":  {"classes": {"trunk": {"file": "trunks.json", "protect": true, "trim": true},   # copper lock classes (none by default)
                           "ref":   {"file": "ref.json", "key": "copied"}},                   # plain class; "name": "file[:key]" is shorthand
               "trim":    {"nets": "^SIG\\\\d+$", "key_parts": "^U1\\\\d\\\\d$", "start_part": "U1", "band_depth": 5.0}},
    "audit":  {"decap_bypass_mm": 4.0},           # override ROLES / escape-zone thresholds (defaults: badpcb.audit.DEFAULTS)
    "planes": {"assume": ["GND", "+3V3"]},        # nets to treat as plane nets when the board has no zones yet
    "intent": {...} | "intent.json",              # soft design intent (blocks, near, apart, edge, clear; README "Design intent"); penalties only
    "krt":    {"dir": "...", "python": "..."},    # KiCadRoutingTools location (else $KRT_DIR / $KRT_PY)
    "work_dir": "build/work"                      # where intermediates go (default `<state dir>/work`, i.e. .badpcb/work/<kind>/<id>/)
  }

State (sessions, work files, extraction cache) lives in `$BADPCB_HOME` or `<project dir>/.badpcb/`, never in the package.
Intermediates (droute pass boards, strip/repair/fanout copies, apply plans/reports/DRC json, stub plans, logs) go under the nested work directory;
only the board a command was asked to write (and its .kicad_pro/.kicad_dru, swaps_applied.md) lands where the caller said.

Environment variables: BADPCB_PROJECT, BADPCB_HOME, KICAD_CLI (DRC / zone refill / fab only; badpcb reads and writes .kicad_pcb itself), KRT_DIR, KRT_PY,
BADPCB_NO_CACHE, BADPCB_GSCORE_CELL, BADPCB_PROGRESS_S.
"""
import copy
import json
import os
import re
import shutil
import sys

PROJECT_FILE = "badpcb.json"

_CLI_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "C:/Program Files/KiCad/10.0/bin/kicad-cli.exe", "C:/Program Files/KiCad/9.0/bin/kicad-cli.exe",
]

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


class Project:
    def __init__(self, path=None, data=None):
        self.path = path
        self.data = data or {}
        self.root = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()

    # ------------------------------------------------------------------ paths
    def rel(self, p):
        """Project-relative path -> absolute."""
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(self.root, p))

    def short(self, p):
        """Absolute path -> project-relative when inside the project (for portable files and compact messages)."""
        p = os.path.abspath(p)
        if not self.path:
            return p
        r = os.path.relpath(p, self.root)
        return p if r.startswith("..") else r

    @property
    def home(self):
        h = os.environ.get("BADPCB_HOME")
        return os.path.abspath(h) if h else os.path.join(self.root, ".badpcb")

    @property
    def sessions_dir(self):
        return os.path.join(self.home, "sessions")

    @property
    def work_root(self):
        """Nested work directory for every intermediate artifact (droute pass boards, rip/strip/fanout copies, apply plans and reports,
        stub plans, DRC json, KRT logs): `"work_dir"` in badpcb.json (relative to the project), else `<state dir>/work`."""
        w = self.data.get("work_dir")
        return self.rel(w) if w else os.path.join(self.home, "work")

    def work_dir(self, *parts, create=False):
        """`<work root>/<parts...>`, e.g. work_dir("droute", "s1"), work_dir("apply", "out"), work_dir("drc")."""
        d = os.path.join(self.work_root, *parts)
        if create:
            os.makedirs(d, exist_ok=True)
        return d

    @property
    def routes_dir(self):
        """droute pass chains: `<work root>/droute/<session or board name>/passN.kicad_pcb`."""
        return self.work_dir("droute")

    @property
    def cache_dir(self):
        return os.path.join(self.home, "cache")

    # ------------------------------------------------------------------ board settings
    @property
    def board(self):
        b = self.data.get("board")
        if isinstance(b, list):
            b = b[0] if b else None
        return self.rel(b) if b else None

    @property
    def rules_pro(self):
        r = self.data.get("rules")
        p = r.get("project") if isinstance(r, dict) else r
        return self.rel(p) if p else None

    def rules_pro_for(self, board_path=None):
        """.kicad_pro that grades `board_path`: the configured one, else the board's own sibling .kicad_pro (may not exist)."""
        if self.rules_pro:
            return self.rules_pro
        if board_path:
            return os.path.splitext(board_path)[0] + ".kicad_pro"
        return None

    @property
    def mcu(self):
        return self.data.get("mcu") or (self.swaps().get("mcu") or None)

    def fine_pitch_parts(self, board=None):
        """Configured refdes list, else auto-detected from `board` (None -> [])."""
        cfg = self.data.get("fine_pitch_parts")
        if cfg:
            return list(cfg)
        if board is None:
            return [self.mcu] if self.mcu else []
        return auto_fine_parts(board)

    def swaps(self):
        s = self.data.get("swaps")
        if isinstance(s, str):
            with open(self.rel(s)) as fh:
                s = json.load(fh)
        s = dict(s or {})
        s.setdefault("mcu", self.data.get("mcu") or "")
        s.setdefault("pin_names", {})
        s.setdefault("groups", [])
        return s

    def _lock_spec(self, spec):
        """-> (file, key or None, props dict). spec = 'file[:key]' or {"file": .., "key": .., "protect": bool, "trim": bool | {trim params}}."""
        if isinstance(spec, dict):
            f, _, k = str(spec.get("file", "")).partition(":")
            return f, spec.get("key") or k or None, spec
        f, _, k = str(spec).partition(":")
        return f, k or None, {}

    def _lock_specs(self):
        return (self.data.get("locks") or {}).get("classes") or {}

    def lock_classes(self):
        """{class name: (absolute json path, key or None)}. File 'x.json' = a JSON list of uuids, key = d[key]."""
        out = {}
        for name, spec in self._lock_specs().items():
            f, k, _ = self._lock_spec(spec)
            out[name] = (self.rel(f), k)
        return out

    def lock_props(self, name):
        """{'protect': bool, 'trim': bool} of a lock class (both False for the string shorthand and for unknown names)."""
        spec = self._lock_specs().get(name)
        p = self._lock_spec(spec)[2] if spec is not None else {}
        return {"protect": bool(p.get("protect")), "trim": bool(p.get("trim"))}

    def lock_trim(self, name=None):
        """Trim parameters (locks.trim), overridden by the class's own `trim` dict when it has one."""
        t = dict((self.data.get("locks") or {}).get("trim") or {})
        spec = self._lock_specs().get(name)
        if isinstance(spec, dict) and isinstance(spec.get("trim"), dict):
            t.update(spec["trim"])
        return t

    def audit(self):
        """Optional "audit" block: overrides for badpcb.audit.DEFAULTS."""
        return dict(self.data.get("audit") or {})

    def intent(self):
        """Normalised design intent: {id: entry} (see `parse_intent`), from the "intent" key (inline dict or path of a JSON file)."""
        if getattr(self, "_intent", None) is None:
            raw = self.data.get("intent")
            if isinstance(raw, str):
                with open(self.rel(raw)) as fh:
                    raw = json.load(fh)
            try:
                self._intent = parse_intent(raw or {}, self)
            except IntentError as e:
                sys.exit("badpcb: bad \"intent\" in badpcb.json: %s" % e)
        return {k: copy.deepcopy(v) for k, v in self._intent.items()}

    def plane_hints(self):
        return list((self.data.get("planes") or {}).get("assume") or [])

    # ------------------------------------------------------------------ external tools
    def krt_dir(self):
        c = (self.data.get("krt") or {}).get("dir")
        return os.environ.get("KRT_DIR") or (self.rel(c) if c else _default_krt("KiCadRoutingTools"))

    def krt_py(self):
        c = (self.data.get("krt") or {}).get("python")
        return os.environ.get("KRT_PY") or (self.rel(c) if c else _default_krt("python"))


def _default_krt(sub):
    """Documented default: ~/KiCadRoutingTools (+ ~/KiCadRoutingTools/.venv/bin/python)."""
    home = os.path.expanduser("~")
    if sub == "KiCadRoutingTools":
        return os.path.join(home, "KiCadRoutingTools")
    return os.path.join(home, "KiCadRoutingTools", ".venv", "bin", "python")


def auto_fine_parts(board, pitch=0.65, min_pads=16):
    """Refdes of parts that look like fine-pitch ICs: >= min_pads pads of one footprint with a nearest-neighbour pitch < `pitch`."""
    out = []
    for ref, p in board.parts.items():
        if len(p.pads) < min_pads:
            continue
        pts = [pd.pos for pd in p.pads]
        best = []
        for i, a in enumerate(pts):
            d = min(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for j, b in enumerate(pts) if j != i)
            best.append(d)
        best.sort()
        if best[len(best) // 2] < pitch - 1e-6:
            out.append(ref)
    return sorted(out)


# ----------------------------------------------------------------------------- discovery
_current = None


def find_project_file(start=None):
    env = os.environ.get("BADPCB_PROJECT")
    if env:
        return os.path.join(env, PROJECT_FILE) if os.path.isdir(env) else env
    d = os.path.abspath(start or os.getcwd())
    while True:
        f = os.path.join(d, PROJECT_FILE)
        if os.path.isfile(f):
            return f
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def load(path=None):
    """Load (and make current) the project config; `path` = badpcb.json or a directory holding it."""
    global _current
    if path and os.path.isdir(path):
        path = os.path.join(path, PROJECT_FILE)
    path = path or find_project_file()
    if path and not os.path.isfile(path):
        sys.exit("badpcb: project file not found: %s" % path)
    data = {}
    if path:
        with open(path) as fh:
            data = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    _current = Project(path, data)
    return _current


def current():
    return _current if _current is not None else load()


def set_project(path):
    return load(path)


# ----------------------------------------------------------------------------- external tools (env, then documented defaults)
def kicad_cli():
    """kicad-cli: $KICAD_CLI, else on PATH, else a well-known install location."""
    e = os.environ.get("KICAD_CLI")
    if e:
        return e
    w = shutil.which("kicad-cli")
    if w:
        return w
    for c in _CLI_CANDIDATES:
        if os.path.exists(c):
            return c
    return "kicad-cli"


def kicad_cli_available():
    c = kicad_cli()
    return os.path.exists(c) or shutil.which(c) is not None


def use(data=None, root=None, path=None):
    """Install an in-memory project (tests, embedding): `data` is the badpcb.json dict, `root` the directory relative paths refer to."""
    global _current
    _current = Project(path, dict(data or {}))
    if root:
        _current.root = os.path.abspath(root)
    return _current


# ----------------------------------------------------------------------------- design intent (soft: weights only, never vetoes)
INTENT_SIDES = ("front", "back", "left", "right", "any")
INTENT_WEIGHTS = {"block": 1.0, "near": 1.0, "apart": 1.0, "edge": 0.5, "clear": 2.0}   # defaults; every entry may carry "weight"
INTENT_EDGE_MM = 3.0                                                                    # default max_mm of an edge entry
INTENT_KINDS = ("block", "near", "apart", "edge", "clear")
_ID_RX = re.compile(r"^(near|apart|edge|clear)(\d+)$")


class IntentError(ValueError):
    pass


def _num(v, what):
    try:
        return float(v)
    except (TypeError, ValueError):
        raise IntentError("%s: expected a number, got %r" % (what, v))


def _sels(v, what):
    """'C1*, decaps:U1' | ['C1*', 'U2'] -> list of selector strings (refdes, glob, or role:ANCHOR)."""
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\s]+", v) if x]
    if not isinstance(v, (list, tuple)) or not v or not all(isinstance(x, str) and x for x in v):
        raise IntentError("%s: expected a refdes / glob / role selector or a list of them, got %r" % (what, v))
    return list(v)


def _entry_weight(d, kind):
    w = d.get("weight")
    return INTENT_WEIGHTS[kind] if w is None else _num(w, "weight")


def _pos_args(v, names, what):
    """list form [a, b, ...] or dict form -> dict of the named fields (+ weight)."""
    if isinstance(v, dict):
        return dict(v)
    if not isinstance(v, (list, tuple)):
        raise IntentError("%s: expected a list or an object, got %r" % (what, v))
    if len(v) > len(names) + 1:
        raise IntentError("%s: too many fields in %r (%s, weight)" % (what, list(v), ", ".join(names)))
    d = dict(zip(names, v))
    if len(v) == len(names) + 1:
        d["weight"] = v[-1]
    return d


def _need(d, k, what):
    if k not in d or d[k] in (None, ""):
        raise IntentError("%s: missing %s" % (what, k))
    return d[k]


def intent_entry(kind, v, project=None, refs=None, name=None):
    """One normalised entry (no id) of `kind` from its list / dict form. Raises IntentError."""
    what = kind
    if kind == "near" or kind == "apart":
        d = _pos_args(v, ("a", "b", "mm"), kind)
        e = {"kind": kind, "a": _sels(_need(d, "a", what), "%s.a" % kind), "b": str(_need(d, "b", what)), "mm": _num(_need(d, "mm", what), "%s.mm" % kind)}
        if kind == "apart":
            e["b"] = _sels(e["b"], "apart.b")
        elif len(e["b"].split(",")) > 1 or " " in e["b"].strip():
            raise IntentError("near: B must be one part or REF.PAD (got %r); use several near entries" % e["b"])
        if e["mm"] < 0:
            raise IntentError("%s: mm must be >= 0" % kind)
    elif kind == "edge":
        d = _pos_args(v, ("ref", "side", "max_mm"), kind)
        side = str(d.get("side") or "any").lower()
        if side not in INTENT_SIDES:
            raise IntentError("edge: side must be one of %s (got %r)" % ("|".join(INTENT_SIDES), side))
        mm = d.get("max_mm")
        e = {"kind": kind, "ref": _sels(_need(d, "ref", what), "edge.ref"), "side": side, "max_mm": INTENT_EDGE_MM if mm in (None, "") else _num(mm, "edge.max_mm")}
    elif kind == "clear":
        if not isinstance(v, dict):
            raise IntentError("clear: expected {\"zone\": ..., \"except\": [...]}, got %r" % (v,))
        d = v
        z = _need(d, "zone", what)
        if isinstance(z, str):
            m = re.match(r"^escape:([^:\s]+)(?::([NSEWnsew]))?$", z.strip())
            if m:
                z = "escape:%s%s" % (m.group(1), ":" + m.group(2).upper() if m.group(2) else "")
            else:
                z = [x for x in re.split(r"[,\s]+", z.strip()) if x]
        if isinstance(z, (list, tuple)):
            if len(z) != 4:
                raise IntentError("clear.zone: a box is x0,y0,x1,y1 (4 numbers), or \"escape:REF[:SIDE]\"")
            z = [_num(x, "clear.zone") for x in z]
            z = [min(z[0], z[2]), min(z[1], z[3]), max(z[0], z[2]), max(z[1], z[3])]
        e = {"kind": kind, "zone": z, "except": _sels(d["except"], "clear.except") if d.get("except") else []}
    elif kind == "block":
        if not isinstance(v, dict):
            raise IntentError("block: expected an object with parts, got %r" % (v,))
        d = v
        nm = name or d.get("name")
        if not nm or not isinstance(nm, str) or ":" in nm or " " in nm:
            raise IntentError("block: needs a name without spaces or ':' (got %r)" % (nm,))
        e = {"kind": kind, "name": nm, "parts": _sels(_need(d, "parts", "block %s" % nm), "block %s.parts" % nm),
             "max_span_mm": None if d.get("max_span_mm") in (None, "") else _num(d["max_span_mm"], "max_span_mm")}
        if d.get("anchor"):
            e["anchor"] = str(d["anchor"])
        e["ref"] = _block_ref(d.get("ref"), project, refs or {}, nm)
    else:
        raise IntentError("unknown intent kind '%s' (have %s)" % (kind, ", ".join(INTENT_KINDS)))
    e["weight"] = _entry_weight(d, kind)
    return e


def _block_ref(r, project, refs, nm):
    """block `ref`: None | name of a top-level "refs" entry | path of a .kicad_pcb | {pcb, anchor, map} | {anchor, pos: {...}} (captured)."""
    if r in (None, "", {}):
        return None
    if isinstance(r, str):
        if r in refs:
            r = refs[r]
        else:
            r = {"pcb": r}
    if not isinstance(r, dict):
        raise IntentError("block %s: ref must be a path, a name from \"refs\", or an object (got %r)" % (nm, r))
    out = {}
    if r.get("pcb"):
        out["pcb"] = project.rel(r["pcb"]) if project else str(r["pcb"])
    if r.get("anchor"):
        out["anchor"] = str(r["anchor"])
    if r.get("map"):
        if not isinstance(r["map"], dict):
            raise IntentError("block %s: ref.map is {part_on_this_board: part_in_reference}" % nm)
        out["map"] = {str(k): str(v) for k, v in r["map"].items()}
    if r.get("pos"):                                    # captured: {"pads": {num: [x, y]} of the anchor, "pos": {part: [x, y, rot]}}
        out["pos"] = {str(k): [float(x) for x in v] for k, v in r["pos"].items()}
        out["pads"] = {str(k): [float(x) for x in v] for k, v in (r.get("pads") or {}).items()}
    if not out.get("pcb") and not out.get("pos"):
        raise IntentError("block %s: ref needs a pcb path (or a captured pos); got %r" % (nm, r))
    return out


def _as_list(kind, v):
    if v in (None, [], {}):
        return []
    if kind == "clear":
        return [v] if isinstance(v, dict) else list(v)
    if isinstance(v, dict):
        return [v]
    if isinstance(v, (list, tuple)) and v and isinstance(v[0], (list, tuple, dict)):
        return list(v)
    return [v]


def intent_entries(raw, project=None):
    """The entries (no ids) described by an intent object: sections blocks/near/apart/edge/clear (plural lists) or one singular
    entry (`{"near": ["C5", "U1.5", 3]}`, `{"block": {"name": "vreg", "parts": [...]}}`). Raises IntentError."""
    if not isinstance(raw, dict):
        raise IntentError("intent: expected an object, got %r" % (raw,))
    refs = raw.get("refs") or {}
    out = []
    unknown = [k for k in raw if not k.startswith("_") and k not in ("blocks", "block", "near", "apart", "edge", "clear", "refs", "weight")]
    if unknown:
        raise IntentError("unknown intent key(s) %s (have blocks, near, apart, edge, clear, refs)" % ", ".join(unknown))
    bl = raw.get("blocks")
    if bl:
        if not isinstance(bl, dict):
            raise IntentError("blocks: expected {name: {parts: [...], max_span_mm, ref}}")
        for nm, spec in bl.items():
            out.append(intent_entry("block", spec, project, refs, name=nm))
    if raw.get("block"):
        out.append(intent_entry("block", raw["block"], project, refs))
    for kind in ("near", "apart", "edge", "clear"):
        for v in _as_list(kind, raw.get(kind)):
            e = intent_entry(kind, v, project)
            if raw.get("weight") is not None and len(_as_list(kind, raw.get(kind))) == 1 and not isinstance(v, dict) and len(v) <= 3:
                e["weight"] = _num(raw["weight"], "weight")        # {"near": [A, B, mm], "weight": 2}
            out.append(e)
    return out


def assign_ids(entries, taken=()):
    """Give each entry without an id the smallest unused `kindN` (blocks: `block:NAME`). Returns {id: entry} in order."""
    used = set(taken) | {e["id"] for e in entries if e.get("id")}
    out = {}
    for e in entries:
        if not e.get("id"):
            if e["kind"] == "block":
                e["id"] = "block:" + e["name"]
            else:
                n = 1
                while "%s%d" % (e["kind"], n) in used:
                    n += 1
                e["id"] = "%s%d" % (e["kind"], n)
            if e["id"] in used and e["kind"] == "block":
                raise IntentError("block '%s' exists (use intent rm block:%s first)" % (e["name"], e["name"]))
            used.add(e["id"])
        out[e["id"]] = e
    return out


def parse_intent(raw, project=None):
    """badpcb.json "intent" -> {id: entry}. Entry fields by kind (all carry `kind`, `id`, `weight`):
      block  name, parts [selectors], max_span_mm, anchor?, ref {pcb, anchor, map, pos, side}|None
      near   a [selectors], b "REF"|"REF.PAD", mm      apart  a, b [selectors], mm
      edge   ref [selectors], side, max_mm             clear  zone "escape:REF[:SIDE]"|[x0,y0,x1,y1], except [selectors]"""
    if isinstance(raw, dict) and not raw:
        return {}
    return assign_ids(intent_entries(raw, project))


def intent_to_obj(e):
    """Normalised entry -> the singular JSON form `intent add` accepts (round-trips through intent_entries)."""
    k = e["kind"]
    f = lambda v: int(v) if float(v) == int(v) else round(v, 3)
    w = [] if abs(e["weight"] - INTENT_WEIGHTS[k]) < 1e-9 else [f(e["weight"])]
    j = lambda L: L[0] if len(L) == 1 else ",".join(L)
    if k == "near":
        return {"near": [j(e["a"]), e["b"], f(e["mm"])] + w}
    if k == "apart":
        return {"apart": [j(e["a"]), j(e["b"]), f(e["mm"])] + w}
    if k == "edge":
        return {"edge": [j(e["ref"]), e["side"], f(e["max_mm"])] + w}
    if k == "clear":
        d = {"zone": e["zone"], "except": e["except"]}
        if w:
            d["weight"] = w[0]
        return {"clear": d}
    d = {"name": e["name"], "parts": e["parts"]}
    if e.get("max_span_mm") is not None:
        d["max_span_mm"] = e["max_span_mm"]
    if e.get("anchor"):
        d["anchor"] = e["anchor"]
    if e.get("ref"):
        d["ref"] = {kk: vv for kk, vv in e["ref"].items()}
    if w:
        d["weight"] = w[0]
    return {"block": d}
