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
    "krt":    {"dir": "...", "python": "..."}     # KiCadRoutingTools location (else $KRT_DIR / $KRT_PY)
  }

State (sessions, routed passes, extraction cache) lives in `$BADPCB_HOME` or `<project dir>/.badpcb/`, never in the package.

Environment variables: BADPCB_PROJECT, BADPCB_HOME, KIPY (KiCad's python, has pcbnew), KICAD_CLI, KRT_DIR, KRT_PY,
BADPCB_NO_CACHE, BADPCB_GSCORE_CELL, BADPCB_PROGRESS_S.
"""
import json
import os
import shutil
import sys

PROJECT_FILE = "badpcb.json"

_KIPY_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",   # macOS
    "/usr/lib/kicad/bin/python3", "/usr/bin/python3",                                                     # Linux (distro python has pcbnew)
    "C:/Program Files/KiCad/10.0/bin/python.exe", "C:/Program Files/KiCad/9.0/bin/python.exe",           # Windows
]
_CLI_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "C:/Program Files/KiCad/10.0/bin/kicad-cli.exe", "C:/Program Files/KiCad/9.0/bin/kicad-cli.exe",
]

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_ROOT = os.path.dirname(PACKAGE_DIR)          # the directory that must be on PYTHONPATH for `-m badpcb.<mod>`


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
    def routes_dir(self):
        return os.path.join(self.home, "routes")

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
def kipy():
    """KiCad's python (has pcbnew): $KIPY, else the first existing well-known location."""
    e = os.environ.get("KIPY")
    if e:
        return e
    for c in _KIPY_CANDIDATES:
        if os.path.exists(c):
            return c
    return _KIPY_CANDIDATES[0]


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


def kipy_available():
    return os.path.exists(kipy())


def kicad_cli_available():
    c = kicad_cli()
    return os.path.exists(c) or shutil.which(c) is not None


def kipy_run(args, **kw):
    """Run `$KIPY -m <module> ...` with the badpcb package importable (works from source or when installed)."""
    import subprocess
    env = dict(os.environ)
    env["PYTHONPATH"] = SOURCE_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if current().path:
        env["BADPCB_PROJECT"] = current().path
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run([kipy()] + list(args), env=env, **kw)


def use(data=None, root=None, path=None):
    """Install an in-memory project (tests, embedding): `data` is the badpcb.json dict, `root` the directory relative paths refer to."""
    global _current
    _current = Project(path, dict(data or {}))
    if root:
        _current.root = os.path.abspath(root)
    return _current
