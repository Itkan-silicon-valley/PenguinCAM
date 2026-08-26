"""Validate the feeds_speeds model against PenguinCAM's existing presets.

Goal (A) from the project: prove the derived model reproduces the hand-tuned presets
for the reference 4mm 1-flute tool, so we can trust it to extrapolate to other tools.

For each (machine, material) it runs ``calculate_feeds`` with the reference tool and a
profile (full-slot) operation, then compares to the preset feed/ramp/stepover/stepdown.
Exits non-zero if any compared value exceeds the tolerance, so it can gate changes.

Run:  uv run python validate_feeds_speeds.py
"""

import sys

from feeds_speeds import calculate_feeds, TOOL_PRESETS

# Tolerance (fraction) beyond which a delta is flagged as a failure.
TOLERANCE = 0.10

# Presets copied from team_config.py / static/docs/PenguinCAM-config-template.yaml.
# Each entry: machine key -> material key -> preset values for the 4mm 1F tool.
PRESETS = {
    'omio_x8': {
        'plywood':       {'feed_rate': 70.0, 'ramp_feed_rate': 45.0, 'stepover_percentage': 0.65, 'max_slotting_depth': 0.4,  'spindle_speed': 18000},
        'aluminum_6061': {'feed_rate': 50.0, 'ramp_feed_rate': 32.0, 'stepover_percentage': 0.25, 'max_slotting_depth': 0.2,  'spindle_speed': 18000},
        'polycarbonate': {'feed_rate': 70.0, 'ramp_feed_rate': 45.0, 'stepover_percentage': 0.55, 'max_slotting_depth': 0.25, 'spindle_speed': 18000},
    },
    'avid_pro2424': {
        'plywood':       {'feed_rate': 75.0, 'ramp_feed_rate': 50.0, 'stepover_percentage': 0.65, 'max_slotting_depth': 0.4,  'spindle_speed': 18000},
        'aluminum_6061': {'feed_rate': 55.0, 'ramp_feed_rate': 35.0, 'stepover_percentage': 0.25, 'max_slotting_depth': 0.2,  'spindle_speed': 18000},
        'polycarbonate': {'feed_rate': 75.0, 'ramp_feed_rate': 50.0, 'stepover_percentage': 0.55, 'max_slotting_depth': 0.25, 'spindle_speed': 18000},
    },
}

# What we compare: label -> (model result key, preset key).
COMPARISONS = [
    ('feed IPM',      'feed_xy',       'feed_rate'),
    ('ramp IPM',      'ramp_feed',     'ramp_feed_rate'),
    ('stepover %',    'stepover_percentage', 'stepover_percentage'),
    ('slot depth in', 'slot_stepdown', 'max_slotting_depth'),
    ('RPM',           'rpm',           'spindle_speed'),
]


def main():
    ref_tool = TOOL_PRESETS['4mm_1f']
    header = f"{'machine':<14}{'material':<15}{'metric':<15}{'model':>10}{'preset':>10}{'delta':>9}"
    print(header)
    print('-' * len(header))

    failures = 0
    for machine, materials in PRESETS.items():
        for material, preset in materials.items():
            result = calculate_feeds(machine, material, ref_tool, operation='profile')
            for label, model_key, preset_key in COMPARISONS:
                model_val = result[model_key]
                preset_val = preset[preset_key]
                if preset_val:
                    delta = (model_val - preset_val) / preset_val
                else:
                    delta = 0.0
                flag = ''
                if abs(delta) > TOLERANCE:
                    flag = '  <-- FAIL'
                    failures += 1
                print(f"{machine:<14}{material:<15}{label:<15}"
                      f"{model_val:>10.4g}{preset_val:>10.4g}{delta:>+8.1%}{flag}")
            print()

    if failures:
        print(f"FAILED: {failures} value(s) outside +/-{TOLERANCE:.0%} tolerance.")
        return 1
    print(f"OK: all values within +/-{TOLERANCE:.0%} of PenguinCAM presets.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
