"""Shared DXF geometry stitching.

Onshape (and other CAD) DXF exports represent a face boundary as many individual
LINE / ARC / ELLIPSE / SPLINE / open-POLYLINE segments rather than one closed
polyline. Turning those back into closed boundary paths is needed in two places:

  1. 2D import  - frc_cam_postprocessor.load_dxf (machines the paths directly)
  2. 2.5D build - onshape_integration._convert_geometry_to_solid_hatch (rebuilds a
                  solid-HATCH negative-space DXF from per-depth Onshape exports)

Both call entities_to_closed_paths() so the sampling and stitching live in ONE place
(a missing entity type here previously had to be fixed twice - e.g. ELLIPSE).
"""

import math

from shapely.geometry import LineString
from shapely.ops import linemerge


def sample_arc(arc, num_points=20):
    """Sample an ARC entity into a list of (x, y) points."""
    center = (arc.dxf.center.x, arc.dxf.center.y)
    radius = arc.dxf.radius
    start_angle = math.radians(arc.dxf.start_angle)
    end_angle = math.radians(arc.dxf.end_angle)
    if end_angle <= start_angle:
        end_angle += 2 * math.pi
    return [
        (center[0] + radius * math.cos(start_angle + (end_angle - start_angle) * k / num_points),
         center[1] + radius * math.sin(start_angle + (end_angle - start_angle) * k / num_points))
        for k in range(num_points + 1)
    ]


def sample_ellipse(ellipse, distance=0.001):
    """Sample an ELLIPSE entity (full or arc) into a list of (x, y) points.

    Onshape exports curved perimeter transitions/fillets as ELLIPSE arcs; a full
    ellipse (an elliptical hole/pocket) samples to a loop whose ends coincide, which
    entities_to_closed_paths then recognizes as already-closed.
    """
    try:
        return [(p.x, p.y) for p in ellipse.flattening(distance)]
    except Exception:
        return []


def sample_spline(spline, distance=0.01):
    """Sample a SPLINE entity into a list of (x, y) points (control points as fallback)."""
    try:
        points = [(p[0], p[1]) for p in spline.flattening(distance=distance)]
        if points:
            return points
    except Exception:
        pass
    try:
        control_points = [(p[0], p[1]) for p in spline.control_points]
        return control_points if len(control_points) > 1 else []
    except Exception:
        return []


def entities_to_closed_paths(lines=(), arcs=(), ellipses=(), splines=(), polylines=(),
                             snap=0.001, close_tolerance=0.1, on_open_loop=None):
    """Sample and stitch open DXF entities into closed boundary paths.

    Shared endpoints in a CAD export land sub-micron apart, so exact-match stitching
    fragments a loop and it never closes. Snapping every coordinate to a fine grid
    (default 0.001", far below machining tolerance and the closure check) unifies
    coincident-but-not-identical junctions so shapely.linemerge can join them.

    Args:
        lines/arcs/ellipses/splines: ezdxf LINE/ARC/ELLIPSE/SPLINE entities.
        polylines: ezdxf open LWPOLYLINE entities (contribute their points as a segment).
        snap: coordinate snap grid in drawing units (inches).
        close_tolerance: max end-to-end gap for a merged chain to count as closed.
        on_open_loop: optional callback(coords, gap) invoked for a merged chain that did
            NOT close - lets callers warn about a dropped boundary loop (e.g. a lost
            perimeter) instead of silently discarding it.

    Returns:
        List of closed paths, each a list of (x, y) points (no duplicated closing point).
    """
    def snap_point(x, y):
        return (round(x / snap) * snap, round(y / snap) * snap)

    segments = []
    for line in lines:
        segments.append(LineString([snap_point(line.dxf.start.x, line.dxf.start.y),
                                    snap_point(line.dxf.end.x, line.dxf.end.y)]))
    for arc in arcs:
        pts = [snap_point(x, y) for x, y in sample_arc(arc)]
        if len(pts) >= 2:
            segments.append(LineString(pts))
    for ellipse in ellipses:
        pts = [snap_point(x, y) for x, y in sample_ellipse(ellipse)]
        if len(pts) >= 2:
            segments.append(LineString(pts))
    for spline in splines:
        pts = [snap_point(x, y) for x, y in sample_spline(spline)]
        if len(pts) >= 2:
            segments.append(LineString(pts))
    for polyline in polylines:
        pts = [snap_point(p[0], p[1]) for p in polyline.get_points('xy')]
        if len(pts) >= 2:
            segments.append(LineString(pts))

    if not segments:
        return []

    merged = linemerge(segments)
    geoms = list(merged.geoms) if hasattr(merged, 'geoms') else [merged]

    closed_paths = []
    for geom in geoms:
        coords = list(geom.coords)
        if len(coords) < 3:
            continue
        gap = math.hypot(coords[0][0] - coords[-1][0], coords[0][1] - coords[-1][1])
        if gap < close_tolerance:
            if coords[0] == coords[-1]:
                coords = coords[:-1]
            closed_paths.append(coords)
        elif on_open_loop is not None:
            on_open_loop(coords, gap)
    return closed_paths
