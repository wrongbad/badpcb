"""Lossless, lazy S-expression document for KiCad files (pure stdlib).

A `Doc` keeps the original text. Nodes only remember their source span and are expanded on demand, so reading a
5 MB board costs little. Edits are recorded as text patches against the original spans (`replace`, `delete`,
`insert_into`, ...) and applied by `render()`: everything that was not edited stays byte-identical, and an edited
node differs only in the tokens that changed. Formatting of new nodes follows KiCad's writer (`dumps`).
"""
import re

_TOK = re.compile(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+')
_SKIP = re.compile(r'"(?:[^"\\]|\\.)*"|[()]')
_SAFE = re.compile(r'^[^\s()"\\]+$')


def unquote(raw):
    if raw[:1] != '"':
        return raw
    s = raw[1:-1]
    if "\\" not in s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            i += 1
            c = s[i]
            c = {"n": "\n", "t": "\t", "r": "\r"}.get(c, c)
        out.append(c)
        i += 1
    return "".join(out)


def quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


class Q(str):
    """A string that is written quoted."""
    __slots__ = ()


def num(v):
    """KiCad number text: at most 6 decimals, trailing zeros trimmed."""
    if isinstance(v, int):
        return str(v)
    r = round(v, 6)
    if r == 0:
        return "0"
    s = "%.6f" % r
    return s.rstrip("0").rstrip(".")


class Atom:
    __slots__ = ("doc", "s", "e")

    def __init__(self, doc, s, e):
        self.doc, self.s, self.e = doc, s, e

    @property
    def raw(self):
        return self.doc.text[self.s:self.e]

    @property
    def quoted(self):
        return self.doc.text[self.s] == '"'

    @property
    def value(self):
        return unquote(self.doc.text[self.s:self.e])

    def __repr__(self):
        return "Atom(%s)" % self.raw


class Node:
    """A parenthesised list; `kids` (atoms and nodes) is parsed on first use."""
    __slots__ = ("doc", "s", "e", "_kids")

    def __init__(self, doc, s, e):
        self.doc, self.s, self.e, self._kids = doc, s, e, None

    @property
    def kids(self):
        k = self._kids
        if k is None:
            k = self._kids = self._expand()
        return k

    def _expand(self):
        doc = self.doc
        text = doc.text
        pos, end = self.s + 1, self.e - 1
        kids = []
        search = _TOK.search
        while True:
            m = search(text, pos, end)
            if m is None:
                break
            a = m.start()
            if text[a] == "(":
                depth = 1
                for m2 in _SKIP.finditer(text, m.end(), end + 1):
                    c = text[m2.start()]
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            break
                else:
                    raise ValueError("unbalanced parenthesis at %d" % a)
                kids.append(Node(doc, a, m2.end()))
                pos = m2.end()
            else:
                kids.append(Atom(doc, a, m.end()))
                pos = m.end()
        return kids

    # ---- reading helpers
    @property
    def head(self):
        k = self.kids
        if k and isinstance(k[0], Atom):
            return k[0].raw
        return None

    def nodes(self, name=None):
        if name is None:
            return [k for k in self.kids if isinstance(k, Node)]
        return [k for k in self.kids if isinstance(k, Node) and k.head == name]

    def node(self, name):
        for k in self.kids:
            if isinstance(k, Node) and k.head == name:
                return k
        return None

    def atoms(self):
        return [k for k in self.kids if isinstance(k, Atom)]

    def vals(self):
        """Values of the atom children after the head (numbers as raw text, strings unquoted)."""
        return [k.value for k in self.kids[1:] if isinstance(k, Atom)]

    def floats(self):
        return [float(k.raw) for k in self.kids[1:] if isinstance(k, Atom)]

    def has_flag(self, name):
        """Bare atom `name` among the children, or a child `(name yes)`."""
        for k in self.kids:
            if isinstance(k, Atom):
                if k.raw == name:
                    return True
            elif k.head == name:
                v = k.vals()
                return not v or v[0] == "yes"
        return False

    @property
    def text(self):
        return self.doc.text[self.s:self.e]

    def __repr__(self):
        return "Node(%s @%d)" % (self.head, self.s)


class Doc:
    def __init__(self, text):
        self.text = text
        self._patches = []          # (start, end, seq, replacement, tag)
        self._seq = 0
        self.tag = None             # patches added while set carry this tag (see drop)
        m = _TOK.search(text)
        if m is None or text[m.start()] != "(":
            raise ValueError("not an S-expression file")
        depth = 0
        for m2 in _SKIP.finditer(text, m.start()):
            c = text[m2.start()]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
        else:
            raise ValueError("unbalanced parenthesis")
        self.root = Node(self, m.start(), m2.end())

    # ---- editing (patches against the original text)
    def _add(self, s, e, repl):
        self._seq += 1
        if e > s:                                    # a later edit of the same span supersedes the earlier one
            self._patches = [p for p in self._patches if not (p[0] == s and p[1] == e)]
        self._patches.append((s, e, self._seq, repl, self.tag))

    def drop(self, tag):
        """Forget every patch that was added under `tag`."""
        self._patches = [p for p in self._patches if p[4] != tag]

    @property
    def dirty(self):
        return bool(self._patches)

    def replace(self, item, text):
        self._add(item.s, item.e, text)

    def set_atom(self, atom, value):
        """Replace one atom with a number, bool, plain word or (for Q / str with quoted=True) quoted string."""
        self._add(atom.s, atom.e, fmt_atom(value))

    def _line_start(self, pos):
        nl = self.text.rfind("\n", 0, pos)
        if nl >= 0 and not self.text[nl + 1:pos].strip():
            return nl
        return None

    def indent_of(self, item):
        nl = self.text.rfind("\n", 0, item.s)
        pre = self.text[nl + 1:item.s]
        return pre if not pre.strip() else re.match(r"\s*", pre).group(0)

    def delete(self, item):
        ls = self._line_start(item.s)
        self._add(item.s if ls is None else ls, item.e, "")

    def insert_before(self, item, text):
        """New item on its own line before `item` (same indent)."""
        ls = self._line_start(item.s)
        if ls is None:
            self._add(item.s, item.s, text + " ")
        else:
            self._add(ls, ls, "\n" + self.indent_of(item) + text)

    def insert_after(self, item, text):
        ls = self._line_start(item.s)
        if ls is None:
            self._add(item.e, item.e, " " + text)
        else:
            self._add(item.e, item.e, "\n" + self.indent_of(item) + text)

    def insert_into(self, parent, text):
        """Append `text` as the last child of `parent`."""
        kids = parent.kids
        close = parent.e - 1
        if kids:
            last = kids[-1]
            pos = last.e
            multi = "\n" in self.text[parent.s:close]
            if multi:
                ind = self.indent_of(last) if self._line_start(last.s) is not None else self.indent_of(parent) + "\t"
                self._add(pos, pos, "\n" + ind + text)
            else:
                self._add(pos, pos, " " + text)
        else:
            self._add(close, close, " " + text)

    def render(self, start=0, end=None):
        text = self.text
        if end is None:
            end = len(text)
        out, cur = [], start
        for s, e, _, repl, _t in sorted(self._patches, key=lambda p: (p[0], 1 if p[1] > p[0] else 0, -p[1], p[2])):
            if s < start or e > end:
                continue
            if s < cur:
                if e <= cur:
                    continue                    # nested inside an already replaced range
                raise ValueError("overlapping patches at %d" % s)
            out.append(text[cur:s])
            out.append(repl)
            cur = e
        out.append(text[cur:end])
        return "".join(out)


def fmt_atom(v):
    if isinstance(v, Q):
        return quote(v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return num(v)
    if _SAFE.match(v):
        return v
    return quote(v)


def dumps(tree, indent=0):
    """Format a python tree in KiCad's style. `tree` is [head, child, ...]; lists are nodes, everything else atoms
    (use Q for quoted strings, float/int for numbers). Leading atoms stay on the head line, sub-lists go one per line;
    a list of only atoms is written inline; `pts` writes xy items four per line."""
    if not isinstance(tree, (list, tuple)):
        return fmt_atom(tree)
    head, rest = tree[0], tree[1:]
    if all(not isinstance(c, (list, tuple)) for c in rest):
        return "(" + " ".join(fmt_atom(x) for x in tree) + ")"
    pad = "\t" * (indent + 1)
    lead, subs = [], []
    for c in rest:
        if isinstance(c, (list, tuple)) or subs:
            subs.append(c)
        else:
            lead.append(c)
    first = "(" + " ".join(fmt_atom(x) for x in [head] + lead)
    if head == "pts" and all(isinstance(c, (list, tuple)) and c[0] == "xy" for c in subs):
        lines = []
        for i in range(0, len(subs), 4):
            lines.append(pad + " ".join(dumps(c, indent + 1) for c in subs[i:i + 4]))
        return first + "\n" + "\n".join(lines) + "\n" + "\t" * indent + ")"
    body = [pad + dumps(c, indent + 1) for c in subs]
    return first + "\n" + "\n".join(body) + "\n" + "\t" * indent + ")"
