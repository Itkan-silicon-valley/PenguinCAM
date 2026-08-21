"""Flask test-client coverage for POST /session/config (the upload flow's config source).

The anonymous upload flow lets a team fetch a YAML config client-side and POST it here to
override the built-in Team 6238 defaults. These tests cover the set/clear/reject behavior,
the session gate, and isolation from the Onshape config key."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frc_cam_gui_app import app


CONFIG_YAML = """
team:
  number: 4321
  name: "Test Robotics"
machine:
  name: "Custom Router"
  dimensions:
    x_max: 36.0
    y_max: 36.0
materials:
  plywood:
    feed_rate: 90.0
"""


class TestSessionConfigRoute(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        # Mirror the upload-flow gate: a verified (Turnstile-passed) session.
        with self.client.session_transaction() as sess:
            sess['app_verified'] = True

    def _post(self, payload):
        return self.client.post('/session/config', json=payload)

    def test_requires_verified_session(self):
        anon = app.test_client()  # no app_verified
        resp = anon.post('/session/config', json={'yaml': CONFIG_YAML})
        self.assertEqual(resp.status_code, 401)

    def test_set_valid_config(self):
        resp = self._post({'yaml': CONFIG_YAML, 'url': 'https://raw.githubusercontent.com/x/y/config.yaml'})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['team_name'], 'Test Robotics')
        self.assertEqual(body['team_number'], 4321)
        self.assertFalse(body['using_default_config'])
        self.assertEqual(body['config_url'], 'https://raw.githubusercontent.com/x/y/config.yaml')
        # It landed in the upload-specific session key, NOT the Onshape one.
        with self.client.session_transaction() as sess:
            self.assertIn('upload_config_data', sess)
            self.assertNotIn('team_config_data', sess)

    def test_config_drives_template_context(self):
        self._post({'yaml': CONFIG_YAML})
        body = self.client.post('/session/config', json={'yaml': CONFIG_YAML}).get_json()
        self.assertEqual(body['machine_x_max'], 36.0)

    def test_clear_config_reverts_to_defaults(self):
        self._post({'yaml': CONFIG_YAML})
        resp = self._post({'yaml': ''})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['cleared'])
        self.assertTrue(body['using_default_config'])
        self.assertEqual(body['team_number'], 6238)  # back to Popcorn Penguins defaults
        with self.client.session_transaction() as sess:
            self.assertNotIn('upload_config_data', sess)

    def test_reject_invalid_yaml(self):
        resp = self._post({'yaml': 'team:\n  name: "unterminated\n'})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['ok'])

    def test_reject_out_of_range_value(self):
        resp = self._post({'yaml': 'materials:\n  plywood:\n    feed_rate: 999999\n'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())

    def test_parens_sanitized_returns_warning(self):
        resp = self._post({'yaml': 'machine:\n  name: "Router (v2)"\n'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get('warnings'))


if __name__ == '__main__':
    unittest.main()
