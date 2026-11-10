import unittest

from badpcb import geom


class GeomTest(unittest.TestCase):
    def test_seg_intersect(self):
        p = geom.seg_intersect((0, 0), (10, 10), (0, 10), (10, 0))
        self.assertAlmostEqual(p[0], 5)
        self.assertAlmostEqual(p[1], 5)
        self.assertIsNone(geom.seg_intersect((0, 0), (10, 0), (0, 1), (10, 1)))       # parallel
        self.assertIsNone(geom.seg_intersect((0, 0), (5, 5), (5, 5), (10, 0)))        # touching endpoint
        self.assertIsNone(geom.seg_intersect((0, 0), (5, 0), (6, -1), (6, 1)))        # miss

    def test_point_seg(self):
        self.assertAlmostEqual(geom.pt_seg_dist((5, 3), (0, 0), (10, 0)), 3)
        self.assertAlmostEqual(geom.pt_seg_dist((-4, 3), (0, 0), (10, 0)), 5)
        self.assertAlmostEqual(geom.seg_seg_dist((0, 0), (10, 0), (0, 2), (10, 2)), 2)

    def test_poly(self):
        sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
        self.assertTrue(geom.point_in_poly((5, 5), sq))
        self.assertFalse(geom.point_in_poly((15, 5), sq))
        self.assertAlmostEqual(geom.poly_area(sq), 100)

    def test_clip_rect_rotated(self):
        self.assertEqual(geom.clip_seg_rect((0, 0), (10, 0), (5, 0), (1, 1)), (0.4, 0.6))
        t = geom.clip_seg_rect((0, 0), (10, 0), (5, 0), (0.2, 3), 90)   # long axis along x after 90 deg
        self.assertAlmostEqual(t[0], 0.2)
        self.assertAlmostEqual(t[1], 0.8)
        self.assertIsNone(geom.clip_seg_rect((0, 5), (10, 5), (5, 0), (1, 1)))

    def test_circle(self):
        t = geom.seg_circle_interval((0, 0), (10, 0), (5, 0), 1)
        self.assertAlmostEqual(t[0], 0.4)
        self.assertAlmostEqual(t[1], 0.6)
        self.assertIsNone(geom.seg_circle_interval((0, 3), (10, 3), (5, 0), 1))

    def test_mst_and_inversions(self):
        pts = [(0, 0), (1, 0), (10, 0)]
        e = geom.mst(3, lambda i, j: abs(pts[i][0] - pts[j][0]))
        self.assertEqual(sorted(tuple(sorted(x)) for x in e), [(0, 1), (1, 2)])
        self.assertEqual(geom.inversions(list("abc"), list("cba")), [("a", "b"), ("a", "c"), ("b", "c")])
        self.assertEqual(geom.inversions(list("abc"), list("abc")), [])


if __name__ == "__main__":
    unittest.main()
