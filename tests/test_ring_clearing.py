"""Regression test for the circular-ring clearing gouge (Class A toolpath bug).

The ring spiral used to reposition to angle 0 with a straight G1 after each
Archimedean spiral, cutting a CHORD across the ring interior that gouged the
central island (found on the Turntable part: a 3.76" x 0.07"-deep slot across
solid keep-material). This asserts no cutting move in the ring path comes closer
to the ring center than the inner radius.
"""
import math
import unittest

from shapely.geometry import Point

from frc_cam_postprocessor import FRCPostProcessor
from gcode_sim import parse_moves


def _pt_seg_dist(px, py, ax, ay, bx, by):
    """Distance from point (px,py) to segment (ax,ay)-(bx,by)."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class RingClearingTest(unittest.TestCase):

    def test_ring_does_not_gouge_the_island(self):
        pp = FRCPostProcessor(0.25, 0.157, units='inch')
        pp.apply_material_preset('plywood')

        cx, cy = 5.0, 5.0
        outer_r, inner_r = 2.0, 1.0   # a wide ring -> big spirals -> big chord if buggy
        ring = Point(cx, cy).buffer(outer_r).difference(Point(cx, cy).buffer(inner_r))

        gcode = pp._generate_circular_ring_gcode(ring, ring, cx, cy, outer_r, inner_r)

        # Prefix a safe-Z rapid so the initial XY positioning move is above stock
        # (and thus excluded as non-cutting).
        text = "G20\nG0 Z1.0\n" + "\n".join(gcode)
        moves = parse_moves(text)

        mt = pp.material_top
        worst = inner_r
        for kind, x0, y0, z0, x1, y1, z1 in moves:
            if kind != 'feed':
                continue
            if z0 >= mt - 1e-9 and z1 >= mt - 1e-9:
                continue  # not cutting
            worst = min(worst, _pt_seg_dist(cx, cy, x0, y0, x1, y1))

        # No cutting move should dip meaningfully inside the inner wall.
        self.assertGreaterEqual(
            worst, inner_r - 0.02,
            f"ring cutting move gouged to r={worst:.4f} (inner wall r={inner_r})")

    def test_ring_still_clears_the_band(self):
        # Sanity: the ring path still reaches both walls (didn't get gutted).
        pp = FRCPostProcessor(0.25, 0.157, units='inch')
        pp.apply_material_preset('plywood')
        cx, cy = 5.0, 5.0
        outer_r, inner_r = 2.0, 1.0
        ring = Point(cx, cy).buffer(outer_r).difference(Point(cx, cy).buffer(inner_r))
        gcode = "\n".join(pp._generate_circular_ring_gcode(ring, ring, cx, cy, outer_r, inner_r))
        self.assertIn('Circular ring spiral clearing', gcode)
        self.assertIn('Outer cleanup circle', gcode)
        self.assertIn('Inner cleanup circle', gcode)


if __name__ == '__main__':
    unittest.main()
