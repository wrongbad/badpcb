"""S-expression layer: lossless round trip, lazy nodes, patch edits, KiCad-style formatting (pure python, no KiCad)."""
import os
import unittest

from badpcb import sexpr
from badpcb.sexpr import Atom, Doc, Node, Q

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def read(name):
    with open(os.path.join(FIX, name), encoding="utf-8", newline="") as fh:
        return fh.read()


class TestParse(unittest.TestCase):
    def test_atoms_and_nesting(self):
        d = Doc('(a 1 "two words" (b -0.5 "q\\"x") (c))')
        r = d.root
        self.assertEqual(r.head, "a")
        self.assertEqual([k.raw for k in r.atoms()], ["a", "1", '"two words"'])
        self.assertEqual(r.vals(), ["1", "two words"])
        self.assertEqual(r.node("b").vals(), ["-0.5", 'q"x'])
        self.assertEqual(r.node("c").kids[0].raw, "c")
        self.assertIsNone(r.node("nope"))

    def test_parens_inside_strings(self):
        d = Doc('(a "(not a list)" (b ")"))')
        self.assertEqual(d.root.vals(), ["(not a list)"])
        self.assertEqual(d.root.node("b").vals(), [")"])

    def test_flags(self):
        d = Doc("(fp locked (hide yes) (x no) (y))")
        self.assertTrue(d.root.has_flag("locked"))
        self.assertTrue(d.root.has_flag("hide"))
        self.assertFalse(d.root.has_flag("x"))
        self.assertTrue(d.root.has_flag("y"))

    def test_roundtrip_fixtures_byte_identical(self):
        for n in sorted(os.listdir(FIX)):
            if n.endswith(".kicad_pcb"):
                t = read(n)
                d = Doc(t)
                for k in d.root.kids:                     # expand everything: parsing must not change the text
                    if isinstance(k, Node):
                        k.kids
                self.assertEqual(d.render(), t, n)

    def test_unbalanced_raises(self):
        with self.assertRaises(ValueError):
            Doc("(a (b)").root.kids

    def test_quote_roundtrip(self):
        for s in ['plain', 'with "quote"', 'back\\slash', "line\nbreak"]:
            self.assertEqual(sexpr.unquote(sexpr.quote(s)), s)


class TestPatch(unittest.TestCase):
    SRC = '(r\n\t(a 1)\n\t(b\n\t\t(c "x")\n\t\t(d 2)\n\t)\n\t(e 3)\n)\n'

    def test_untouched_is_identical(self):
        self.assertEqual(Doc(self.SRC).render(), self.SRC)

    def test_set_atom_changes_only_that_token(self):
        d = Doc(self.SRC)
        d.set_atom(d.root.node("a").kids[1], 2.5)
        d.set_atom(d.root.node("b").node("c").kids[1], Q("y z"))
        self.assertEqual(d.render(), self.SRC.replace("(a 1)", "(a 2.5)").replace('(c "x")', '(c "y z")'))

    def test_delete_removes_whole_line(self):
        d = Doc(self.SRC)
        d.delete(d.root.node("b").node("c"))
        self.assertEqual(d.render(), self.SRC.replace('\t\t(c "x")\n', ""))

    def test_insert_after_before_into(self):
        d = Doc(self.SRC)
        b = d.root.node("b")
        d.insert_into(b, "(z 9)")
        d.insert_before(d.root.node("e"), "(pre 0)")
        d.insert_after(d.root.node("a"), "(post 0)")
        out = d.render()
        self.assertEqual(out, '(r\n\t(a 1)\n\t(post 0)\n\t(b\n\t\t(c "x")\n\t\t(d 2)\n\t\t(z 9)\n\t)\n\t(pre 0)\n\t(e 3)\n)\n')
        Doc(out).root.kids                                # still parses

    def test_insert_order_kept_at_same_position(self):
        d = Doc(self.SRC)
        e = d.root.node("e")
        d.insert_after(e, "(one 1)")
        d.insert_after(e, "(two 2)")
        self.assertIn("(e 3)\n\t(one 1)\n\t(two 2)\n)", d.render())

    def test_nested_patches_swallowed_by_delete(self):
        d = Doc(self.SRC)
        b = d.root.node("b")
        d.set_atom(b.node("d").kids[1], 7)
        d.delete(b)
        self.assertEqual(d.render(), '(r\n\t(a 1)\n\t(e 3)\n)\n')

    def test_render_span(self):
        d = Doc(self.SRC)
        b = d.root.node("b")
        d.set_atom(b.node("d").kids[1], 7)
        self.assertEqual(d.render(b.s, b.e), '(b\n\t\t(c "x")\n\t\t(d 7)\n\t)')


class TestDumps(unittest.TestCase):
    def test_inline_and_nested(self):
        self.assertEqual(sexpr.dumps(["at", 1.5, -2, 0.0]), "(at 1.5 -2 0)")
        self.assertEqual(sexpr.dumps(["net", Q("GND")]), '(net "GND")')
        t = sexpr.dumps(["via", ["at", 1, 2], ["layers", Q("F.Cu"), Q("B.Cu")]], 1)
        self.assertEqual(t, '(via\n\t\t(at 1 2)\n\t\t(layers "F.Cu" "B.Cu")\n\t)')

    def test_pts_four_per_line(self):
        t = sexpr.dumps(["pts"] + [["xy", i, i] for i in range(6)], 2)
        self.assertEqual(t, "(pts\n\t\t\t(xy 0 0) (xy 1 1) (xy 2 2) (xy 3 3)\n\t\t\t(xy 4 4) (xy 5 5)\n\t\t)")

    def test_number_format(self):
        self.assertEqual([sexpr.num(v) for v in (1.0, 0.1 + 0.2, 1e-9, -0.0, 12.3456789)], ["1", "0.3", "0", "0", "12.345679"])

    def test_dumps_parses_back(self):
        t = sexpr.dumps(["segment", ["start", 1, 2], ["end", 3.25, 4], ["layer", Q("F.Cu")], ["uuid", Q("x")]], 1)
        n = Doc(t).root
        self.assertEqual(n.node("end").floats(), [3.25, 4.0])


if __name__ == "__main__":
    unittest.main()
