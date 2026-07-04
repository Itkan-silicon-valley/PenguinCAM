// FRC Router Feeds & Speeds Explainer - talks to the shared Python calc core.
'use strict';

// Which editable inputs map onto which fields of the machine/material dicts. The
// full preset dict is kept in state and these fields are overlaid from the inputs,
// so the backend receives a complete dict (including fields not exposed in the UI).
const MACHINE_FIELDS = [
    ['machine-xy_feed_max', 'xy_feed_max', 'number'],
    ['machine-rpm_min', 'rpm_min', 'number'],
    ['machine-rpm_max', 'rpm_max', 'number'],
    ['machine-rigidity', 'rigidity', 'string'],
];
const MATERIAL_FIELDS = [
    ['material-preferred_rpm', 'preferred_rpm', 'number'],
    ['material-chipload_ref', 'chipload_ref', 'number'],
    ['material-slotting_multiplier', 'slotting_multiplier', 'number'],
    ['material-stepover_ratio', 'stepover_ratio', 'number'],
];

const state = {
    presets: null,
    machine: null,   // full dict currently in effect
    material: null,
    lastResult: null,
};

const $ = (id) => document.getElementById(id);

async function init() {
    const resp = await fetch('/api/feeds-speeds/presets');
    state.presets = await resp.json();

    populateSelect('machine-preset', state.presets.machines);
    populateSelect('material-preset', state.presets.materials);
    populateSelect('tool-preset', state.presets.tools);

    // Load default presets into state + inputs.
    loadMachinePreset('omio_x8');
    loadMaterialPreset('plywood');
    loadToolPreset('4mm_1f');

    // Wire events.
    $('machine-preset').addEventListener('change', (e) => { loadMachinePreset(e.target.value); recalc(); });
    $('material-preset').addEventListener('change', (e) => { loadMaterialPreset(e.target.value); recalc(); });
    $('tool-preset').addEventListener('change', (e) => { loadToolPreset(e.target.value); recalc(); });

    MACHINE_FIELDS.forEach(([id]) => $(id).addEventListener('input', () => { syncMachine(); recalc(); }));
    MATERIAL_FIELDS.forEach(([id]) => $(id).addEventListener('input', () => { syncMaterial(); recalc(); }));
    ['tool-diameter', 'tool-flutes', 'operation'].forEach((id) =>
        $(id).addEventListener('input', recalc));

    document.querySelectorAll('.copy-buttons button').forEach((btn) =>
        btn.addEventListener('click', () => copyAs(btn)));

    recalc();
}

function populateSelect(id, items) {
    const sel = $(id);
    sel.innerHTML = '';
    Object.entries(items).forEach(([key, val]) => {
        const opt = document.createElement('option');
        opt.value = key;
        opt.textContent = val.name || key;
        sel.appendChild(opt);
    });
}

// --- Preset loading: copy preset dict into state, then reflect into inputs. -----
function loadMachinePreset(key) {
    state.machine = Object.assign({}, state.presets.machines[key]);
    $('machine-preset').value = key;
    MACHINE_FIELDS.forEach(([id, field]) => { $(id).value = state.machine[field]; });
}

function loadMaterialPreset(key) {
    state.material = Object.assign({}, state.presets.materials[key]);
    $('material-preset').value = key;
    MATERIAL_FIELDS.forEach(([id, field]) => { $(id).value = state.material[field]; });
}

function loadToolPreset(key) {
    const tool = state.presets.tools[key];
    $('tool-preset').value = key;
    $('tool-diameter').value = tool.diameter;
    $('tool-flutes').value = tool.flutes;
}

// --- Sync edited inputs back into the full state dicts. -------------------------
function syncMachine() {
    MACHINE_FIELDS.forEach(([id, field, type]) => {
        state.machine[field] = type === 'number' ? parseFloat($(id).value) : $(id).value;
    });
}

function syncMaterial() {
    MATERIAL_FIELDS.forEach(([id, field, type]) => {
        state.material[field] = type === 'number' ? parseFloat($(id).value) : $(id).value;
    });
}

let recalcTimer = null;
function recalc() {
    clearTimeout(recalcTimer);
    recalcTimer = setTimeout(doRecalc, 150);
}

async function doRecalc() {
    const payload = {
        machine: state.machine,
        material: state.material,
        tool: {
            diameter: parseFloat($('tool-diameter').value),
            flutes: parseInt($('tool-flutes').value, 10),
        },
        operation: $('operation').value,
    };

    let result;
    try {
        const resp = await fetch('/api/feeds-speeds', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        result = await resp.json();
    } catch (err) {
        result = { error: String(err) };
    }

    if (result.error) {
        $('explanation').textContent = 'Error: ' + result.error;
        return;
    }
    state.lastResult = result;
    render(result);
}

function render(r) {
    $('r-rpm').textContent = r.rpm;
    $('r-feed').textContent = r.feed_xy;
    $('r-ramp').textContent = r.ramp_feed;
    $('r-peck').textContent = r.peck_feed;
    $('r-stepover').textContent = r.stepover + ' (' + Math.round(r.stepover_percentage * 100) + '%)';
    $('r-stepdown').textContent = r.slot_stepdown;
    $('r-chipload').textContent = r.chipload_achieved;

    const warns = $('warnings');
    warns.innerHTML = '';
    r.warnings.forEach((w) => {
        const div = document.createElement('div');
        div.className = 'warning';
        div.textContent = w;
        warns.appendChild(div);
    });

    $('explanation').textContent = r.explanation;
    $('formulas').textContent = r.formulas.join('\n');
}

// --- Copy buttons --------------------------------------------------------------
function copyAs(btn) {
    const r = state.lastResult;
    if (!r) return;
    const kind = btn.dataset.copy;
    let text = '';
    if (kind === 'yaml') text = toYaml(r);
    else if (kind === 'json') text = JSON.stringify(r, null, 2);
    else if (kind === 'sheet') text = toSheet(r);

    navigator.clipboard.writeText(text).then(() => {
        const original = btn.textContent;
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(() => { btn.textContent = original; btn.classList.remove('copied'); }, 1500);
    });
}

function toYaml(r) {
    return [
        '# PenguinCAM material block (feeds/speeds)',
        'spindle_speed: ' + r.rpm,
        'feed_rate: ' + r.feed_xy,
        'ramp_feed_rate: ' + r.ramp_feed,
        'plunge_rate: ' + r.peck_feed,
        'stepover_percentage: ' + r.stepover_percentage,
        'max_slotting_depth: ' + r.slot_stepdown,
    ].join('\n');
}

function toSheet(r) {
    const machineName = state.machine.name || 'machine';
    const materialName = state.material.name || 'material';
    const dia = parseFloat($('tool-diameter').value);
    const flutes = parseInt($('tool-flutes').value, 10);
    return [
        'FRC Feeds & Speeds Setup Sheet',
        '================================',
        'Machine:   ' + machineName,
        'Material:  ' + materialName,
        'Tool:      ' + dia.toFixed(3) + '" ' + flutes + '-flute',
        'Operation: ' + r.operation,
        '',
        'Spindle RPM:          ' + r.rpm,
        'XY feed:              ' + r.feed_xy + ' IPM',
        'Ramp feed:            ' + r.ramp_feed + ' IPM',
        'Peck/plunge feed:     ' + r.peck_feed + ' IPM',
        'Stepover:             ' + r.stepover + ' in',
        'Max slotting stepdown:' + r.slot_stepdown + ' in',
        'Achieved chipload:    ' + r.chipload_achieved + ' in/tooth',
        '',
        r.warnings.length ? 'Warnings:' : 'No warnings.',
        ...r.warnings.map((w) => ' - ' + w),
        '',
        'Note: starting points only. Verify workholding, chip evacuation, and tool quality.',
    ].join('\n');
}

init();
