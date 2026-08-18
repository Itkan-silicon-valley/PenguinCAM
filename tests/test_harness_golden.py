"""Unit tests for the harness golden-master oracle (bless / check plumbing).

Self-contained: hand-writes a tiny G-code program, blesses it as a golden
heightmap, and verifies an identical program passes while one with an extra
interior cut (removing material the golden left) fails. No Onshape, no fixtures.
"""
import os
import shutil
import tempfile
import unittest

from harness_pipeline import bless_nc, check_nc

# A 1x1 square perimeter cut to Z=-0.02 through 0.25" stock.
BASE_NC = """(Material: Plywood - 0.25" thick)
(Tool: 0.125" diam Flat End Mill)
(  Material top: Z=0.2500")
G20
G0 X0.0 Y0.0 Z0.3000
G1 Z-0.0200 F10
G1 X1.0 Y0.0 F30
G1 X1.0 Y1.0
G1 X0.0 Y1.0
G1 X0.0 Y0.0
G0 Z0.3000
"""

# An extra plunge + cut in the center - material the golden leaves untouched.
EXTRA_CUT = """G0 X0.5 Y0.5 Z0.3000
G1 Z-0.0200 F10
G1 X0.7 Y0.5 F30
G0 Z0.3000
"""


class GoldenOracleTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, 'w') as fh:
            fh.write(text)
        return path

    def test_bless_then_check_passes(self):
        nc = self._write('part.nc', BASE_NC)
        rec = bless_nc(nc, self.dir)
        self.assertEqual(rec['status'], 'blessed', rec.get('error'))
        self.assertTrue(os.path.exists(rec['golden']))
        chk = check_nc(nc, self.dir)
        self.assertEqual(chk['status'], 'pass', chk)

    def test_extra_interior_cut_fails(self):
        nc = self._write('part.nc', BASE_NC)
        bless_nc(nc, self.dir)
        # Same part, but now also cuts the center -> over-cut vs the golden.
        with open(nc, 'w') as fh:
            fh.write(BASE_NC + EXTRA_CUT)
        chk = check_nc(nc, self.dir)
        self.assertEqual(chk['status'], 'fail', chk)
        self.assertGreater(chk['over_cut_cells'], 0)

    def test_check_without_golden_reports_no_golden(self):
        nc = self._write('never_blessed.nc', BASE_NC)
        chk = check_nc(nc, self.dir)
        self.assertEqual(chk['status'], 'no-golden')


if __name__ == '__main__':
    unittest.main()
