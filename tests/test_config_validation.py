"""Tests for config_validation.validate_and_sanitize_config.

These cover the untrusted-input path used by the anonymous upload flow: size limits, YAML
safety, string sanitization (G-code comment safety), and numeric range checks."""

import unittest

from config_validation import (
    ConfigValidationError,
    MAX_CONFIG_BYTES,
    validate_and_sanitize_config,
)
from team_config import TeamConfig


class TestConfigValidation(unittest.TestCase):

    def test_valid_minimal_config(self):
        data, warnings = validate_and_sanitize_config('team:\n  number: 1234\n  name: "Test Team"\n')
        self.assertEqual(data['team']['number'], 1234)
        self.assertEqual(data['team']['name'], 'Test Team')
        self.assertEqual(warnings, [])
        # Sanity: the result is consumable by TeamConfig.
        self.assertEqual(TeamConfig(data).team_number, 1234)

    def test_empty_and_none_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('')
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config(None)

    def test_non_mapping_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('- just\n- a\n- list\n')
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('just a scalar')

    def test_oversized_rejected(self):
        huge = 'team:\n  name: "' + ('x' * (MAX_CONFIG_BYTES + 10)) + '"\n'
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config(huge)

    def test_malformed_yaml_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('team:\n  name: "unterminated\n')

    def test_unsupported_version_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('version: 99\nteam:\n  number: 1\n')

    def test_yaml_safe_load_blocks_object_construction(self):
        # A python object tag must not be instantiated; safe_load raises -> we reject.
        payload = "team: !!python/object/apply:os.system ['echo pwned']\n"
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config(payload)

    def test_parens_stripped_from_strings(self):
        data, warnings = validate_and_sanitize_config('machine:\n  name: "Router (v2) (fast)"\n')
        self.assertNotIn('(', data['machine']['name'])
        self.assertNotIn(')', data['machine']['name'])
        self.assertTrue(warnings)

    def test_unicode_stripped_from_strings(self):
        # Curly quote + arrow + degree sign must not survive into a G-code comment.
        data, warnings = validate_and_sanitize_config('machine:\n  name: "Cut “deep” → 90°"\n')
        self.assertTrue(all(ord(c) < 127 for c in data['machine']['name']))
        self.assertTrue(warnings)

    def test_clean_string_produces_no_warning(self):
        data, warnings = validate_and_sanitize_config('machine:\n  name: "Avid Pro4896"\n')
        self.assertEqual(data['machine']['name'], 'Avid Pro4896')
        self.assertEqual(warnings, [])

    def test_numeric_out_of_range_rejected(self):
        # Absurd feed rate.
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('materials:\n  plywood:\n    feed_rate: 999999\n')
        # Stepover fraction must be <= 1.
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('materials:\n  plywood:\n    stepover_percentage: 5\n')

    def test_numeric_non_finite_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('materials:\n  plywood:\n    feed_rate: .inf\n')
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('materials:\n  plywood:\n    feed_rate: .nan\n')

    def test_numeric_in_range_accepted(self):
        data, warnings = validate_and_sanitize_config('materials:\n  plywood:\n    feed_rate: 80\n')
        self.assertEqual(data['materials']['plywood']['feed_rate'], 80)

    def test_length_unit_string_accepted(self):
        data, _ = validate_and_sanitize_config('machine:\n  dimensions:\n    x_max: "600mm"\n')
        self.assertEqual(data['machine']['dimensions']['x_max'], '600mm')

    def test_length_unparseable_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('machine:\n  dimensions:\n    x_max: "not-a-length"\n')

    def test_length_absurd_magnitude_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('machine:\n  dimensions:\n    x_max: 999999\n')

    def test_negative_length_allowed(self):
        # park_position.z is legitimately negative.
        data, _ = validate_and_sanitize_config('machine:\n  park_position:\n    x: 0.5\n    y: 23.5\n    z: -0.5\n')
        self.assertEqual(data['machine']['park_position']['z'], -0.5)

    # ------------------------------------------------------------------
    # Lenient mode (strict=False) -- the Onshape background-render policy.
    # ------------------------------------------------------------------

    def test_lenient_drops_out_of_range_value(self):
        data, warnings = validate_and_sanitize_config(
            'materials:\n  plywood:\n    feed_rate: 999999\n    spindle_speed: 18000\n',
            strict=False)
        # Bad key dropped (falls back to default), good key kept.
        self.assertNotIn('feed_rate', data['materials']['plywood'])
        self.assertEqual(data['materials']['plywood']['spindle_speed'], 18000)
        self.assertTrue(warnings)

    def test_lenient_structural_failure_returns_none(self):
        data, warnings = validate_and_sanitize_config('- a\n- b\n', strict=False)
        self.assertIsNone(data)
        self.assertTrue(warnings)

    def test_lenient_parse_failure_returns_none(self):
        data, warnings = validate_and_sanitize_config('name: "unterminated\n', strict=False)
        self.assertIsNone(data)
        self.assertTrue(warnings)

    def test_lenient_still_sanitizes_strings(self):
        data, warnings = validate_and_sanitize_config(
            'machine:\n  name: "Router (v2)"\n', strict=False)
        self.assertNotIn('(', data['machine']['name'])
        self.assertTrue(warnings)

    def test_full_template_is_valid(self):
        # The shipped CONFIG_TEMPLATE must pass validation unchanged.
        from team_config import CONFIG_TEMPLATE
        data, warnings = validate_and_sanitize_config(CONFIG_TEMPLATE)
        self.assertEqual(warnings, [])
        self.assertIn('materials', data)


if __name__ == '__main__':
    unittest.main()
