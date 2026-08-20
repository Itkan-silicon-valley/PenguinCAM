"""Flask test-client coverage for the multi-part /process-job route."""
import io
import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf
from frc_cam_gui_app import app, normalize_material


class TestNormalizeMaterial(unittest.TestCase):
    """The shared material-id canonicalizer used by both /process and /process-job."""

    def test_special_cases_and_passthrough(self):
        self.assertEqual(normalize_material('aluminum_tube'), 'aluminum')   # UI-only id -> aluminum preset
        self.assertEqual(normalize_material('ALUMINUM_TUBE'), 'aluminum')   # case-insensitive
        self.assertEqual(normalize_material('polycarb'), 'polycarbonate')   # legacy alias
        self.assertEqual(normalize_material('aluminum'), 'aluminum')        # already canonical
        self.assertEqual(normalize_material('MyCustom'), 'MyCustom')        # custom passes through, case kept


def _square_dxf_bytes(size):
    """Return the bytes of a DXF containing one closed square of the given size."""
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    pts = [(0, 0), (size, 0), (size, size), (0, size), (0, 0)]
    msp.add_lwpolyline(pts, close=True)
    with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
        path = f.name
    try:
        doc.saveas(path)
        with open(path, 'rb') as fh:
            return fh.read()
    finally:
        os.remove(path)


class TestProcessJobRoute(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        # Compute endpoints now require a verified session (Turnstile bot-gate for the
        # upload flow). Tests run with Turnstile unconfigured; mark the session verified.
        with self.client.session_transaction() as sess:
            sess['app_verified'] = True

    def _post_job(self, parts, stock=None, files=None):
        stock = stock or {'width': 24, 'height': 24}
        job = {
            'material': 'plywood',
            'tool_diameter': 0.157,
            'thickness': 0.25,
            'tab_spacing': 6.0,
            'stock': stock,
            'name': 'testjob',
            'parts': parts,
        }
        data = {'job': json.dumps(job), 'timestamp': '2026-06-30 12:00:00'}
        for key, content in (files or {}).items():
            data[key] = (io.BytesIO(content), f'{key}.dxf')
        return self.client.post('/process-job', data=data,
                                content_type='multipart/form-data')

    def test_two_parts_succeeds(self):
        a, b = _square_dxf_bytes(4.0), _square_dxf_bytes(3.0)
        resp = self._post_job(
            parts=[
                {'file_index': 0, 'name': 'plate A', 'place_x': 0, 'place_y': 0, 'rotation': 0},
                {'file_index': 1, 'name': 'plate B', 'place_x': 6, 'place_y': 0, 'rotation': 0},
            ],
            files={'file_0': a, 'file_1': b},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(len(payload['parts']), 2)
        # Stock is the parts' combined bounding box (A 4x4 at x0, B 3x3 at x6 => 9x4).
        self.assertAlmostEqual(payload['stock']['width'], 9.0, places=2)
        self.assertAlmostEqual(payload['stock']['height'], 4.0, places=2)
        self.assertIn('MULTI-PART JOB', payload['gcode'])
        self.assertEqual(payload['gcode'].count('M30'), 1)
        # Part B's reported footprint is shifted right by its placement.
        self.assertAlmostEqual(payload['parts'][1]['bbox']['minX'], 6.0, places=2)

    def test_overlap_rejected(self):
        a, b = _square_dxf_bytes(4.0), _square_dxf_bytes(4.0)
        resp = self._post_job(
            parts=[
                {'file_index': 0, 'name': 'A', 'place_x': 0, 'place_y': 0, 'rotation': 0},
                {'file_index': 1, 'name': 'B', 'place_x': 1, 'place_y': 1, 'rotation': 0},
            ],
            files={'file_0': a, 'file_1': b},
        )
        self.assertEqual(resp.status_code, 400)
        payload = resp.get_json()
        self.assertFalse(payload['success'])
        self.assertTrue(any('overlap' in e['error'].lower() for e in payload['part_errors']))

    def test_exceeds_machine_rejected(self):
        # Two parts spread far apart -> combined bounding box (~204") exceeds any machine.
        a, b = _square_dxf_bytes(4.0), _square_dxf_bytes(4.0)
        resp = self._post_job(
            parts=[
                {'file_index': 0, 'name': 'A', 'place_x': 0, 'place_y': 0, 'rotation': 0},
                {'file_index': 1, 'name': 'B', 'place_x': 200, 'place_y': 0, 'rotation': 0},
            ],
            files={'file_0': a, 'file_1': b},
        )
        self.assertEqual(resp.status_code, 400)
        payload = resp.get_json()
        self.assertTrue(any('exceed' in e['error'].lower() for e in payload['part_errors']))

    def test_missing_job_spec(self):
        resp = self.client.post('/process-job', data={}, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 400)


class TestPartOutlineRoute(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['app_verified'] = True

    def _square_with_hole_bytes(self, size, hole_r):
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        pts = [(0, 0), (size, 0), (size, size), (0, size), (0, 0)]
        msp.add_lwpolyline(pts, close=True)
        msp.add_circle((size / 2, size / 2), hole_r)
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            path = f.name
        try:
            doc.saveas(path)
            with open(path, 'rb') as fh:
                return fh.read()
        finally:
            os.remove(path)

    def test_outline_and_dims(self):
        content = self._square_with_hole_bytes(4.0, 0.5)
        resp = self.client.post('/part-outline',
                                data={'file': (io.BytesIO(content), 'plate.dxf')},
                                content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['name'], 'plate')
        self.assertAlmostEqual(payload['width'], 4.0, places=2)
        self.assertAlmostEqual(payload['height'], 4.0, places=2)
        self.assertGreaterEqual(len(payload['outline']), 3)
        self.assertEqual(len(payload['holes']), 1)
        self.assertAlmostEqual(payload['holes'][0]['r'], 0.5, places=2)

    def test_rejects_non_dxf(self):
        resp = self.client.post('/part-outline',
                                data={'file': (io.BytesIO(b'nope'), 'x.txt')},
                                content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 400)


class TestSessionGate(unittest.TestCase):
    """The compute endpoints require a verified (or Onshape) session — the upload
    flow's Turnstile bot-gate. These run WITHOUT a pre-set session."""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_process_job_requires_session(self):
        resp = self.client.post('/process-job', data={}, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(resp.get_json().get('need_verification'))

    def test_part_outline_requires_session(self):
        resp = self.client.post('/part-outline', data={}, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 401)

    def test_process_requires_session(self):
        resp = self.client.post('/process', data={}, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 401)

    def test_verify_then_allowed(self):
        # Turnstile is unconfigured in tests, so /session/verify bypasses the captcha
        # check and mints the anonymous session.
        v = self.client.post('/session/verify', json={'token': None})
        self.assertEqual(v.status_code, 200)
        self.assertTrue(v.get_json()['ok'])
        # Gate now passes: /process-job reaches its own validation (400 missing job),
        # not the 401 session gate.
        resp = self.client.post('/process-job', data={}, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 400)


if __name__ == '__main__':
    unittest.main()
