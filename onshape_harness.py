"""PenguinCAM end-to-end test harness.

Fetches the corpus of real user parts from an Onshape folder and (in later
phases) runs each through PenguinCAM automatically to check the generated
G-code. This first phase covers headless Onshape access only.

Auth: reads ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY from the environment. The
secret never leaves this process and is never printed -- only a masked
fingerprint of the access key is logged (see onshape_integration.mask).

Usage:
    export ONSHAPE_ACCESS_KEY=...        # set in your shell, not in the repo
    export ONSHAPE_SECRET_KEY=...
    uv run python onshape_harness.py list-folder <FOLDER_ID>
"""

import argparse
import glob
import json
import os
import sys

from onshape_integration import OnshapeClient
from harness_pipeline import run_entry, regen_entry, bless_nc, check_nc, render_nc


def cmd_list_folder(args):
    """List documents and their Part Studios in an Onshape folder."""
    client = OnshapeClient.from_api_keys()

    documents = client.list_folder_documents(args.folder_id)
    if not documents:
        print(f"No documents found in folder {args.folder_id} "
              "(check the folder ID and that the API key can see it).",
              file=sys.stderr)
        return 1

    failures = 0
    for doc in documents:
        print(f"{doc['name']}  [{doc['id']}]")
        try:
            for studio in client.list_part_studios(doc['id']):
                print(f"    - {studio['name']}  (element {studio['id']})")
        except Exception as e:
            # A transient failure on one document shouldn't abort the corpus.
            failures += 1
            print(f"    ! could not list part studios: {type(e).__name__}: {e}",
                  file=sys.stderr)

    print(f"\n{len(documents)} document(s), {failures} with errors.", file=sys.stderr)
    return 1 if failures else 0


# Setup fields that this phase cannot derive from geometry alone. Tool diameter
# is filled at export time (min hole vs 4mm) once we have the flattened DXF;
# material defaults to plywood and stays editable in the manifest.
_DEFAULT_MATERIAL = 'plywood'


def _classify_studio(client, doc, studio):
    """Build one manifest entry for a Part Studio (source fields + classification)."""
    setup = client.classify_part(doc['id'], studio['workspace_id'], studio['id'])
    entry = {
        'doc_id': doc['id'],
        'doc_name': doc['name'],
        'workspace_id': studio['workspace_id'],
        'element_id': studio['id'],
        'part_name': setup.get('part_name') or studio['name'],
        'part_type': setup['part_type'],
        'export_strategy': setup['export_strategy'],
        'thickness_in': setup.get('thickness_in'),
        'tube_height_in': setup.get('tube_height_in'),
        'tool_diameter_in': None,   # filled at export from the DXF (min hole vs 4mm)
        'material': _DEFAULT_MATERIAL,
        'body_id': setup.get('body_id'),
        'face_id': setup.get('face_id'),
        'face_normal': setup.get('face_normal'),
        'depth_bins_in': setup.get('depth_bins_in', []),
        'confidence': setup['confidence'],
        'needs_review': setup['needs_review'],
        'notes': setup.get('notes', []),
    }
    return entry


def cmd_build_manifest(args):
    """Classify every part in a folder and write a reviewable setup manifest."""
    client = OnshapeClient.from_api_keys()

    documents = client.list_folder_documents(args.folder_id)
    if not documents:
        print(f"No documents found in folder {args.folder_id}.", file=sys.stderr)
        return 1

    entries = []
    errors = 0
    for doc in documents:
        try:
            studios = client.list_part_studios(doc['id'])
        except Exception as e:
            errors += 1
            print(f"! {doc['name']}: could not list part studios: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            continue

        for studio in studios:
            try:
                entry = _classify_studio(client, doc, studio)
            except Exception as e:
                errors += 1
                print(f"! {doc['name']} / {studio['name']}: classify failed: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                continue
            entries.append(entry)
            flag = ' [REVIEW]' if entry['needs_review'] else ''
            print(f"{entry['part_name']}: {entry['part_type']} "
                  f"(t={entry['thickness_in']}, {entry['confidence']}){flag}",
                  file=sys.stderr)

    manifest = {'folder_id': args.folder_id, 'parts': entries}
    with open(args.out, 'w') as fh:
        json.dump(manifest, fh, indent=2)

    review = sum(1 for e in entries if e['needs_review'])
    print(f"\nWrote {len(entries)} part(s) to {args.out} "
          f"({review} need review, {errors} error(s)).", file=sys.stderr)
    return 1 if errors else 0


def cmd_classify(args):
    """Classify a single Part Studio and print its inferred setup as JSON."""
    client = OnshapeClient.from_api_keys()
    setup = client.classify_part(args.document_id, args.workspace_id, args.element_id)
    print(json.dumps(setup, indent=2))
    return 0


def cmd_run(args):
    """Run every part in a manifest through PenguinCAM, writing DXFs + .nc files."""
    client = OnshapeClient.from_api_keys()
    with open(args.manifest) as fh:
        manifest = json.load(fh)
    entries = manifest.get('parts', [])
    os.makedirs(args.out_dir, exist_ok=True)

    results = []
    counts = {'ok': 0, 'skipped': 0, 'error': 0}
    for entry in entries:
        result = run_entry(client, entry, args.out_dir)
        results.append(result)
        counts[result['status']] = counts.get(result['status'], 0) + 1

        # Lead with the document name (what the user recognizes / can hover in
        # Onshape), then the part name.
        label = f"{result.get('doc_name')} / {result['part_name']}"
        if result['status'] == 'ok':
            print(f"[ok]      {label}: {result['part_type']}, "
                  f"tool {result['tool_diameter_in']}\" -> {os.path.basename(result['nc_path'])}",
                  file=sys.stderr)
        elif result['status'] == 'skipped':
            print(f"[skip]    {label}: {result['error']}", file=sys.stderr)
        else:
            print(f"[ERROR]   {label} ({result.get('stage')}): {result['error']}",
                  file=sys.stderr)

    summary_path = os.path.join(args.out_dir, 'run_results.json')
    with open(summary_path, 'w') as fh:
        json.dump({'results': results}, fh, indent=2)

    print(f"\n{counts['ok']} ok, {counts['skipped']} skipped, {counts['error']} error(s). "
          f"Details in {summary_path}", file=sys.stderr)
    return 1 if counts['error'] else 0


def _nc_files(out_dir):
    return sorted(glob.glob(os.path.join(out_dir, '*.nc')))


def cmd_render(args):
    """Render heightmap PNGs of current .nc output for visual inspection.

    Does NOT write or touch goldens - this is the periodic eyeball workflow.
    """
    nc_files = _nc_files(args.out_dir)
    if not nc_files:
        print(f"No .nc files in {args.out_dir} (run first).", file=sys.stderr)
        return 1
    os.makedirs(args.png_dir, exist_ok=True)
    errors = 0
    for nc in nc_files:
        rec = render_nc(nc, args.png_dir)
        if rec['status'] == 'ok':
            print(f"[img] {os.path.basename(rec['png'])}", file=sys.stderr)
        else:
            errors += 1
            print(f"[ERROR] {rec['part']}: {rec['error']}", file=sys.stderr)
    print(f"\nRendered {len(nc_files) - errors}/{len(nc_files)} PNGs into {args.png_dir}.",
          file=sys.stderr)
    return 1 if errors else 0


def cmd_bless(args):
    """Bless every .nc in the output dir as a golden heightmap (+ verify PNG).

    Enshrines the CURRENT output as correct - review the PNGs before trusting.
    """
    nc_files = _nc_files(args.out_dir)
    if not nc_files:
        print(f"No .nc files in {args.out_dir} (run first).", file=sys.stderr)
        return 1
    os.makedirs(args.golden_dir, exist_ok=True)
    errors = 0
    for nc in nc_files:
        rec = bless_nc(nc, args.golden_dir)
        if rec['status'] == 'blessed':
            print(f"[blessed] {rec['part']} -> {os.path.basename(rec['png'])}",
                  file=sys.stderr)
        else:
            errors += 1
            print(f"[ERROR]   {rec['part']}: {rec['error']}", file=sys.stderr)
    print(f"\nBlessed {len(nc_files) - errors}/{len(nc_files)} into {args.golden_dir}. "
          f"REVIEW THE PNGs before trusting these goldens.", file=sys.stderr)
    return 1 if errors else 0


def cmd_check(args):
    """Regression-check .nc output against blessed goldens (heightmap diff).

    With --regen, first re-runs the postprocessor on the saved DXFs (offline) so
    the check reflects the current code without re-fetching from Onshape.
    """
    if args.regen:
        summary_path = os.path.join(args.out_dir, 'run_results.json')
        if not os.path.exists(summary_path):
            print(f"--regen needs {summary_path} (run first).", file=sys.stderr)
            return 1
        with open(summary_path) as fh:
            results = json.load(fh)['results']
        for r in results:
            regen_entry(r, args.out_dir)

    nc_files = _nc_files(args.out_dir)
    if not nc_files:
        print(f"No .nc files in {args.out_dir}.", file=sys.stderr)
        return 1

    counts = {'pass': 0, 'fail': 0, 'no-golden': 0, 'error': 0}
    for nc in nc_files:
        rec = check_nc(nc, args.golden_dir, tol=args.tol, edge_cells=args.edge_cells)
        counts[rec['status']] = counts.get(rec['status'], 0) + 1
        if rec['status'] == 'pass':
            print(f"[PASS]    {rec['part']}", file=sys.stderr)
        elif rec['status'] == 'fail':
            w = rec.get('worst_over') or rec.get('worst_under')
            where = (f" worst {w['delta']:+.4f}\" at ({w['x']:.2f},{w['y']:.2f})"
                     if w else '')
            print(f"[FAIL]    {rec['part']}: {rec['bad_cells']} bad cells "
                  f"({rec['over_cut_cells']} over / {rec['under_cut_cells']} under){where}"
                  f" -> {os.path.basename(rec.get('diff_png', ''))}", file=sys.stderr)
        elif rec['status'] == 'no-golden':
            print(f"[no-gold] {rec['part']}: no golden (bless first)", file=sys.stderr)
        else:
            print(f"[ERROR]   {rec['part']}: {rec['error']}", file=sys.stderr)

    print(f"\n{counts['pass']} pass, {counts['fail']} fail, "
          f"{counts['no-golden']} un-blessed, {counts['error']} error(s).", file=sys.stderr)
    return 1 if (counts['fail'] or counts['error']) else 0


def cmd_regen(args):
    """Re-run the postprocessor on saved DXFs (offline) from a prior run's args."""
    summary_path = os.path.join(args.out_dir, 'run_results.json')
    if not os.path.exists(summary_path):
        print(f"No {summary_path} (run first).", file=sys.stderr)
        return 1
    with open(summary_path) as fh:
        results = json.load(fh)['results']

    errors = 0
    for r in results:
        if not r.get('dxf_path') or not r.get('pp_args'):
            continue
        regen_entry(r, args.out_dir)
        if r['status'] == 'ok':
            print(f"[ok]      {os.path.basename(r['nc_path'])}", file=sys.stderr)
        else:
            errors += 1
            print(f"[ERROR]   {r['part_name']} ({r.get('stage')}): {r['error']}",
                  file=sys.stderr)

    with open(summary_path, 'w') as fh:
        json.dump({'results': results}, fh, indent=2)
    print(f"\nRegenerated .nc from saved DXFs ({errors} error(s)).", file=sys.stderr)
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(
        description='PenguinCAM Onshape end-to-end test harness')
    sub = parser.add_subparsers(dest='command', required=True)

    p_list = sub.add_parser(
        'list-folder',
        help='List documents and Part Studios in an Onshape folder')
    p_list.add_argument('folder_id', help='Onshape folder ID (from the folder URL)')
    p_list.set_defaults(func=cmd_list_folder)

    p_manifest = sub.add_parser(
        'build-manifest',
        help='Classify every part in a folder into a reviewable setup manifest')
    p_manifest.add_argument('folder_id', help='Onshape folder ID (from the folder URL)')
    p_manifest.add_argument('--out', default='harness_manifest.json',
                            help='Manifest output path (default: harness_manifest.json)')
    p_manifest.set_defaults(func=cmd_build_manifest)

    p_classify = sub.add_parser(
        'classify', help='Classify a single Part Studio and print its inferred setup')
    p_classify.add_argument('document_id')
    p_classify.add_argument('workspace_id')
    p_classify.add_argument('element_id')
    p_classify.set_defaults(func=cmd_classify)

    p_run = sub.add_parser(
        'run', help='Run every part in a manifest through PenguinCAM (export + gcode)')
    p_run.add_argument('manifest', help='Manifest JSON from build-manifest')
    p_run.add_argument('--out-dir', default='harness_out',
                       help='Directory for exported DXFs and .nc files (default: harness_out)')
    p_run.set_defaults(func=cmd_run)

    p_render = sub.add_parser(
        'render', help='Render heightmap PNGs of current output for visual inspection (no goldens)')
    p_render.add_argument('--out-dir', default='harness_out',
                          help='Directory of .nc files to render (default: harness_out)')
    p_render.add_argument('--png-dir', default='harness_images',
                          help='Where to write PNGs (default: harness_images)')
    p_render.set_defaults(func=cmd_render)

    p_bless = sub.add_parser(
        'bless', help='Bless current .nc output as golden heightmaps (review the PNGs!)')
    p_bless.add_argument('--out-dir', default='harness_out',
                         help='Directory of .nc files to bless (default: harness_out)')
    p_bless.add_argument('--golden-dir', default='tests/golden',
                         help='Where to write goldens (default: tests/golden)')
    p_bless.set_defaults(func=cmd_bless)

    p_check = sub.add_parser(
        'check', help='Regression-check .nc output against blessed goldens')
    p_check.add_argument('--out-dir', default='harness_out',
                         help='Directory of .nc files to check (default: harness_out)')
    p_check.add_argument('--golden-dir', default='tests/golden',
                         help='Directory of goldens (default: tests/golden)')
    p_check.add_argument('--regen', action='store_true',
                         help='Re-run the postprocessor on saved DXFs first (offline)')
    p_check.add_argument('--tol', type=float, default=0.005,
                         help='Depth tolerance in inches (default: 0.005)')
    p_check.add_argument('--edge-cells', type=int, default=2,
                         help='Wall-band cells excluded near depth transitions (default: 2)')
    p_check.set_defaults(func=cmd_check)

    p_regen = sub.add_parser(
        'regen', help='Re-run the postprocessor on saved DXFs (offline, no Onshape)')
    p_regen.add_argument('--out-dir', default='harness_out',
                         help='Directory of saved DXFs / run_results.json (default: harness_out)')
    p_regen.set_defaults(func=cmd_regen)

    args = parser.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
