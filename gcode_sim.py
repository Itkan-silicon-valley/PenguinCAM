#!/usr/bin/env python3
"""
gcode_sim.py - Heightmap material-removal simulator + golden-master tester.

Rasterizes PenguinCAM G-code into a Z-heightmap (a flat-endmill "material
removal" simulation) so we can answer: does this G-code, when run, remove the
right material to the right depths in the right places?

Coordinate system matches the postprocessor: Z=0 is the sacrifice-board surface,
material top is at Z=material_thickness. Every grid cell starts at material_top;
as the tool sweeps, each cell is lowered to the lowest tool-tip Z that passes
within tool_radius of it. Cells never reached keep material_top (uncut).

Usage:
    # Simulate and render a PNG (self-contained; reads tool/material from header)
    uv run python gcode_sim.py simulate OUT.nc --png out.png

    # Bless a known-good result as the golden for a real part (verify the PNG!)
    uv run python gcode_sim.py bless OUT.nc --golden tests/golden/part.npz

    # Regression-check current output against a blessed golden
    uv run python gcode_sim.py check OUT.nc --golden tests/golden/part.npz --png diff.png

Notes:
- Only flat end mills are modeled (matches PenguinCAM's tooling).
- Edges are inherently approximate at grid resolution; `check` excludes a band
  around expected wall transitions so resolution noise doesn't cause failures.
"""

import argparse
import math
import re
import sys

import numpy as np

# ---------------------------------------------------------------------------
# Header metadata extraction
# ---------------------------------------------------------------------------


def parse_header_metadata(text):
    """Pull tool diameter, material thickness/top, and units from G-code comments.

    Returns a dict with any keys it could find: units ('inch'|'mm'),
    material_thickness, material_top, tool_diameter.
    """
    meta = {}

    m = re.search(r"\(Units:\s*(\w+)", text, re.IGNORECASE)
    if m:
        unit_label = m.group(1).lower()
        meta["units"] = "mm" if unit_label.startswith(('mm', 'mill')) else "inch"
    elif re.search(r"^\s*G21\b", text, re.MULTILINE):
        meta["units"] = "mm"
    elif re.search(r"^\s*G20\b", text, re.MULTILINE):
        meta["units"] = "inch"

    def in_program_units(value, suffix):
        """Convert an explicitly labeled header length into the program's units."""
        value = float(value)
        suffix = (suffix or '').lower()
        if suffix == '"' and meta.get('units') == 'mm':
            return value * 25.4
        if suffix == 'mm' and meta.get('units') == 'inch':
            return value / 25.4
        return value

    m = re.search(r"\(Material:.*?([\d.]+)\s*(\"|mm)?\s*thick", text, re.IGNORECASE)
    if m:
        meta["material_thickness"] = in_program_units(m.group(1), m.group(2))

    m = re.search(r"\(\s*Material top:\s*Z=([\d.\-]+)\s*(\"|mm)?", text, re.IGNORECASE)
    if m:
        meta["material_top"] = in_program_units(m.group(1), m.group(2))

    # Accept both the flat header "(Tool: 0.15748" diam Flat End Mill)" and the
    # tube header "( Tool: 0.157" end mill )" - just grab the first number after
    # "Tool:".
    m = re.search(r"\(\s*Tool:\s*([\d.]+)\s*(\"|mm)?", text, re.IGNORECASE)
    if m:
        meta["tool_diameter"] = in_program_units(m.group(1), m.group(2))

    return meta


# ---------------------------------------------------------------------------
# G-code parsing -> list of linear moves
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?[0-9]*\.?[0-9]+)")

# Work-Z sentinel meaning "tool physically retracted (via a machine-coord move)".
# Any value comfortably above any real material top so it never registers as cutting.
_SAFE_Z = 1e9


def _strip_comment(line):
    """Remove ';' comments and '(...)' comment groups from a G-code line."""
    line = line.split(";", 1)[0]
    line = re.sub(r"\([^)]*\)", "", line)
    return line.strip()


def parse_moves(text):
    """Parse G-code into a list of (kind, x0, y0, z0, x1, y1, z1) tuples.

    Arcs (G2/G3) are tessellated into short linear segments here, interpolating
    Z linearly across the arc (handles helical entry). kind is 'rapid' for G0,
    'feed' otherwise. Modal motion mode and modal coordinates are honored.
    """
    x = y = z = 0.0
    motion = None  # 0,1,2,3
    moves = []

    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line:
            continue
        # Machine-coordinate positioning moves: G53 (one-shot machine coords, used for
        # safe-Z and gantry parking) and G28 (homing). Their coordinates are in the
        # machine frame, not the work (G54) frame we simulate, so we don't emit a
        # segment or trust their X/Y/Z as work coordinates. But they DO physically
        # retract the tool to a safe height, so we lift the tracked work-Z to a safe
        # sentinel — otherwise the following XY positioning move (which omits Z,
        # relying on the tool being safely up) would inherit a stale cutting depth and
        # carve a phantom gouge across the part.
        if re.search(r"\bG(53|28)\b", line):
            z = _SAFE_Z
            continue
        words = {letter.upper(): float(val) for letter, val in _WORD_RE.findall(line)}
        if not words:
            continue

        if "G" in words:
            g = int(round(words["G"]))
            if g in (0, 1, 2, 3):
                motion = g
        if motion is None:
            continue
        # Lines that don't move an axis (e.g. pure G-mode setup) are skipped.
        if not any(a in words for a in ("X", "Y", "Z", "I", "J")):
            continue

        nx = words.get("X", x)
        ny = words.get("Y", y)
        nz = words.get("Z", z)

        if motion in (0, 1):
            kind = "rapid" if motion == 0 else "feed"
            moves.append((kind, x, y, z, nx, ny, nz))
            x, y, z = nx, ny, nz
        else:  # arc
            i = words.get("I", 0.0)
            j = words.get("J", 0.0)
            for seg in _tessellate_arc(x, y, z, nx, ny, nz, i, j, ccw=(motion == 3)):
                moves.append(("feed",) + seg)
            x, y, z = nx, ny, nz

    return moves


def _tessellate_arc(x0, y0, z0, x1, y1, z1, i, j, ccw, chord=0.01):
    """Yield (x0,y0,z0,x1,y1,z1) linear segments approximating an arc.

    Arc center is incremental (G91.1): center = start + (I, J). Full circles
    (start == end) sweep a full turn. Z is interpolated linearly along the sweep.
    """
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    if r < 1e-9:
        yield (x0, y0, z0, x1, y1, z1)
        return

    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    full = math.hypot(x1 - x0, y1 - y0) < 1e-6

    if full:
        sweep = 2 * math.pi if ccw else -2 * math.pi
    elif ccw:
        while a1 <= a0:
            a1 += 2 * math.pi
        sweep = a1 - a0
    else:
        while a1 >= a0:
            a1 -= 2 * math.pi
        sweep = a1 - a0

    n = max(2, int(math.ceil(abs(sweep) * r / chord)))
    px, py, pz = x0, y0, z0
    for k in range(1, n + 1):
        t = k / n
        a = a0 + sweep * t
        qx = cx + r * math.cos(a)
        qy = cy + r * math.sin(a)
        qz = z0 + (z1 - z0) * t
        yield (px, py, pz, qx, qy, qz)
        px, py, pz = qx, qy, qz


# ---------------------------------------------------------------------------
# Heightmap simulation
# ---------------------------------------------------------------------------


class Heightmap:
    """A Z-heightmap over an XY grid, plus the metadata needed to compare two."""

    def __init__(self, grid, material_top, tool_diameter, units, z):
        self.grid = grid  # dict: minx, miny, res, nx, ny
        self.material_top = material_top
        self.tool_diameter = tool_diameter
        self.units = units
        self.z = z  # np.float32 array, shape (ny, nx)

    def save(self, path):
        g = self.grid
        np.savez_compressed(
            path,
            z=self.z,
            minx=g["minx"], miny=g["miny"], res=g["res"],
            nx=g["nx"], ny=g["ny"],
            material_top=self.material_top,
            tool_diameter=self.tool_diameter,
            units=self.units,
        )

    @classmethod
    def load(cls, path):
        d = np.load(path, allow_pickle=True)
        grid = {
            "minx": float(d["minx"]), "miny": float(d["miny"]),
            "res": float(d["res"]), "nx": int(d["nx"]), "ny": int(d["ny"]),
        }
        return cls(
            grid=grid,
            material_top=float(d["material_top"]),
            tool_diameter=float(d["tool_diameter"]),
            units=str(d["units"]),
            z=d["z"].astype(np.float32),
        )


def _make_grid(moves, tool_radius, res, margin_cells=2):
    xs, ys = [], []
    for _, x0, y0, _, x1, y1, _ in moves:
        xs.extend((x0, x1))
        ys.extend((y0, y1))
    if not xs:
        raise ValueError("No motion found in G-code")
    pad = tool_radius + margin_cells * res
    minx, maxx = min(xs) - pad, max(xs) + pad
    miny, maxy = min(ys) - pad, max(ys) + pad
    nx = int(math.ceil((maxx - minx) / res)) + 1
    ny = int(math.ceil((maxy - miny) / res)) + 1
    return {"minx": minx, "miny": miny, "res": res, "nx": nx, "ny": ny}


def _disc_mask(radius_cells):
    """Boolean mask of a filled disc of the given radius (in cells)."""
    r = int(math.ceil(radius_cells))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    return (xx * xx + yy * yy) <= (radius_cells * radius_cells)


def simulate(text, res=0.01, tool_diameter=None, material_top=None,
             material_thickness=None, units=None, grid=None):
    """Simulate material removal from G-code text; return a Heightmap.

    Missing tool/material/units are read from the header. If `grid` is given
    (from a golden), the simulation uses that exact grid so results align.
    """
    meta = parse_header_metadata(text)
    units = units or meta.get("units", "inch")
    tool_diameter = tool_diameter or meta.get("tool_diameter")
    if tool_diameter is None:
        raise ValueError("tool diameter not found in header; pass --tool")
    material_thickness = material_thickness or meta.get("material_thickness")
    if material_top is None:
        material_top = meta.get("material_top")
    if material_top is None and material_thickness is not None:
        material_top = material_thickness  # Z=0 at sacrifice board

    tool_radius = tool_diameter / 2.0
    moves = parse_moves(text)

    if material_top is None:
        # Header carried neither a material top nor thickness (e.g. tube-pattern
        # programs). Fall back to the highest real cutting Z so the sim still
        # captures every downward removal. The flat-stock heightmap won't
        # physically describe a tube, but it's a deterministic, self-consistent
        # function of the G-code, which is all a regression golden needs.
        real_z = [zz for _, _, _, z0, _, _, z1 in moves
                  for zz in (z0, z1) if zz < _SAFE_Z]
        if not real_z:
            raise ValueError(
                "material top/thickness not found and no cutting moves; "
                "pass --material-top"
            )
        material_top = max(real_z)
    if grid is None:
        grid = _make_grid(moves, tool_radius, res)

    minx, miny, res = grid["minx"], grid["miny"], grid["res"]
    nx, ny = grid["nx"], grid["ny"]
    z = np.full((ny, nx), np.float32(material_top), dtype=np.float32)

    mask = _disc_mask(tool_radius / res)
    mr = mask.shape[0] // 2  # mask "radius" in cells

    step = res * 0.5  # sample spacing along a segment
    for _, x0, y0, z0, x1, y1, z1 in moves:
        seg_len = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(math.ceil(seg_len / step)))
        for k in range(n + 1):
            t = k / n
            zt = z0 + (z1 - z0) * t
            if zt >= material_top:  # above stock -> no cutting
                continue
            xt = x0 + (x1 - x0) * t
            yt = y0 + (y1 - y0) * t
            ci = int(round((yt - miny) / res))
            cj = int(round((xt - minx) / res))
            _stamp(z, mask, mr, ci, cj, np.float32(zt))

    return Heightmap(grid, material_top, tool_diameter, units, z)


def _stamp(z, mask, mr, ci, cj, zt):
    """Lower cells under the disc centered at (ci,cj) to at most zt."""
    ny, nx = z.shape
    i0, i1 = ci - mr, ci + mr + 1
    j0, j1 = cj - mr, cj + mr + 1
    # Clip window to array bounds, cropping the mask to match.
    mi0 = max(0, -i0)
    mj0 = max(0, -j0)
    i0c, i1c = max(0, i0), min(ny, i1)
    j0c, j1c = max(0, j0), min(nx, j1)
    if i0c >= i1c or j0c >= j1c:
        return
    m = mask[mi0:mi0 + (i1c - i0c), mj0:mj0 + (j1c - j0c)]
    sub = z[i0c:i1c, j0c:j1c]
    np.minimum(sub, zt, out=sub, where=m)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _edge_band(golden_z, tol, edge_cells):
    """Boolean mask of cells near a depth transition in the golden, dilated.

    These are the wall regions where rasterization is inherently fuzzy; we
    exclude them from pass/fail so grid resolution doesn't cause false failures.
    """
    gz = golden_z
    # A cell is on a transition if it differs from a neighbor by more than tol.
    diff = np.zeros(gz.shape, dtype=bool)
    diff[:-1, :] |= np.abs(gz[:-1, :] - gz[1:, :]) > tol
    diff[1:, :] |= np.abs(gz[1:, :] - gz[:-1, :]) > tol
    diff[:, :-1] |= np.abs(gz[:, :-1] - gz[:, 1:]) > tol
    diff[:, 1:] |= np.abs(gz[:, 1:] - gz[:, :-1]) > tol
    if edge_cells <= 0:
        return diff
    # Dilate by edge_cells using a simple iterative neighbor-OR.
    band = diff.copy()
    for _ in range(edge_cells):
        nxt = band.copy()
        nxt[:-1, :] |= band[1:, :]
        nxt[1:, :] |= band[:-1, :]
        nxt[:, :-1] |= band[:, 1:]
        nxt[:, 1:] |= band[:, :-1]
        band = nxt
    return band


def compare(sim, golden, tol=0.005, edge_cells=2):
    """Compare two aligned Heightmaps. Returns a result dict."""
    if sim.z.shape != golden.z.shape:
        raise ValueError(
            f"grid mismatch: sim {sim.z.shape} vs golden {golden.z.shape}"
        )
    diff = sim.z - golden.z  # <0 = over-cut (deeper), >0 = under-cut (shallower)
    band = _edge_band(golden.z, tol, edge_cells)
    significant = (np.abs(diff) > tol) & ~band

    over = significant & (diff < 0)   # removed material that should remain
    under = significant & (diff > 0)  # left material that should be removed

    res = golden.grid["res"]
    minx, miny = golden.grid["minx"], golden.grid["miny"]

    def worst(mask):
        if not mask.any():
            return None
        idx = np.argmax(np.where(mask, np.abs(diff), 0))
        iy, ix = np.unravel_index(idx, diff.shape)
        return {
            "x": minx + ix * res,
            "y": miny + iy * res,
            "delta": float(diff[iy, ix]),
        }

    return {
        "passed": not significant.any(),
        "over_cut_cells": int(over.sum()),
        "under_cut_cells": int(under.sum()),
        "total_bad_cells": int(significant.sum()),
        "worst_over": worst(over),
        "worst_under": worst(under),
        "tol": tol,
        "diff": diff,
        "over": over,
        "under": under,
        "band": band,
    }


# ---------------------------------------------------------------------------
# PNG rendering (via PIL, no matplotlib dependency)
# ---------------------------------------------------------------------------


def _depth_to_rgb(z, material_top, floor):
    """Grayscale by depth: material_top -> light, deepest -> dark."""
    span = max(1e-6, material_top - floor)
    t = np.clip((z - floor) / span, 0.0, 1.0)  # 0=deep .. 1=surface
    g = (40 + 200 * t).astype(np.uint8)
    rgb = np.stack([g, g, g], axis=-1)
    return rgb


def render_heightmap_png(hm, path):
    from PIL import Image

    floor = float(np.min(hm.z))
    rgb = _depth_to_rgb(hm.z, hm.material_top, floor)
    # Flip vertically so +Y is up in the image.
    Image.fromarray(rgb[::-1], mode="RGB").save(path)


def render_diff_png(sim, golden, result, path):
    from PIL import Image

    floor = min(float(np.min(golden.z)), float(np.min(sim.z)))
    rgb = _depth_to_rgb(golden.z, golden.material_top, floor).copy()
    rgb[result["band"]] = [70, 70, 30]        # excluded wall band (dim yellow)
    rgb[result["under"]] = [40, 120, 255]     # under-cut (blue)
    rgb[result["over"]] = [255, 40, 40]       # over-cut (red)
    Image.fromarray(rgb[::-1], mode="RGB").save(path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read(path):
    with open(path) as f:
        return f.read()


def _add_sim_args(p):
    p.add_argument("gcode", help="G-code (.nc) file")
    p.add_argument("--res", type=float, default=0.01, help="grid resolution (default 0.01)")
    p.add_argument("--tool", type=float, default=None, help="tool diameter override")
    p.add_argument("--material-top", type=float, default=None, help="material top Z override")


def cmd_simulate(args):
    hm = simulate(_read(args.gcode), res=args.res,
                  tool_diameter=args.tool, material_top=args.material_top)
    print(f"Grid: {hm.grid['nx']}x{hm.grid['ny']} @ {hm.grid['res']} "
          f"({hm.units}), tool {hm.tool_diameter}, material_top {hm.material_top}")
    cut = hm.z < hm.material_top - 1e-6
    print(f"Cut area: {int(cut.sum())} cells; deepest Z {float(hm.z.min()):.4f}")
    if args.out:
        hm.save(args.out)
        print(f"Saved heightmap -> {args.out}")
    if args.png:
        render_heightmap_png(hm, args.png)
        print(f"Saved render -> {args.png}")
    return 0


def cmd_bless(args):
    hm = simulate(_read(args.gcode), res=args.res,
                  tool_diameter=args.tool, material_top=args.material_top)
    hm.save(args.golden)
    png = args.png or (args.golden.rsplit(".", 1)[0] + ".png")
    render_heightmap_png(hm, png)
    print(f"Blessed golden -> {args.golden}")
    print(f"Verify this render before trusting the golden -> {png}")
    return 0


def cmd_check(args):
    golden = Heightmap.load(args.golden)
    sim = simulate(_read(args.gcode), res=golden.grid["res"],
                   tool_diameter=args.tool or golden.tool_diameter,
                   material_top=args.material_top,
                   grid=golden.grid)
    r = compare(sim, golden, tol=args.tol, edge_cells=args.edge_cells)

    status = "PASS" if r["passed"] else "FAIL"
    print(f"[{status}] {args.gcode} vs {args.golden}")
    print(f"  tolerance: {r['tol']}  bad cells: {r['total_bad_cells']}")
    print(f"  over-cut (removed too much): {r['over_cut_cells']} cells")
    if r["worst_over"]:
        w = r["worst_over"]
        print(f"    worst {w['delta']:+.4f} at ({w['x']:.3f}, {w['y']:.3f})")
    print(f"  under-cut (left material):   {r['under_cut_cells']} cells")
    if r["worst_under"]:
        w = r["worst_under"]
        print(f"    worst {w['delta']:+.4f} at ({w['x']:.3f}, {w['y']:.3f})")
    if args.png:
        render_diff_png(sim, golden, r, args.png)
        print(f"  diff render -> {args.png}")
    return 0 if r["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("simulate", help="simulate and optionally render/save")
    _add_sim_args(p)
    p.add_argument("--out", help="save heightmap .npz")
    p.add_argument("--png", help="save color PNG render")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("bless", help="save a known-good result as golden")
    _add_sim_args(p)
    p.add_argument("--golden", required=True, help="output golden .npz path")
    p.add_argument("--png", help="verification PNG path (default: alongside golden)")
    p.set_defaults(func=cmd_bless)

    p = sub.add_parser("check", help="regression-check output against a golden")
    p.add_argument("gcode", help="G-code (.nc) file")
    p.add_argument("--golden", required=True, help="golden .npz to compare against")
    p.add_argument("--tol", type=float, default=0.005, help="depth tolerance (default 0.005)")
    p.add_argument("--edge-cells", type=int, default=2,
                   help="wall-band cells to exclude near transitions (default 2)")
    p.add_argument("--tool", type=float, default=None, help="tool diameter override")
    p.add_argument("--material-top", type=float, default=None, help="material top Z override")
    p.add_argument("--png", help="save diff PNG")
    p.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
