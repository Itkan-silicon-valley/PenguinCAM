"""Feeds & speeds calculation core for FRC CNC routers.

This module is intentionally free of any web-framework or PenguinCAM dependency so
that the same model can later be imported directly by ``frc_cam_postprocessor.py``
(the v3 config path) as well as served by the standalone web calculator.

The core problem this addresses: PenguinCAM's config stores *fixed* feeds/speeds per
material, all tuned for a 4mm single-flute tool. Those numbers stop working when the
tool changes (diameter or flute count). Here we *derive* feeds/speeds from
machine + material + tool inputs so that varying the tool still yields sane numbers.

Model (all lengths in inches, feeds in IPM, RPM in rev/min)::

    chipload_target = chipload_ref * (D / D_ref) ** DIAMETER_EXPONENT * op_factor
    rpm             = clamp(material.preferred_rpm, machine.rpm_min, machine.rpm_max)
    feed_raw        = rpm * flutes * chipload_target * rigidity_factor
    feed            = min(feed_raw, machine.xy_feed_max)          # machine limit
    chipload_done   = feed / (rpm * flutes)                       # achieved chipload
    ramp_feed       = feed * ramp_multiplier
    peck_feed       = feed * plunge_multiplier
    stepover        = stepover_ratio * D
    slot_stepdown   = slot_stepdown_ratio * D

``op_factor`` is the material ``slotting_multiplier`` for full-engagement slot/profile
cuts (what the PenguinCAM presets represent) and 1.0 for pocket/clearing, where the
lower radial engagement lets the tool run at the full reference chipload.

Constants are seeded from published tooling references and validated against the
existing PenguinCAM presets by ``validate_feeds_speeds.py`` (they land within ~10%).
"""

# --- Reference tool the material chipload constants are quoted for --------------
REFERENCE_TOOL = {'diameter': 0.157, 'flutes': 1}   # 4mm single-flute

# How chipload scales with tool diameter (from docs/FEEDSandSPEEDS.md).
DIAMETER_EXPONENT = 0.70

# Machine rigidity multiplies feed: a stiffer machine can push a bigger chip.
RIGIDITY_FACTOR = {'light': 0.85, 'medium': 1.00, 'heavy': 1.10}

# Operations that engage the tool at full width (slotting). Others (pocket/clearing)
# run at lower radial engagement and can use the full reference chipload.
FULL_SLOT_OPERATIONS = {'profile', 'slot'}


MACHINES = {
    'omio_x8': {
        'name': 'Omio X8-2200',
        'rpm_min': 6000, 'rpm_max': 24000,
        'xy_feed_max': 150.0, 'z_feed_max': 60.0,
        'rigidity': 'medium',
    },
    'avid_pro2424': {
        'name': 'Avid CNC Pro2424',
        'rpm_min': 6000, 'rpm_max': 24000,
        'xy_feed_max': 400.0, 'z_feed_max': 100.0,
        'rigidity': 'heavy',
    },
    'generic_light_router': {
        'name': 'Generic light router',
        'rpm_min': 8000, 'rpm_max': 30000,
        'xy_feed_max': 100.0, 'z_feed_max': 40.0,
        'rigidity': 'light',
    },
}


# chipload_ref values are in/tooth for the REFERENCE_TOOL (4mm 1F). slotting_multiplier
# derates them for full-width slotting; the product is what reproduces the presets.
MATERIALS = {
    'plywood': {
        'name': 'Plywood',
        'preferred_rpm': 18000,
        'chipload_ref': 0.0050, 'chipload_min': 0.0020, 'chipload_max': 0.0090,
        'slotting_multiplier': 0.80,
        'ramp_multiplier': 0.64, 'plunge_multiplier': 0.46,
        'stepover_ratio': 0.65, 'slot_stepdown_ratio': 2.55,
        'max_flutes_soft': 2,
    },
    'polycarbonate': {
        'name': 'Polycarbonate',
        'preferred_rpm': 18000,
        'chipload_ref': 0.0050, 'chipload_min': 0.0025, 'chipload_max': 0.0090,
        'slotting_multiplier': 0.80,
        'ramp_multiplier': 0.64, 'plunge_multiplier': 0.26,
        'stepover_ratio': 0.55, 'slot_stepdown_ratio': 1.59,
        'max_flutes_soft': 1,
    },
    'hdpe': {
        'name': 'HDPE',
        'preferred_rpm': 18000,
        'chipload_ref': 0.0060, 'chipload_min': 0.0030, 'chipload_max': 0.0110,
        'slotting_multiplier': 0.83,
        'ramp_multiplier': 0.64, 'plunge_multiplier': 0.30,
        'stepover_ratio': 0.55, 'slot_stepdown_ratio': 1.60,
        'max_flutes_soft': 1,
    },
    'srpp': {
        'name': 'SRPP (polypropylene composite)',
        'preferred_rpm': 18000,
        'chipload_ref': 0.0050, 'chipload_min': 0.0025, 'chipload_max': 0.0090,
        'slotting_multiplier': 0.80,
        'ramp_multiplier': 0.64, 'plunge_multiplier': 0.28,
        'stepover_ratio': 0.55, 'slot_stepdown_ratio': 1.59,
        'max_flutes_soft': 1,
    },
    'aluminum_6061': {
        'name': '6061 Aluminum',
        'preferred_rpm': 18000,
        'chipload_ref': 0.0032, 'chipload_min': 0.0015, 'chipload_max': 0.0050,
        'slotting_multiplier': 0.875,
        'ramp_multiplier': 0.64, 'plunge_multiplier': 0.28,
        'stepover_ratio': 0.25, 'slot_stepdown_ratio': 1.27,
        'max_flutes_soft': 3,
    },
}


TOOL_PRESETS = {
    '3mm_1f': {'name': '3mm 1-flute', 'diameter': 0.118, 'flutes': 1},
    '4mm_1f': {'name': '4mm 1-flute (default)', 'diameter': 0.157, 'flutes': 1},
    '125_1f': {'name': '1/8" 1-flute', 'diameter': 0.125, 'flutes': 1},
    '250_1f': {'name': '1/4" 1-flute', 'diameter': 0.250, 'flutes': 1},
    '250_2f': {'name': '1/4" 2-flute', 'diameter': 0.250, 'flutes': 2},
}


def _clamp(value, low, high):
    return max(low, min(high, value))


def _resolve(spec, presets, kind):
    """Resolve a machine/material argument to a dict.

    ``spec`` may be a preset key (str), or a dict. A dict may carry a ``preset`` key
    naming a base preset whose values are overlaid with the remaining keys (so the
    public 'custom' path can start from a preset and tweak a field or two).
    """
    if isinstance(spec, str):
        if spec not in presets:
            raise ValueError(f"Unknown {kind}: {spec!r}. Options: {sorted(presets)}")
        return dict(presets[spec])
    if isinstance(spec, dict):
        base = {}
        if spec.get('preset'):
            base = dict(presets.get(spec['preset'], {}))
        base.update({k: v for k, v in spec.items() if k != 'preset'})
        return base
    raise TypeError(f"{kind} must be a preset key or dict, got {type(spec).__name__}")


def calculate_feeds(machine, material, tool, operation='profile'):
    """Compute derived feeds & speeds.

    Args:
        machine: preset key (e.g. ``'omio_x8'``) or a dict of machine fields.
        material: preset key (e.g. ``'plywood'``) or a dict of material fields.
        tool: dict with ``diameter`` (inches) and ``flutes``.
        operation: one of ``profile``, ``slot``, ``pocket``, ``clearing``,
            ``peck_drill``.

    Returns a dict of results, warnings (list of str), a prose ``explanation`` and
    the ``formulas`` used, suitable for direct JSON serialization.
    """
    m = _resolve(machine, MACHINES, 'machine')
    mat = _resolve(material, MATERIALS, 'material')

    diameter = float(tool['diameter'])
    flutes = int(tool['flutes'])
    if diameter <= 0 or flutes <= 0:
        raise ValueError("tool diameter and flutes must be positive")

    d_ref = REFERENCE_TOOL['diameter']
    rigidity = m.get('rigidity', 'medium')
    rigidity_factor = RIGIDITY_FACTOR.get(rigidity, 1.0)

    warnings = []

    # RPM: material preference clamped to the machine's spindle range.
    rpm = _clamp(mat['preferred_rpm'], m['rpm_min'], m['rpm_max'])
    if rpm != mat['preferred_rpm']:
        warnings.append(
            f"RPM clamped to machine range: preferred {mat['preferred_rpm']:.0f} -> "
            f"{rpm:.0f} (machine allows {m['rpm_min']:.0f}-{m['rpm_max']:.0f})")

    # Chipload target: reference chipload scaled by diameter, derated for slotting.
    is_slot = operation in FULL_SLOT_OPERATIONS
    op_factor = mat['slotting_multiplier'] if is_slot else 1.0
    diameter_scale = (diameter / d_ref) ** DIAMETER_EXPONENT
    chipload_target = mat['chipload_ref'] * diameter_scale * op_factor

    # Feed from chipload, boosted by machine rigidity, then clamped to machine limit.
    feed_raw = rpm * flutes * chipload_target * rigidity_factor
    feed = min(feed_raw, m['xy_feed_max'])
    if feed_raw > m['xy_feed_max']:
        warnings.append(
            f"Feed clamped by machine limit: wanted {feed_raw:.1f} IPM, "
            f"machine max is {m['xy_feed_max']:.0f} IPM")

    chipload_achieved = feed / (rpm * flutes)

    # Achieved-chipload sanity checks (use the un-derated bounds; op_factor only
    # trims the target, the physical min/max are properties of the material/tool).
    if chipload_achieved < mat['chipload_min']:
        warnings.append(
            f"Achieved chipload {chipload_achieved:.4f} in/tooth is below the "
            f"recommended minimum {mat['chipload_min']:.4f} - risk of rubbing and heat. "
            f"Consider fewer flutes or lower RPM.")
    elif chipload_achieved > mat['chipload_max']:
        warnings.append(
            f"Achieved chipload {chipload_achieved:.4f} in/tooth exceeds the "
            f"recommended maximum {mat['chipload_max']:.4f} - risk of tool deflection "
            f"or breakage. Consider more flutes or higher RPM.")

    if flutes > mat['max_flutes_soft']:
        warnings.append(
            f"{flutes}-flute tool in {mat.get('name', 'this material')}: soft/gummy "
            f"materials evacuate chips poorly with high flute counts - the tool may "
            f"rub or pack. A 1- or 2-flute cutter is usually better.")

    ramp_feed = feed * mat['ramp_multiplier']
    peck_feed = feed * mat['plunge_multiplier']

    stepover = mat['stepover_ratio'] * diameter
    slot_stepdown = mat['slot_stepdown_ratio'] * diameter
    if mat['slot_stepdown_ratio'] > 3.0:
        warnings.append(
            f"Slotting stepdown of {slot_stepdown:.3f} in ({mat['slot_stepdown_ratio']:.1f}x "
            f"diameter) is aggressive - verify your tool's flute length and rigidity.")

    explanation = _build_explanation(
        m, mat, diameter, flutes, rpm, chipload_target, chipload_achieved,
        feed, ramp_feed, is_slot, op_factor, rigidity, rigidity_factor)

    formulas = [
        f"chipload_target = chipload_ref * (D / {d_ref:.3f})^{DIAMETER_EXPONENT}"
        + (" * slotting_multiplier" if is_slot else ""),
        "feed = RPM * flutes * chipload_target * rigidity_factor",
        "ramp_feed = feed * ramp_multiplier",
        "peck_feed = feed * plunge_multiplier",
        "stepover = stepover_ratio * D",
        "slot_stepdown = slot_stepdown_ratio * D",
    ]

    return {
        'rpm': round(rpm),
        'feed_xy': round(feed, 1),
        'ramp_feed': round(ramp_feed, 1),
        'peck_feed': round(peck_feed, 1),
        'stepover': round(stepover, 4),
        'stepover_percentage': mat['stepover_ratio'],
        'slot_stepdown': round(slot_stepdown, 4),
        'chipload_target': round(chipload_target, 5),
        'chipload_achieved': round(chipload_achieved, 5),
        'feed_clamped': feed_raw > m['xy_feed_max'],
        'operation': operation,
        'warnings': warnings,
        'explanation': explanation,
        'formulas': formulas,
    }


def _build_explanation(m, mat, diameter, flutes, rpm, chipload_target,
                       chipload_achieved, feed, ramp_feed, is_slot, op_factor,
                       rigidity, rigidity_factor):
    dia_mm = diameter * 25.4
    parts = [
        f"For a {dia_mm:.1f}mm ({diameter:.3f}\") {flutes}-flute tool in "
        f"{mat.get('name', 'this material')} at {rpm:.0f} RPM, the target chipload is "
        f"{chipload_target:.4f} in/tooth."
    ]
    if is_slot and op_factor != 1.0:
        parts.append(
            f"Because this is a slotting/profile cut, the {op_factor:.2f} slotting "
            f"multiplier is applied to the reference chipload.")
    parts.append(
        f"Feed = RPM x flutes x chipload = {rpm:.0f} x {flutes} x "
        f"{chipload_target:.4f} = {rpm * flutes * chipload_target:.1f} IPM"
        + (f", scaled by the {rigidity} machine's {rigidity_factor:.2f} rigidity factor"
           if rigidity_factor != 1.0 else "")
        + f", giving {feed:.1f} IPM.")
    if abs(chipload_achieved - chipload_target) > 1e-6:
        parts.append(
            f"After machine limits the achieved chipload is "
            f"{chipload_achieved:.4f} in/tooth.")
    parts.append(
        f"Ramp feed is {mat['ramp_multiplier']:.2f} x the XY feed, or {ramp_feed:.1f} IPM.")
    return " ".join(parts)


if __name__ == '__main__':
    import json
    demo = calculate_feeds('omio_x8', 'plywood', TOOL_PRESETS['4mm_1f'], 'profile')
    print(json.dumps(demo, indent=2))
