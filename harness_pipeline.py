"""Phase 3 of the end-to-end test harness: run a manifest part through PenguinCAM.

For each manifest entry this:
  1. Exports a DXF the same way the live app does, dispatched on export_strategy
     (standard Onshape DXF for 2D, our constructed multilayer DXF for 2.5D, a
     flat face DXF for tube).
  2. Picks the tool diameter the way a user would: 4mm, unless the part has
     smaller holes, in which case the smallest hole's diameter (floored at 1/16"
     so we never "test" a bit no team owns).
  3. Runs the postprocessor exactly as the web path does (identify perimeter/
     pockets, then classify holes, then generate) and writes the .nc.

The DXF-reading tool-diameter logic is unit-tested; the rest is integration
glue around the real client + postprocessor.
"""

import contextlib
import io
import os
import re

from onshape_integration import build_multilayer_dxf
from frc_cam_postprocessor import FRCPostProcessor
from gcode_sim import (
    simulate, Heightmap, compare, render_heightmap_png, render_diff_png,
)

# 4mm in inches: the default endmill, matching the postprocessor CLI default.
FALLBACK_TOOL_IN = 0.15748
# Smallest realistic endmill (1/16"). A hole smaller than this can't be cut by
# any bit a team is likely to own, so we clamp rather than emit a fantasy tool.
MIN_TOOL_IN = 0.0625


def smallest_hole_diameter(dxf_path):
    """Return the smallest hole diameter the postprocessor will see (inches), or None.

    Loads the DXF through the postprocessor's OWN reader rather than reading
    CIRCLE entities directly, because the two DXF flavors represent holes
    differently: a flat Onshape DXF uses CIRCLE entities, but our constructed
    2.5D DXF stores holes as HATCH negative space that load_dxf recovers into
    per-layer circles (via _path_as_circle). Scanning all depth layers here means
    the tool we pick matches the holes the postprocessor will actually classify.
    Large circles (a round part's outer profile) never win the min.
    """
    pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=FALLBACK_TOOL_IN,
                          units='inch')
    with contextlib.redirect_stdout(io.StringIO()):  # this is a silent probe load
        pp.load_dxf(dxf_path)

    if pp.layer_data:
        diameters = [c['diameter']
                     for layer in pp.layer_data.values()
                     for c in layer.get('circles', [])]
    else:
        diameters = [c['diameter'] for c in pp.circles]
    return min(diameters) if diameters else None


def choose_tool_diameter(dxf_path):
    """Pick a tool diameter for a part and explain the choice.

    Returns (tool_diameter_in, note). 4mm by default; the smallest hole's
    diameter when the part has holes smaller than 4mm; floored at 1/16".
    """
    smallest = smallest_hole_diameter(dxf_path)
    if smallest is None:
        return FALLBACK_TOOL_IN, 'no holes found; using default 4mm'
    if smallest >= FALLBACK_TOOL_IN:
        return FALLBACK_TOOL_IN, f'smallest hole {smallest:.4f}" >= 4mm; using default 4mm'
    if smallest < MIN_TOOL_IN:
        return MIN_TOOL_IN, (f'smallest hole {smallest:.4f}" below 1/16" floor; '
                             f'clamped to {MIN_TOOL_IN:.4f}"')
    return smallest, f'smallest hole {smallest:.4f}" < 4mm; sized tool to it'


def _sanitize(name):
    """Make a filesystem-safe base filename from a part name."""
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', (name or 'part').strip())
    return base.strip('_') or 'part'


def export_dxf_for_entry(client, entry):
    """Fetch the DXF bytes for a manifest entry, dispatched on export_strategy."""
    did, wid, eid = entry['doc_id'], entry['workspace_id'], entry['element_id']
    fid, bid, normal = entry['face_id'], entry['body_id'], entry['face_normal']
    strategy = entry['export_strategy']

    if strategy in ('onshape_standard_dxf', 'tube'):
        # 2D and tube both start from a single flat face export.
        return client.export_face_to_dxf(did, wid, eid, fid, body_id=bid,
                                          face_normal=normal)
    if strategy == 'constructed_multilayer':
        dxf_bytes, _thickness = build_multilayer_dxf(client, did, wid, eid, fid, bid, normal)
        return dxf_bytes
    return None


def _postprocess(pp_args, dxf_path, out_dir, base):
    """Run the postprocessor as the web path does on a saved DXF. Returns
    (nc_path, stats) or (None, error_message).

    pp_args is a self-contained dict of everything needed to reproduce the run
    offline (no Onshape): strategy, thickness, tube_height, material,
    tool_diameter. This is what makes `regen` possible from saved DXFs.

    Uses enforce_bounds=False: the harness tests G-code geometry, not machine
    fitment, so an oversize part shouldn't block its correctness test.
    """
    material = pp_args.get('material') or 'plywood'
    tool_in = pp_args['tool_diameter']

    if pp_args['strategy'] == 'tube':
        # Tube: material_thickness is the WALL; tube_height is the section height.
        pp = FRCPostProcessor(material_thickness=pp_args['thickness'],
                              tool_diameter=tool_in, units='inch')
        pp.tube_height = pp_args['tube_height']
        pp.apply_material_preset(material)
        pp.load_dxf(dxf_path)
        pp.transform_coordinates('bottom-left', 0, enforce_bounds=False)
        pp.identify_perimeter_and_pockets()   # before classify_holes (removes perimeter circles)
        pp.classify_holes()
        result = pp.generate_tube_pattern_gcode(
            tube_height=pp_args['tube_height'], square_end=False,
            cut_to_length=False, tube_width=None, tube_length=None,
            suggested_filename=base)
    else:
        pp = FRCPostProcessor(material_thickness=(pp_args['thickness'] or 0.25),
                              tool_diameter=tool_in, units='inch')
        pp.apply_material_preset(material)
        pp.tab_spacing = 6.0
        pp.sacrifice_board_depth = 0.02
        pp.cut_depth = -pp.sacrifice_board_depth
        pp.load_dxf(dxf_path)
        pp.transform_coordinates('bottom-left', 0, enforce_bounds=False)
        pp.identify_perimeter_and_pockets()   # web order: perimeter before holes
        pp.classify_holes()
        result = pp.generate_gcode(suggested_filename=base)  # auto-dispatches 2D vs 2.5D

    if not result.success:
        return None, '; '.join(result.errors) if result.errors else 'postprocessor reported failure'

    nc_path = os.path.join(out_dir, base + '.nc')
    with open(nc_path, 'w') as fh:
        fh.write(result.gcode)
    stats = {}
    if getattr(result, 'stats', None):
        stats['total_lines'] = result.stats.get('total_lines')
        stats['cycle_time_display'] = result.stats.get('cycle_time_display')
    return nc_path, stats


def run_entry(client, entry, out_dir):
    """Export + tool-select + postprocess one manifest entry.

    Returns a result dict with a 'status' of 'ok' | 'skipped' | 'error' and,
    on success, the written nc_path / dxf_path / tool choice.
    """
    name = entry.get('part_name') or 'part'
    result = {
        'part_name': name,
        'doc_name': entry.get('doc_name'),
        'part_type': entry.get('part_type'),
        'status': None,
    }

    strategy = entry.get('export_strategy')
    if not strategy:
        result['status'] = 'skipped'
        result['error'] = 'no export strategy (classification was unknown)'
        return result
    if strategy == 'tube' and not entry.get('tube_height_in'):
        result['status'] = 'skipped'
        result['error'] = 'tube height unknown (name-only tube); set tube_height_in manually'
        return result

    # Lead the filename with the document name so parts that share a generic part
    # name ("Part 1") across different documents don't collide and overwrite each
    # other's DXF/.nc (which would silently corrupt goldens).
    base = _sanitize(f"{entry.get('doc_name') or ''}__{name}")

    try:
        dxf_bytes = export_dxf_for_entry(client, entry)
    except Exception as e:
        result['status'] = 'error'
        result['stage'] = 'export'
        result['error'] = f'{type(e).__name__}: {e}'
        return result
    if not dxf_bytes:
        result['status'] = 'error'
        result['stage'] = 'export'
        result['error'] = 'Onshape returned no DXF'
        return result

    dxf_path = os.path.join(out_dir, base + '.dxf')
    with open(dxf_path, 'wb') as fh:
        fh.write(dxf_bytes)
    result['dxf_path'] = dxf_path

    tool_in, tool_note = choose_tool_diameter(dxf_path)
    result['tool_diameter_in'] = round(tool_in, 4)
    result['tool_note'] = tool_note

    # Record everything needed to reproduce this .nc offline from the saved DXF
    # (used by regen_entry, so regression checks don't need Onshape).
    pp_args = {
        'strategy': strategy,
        'thickness': entry.get('thickness_in'),
        'tube_height': entry.get('tube_height_in'),
        'material': entry.get('material') or 'plywood',
        'tool_diameter': tool_in,
    }
    result['pp_args'] = pp_args

    try:
        nc_path, stats = _postprocess(pp_args, dxf_path, out_dir, base)
    except Exception as e:
        result['status'] = 'error'
        result['stage'] = 'postprocess'
        result['error'] = f'{type(e).__name__}: {e}'
        return result

    if nc_path is None:
        result['status'] = 'error'
        result['stage'] = 'postprocess'
        result['error'] = stats  # error message string
        return result

    result['status'] = 'ok'
    result['nc_path'] = nc_path
    result.update(stats)
    return result


def regen_entry(result, out_dir):
    """Re-run the postprocessor on an already-exported DXF using the args recorded
    at run time. Offline (no Onshape) - this is the regression path: after a code
    change, regen the .nc from the saved DXF and re-check it against the golden.

    Mutates and returns `result` (updates nc_path/status/error in place). Only
    parts that previously produced a DXF + pp_args are regenerated.
    """
    dxf_path = result.get('dxf_path')
    pp_args = result.get('pp_args')
    if not dxf_path or not pp_args:
        return result
    if not os.path.exists(dxf_path):
        result['status'] = 'error'
        result['stage'] = 'regen'
        result['error'] = f'saved DXF missing: {dxf_path}'
        return result

    base = os.path.splitext(os.path.basename(dxf_path))[0]
    try:
        nc_path, stats = _postprocess(pp_args, dxf_path, out_dir, base)
    except Exception as e:
        result['status'] = 'error'
        result['stage'] = 'regen'
        result['error'] = f'{type(e).__name__}: {e}'
        return result

    if nc_path is None:
        result['status'] = 'error'
        result['stage'] = 'regen'
        result['error'] = stats
        return result

    result['status'] = 'ok'
    result['nc_path'] = nc_path
    result.update(stats)
    return result


# ---------------------------------------------------------------------------
# Golden-master oracle (heightmap material-removal, via gcode_sim)
# ---------------------------------------------------------------------------


def render_nc(nc_path, png_dir):
    """Render a heightmap PNG of a .nc for visual inspection (no golden written).

    Same simulation as bless_nc, but purely for eyeballing current output -
    decoupled from golden-writing so regenerating images doesn't imply accepting
    them as correct.
    """
    base = os.path.splitext(os.path.basename(nc_path))[0]
    rec = {'part': base, 'nc': nc_path}
    try:
        with open(nc_path) as fh:
            hm = simulate(fh.read())
        png = os.path.join(png_dir, base + '.png')
        render_heightmap_png(hm, png)
        rec.update(status='ok', png=png)
    except Exception as e:
        rec.update(status='error', error=f'{type(e).__name__}: {e}')
    return rec


def bless_nc(nc_path, golden_dir):
    """Simulate one .nc and save it as the golden heightmap + verification PNG.

    Returns a record dict. Blessing enshrines the CURRENT output as correct, so
    the PNG must be eyeballed before the golden is trusted (see tests/golden/).
    """
    base = os.path.splitext(os.path.basename(nc_path))[0]
    rec = {'part': base, 'nc': nc_path}
    try:
        with open(nc_path) as fh:
            hm = simulate(fh.read())
        golden = os.path.join(golden_dir, base + '.npz')
        png = os.path.join(golden_dir, base + '.png')
        hm.save(golden)
        render_heightmap_png(hm, png)
        rec.update(status='blessed', golden=golden, png=png)
    except Exception as e:
        rec.update(status='error', error=f'{type(e).__name__}: {e}')
    return rec


def check_nc(nc_path, golden_dir, tol=0.005, edge_cells=2, diff_png=True):
    """Check one .nc against its blessed golden. Returns a record dict.

    Simulates on the golden's exact grid and compares depth-for-depth with a
    tolerance and a wall-band exclusion (grid noise near depth transitions).
    """
    base = os.path.splitext(os.path.basename(nc_path))[0]
    rec = {'part': base, 'nc': nc_path}
    golden_path = os.path.join(golden_dir, base + '.npz')
    if not os.path.exists(golden_path):
        rec.update(status='no-golden')
        return rec
    try:
        golden = Heightmap.load(golden_path)
        with open(nc_path) as fh:
            sim = simulate(fh.read(), res=golden.grid['res'],
                           tool_diameter=golden.tool_diameter,
                           material_top=golden.material_top, grid=golden.grid)
        result = compare(sim, golden, tol=tol, edge_cells=edge_cells)
        rec.update(status='pass' if result['passed'] else 'fail',
                   bad_cells=result['total_bad_cells'],
                   over_cut_cells=result['over_cut_cells'],
                   under_cut_cells=result['under_cut_cells'],
                   worst_over=result['worst_over'],
                   worst_under=result['worst_under'])
        if diff_png and not result['passed']:
            png = os.path.join(golden_dir, base + '.diff.png')
            render_diff_png(sim, golden, result, png)
            rec['diff_png'] = png
    except Exception as e:
        rec.update(status='error', error=f'{type(e).__name__}: {e}')
    return rec
