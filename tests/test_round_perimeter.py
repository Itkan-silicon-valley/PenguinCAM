"""Regression tests for round-part perimeter detection.

A part whose OUTER boundary is a circle but which also has non-circular interior
features (a hex bore, slots, lightening cuts) used to fail: circles were only
considered as perimeter candidates when NO polylines were present, so the
circular perimeter was ignored and the largest interior polyline was mis-chosen
as the perimeter (then rejected as "too small"). See the HTD Pulley / Hex Hub
parts in the test corpus.
"""
import math
import unittest

from frc_cam_postprocessor import FRCPostProcessor


def _circle_pts(cx, cy, r, n=64):
    return [(cx + r * math.cos(2 * math.pi * i / n),
             cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


class RoundPerimeterTest(unittest.TestCase):

    def _pp(self):
        pp = FRCPostProcessor(0.25, 0.157, units='inch')
        pp.circles = []
        pp.polylines = []
        pp.layer_data = None
        return pp

    def test_circular_perimeter_with_interior_polyline(self):
        # Round OD (r=1.0) + a square interior pocket (polyline) + a small hole.
        pp = self._pp()
        pp.circles = [
            {'center': (0.0, 0.0), 'radius': 1.0, 'diameter': 2.0},   # OUTER perimeter
            {'center': (0.5, 0.0), 'radius': 0.05, 'diameter': 0.1},  # a bolt hole
        ]
        pp.polylines = [[(-0.3, -0.3), (0.3, -0.3), (0.3, 0.3), (-0.3, 0.3)]]  # interior pocket

        pp.identify_perimeter_and_pockets()

        # The circle must win as perimeter (no "too small" error).
        self.assertEqual(pp.errors, [])
        self.assertIsNotNone(pp.perimeter)
        # Interior polyline becomes a pocket; the small hole stays a hole.
        self.assertEqual(len(pp.pockets), 1)
        self.assertEqual(len(pp.circles), 1)
        self.assertAlmostEqual(pp.circles[0]['diameter'], 0.1)

    def test_rectangular_perimeter_still_wins_over_holes(self):
        # A normal plate: polyline perimeter must still be chosen over hole circles.
        pp = self._pp()
        pp.polylines = [[(0, 0), (10, 0), (10, 8), (0, 8)]]   # big rectangular perimeter
        pp.circles = [{'center': (5, 4), 'radius': 0.5, 'diameter': 1.0}]  # a hole

        pp.identify_perimeter_and_pockets()

        self.assertEqual(pp.errors, [])
        self.assertIsNotNone(pp.perimeter)
        self.assertEqual(len(pp.pockets), 0)          # the hole is not a pocket
        self.assertEqual(len(pp.circles), 1)          # hole retained

    def test_washer_two_concentric_circles(self):
        # No polylines: largest circle is the perimeter, inner circle stays a hole.
        pp = self._pp()
        pp.circles = [
            {'center': (0, 0), 'radius': 1.0, 'diameter': 2.0},   # outer
            {'center': (0, 0), 'radius': 0.3, 'diameter': 0.6},   # bore
        ]

        pp.identify_perimeter_and_pockets()

        self.assertEqual(pp.errors, [])
        self.assertIsNotNone(pp.perimeter)
        self.assertEqual(len(pp.pockets), 0)
        self.assertEqual(len(pp.circles), 1)
        self.assertAlmostEqual(pp.circles[0]['diameter'], 0.6)


if __name__ == '__main__':
    unittest.main()
