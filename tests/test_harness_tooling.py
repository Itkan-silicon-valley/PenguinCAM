"""Unit tests for the harness tool-diameter heuristic (pure DXF reading).

Builds tiny in-memory DXFs and checks choose_tool_diameter against the rule:
4mm default, drop to the smallest hole when smaller, floored at 1/16".
"""
import os
import tempfile
import unittest

import ezdxf

from harness_pipeline import (
    choose_tool_diameter, smallest_hole_diameter,
    FALLBACK_TOOL_IN, MIN_TOOL_IN,
)


def _dxf_with_circles(radii):
    """Write a DXF containing CIRCLEs of the given radii; return its path."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    for i, r in enumerate(radii):
        msp.add_circle((i * 10, 0), radius=r)
    fd, path = tempfile.mkstemp(suffix='.dxf')
    os.close(fd)
    doc.saveas(path)
    return path


class ToolDiameterTest(unittest.TestCase):

    def setUp(self):
        self._paths = []

    def tearDown(self):
        for p in self._paths:
            if os.path.exists(p):
                os.remove(p)

    def _dxf(self, radii):
        p = _dxf_with_circles(radii)
        self._paths.append(p)
        return p

    def test_no_holes_uses_default(self):
        tool, note = choose_tool_diameter(self._dxf([]))
        self.assertAlmostEqual(tool, FALLBACK_TOOL_IN)
        self.assertIn('default', note)

    def test_all_holes_larger_than_4mm_uses_default(self):
        # 0.25" and 0.5" diameter holes (radii 0.125, 0.25): both > 4mm.
        tool, note = choose_tool_diameter(self._dxf([0.125, 0.25]))
        self.assertAlmostEqual(tool, FALLBACK_TOOL_IN)

    def test_small_hole_sizes_tool_to_it(self):
        # A 0.125" diameter hole (radius 0.0625) is < 4mm and >= floor.
        tool, note = choose_tool_diameter(self._dxf([0.0625, 0.25]))
        self.assertAlmostEqual(tool, 0.125)
        self.assertIn('sized tool to it', note)

    def test_tiny_hole_is_floored(self):
        # A 0.04" diameter hole (radius 0.02) is below the 1/16" floor.
        tool, note = choose_tool_diameter(self._dxf([0.02]))
        self.assertAlmostEqual(tool, MIN_TOOL_IN)
        self.assertIn('floor', note)

    def test_smallest_of_many_wins(self):
        # Outer profile circle (big) must not mask the real smallest hole.
        tool, _ = choose_tool_diameter(self._dxf([1.125, 0.05, 0.1]))
        self.assertAlmostEqual(tool, 0.1)   # 0.05 radius -> 0.1 diameter

    def test_smallest_hole_diameter_none_when_empty(self):
        self.assertIsNone(smallest_hole_diameter(self._dxf([])))


if __name__ == '__main__':
    unittest.main()
