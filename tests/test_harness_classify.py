"""Unit tests for the harness part-type classifier (pure depth-bin logic).

These exercise OnshapeClient._classify_from_depths with no network access, so
the heuristic that maps a part's parallel-face depths to a wizard setup is
regression-covered by `make test`.
"""
import unittest

from onshape_integration import OnshapeClient


class ClassifyFromDepthsTest(unittest.TestCase):

    def classify(self, depths, name=''):
        return OnshapeClient._classify_from_depths(depths, name_hint=name)

    def test_simple_plate_is_2d(self):
        # Top + bottom face only: a through-cut plate.
        r = self.classify([0.0, -0.25])
        self.assertEqual(r['part_type'], '2d')
        self.assertEqual(r['export_strategy'], 'onshape_standard_dxf')
        self.assertAlmostEqual(r['thickness_in'], 0.25)
        self.assertFalse(r['needs_review'])

    def test_pocketed_part_is_2p5d(self):
        # Top, a pocket floor, and the bottom: three distinct depths.
        r = self.classify([0.0, -0.1, -0.25])
        self.assertEqual(r['part_type'], '2.5d')
        self.assertEqual(r['export_strategy'], 'constructed_multilayer')
        self.assertAlmostEqual(r['thickness_in'], 0.25)
        self.assertFalse(r['needs_review'])

    def test_depth_order_does_not_matter(self):
        a = self.classify([0.0, -0.1, -0.25])
        b = self.classify([-0.25, 0.0, -0.1])
        self.assertEqual(a['part_type'], b['part_type'])
        self.assertAlmostEqual(a['thickness_in'], b['thickness_in'])

    def test_single_face_is_unknown(self):
        r = self.classify([0.0])
        self.assertEqual(r['part_type'], 'unknown')
        self.assertIsNone(r['thickness_in'])
        self.assertTrue(r['needs_review'])

    def test_no_faces_is_unknown(self):
        r = self.classify([])
        self.assertEqual(r['part_type'], 'unknown')
        self.assertTrue(r['needs_review'])

    def test_tube_by_geometry(self):
        # 1" tube, 1/16" walls: thin gap, big hollow, thin gap.
        r = self.classify([0.0, -0.0625, -0.9375, -1.0])
        self.assertEqual(r['part_type'], 'tube')
        self.assertEqual(r['export_strategy'], 'tube')
        self.assertAlmostEqual(r['thickness_in'], 0.0625)   # wall thickness
        self.assertAlmostEqual(r['tube_height_in'], 1.0)    # full section
        self.assertTrue(r['needs_review'])

    def test_tube_by_name_hint(self):
        # Geometry alone reads as a plate, but the name says tube -> flag it.
        r = self.classify([0.0, -0.125], name='2x1 Tube test')
        self.assertEqual(r['part_type'], 'tube')
        self.assertTrue(r['needs_review'])
        self.assertEqual(r['confidence'], 'low')

    def test_tube_geometry_and_name_is_high_confidence(self):
        r = self.classify([0.0, -0.0625, -0.9375, -1.0], name='Tube test')
        self.assertEqual(r['part_type'], 'tube')
        self.assertEqual(r['confidence'], 'high')

    def test_thick_walled_pocket_not_mistaken_for_tube(self):
        # Four levels but no thin walls / no dominant hollow: stays 2.5D.
        r = self.classify([0.0, -0.25, -0.5, -0.75])
        self.assertEqual(r['part_type'], '2.5d')

    def test_near_duplicate_depths_collapse(self):
        # Rounding to 4 decimals collapses numerically-equal faces.
        r = self.classify([0.0, -0.25000001, -0.24999998])
        self.assertEqual(r['part_type'], '2d')


if __name__ == '__main__':
    unittest.main()
