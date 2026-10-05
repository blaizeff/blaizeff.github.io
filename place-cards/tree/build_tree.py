#!/usr/bin/env python3
"""Gold tree relief for the wedding place cards (final build, promoted from variant B).

Turns the Tripo tree (part 0 of place-cards/source/tree_tripo_meshopt.glb) into a print-ready FDM
heightfield relief: flat glue face on the bed at z = 0, relief up, one watertight body, in the canonical
tree frame (origin = centre of the straight trunk-bottom edge, +x right, +y up, z = thickness).

Pipeline
  1. Orthographic depth maps of the tree front/back, from the committed cache cache/tree_depth_q16.npz
     (2.4 MB, 16-bit quantised, depth error < 0.00004 mm), so a fresh clone builds without node or network.
     --regen (or a missing cache) rebuilds it from the GLB: meshopt decode with gltf-transform via npx,
     node tripo_part_0 in world transform, embree ray casting.
  2. Raster clean-up of the silhouette: horizontal trunk cut, largest body only, pin-holes filled,
     left root flare.
  3. Vector outline: sub-pixel contour of a smoothed mask -> shapely. Rounds of: thicken stems below
     --min-width (discs on the medial axis, free ends excluded), close gaps below --min-gap, drop tiny
     holes, open with --tip-radius (rounds every tip). Then the thin root needle at the trunk cut is
     trimmed to a round cap (--root-radius) and a gap fixer widens pinches / fills dead-end notches
     narrower than --min-gap without leaving spikes. The cut edge is snapped exactly onto y = 0.
  4. Heights: source relief above the flat back, spike removal, bilateral denoise, the trunk continued
     straight down to the cut along its flutes (--base-band; the source droops into its roots there),
     stretched stem profiles, closed gaps filled as low grooves, the grazing-angle rim rebuilt by a smooth
     extrapolation (also a few pixels beyond the outline, blended back in), band-pass detail boost,
     base + gain mapping, soft floor (--floor-out, 1.4 mm everywhere by default) and soft cap.
  5. Constrained Delaunay triangulation (triangle) of the exact outline, refined adaptively by
     greedy insertion of the worst pixel until the max vertical error is below --tol.
  6. Vertical walls, a width-adaptive 45 deg bed chamfer (--chamfer-min on ~1 mm stems up to --chamfer
     on wide parts; each glue vertex is its wall vertex moved inward by the local chamfer) and a planar
     bottom, so tree_footprint.json 'polygons' is exactly the mesh's z = 0 face and 'outline_full' is
     exactly its vertical walls (the pocket shape).
  7. Checks: trimesh / manifold3d, overhangs, chamfer angles, vertical-ray heightfield + thickness test,
     exact manifold3d sections, medial-axis widths, gaps, tip radii, exact openings, rim roughness,
     surface error. Then previews and out/report.md.

Run (from anywhere; Python 3.11 with numpy, scipy, shapely 2, scikit-image, opencv, triangle, trimesh
with embree, manifold3d, matplotlib, pillow):
  python3 place-cards/tree/build_tree.py            # full build, about 3 min on 4 CPUs
  python3 place-cards/tree/build_tree.py --regen    # re-decode the GLB and rewrite the depth cache first (node/npx)
  python3 place-cards/tree/slice_check.py           # PrusaSlicer extrusion coverage check, about 1 min
Outputs go to place-cards/tree/out/ (override with --out), scratch to place-cards/tree/work/ (git-ignored).
The output is deterministic: the same cache and library versions give byte-identical files.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time

import numpy as np
from scipy import ndimage as ndi

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_PC = os.path.abspath(os.path.join(HERE, '..'))                     # place-cards/
DEFAULT_GLB = os.path.join(REPO_PC, 'source', 'tree_tripo_meshopt.glb')
DEFAULT_DEPTH = os.path.join(HERE, 'cache', 'tree_depth_q16.npz')    # committed: builds need no node/npx
DEFAULT_WORK = os.path.join(HERE, 'work')                              # scratch (git-ignored)
T0 = time.time()
DEBUG_SHAPES = None          # set to a dict to keep the intermediate outlines (debugging)


def log(*a):
    print(f'[{time.time() - T0:6.1f}s]', *a, flush=True)


# --------------------------------------------------------------------------------------------
# 1. source depth maps
# --------------------------------------------------------------------------------------------
def regen_depth(glb, work, res):
    """Decode the meshopt GLB, extract tripo_part_0 in world coordinates, ray-cast the depth maps.
    Needs node + npx (@gltf-transform/cli is fetched by npx on first use) and trimesh with embree."""
    import trimesh
    tmp = os.path.join(work, 'regen')
    os.makedirs(tmp, exist_ok=True)
    dec = os.path.join(tmp, 'tree_decoded.glb')
    log('decoding GLB with gltf-transform ...')
    subprocess.run(['npx', '-y', '@gltf-transform/cli@4', 'copy', glb, dec], check=True,
                   stdout=subprocess.DEVNULL)
    scene = trimesh.load(dec)
    mesh = None
    for node in scene.graph.nodes_geometry:
        T, gname = scene.graph[node]
        if node == 'tripo_part_0':
            mesh = scene.geometry[gname].copy()
            mesh.apply_transform(T)
    if mesh is None:
        raise SystemExit('node tripo_part_0 not found in ' + glb)
    b = mesh.bounds
    zs = np.arange(b[0, 2], b[1, 2], res)
    ys = np.arange(b[1, 1], b[0, 1], -res)
    Z, Y = np.meshgrid(zs, ys)
    n = Z.size
    maps = {'zs': zs, 'ys': ys, 'res': res}
    for name, x0, dx in (('front', b[1, 0] + 0.01, -1.0), ('back', b[0, 0] - 0.01, 1.0)):
        O = np.stack([np.full(n, x0), Y.ravel(), Z.ravel()], 1)
        D = np.tile([dx, 0.0, 0.0], (n, 1))
        loc, idx, _ = mesh.ray.intersects_location(O, D, multiple_hits=False)
        depth = np.full(n, np.nan)
        depth[idx] = loc[:, 0]
        maps[name] = depth.reshape(Z.shape)
        log(f'  depth map {name}: {Z.shape}, coverage {np.mean(~np.isnan(depth)):.3f}')
    return maps


def save_depth_q16(path, maps):
    """Compact depth-map cache (committed, ~2.5 MB): the hit mask as bits, and per map the depth of the
    hit pixels quantised to 16 bits over its own range (step ~7e-5 mm at the default scale, far below the
    0.015 mm surface tolerance) and delta-coded in row-major order (mod 2^16), so deflate packs it well."""
    F, B = maps['front'], maps['back']
    m = ~np.isnan(F)
    if not np.array_equal(m, ~np.isnan(B)):
        m &= ~np.isnan(B)
    out = {'shape': np.array(F.shape), 'mask_bits': np.packbits(m), 'ys': maps['ys'], 'zs': maps['zs'],
           'res': np.float64(maps['res']), 'format': np.array('tree depth q16 v1')}
    for name, D in (('front', F), ('back', B)):
        v = D[m]
        lo, hi = float(v.min()), float(v.max())
        step = (hi - lo) / 65534.0
        q = np.round((v - lo) / step).astype(np.int64)
        out[name + '_lo'] = np.float64(lo)
        out[name + '_step'] = np.float64(step)
        out[name + '_dq'] = (np.diff(q, prepend=0) % 65536).astype(np.uint16)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    np.savez_compressed(path, **out)


def load_depth(path):
    """Front/back depth maps from the q16 cache, columns flipped to the front view (tree on the left,
    canopy sweeping right). Returns front, back (model x, NaN = miss), ys (row -> model y), res."""
    d = np.load(path)
    shape = tuple(int(v) for v in d['shape'])
    m = np.unpackbits(d['mask_bits'])[:shape[0] * shape[1]].reshape(shape).astype(bool)
    maps = []
    for name in ('front', 'back'):
        q = np.cumsum(d[name + '_dq'], dtype=np.uint16)          # wraps mod 2^16, undoing the delta code
        D = np.full(shape, np.nan)
        D[m] = float(d[name + '_lo']) + q.astype(np.float64) * float(d[name + '_step'])
        maps.append(D[:, ::-1].copy())
    return maps[0], maps[1], d['ys'], float(d['res'])


def load_depth_float(cache):
    """Full-precision maps (tree_front.npz / tree_back.npz as written by the first exploration), for checks."""
    f = np.load(os.path.join(cache, 'tree_front.npz'))
    b = np.load(os.path.join(cache, 'tree_back.npz'))
    return f['depth'][:, ::-1].copy(), b['depth'][:, ::-1].copy(), f['ys'], float(f['res'])


def fit_back_plane(B, m):
    """The Tripo tree has a flat back: fit x = a*r + b*c + d to the back depth near its mode."""
    rr, cc = np.nonzero(m)
    bv = B[m]
    sel = np.abs(bv - np.median(bv)) < 0.0015
    A = np.c_[rr[sel], cc[sel], np.ones(sel.sum())]
    coef, *_ = np.linalg.lstsq(A, bv[sel], rcond=None)
    return coef


# --------------------------------------------------------------------------------------------
# small raster helpers
# --------------------------------------------------------------------------------------------
def nearest_fill(X, valid):
    """Extend values of X from `valid` pixels to every pixel (nearest valid pixel)."""
    idx = ndi.distance_transform_edt(~valid, return_distances=False, return_indices=True)
    return X[tuple(idx)]


def masked_blur(X, mask, sigma):
    mf = mask.astype(np.float32)
    num = ndi.gaussian_filter(np.where(mask, X, 0).astype(np.float32), sigma)
    den = ndi.gaussian_filter(mf, sigma)
    return num / np.maximum(den, 1e-6)


def disk(rpx):
    r = int(math.ceil(rpx))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    return (xx * xx + yy * yy) <= rpx * rpx + 1e-9


def raster_polygon(poly, shape, px, pad_val=1):
    """Rasterize a shapely (Multi)Polygon given in image-mm coords (X = c*px, Y = -r*px)."""
    import cv2
    img = np.zeros(shape, np.uint8)
    SH = 8
    geoms = getattr(poly, 'geoms', [poly])
    for g in geoms:
        rings = [g.exterior] + list(g.interiors)
        pts = []
        for ring in rings:
            xy = np.asarray(ring.coords)
            col = xy[:, 0] / px
            row = -xy[:, 1] / px
            pts.append(np.round(np.c_[col, row] * (1 << SH)).astype(np.int32))
        cv2.fillPoly(img, pts, pad_val, lineType=cv2.LINE_8, shift=SH)
    return img.astype(bool)


# --------------------------------------------------------------------------------------------
# 2-3. silhouette -> vector outline
# --------------------------------------------------------------------------------------------
def clean_mask(F, r_cut, px, a):
    m = ~np.isnan(F)
    m[int(math.floor(r_cut)) + 3:, :] = False          # trunk cut (exact cut is done in vector)
    lab, n = ndi.label(m, structure=np.ones((3, 3)))
    sizes = ndi.sum(m, lab, range(1, n + 1))
    keep = 1 + int(np.argmax(sizes))
    dropped = float((sizes.sum() - sizes.max()) * px * px)
    m = lab == keep
    holes = ndi.binary_fill_holes(m) & ~m
    hl, hn = ndi.label(holes)
    hs = ndi.sum(holes, hl, range(1, hn + 1)) * px * px if hn else np.zeros(0)
    small = [i + 1 for i, s in enumerate(hs) if s < a.hole_area]
    m |= np.isin(hl, small)
    info = {'specks_removed_mm2': round(dropped, 4), 'source_holes_mm2': [round(float(s), 3) for s in hs],
            'source_holes_filled': len(small)}
    # left root flare: push the trunk's left edge outward just above the cut (concave fillet curve)
    flare = np.zeros_like(m)
    if a.flare_left > 0:
        hpx = a.flare_height / px
        r0 = int(math.floor(r_cut))
        trunk_cols = np.nonzero(m[r0 - 1])[0]
        cmin_base = trunk_cols.min()
        for r in range(int(r0 - hpx), r0 + 3):
            t = min(max((r_cut - r) / hpx, 0.0), 1.0)
            d = a.flare_left / px * (1.0 - t) ** 2
            cols = np.nonzero(m[r, : cmin_base + 200])[0]
            if len(cols) == 0 or d < 0.25:
                continue
            c_left = cols.min()
            c0 = int(round(c_left - d))
            flare[r, max(c0, 0):c_left] = True
        m |= flare
    info['flare_px'] = int(flare.sum())
    return m, flare, info


def thin_skeleton(m, px, a):
    """Medial axis of the source mask with spur pruning, and the thin part of it (width < min_width)."""
    from skimage.morphology import medial_axis
    sk, dist = medial_axis(m, return_distance=True, rng=0)   # seeded: deterministic output
    sk = prune_spurs(sk, int(round(a.spur_prune / px)))
    r = np.maximum(dist - 0.5, 0) * px                  # inscribed radius in mm
    half = a.min_width / 2.0
    eps = a.width_soft
    rp = 0.5 * (r + half + np.sqrt((r - half) ** 2 + eps ** 2))   # smooth max(r, half)
    thin = sk & (rp - r > 0.01)
    return sk, dist, thin, r, rp


def contour_polygon(m, px, sigma):
    from skimage import measure
    from shapely.geometry import Polygon
    pad = 4
    mp = np.pad(m.astype(np.float32), pad)
    sm = ndi.gaussian_filter(mp, sigma)
    cs = measure.find_contours(sm, 0.5)
    rings = []
    for c in cs:
        if len(c) < 8:
            continue
        rr = c[:, 0] - pad
        cc = c[:, 1] - pad
        xy = np.c_[cc * px, -rr * px]
        p = Polygon(xy)
        if not p.is_valid:
            p = p.buffer(0)
        if p.area > 0:
            rings.append((abs(p.area), xy))
    rings.sort(key=lambda t: -t[0])
    shell = Polygon(rings[0][1]).buffer(0)
    holes = []
    for _, xy in rings[1:]:
        q = Polygon(xy).buffer(0)
        if shell.contains(q.representative_point()):
            holes.append(q)
    from shapely.ops import unary_union
    P = shell.difference(unary_union(holes)) if holes else shell
    return P


def largest(g):
    if g.geom_type == 'Polygon':
        return g, 0.0
    parts = sorted(g.geoms, key=lambda q: -q.area)
    return parts[0], float(sum(q.area for q in parts[1:]))


def drop_small_holes(P, amin):
    from shapely.geometry import Polygon
    keep = [h for h in P.interiors if Polygon(h).area >= amin]
    return Polygon(P.exterior, keep), len(P.interiors) - len(keep)


def prune_spurs(sk, k):
    """Peel k pixels off every free end of a skeleton (leaf tips and twig ends are not stems)."""
    sk = sk.copy()
    ker = np.ones((3, 3), np.float32)
    for _ in range(k):
        nb = ndi.convolve(sk.astype(np.float32), ker, mode='constant') - 1.0
        ends = sk & (nb <= 1.0)
        if not ends.any():
            break
        sk &= ~ends
    return sk


def poly_raster(poly, res, pad=6):
    """Rasterize a polygon on its own grid; returns image and (X0, Y1) of pixel (0, 0)."""
    from shapely import affinity
    b = poly.bounds
    shp = (int((b[3] - b[1]) / res) + 2 * pad, int((b[2] - b[0]) / res) + 2 * pad)
    X0 = b[0] - pad * res
    Y1 = b[3] + pad * res
    q = affinity.translate(poly, -X0, -Y1)
    return raster_polygon(q, shp, res), X0, Y1


def thin_points_poly(S, a, res, hard=False):
    """Medial-axis points of polygon S whose local width is below min_width (free ends excluded).
    Returns X, Y, r (inscribed radius) and r' (target radius) in mm."""
    from skimage.morphology import medial_axis
    img, X0, Y1 = poly_raster(S, res)
    sk, dist = medial_axis(img, return_distance=True, rng=0)
    sk = prune_spurs(sk, int(round(a.spur_prune / res)))
    r = np.maximum(dist - 0.5, 0) * res            # EDT reaches the outside pixel centre: -0.5 px
    half = a.min_width / 2.0
    eps = a.width_soft
    if hard:      # later rounds: only real necks, so stems already at the target do not creep wider
        rp = np.maximum(r, half)
        sel = sk & (r < half - 0.01)
    else:
        rp = 0.5 * (r + half + np.sqrt((r - half) ** 2 + eps ** 2))
        sel = sk & (rp - r > 0.01)
    rr, cc = np.nonzero(sel)
    return X0 + cc * res, Y1 - rr * res, r[rr, cc], rp[rr, cc]


def add_discs(S, X, Y, RP, ycut):
    from shapely.ops import unary_union
    import shapely
    keep = Y - RP > ycut - 10                      # everything; the cut clip happens afterwards
    if not keep.any():
        return S
    discs = shapely.buffer(shapely.points(np.c_[X[keep], Y[keep]]), RP[keep], quad_segs=12)
    return unary_union([S] + list(discs))


def fix_gaps(S, a, clip, rounds=3):
    """Make every gap of the outline at least min_gap wide, without spikes:
      * pinches (narrow passages with open space on both sides) are widened: they are found as the
        bridges a closing with a 1.3x larger disc would make (a closing at min_gap itself only marks two
        spikes on the walls of a short pinch), and where the walls are closer than min_gap both walls are
        pushed back by (min_gap - width)/2 + 0.01 along the bridge, then the new corners are rounded
        (local opening with tip_radius);
      * dead ends (a notch or slot reached by the open space from one side only) are filled: the fill is
        bounded by one min_gap/2 arc tangent to both walls.
    Returns the new outline and a log of what was done."""
    from shapely.ops import unary_union
    g = a.min_gap / 2
    log_ = []

    def residue(S, r):
        comp = S.convex_hull.buffer(2.0).difference(S)
        op = comp.buffer(-r, quad_segs=32).buffer(r, quad_segs=32)
        res_ = comp.difference(op)
        return op, [q for q in getattr(res_, 'geoms', [res_]) if q.geom_type == 'Polygon' and q.area > a.gap_piece_min]

    def contacts(q, op):
        c = q.buffer(0.003).intersection(op)
        return len([k for k in getattr(c, 'geoms', [c]) if k.area > 1e-6])

    def wall_gap(q):
        w = S.intersection(q.buffer(0.005))
        walls = [k for k in getattr(w, 'geoms', [w]) if k.area > 1e-7]
        return min((walls[i].distance(walls[j]) for i in range(len(walls)) for j in range(i + 1, len(walls))),
                   default=None)

    for rnd in range(rounds):
        changed = False
        # 1. pinches
        op2, pieces2 = residue(S, a.pinch_detect * g)
        carves = []
        for q in pieces2:
            if contacts(q, op2) < 2:
                continue
            w = wall_gap(q)
            if w is None or w >= a.min_gap:
                continue
            d = g - w / 2 + 0.01
            carves.append(q.buffer(d, quad_segs=32))
            c = q.centroid
            log_.append({'round': rnd, 'xy_image_mm': [round(c.x, 2), round(c.y, 2)], 'width_mm': round(w, 3),
                         'action': f'pinch widened: walls pushed back {d:.3f} mm over {q.length / 2:.2f} mm'})
        if carves:
            C = unary_union(carves)
            S = clip(S.difference(C))
            opened = S.buffer(-a.tip_radius, quad_segs=32).buffer(a.tip_radius, quad_segs=32)
            rest = S.difference(opened)
            zone = C.buffer(0.6)
            near = [q for q in getattr(rest, 'geoms', [rest]) if q.geom_type == 'Polygon' and q.intersects(zone)]
            if near:
                S = clip(S.difference(unary_union(near).buffer(1e-5)))
            changed = True
        # 2. dead ends
        op, pieces = residue(S, g)
        fills = []
        for q in pieces:
            nc = contacts(q, op)
            c = q.centroid
            rec = {'round': rnd, 'xy_image_mm': [round(c.x, 2), round(c.y, 2)], 'area_mm2': round(q.area, 5),
                   'contacts': nc}
            if nc == 1:
                fills.append(q)
                rec['action'] = 'dead end filled'
            else:
                rec['action'] = 'left (pinch, next round)'
            log_.append(rec)
        if fills:
            S = clip(unary_union([S] + fills).buffer(1e-5).buffer(-1e-5))
            changed = True
        S, _ = drop_small_holes(S, a.hole_area)
        if not changed:
            break
    return S, log_


def build_outline(m, px, r_cut, sk_info, a):
    """Vector outline: contour -> [thicken thin stems, close narrow gaps, open (round tips)] x N.
    Every step is an exact shapely buffer / union."""
    from shapely.geometry import box
    import shapely
    info = {}
    P0 = contour_polygon(m, px, a.contour_sigma)
    ycut = -r_cut * px
    xb = P0.bounds
    half_plane = box(xb[0] - 5, ycut, xb[2] + 5, xb[3] + 5)

    def clip(g):
        g, lost = largest(g.intersection(half_plane))
        return g

    P0 = clip(P0)
    info['contour_area_mm2'] = round(P0.area, 3)
    g = a.min_gap / 2.0
    rt = a.tip_radius
    S = P0
    hist = []
    # first thickening uses the source-grid medial axis (the same points drive the profile stretch)
    sk, dist, thin, r, rp = sk_info
    tr_, tc_ = np.nonzero(thin)
    X, Y, RP = tc_ * px, -tr_ * px, rp[tr_, tc_]
    for it in range(a.outline_iters):
        if it > 0:
            X, Y, _, RP = thin_points_poly(S, a, a.width_res, hard=True)
        A0 = S.area
        S = clip(add_discs(S, X, Y, RP, ycut)) if len(X) else S
        A1 = S.area
        S = clip(S.buffer(g, quad_segs=16).buffer(-g, quad_segs=16))
        S, nh = drop_small_holes(S, a.hole_area)
        A2 = S.area
        # opening in every round, the last one too: closing leaves small convex cusps where its fill
        # arc meets the walls of a widening gap, and only an opening rounds those; it cannot create a
        # thin neck any more because every neck was thickened to >= min_width > 2 x tip_radius
        S = clip(S.buffer(-rt, quad_segs=16).buffer(rt, quad_segs=16))
        S, _ = drop_small_holes(S, a.hole_area)
        hist.append({'iter': it, 'thin_points': int(len(X)), 'thicken_mm2': round(A1 - A0, 3),
                     'close_mm2': round(A2 - A1, 3), 'open_mm2': round(A2 - S.area, 3)})
        if it == 0:
            P1 = S
        if DEBUG_SHAPES is not None:
            DEBUG_SHAPES[f'iter{it}'] = S
    info['iterations'] = hist
    # root tip at the trunk cut: the source root runs out along the cut line as a needle, so the cut
    # leaves a thin wedge there (about 0.9 mm tall at its end). Trim the parts of the base that are
    # thinner than 2 x root_radius back to a round cap (pieces of an opening, kept only near the cut).
    if a.root_radius > 0:
        rr_ = a.root_radius
        opened = S.buffer(-rr_, quad_segs=32).buffer(rr_, quad_segs=32)
        res_ = S.difference(opened)
        cut_pieces = [q for q in getattr(res_, 'geoms', [res_])
                      if q.geom_type == 'Polygon' and q.bounds[1] < ycut + 0.05 and q.centroid.y < ycut + a.root_zone]
        if cut_pieces:
            from shapely.ops import unary_union as _uu
            S = clip(S.difference(_uu(cut_pieces).buffer(1e-4)))
        info['root_trim_mm2'] = round(float(sum(q.area for q in cut_pieces)), 4)
    # narrow gaps left by the rounds above (variant B had a 0.48 mm pinch at (36.5, 33.9), where a
    # thickened branch passes under a leaf: a closing there only adds two spikes and the next opening
    # removes them again). Pieces of open space a min_gap disc cannot reach are fixed one by one.
    A3 = S.area
    if DEBUG_SHAPES is not None:
        DEBUG_SHAPES['before_gapfix'] = S
    S, ginfo = fix_gaps(S, a, clip)
    info['gap_fix'] = ginfo
    info['gap_fix_net_mm2'] = round(S.area - A3, 4)
    # simplify, but keep the exact cut-edge vertices: Douglas-Peucker would otherwise replace the straight
    # trunk-bottom edge by a chord between two corner-arc points (tilted, a few microns above y = 0)
    from shapely.ops import unary_union
    b_ = S.bounds
    band = box(b_[0] - 1, ycut - 1, b_[2] + 1, ycut + 0.6)
    S = unary_union([S.simplify(a.outline_tol, preserve_topology=True).difference(band), S.intersection(band)])
    S, _ = largest(S)
    S = S.simplify(0.0003, preserve_topology=True)      # drops near-duplicate vertices at the band seam
    # the buffers leave the cut edge up to ~0.1 micron off y = ycut: put it exactly on the line
    from shapely.geometry import Polygon

    def snap(ring):
        xy = np.asarray(ring.coords)[:-1].copy()
        xy[xy[:, 1] < ycut + 1e-4, 1] = ycut
        return xy
    S2 = Polygon(snap(S.exterior), [snap(h) for h in S.interiors])
    S, _ = largest(S2 if S2.is_valid else S2.buffer(0))
    S = shapely.geometry.polygon.orient(S, 1.0)        # exterior CCW, holes CW
    info['final_area_mm2'] = round(S.area, 3)
    info['holes'] = len(S.interiors)
    added = S.difference(P0)
    info['added_vs_contour_mm2'] = round(added.area, 3)
    info['removed_vs_contour_mm2'] = round(P0.difference(S).area, 3)
    return P0, P1, S, added, info


# --------------------------------------------------------------------------------------------
# 4. heights
# --------------------------------------------------------------------------------------------
def smoothstep(t):
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def base_lean_profile(R, orig, px, r_cut, x0, a, u, y_m):
    """Lean of the trunk flutes (deg from vertical, + = top leans left) across the trunk at the anchor row
    y_m, as a function of canonical x = u: orientation of the relief's structure tensor summed over a 1 mm
    strip from y_m - 0.3 up (smoothed along x). The flutes converge upward, so the lean grows from
    about 25 deg at the left edge to 45 deg on the right flank; near the left edge the rim falloff sets it,
    so the rounded flank follows the edge."""
    H, W = R.shape
    G = ndi.gaussian_filter(R.astype(np.float64), 1.0)
    gr, gc = np.gradient(G)
    gX, gY = gc, -gr                                  # image-mm axes (+y up)
    rows = np.arange(int(round(r_cut - (y_m + 0.7) / px)), int(round(r_cut - (y_m - 0.3) / px)) + 1)
    wgt = orig[rows].astype(np.float64)
    J = [np.sum(wgt * q[rows], 0) for q in (gX * gX, gX * gY, gY * gY)]
    sg = a.base_dir_sigma / px
    J = [ndi.gaussian_filter1d(j, sg) for j in J]
    n = ndi.gaussian_filter1d(wgt.sum(0), sg)
    lean = np.degrees(0.5 * np.arctan2(2 * J[1], J[0] - J[2]))
    coh = np.sqrt((J[0] - J[2]) ** 2 + 4 * J[1] ** 2) / np.maximum(J[0] + J[2], 1e-12)
    cols = np.arange(W)
    ok = n > 0.5 * len(rows)                         # columns mostly inside the trunk on the strip
    Xc = cols * px - x0
    ok &= np.abs(Xc) < a.base_halfwidth
    xs, ls = Xc[ok], lean[ok]
    return np.interp(u, xs, ls), float(np.median(coh[ok])), (float(xs.min()), float(xs.max()))


def extend_trunk_base(R, orig, px, r_cut, x0, a):
    """The Tripo trunk droops in the last ~1.5 mm above the cut, where it flares into roots that run down
    and behind. Continue the trunk straight down instead. Each pixel p samples the relief at a point q on
    the straight fan line through p that follows the local flute direction (measured at the anchor row
    y_m = (y_lo + y_hi) / 2): below y_lo, q is on the anchor row (the relief is constant along the flutes,
    so the rounded, fluted cross-section runs on down to the cut); above y_hi, q = p; in between, q slides
    from y_m up to p along a C1 soft-max curve. A warp, not a cross-fade, so no flute is doubled where the
    source flutes curve off toward the roots. Also defines the relief a little below the cut (a virtual
    continuation), so that the later steps do not treat the cut as a rim."""
    y_lo, y_hi = a.base_band
    y_m = 0.5 * (y_lo + y_hi)
    dy = y_hi - y_lo
    H, W = R.shape
    u = np.arange(-a.base_halfwidth - 6.0, a.base_halfwidth + 6.0, px / 4)
    lean, coh, span = base_lean_profile(R, orig, px, r_cut, x0, a, u, y_m)
    tan_th = np.tan(np.radians(lean))
    cols = np.arange(int(math.floor((x0 - a.base_halfwidth) / px)), int(math.ceil((x0 + a.base_halfwidth) / px)) + 1)
    Xc = cols * px - x0
    out = R.copy()
    r_start = int(math.floor(r_cut - y_hi / px)) - 1
    r_end = int(math.ceil(r_cut + a.base_below / px))
    min_slope = np.inf
    for r in range(max(r_start, 0), min(r_end, H - 1) + 1):
        y = (r_cut - r) * px
        if y >= y_hi:
            continue
        yq = y_m if y <= y_lo else y_m + (y - y_lo) ** 2 / (2.0 * dy)   # sample height (C1, monotone)
        xu = u + (y_m - y) * tan_th                   # where each fan line crosses this row
        if y >= 0:                                    # fan spread inside the trunk (1 = parallel lines)
            insp = (xu >= Xc[0]) & (xu <= Xc[-1]) & (u >= span[0]) & (u <= span[1])
            min_slope = min(min_slope, float(np.min(np.diff(xu)[insp[:-1]])) / (u[1] - u[0]))
        xu = np.maximum.accumulate(xu)                # fan lines never cross
        uq = np.interp(Xc, xu, u)
        xq = uq + (y_m - yq) * np.interp(uq, u, tan_th)
        out[r, cols] = ndi.map_coordinates(R, [np.full(len(cols), r_cut - yq / px), (xq + x0) / px],
                                           order=1, mode='nearest')
    sel = (u >= span[0]) & (u <= span[1])
    info = {'band_mm': [y_lo, y_hi], 'anchor_row_mm': y_m, 'flute_lean_deg_on_anchor_row': {
                'left_edge': round(float(lean[sel][0]), 1), 'min': round(float(lean[sel].min()), 1),
                'median': round(float(np.median(lean[sel])), 1), 'max': round(float(lean[sel].max()), 1),
                'right_edge': round(float(lean[sel][-1]), 1)},
            'trunk_x_on_anchor_row_mm': [round(span[0], 3), round(span[1], 3)],
            'flute_coherence_median': round(coh, 3), 'fan_min_dx_du_above_cut': round(min_slope, 3)}
    return out.astype(np.float32), info


def build_heights(F, plane, m_src, region, flare, gapmask, sk_info, px, scale, r_cut, x0, a):
    """Returns Z (mm thickness) on the full grid, extended outside `region` for sampling."""
    import cv2
    H, Wd = F.shape
    rr, cc = np.mgrid[0:H, 0:Wd]
    back = plane[0] * rr + plane[1] * cc + plane[2]
    R = np.where(m_src, (F - back) * scale, np.nan).astype(np.float32)
    orig = m_src & ~flare
    # outlier clean-up: replace isolated spikes / pits (seams) by the local median
    Rf = nearest_fill(np.nan_to_num(R), orig).astype(np.float32)
    med = ndi.median_filter(Rf, size=5)
    spike = orig & (np.abs(Rf - med) > a.spike_thresh)
    Rf = np.where(spike, med, Rf)
    # edge-preserving denoise: bilateral (keeps overlaps, rims and midrib creases)
    Rd = Rf.copy()
    for _ in range(a.bilateral_iters):
        Rd = cv2.bilateralFilter(Rd, d=-1, sigmaColor=a.bilateral_sr, sigmaSpace=a.bilateral_ss)
        Rd = np.where(orig, Rd, Rf)
        Rd = nearest_fill(Rd, orig).astype(np.float32)
    Rext = Rd
    # trunk base: keep the full trunk relief down to the cut (the source droops into its roots there)
    base_info = None
    region_d = region                                   # region for distances: the cut is not a rim
    if a.base_band[1] > a.base_band[0] >= 0:
        Rext, base_info = extend_trunk_base(Rext, orig, px, r_cut, x0, a)
        rows_in = np.nonzero(region.any(1))[0]
        r_last = rows_in.max()
        r_end = min(int(math.ceil(r_cut + a.base_below / px)), H - 1)
        region_d = region.copy()
        region_d[r_last + 1:r_end + 1] = region[r_last][None, :] & (np.abs(cc[0] * px - x0) < a.base_halfwidth)
    # thin stems: stretch the round cross-section to the new width instead of padding it
    sk, dist, thin, r, rp = sk_info
    if thin.any():
        dd, (ir, ic) = ndi.distance_transform_edt(~thin, return_indices=True)
        rpc = rp[ir, ic]
        rc = r[ir, ic]
        zone = region & (dd * px <= rpc + px)
        sr = ir + (rr - ir) * (rc / rpc)
        sc = ic + (cc - ic) * (rc / rpc)
        vals = ndi.map_coordinates(Rext, [sr[zone], sc[zone]], order=1, mode='nearest')
        Rext = Rext.copy()
        Rext[zone] = vals
        stretch_zone = zone
    else:
        stretch_zone = np.zeros_like(region)
    # closed gaps: fill at the lower of the neighbouring heights (reads as a groove, prints solid)
    if gapmask.any():
        k = disk(a.min_gap / px)
        low = ndi.grey_erosion(np.where(orig, Rext, 99.0), footprint=k)
        low = np.where(low > 90, Rext, low)
        Rext = np.where(gapmask & ~orig & ~stretch_zone, np.minimum(Rext, low), Rext)
    # root flare: taper the trunk edge height outward, then smooth it into the trunk
    if flare.any():
        dfl = ndi.distance_transform_edt(~orig) * px
        Rext = np.where(flare, Rext - a.flare_slope * dfl, Rext)
        fz = ndi.binary_dilation(flare, iterations=6) & region
        Rext = np.where(fz, masked_blur(Rext, region_d, 4.0), Rext)
    # small blend across every filled area so no step remains at the seams
    added = region & ~orig
    if added.any():
        band = ndi.binary_dilation(added | stretch_zone, iterations=2) & region
        Rb = masked_blur(Rext, region_d, 1.0)
        Rext = np.where(band, Rb, Rext)
    # rim: the outermost pixels of the depth map are grazing-angle samples (noisy); take the rim
    # height from just inside so the top edge of the walls runs clean
    din_px = ndi.distance_transform_edt(region_d)
    if a.rim_band > 0:
        # first-order normalized convolution from a smoothed surface further in: every band pixel
        # gets the Gaussian-weighted average of the tangent-plane predictions of the good pixels
        # around it (smooth along the edge, continues the rim slope, no lip and no streaks)
        deep = (region_d & (din_px >= a.rim_band + 1.5)).astype(np.float64)
        band = region_d & (din_px < a.rim_band)
        Gs = masked_blur(Rext, region_d & (din_px >= a.rim_band), 1.0).astype(np.float64)
        gy, gx = np.gradient(Gs)
        sg = 2.0
        Wt = ndi.gaussian_filter(deep, sg)
        A = ndi.gaussian_filter(deep * (Gs - gx * cc - gy * rr), sg)
        Bx = ndi.gaussian_filter(deep * gx, sg)
        By = ndi.gaussian_filter(deep * gy, sg)
        A0 = ndi.gaussian_filter(deep * Gs, sg) / np.maximum(Wt, 1e-9)
        v = (A + cc * Bx + rr * By) / np.maximum(Wt, 1e-9)
        v = np.clip(v, A0 - a.rim_max_drop, A0)
        # blend from the extrapolation (outer band) back into the surface over rim_blend pixels: a hard
        # switch leaves a step that wanders along the rim, about 0.08 mm inside every top edge
        wb = np.clip((a.rim_band + a.rim_blend - din_px) / max(a.rim_blend, 1e-6), 0.0, 1.0)
        wb = np.where(region_d & (Wt > 1e-3), wb, 0.0)
        Rext = (wb * v + (1.0 - wb) * Rext).astype(np.float32)
        # the same smooth extrapolation a few pixels beyond the outline: the outline vertices sit between
        # pixel centres and sample the grid bilinearly, and a nearest-pixel extension there would print
        # its Voronoi staircase into the rim as tiny facets and pits
        ext_band = ~region_d & (ndi.distance_transform_edt(~region_d) <= a.rim_ext) & (Wt > 1e-4)
        Rext = np.where(ext_band, v, Rext).astype(np.float32)
    else:
        ext_band = np.zeros_like(region)
    # band-pass detail boost (midribs, fluting, leaf rims) computed inside the final region only
    s1 = a.detail_s1 / px
    s2 = a.detail_s2 / px
    G1 = masked_blur(Rext, region_d, s1) if s1 > 0.2 else Rext
    G2 = masked_blur(Rext, region_d, s2)
    detail = G1 - G2
    # attenuate the boost right at the silhouette (the rim drop is already strong there)
    din = ndi.distance_transform_edt(region_d) * px
    w_edge = np.clip(din / a.detail_edge, 0, 1) ** 2
    Renh = Rext + a.detail_gain * detail * w_edge
    # map to thickness: base at the rim level, gain above it
    Z = a.base + a.gain * (Renh - a.rim_relief)
    # thickness floors: thicker where leaves hang beyond the plaque
    X = cc * px - x0
    Y = (r_cut - rr) * px
    if a.floor_in != a.floor_out:           # zoning only on request (it ties the tree to one card layout)
        pr = a.plaque_rect
        inside = (X > pr[0]) & (X < pr[2]) & (Y > pr[1]) & (Y < pr[3])
        fl = np.where(inside, a.floor_in, a.floor_out).astype(np.float32)
        fl = ndi.gaussian_filter(fl, 1.0 / px)
    else:
        fl = np.float32(a.floor_out)
    # soft floor: identity above floor + d, exponential approach to the floor below it, so low
    # areas (root flare, petiole ends) keep a gentle shape instead of a flat clamp
    d_ = a.floor_soft
    Z = np.where(Z >= fl + d_, Z, fl + d_ * np.exp(np.minimum(Z - fl - d_, 0) / d_))
    # soft cap toward max_thick
    c0 = a.max_thick - a.cap_soft
    over = Z > c0
    Z = np.where(over, c0 + a.cap_soft * np.tanh((Z - c0) / a.cap_soft), Z)
    Z = Z.astype(np.float32)
    Zext = nearest_fill(Z, region_d | ext_band).astype(np.float32)
    stats = {'spikes_replaced_px': int(spike.sum()), 'stretch_zone_px': int(stretch_zone.sum()),
             'added_px': int(added.sum()), 'trunk_base_extension': base_info}
    return Zext, Rext, stats


# --------------------------------------------------------------------------------------------
# 5. adaptive constrained Delaunay triangulation
# --------------------------------------------------------------------------------------------
def sample_bicubic(Zext, X, Y, px, order=1):
    """Sample the thickness grid at image-mm points. Bilinear by default: a B-spline would ring
    at the leaf-overlap cliffs and put vertices above / below the true surface."""
    col = X / px
    row = -Y / px
    return ndi.map_coordinates(Zext, [row, col], order=order, mode='nearest', prefilter=order > 1)


def raster_tri_ids(V, T, shape):
    """Triangle id per pixel centre (row, col) for a 2D triangulation given in pixel coords
    (V[:, 0] = col, V[:, 1] = row). Vectorised by bounding-box size. Returns ids (-1 = none)
    and barycentric weights (l1, l2, l3)."""
    H, W = shape
    ids = np.full(H * W, -1, np.int64)
    tri = V[T]
    x0 = np.floor(tri[:, :, 0].min(1)).astype(np.int64)
    x1 = np.ceil(tri[:, :, 0].max(1)).astype(np.int64)
    y0 = np.floor(tri[:, :, 1].min(1)).astype(np.int64)
    y1 = np.ceil(tri[:, :, 1].max(1)).astype(np.int64)
    span = np.maximum(x1 - x0, y1 - y0) + 1
    lev = np.ceil(np.log2(np.maximum(span, 1))).astype(int)
    for L in np.unique(lev):
        s = 1 << L
        sel = np.nonzero(lev == L)[0]
        dy, dx = np.mgrid[0:s, 0:s]
        dx = dx.ravel()
        dy = dy.ravel()
        for ch in np.array_split(sel, max(1, len(sel) * s * s // 4_000_000 + 1)):
            if len(ch) == 0:
                continue
            cx = x0[ch][:, None] + dx[None, :]
            cy = y0[ch][:, None] + dy[None, :]
            t = tri[ch]
            ax, ay = t[:, 0, 0:1], t[:, 0, 1:2]
            bx, by = t[:, 1, 0:1], t[:, 1, 1:2]
            qx, qy = t[:, 2, 0:1], t[:, 2, 1:2]
            w0 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            w1 = (qx - bx) * (cy - by) - (qy - by) * (cx - bx)
            w2 = (ax - qx) * (cy - qy) - (ay - qy) * (cx - qx)
            inside = ((w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)) | ((w0 <= 1e-9) & (w1 <= 1e-9) & (w2 <= 1e-9))
            inside &= (cx >= 0) & (cx < W) & (cy >= 0) & (cy < H)
            k, j = np.nonzero(inside)
            ids[cy[k, j] * W + cx[k, j]] = ch[k]
    return ids.reshape(H, W)


def bary(V, T, tid, PXc, PYc):
    tt = T[tid]
    x1, y1 = V[tt[:, 0], 0], V[tt[:, 0], 1]
    x2, y2 = V[tt[:, 1], 0], V[tt[:, 1], 1]
    x3, y3 = V[tt[:, 2], 0], V[tt[:, 2], 1]
    det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    l1 = ((y2 - y3) * (PXc - x3) + (x3 - x2) * (PYc - y3)) / det
    l2 = ((y3 - y1) * (PXc - x3) + (x1 - x3) * (PYc - y3)) / det
    return tt, l1, l2, 1 - l1 - l2


def refine_rings(P, Zext, px, tol, max_seg):
    """Outline rings as vertex arrays, split where the height along the edge deviates > tol."""
    rings = [np.asarray(P.exterior.coords)[:-1]] + [np.asarray(h.coords)[:-1] for h in P.interiors]
    out = []
    for ring in rings:
        # drop vertices closer than 5 microns to the previous one (union seams leave such pairs; they
        # would only make sliver triangles in the walls and chamfer)
        keep = [0]
        for i in range(1, len(ring)):
            if np.linalg.norm(ring[i] - ring[keep[-1]]) > 0.005:
                keep.append(i)
        if np.linalg.norm(ring[keep[-1]] - ring[0]) <= 0.005 and len(keep) > 3:
            keep.pop()
        pts = ring[keep]
        for _ in range(12):
            a_ = pts
            b_ = np.roll(pts, -1, axis=0)
            L = np.linalg.norm(b_ - a_, axis=1)
            za = sample_bicubic(Zext, a_[:, 0], a_[:, 1], px)
            zb = sample_bicubic(Zext, b_[:, 0], b_[:, 1], px)
            err = np.zeros(len(a_))
            for t in (0.25, 0.5, 0.75):
                q = a_ + (b_ - a_) * t
                zq = sample_bicubic(Zext, q[:, 0], q[:, 1], px)
                err = np.maximum(err, np.abs(zq - (za + (zb - za) * t)))
            split = (err > tol) | (L > max_seg)
            if not split.any():
                break
            mid = (a_ + b_) / 2
            pts = np.insert(pts, np.nonzero(split)[0] + 1, mid[split], axis=0)
        out.append(pts)
    return out


def triangulate_top(P, rings, Zext, region, px, a):
    import triangle as tr
    verts = np.concatenate(rings)
    segs = []
    off = 0
    for ring in rings:
        n = len(ring)
        i = np.arange(n) + off
        segs.append(np.c_[i, np.roll(i, -1)])
        off += n
    segs = np.concatenate(segs)
    nb = len(verts)
    holes = []
    from shapely.geometry import Polygon
    for h in P.interiors:
        holes.append(np.asarray(Polygon(h).representative_point().coords)[0])
    # pixel centres to measure the error on (inside, at least half a pixel from the outline)
    din = ndi.distance_transform_edt(region)
    pr, pc = np.nonzero(region & (din >= 1.0))
    PX = pc * px
    PY = -pr * px
    PZ = Zext[pr, pc]
    cand_ok = din[pr, pc] >= 1.0
    # seed: coarse grid of interior points
    gs = a.seed_spacing
    sel = (pr % int(round(gs / px)) == 0) & (pc % int(round(gs / px)) == 0) & cand_ok
    steiner = np.c_[PX[sel], PY[sel]]
    used = np.zeros(len(pr), bool)
    used[sel] = True
    opts = f'pYq{a.min_angle:g}Q' if a.min_angle > 0 else 'pYQ'
    hist = []
    for it in range(a.max_iters):
        pts = np.concatenate([verts, steiner]) if len(steiner) else verts
        tin = {'vertices': pts, 'segments': segs}
        if holes:
            tin['holes'] = np.array(holes)
        t = tr.triangulate(tin, opts)
        V = t['vertices']
        T = t['triangles']
        Z = sample_bicubic(Zext, V[:, 0], V[:, 1], px)
        idimg = raster_tri_ids(np.c_[V[:, 0] / px, -V[:, 1] / px], T, region.shape)
        tid = idimg[pr, pc]
        ok = tid >= 0
        tt, l1, l2, l3 = bary(V, T, tid[ok], PX[ok], PY[ok])
        zi = l1 * Z[tt[:, 0]] + l2 * Z[tt[:, 1]] + l3 * Z[tt[:, 2]]
        err = np.zeros(len(pr))
        err[ok] = np.abs(zi - PZ[ok])
        emax = float(err.max())
        e99 = float(np.percentile(err, 99.9))
        hist.append((it, len(T), emax, e99))
        log(f'  refine it {it}: {len(V)} verts, {len(T)} tris, max err {emax:.4f}, p99.9 {e99:.4f}')
        if emax <= a.tol or len(T) >= a.target_tris:
            break
        # worst pixel per triangle above tolerance
        bad = (err > a.tol) & cand_ok & ~used & ok
        if not bad.any():
            break
        idx = np.nonzero(bad)[0]
        order = np.lexsort((-err[idx], tid[idx]))
        idx = idx[order]
        first = np.ones(len(idx), bool)
        first[1:] = tid[idx][1:] != tid[idx][:-1]
        new = idx[first]
        used[new] = True
        steiner = np.concatenate([steiner, np.c_[PX[new], PY[new]]]) if len(steiner) else np.c_[PX[new], PY[new]]
    assert np.allclose(V[:nb], verts), 'triangle moved boundary vertices'
    return V, T, Z, nb, hist, err


# --------------------------------------------------------------------------------------------
# 6. solid assembly
# --------------------------------------------------------------------------------------------
def chamfer_field(P, a):
    """Width-adaptive chamfer size on a raster of the top outline P: c_min on stems about 1 mm wide
    (so their glue face stays about 0.7 mm wide), rising to c_max from 1.6 mm wide features up.
    Local half width = inscribed radius at the nearest medial-axis point (spurs pruned so raster
    stairs do not leak small radii onto wide walls). A min-filter lets the small stem chamfer run on a
    little into the wider part it joins, and a Gaussian keeps |dc/ds| small (chamfer stays ~45 deg)."""
    from skimage.morphology import medial_axis
    res = a.width_res
    img, X0, Y1 = poly_raster(P, res)
    sk, dist = medial_axis(img, return_distance=True, rng=0)
    sk = prune_spurs(sk, int(round(0.3 / res)))
    lt = nearest_fill(np.where(sk, np.maximum(dist - 0.5, 0) * res, 0.0), sk)
    c = np.clip(a.chamfer_min + (lt - a.chamfer_w0 / 2) * (a.chamfer - a.chamfer_min) / ((a.chamfer_w1 - a.chamfer_w0) / 2),
                a.chamfer_min, a.chamfer)
    c = ndi.grey_erosion(c, size=int(round(0.9 / res)) | 1)
    c = ndi.gaussian_filter(c, 0.3 / res)
    return np.clip(c, a.chamfer_min, a.chamfer).astype(np.float32), X0, Y1, res


def ring_normals(ring, h=0.03):
    """Inward unit normals of a ring with the solid on its left (exterior CCW, holes CW), from the chord
    between the points +/- h mm away along the ring: robust to near-duplicate vertices (whose own edge
    direction is noise). The outline is smooth after the rounding steps, so no miter is needed."""
    seg = np.linalg.norm(np.roll(ring, -1, axis=0) - ring, axis=1)
    s = np.r_[0.0, np.cumsum(seg)]
    L = s[-1]
    closed = np.r_[ring, ring[:1]]

    def at(t):
        t = np.mod(t, L)
        return np.c_[np.interp(t, s, closed[:, 0]), np.interp(t, s, closed[:, 1])]
    t = at(s[:-1] + h) - at(s[:-1] - h)
    n = np.c_[-t[:, 1], t[:, 0]]
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)


def glue_rings(rings, cimg, X0, Y1, res, a):
    """Glue-face (z = 0) rings: every top-ring vertex moved inward along the outline normal by its own
    chamfer size c_i (convex corners have radius >= tip_radius > c_max, so the offset cannot fold).
    c_i is sampled just inside the wall, smoothed along the ring and slope-limited."""
    out = []
    for ring in rings:
        b = ring_normals(ring)
        q = ring + 0.04 * b
        ci = ndi.map_coordinates(cimg, [(Y1 - q[:, 1]) / res, (q[:, 0] - X0) / res], order=1, mode='nearest')
        seg = np.linalg.norm(np.roll(ring, -1, axis=0) - ring, axis=1)
        s = np.r_[0.0, np.cumsum(seg)[:-1]]
        L = seg.sum()
        # smooth along arc length (circular, sigma 0.25 mm), on a uniform 0.02 mm resampling
        su = np.arange(0, L, 0.02)
        cu = np.interp(su, np.r_[s, L], np.r_[ci, ci[:1]])
        cu = ndi.gaussian_filter1d(cu, 0.25 / 0.02, mode='wrap')
        # slope limit |dc/ds| <= chamfer_slope (forward/backward passes, circular)
        k = a.chamfer_slope * 0.02
        for _ in range(2):
            for i in range(1, len(cu)):
                cu[i] = min(cu[i], cu[i - 1] + k)
            cu[0] = min(cu[0], cu[-1] + k)
            for i in range(len(cu) - 2, -1, -1):
                cu[i] = min(cu[i], cu[i + 1] + k)
            cu[-1] = min(cu[-1], cu[0] + k)
        ci = np.clip(np.interp(s, su, cu), a.chamfer_min, a.chamfer)
        out.append((ring + ci[:, None] * b, ci))
    return out


def assemble(V, T, Z, rings, a, cimg, X0, Y1, res):
    """Solid: top relief, vertical walls down to z = c_i, a ~45 deg chamfer from (p_i, c_i) to the glue
    vertex g_i at z = 0 (one glue vertex per top-ring vertex: a plain quad strip) and a planar bottom."""
    import triangle as tr
    from shapely.geometry import Polygon
    nt = len(V)
    gl = glue_rings(rings, cimg, X0, Y1, res, a)
    G = Polygon(gl[0][0], [g for g, _ in gl[1:]])
    if not G.is_valid or len(G.interiors) != len(rings) - 1:
        from shapely.validation import explain_validity
        raise RuntimeError('glue face invalid: ' + explain_validity(G))
    nwall = sum(len(r) for r in rings)
    verts = [np.c_[V, Z]]
    wall_faces, cham_faces = [], []
    off = 0
    for ring, (g, ci) in zip(rings, gl):
        n = len(ring)
        ti = np.arange(n) + off                        # top boundary vertices = first nb of V
        wi = ti + nt
        gi = ti + nt + nwall
        off += n
        wall_faces.append(np.c_[ti, wi, np.roll(wi, -1)])
        wall_faces.append(np.c_[ti, np.roll(wi, -1), np.roll(ti, -1)])
        cham_faces.append(np.c_[wi, gi, np.roll(gi, -1)])
        cham_faces.append(np.c_[wi, np.roll(gi, -1), np.roll(wi, -1)])
    verts.append(np.concatenate([np.c_[r, ci] for r, (g, ci) in zip(rings, gl)]))
    bverts = np.concatenate([g for g, _ in gl])
    verts.append(np.c_[bverts, np.zeros(len(bverts))])
    segs, off = [], 0
    for g, _ in gl:
        i = np.arange(len(g)) + off
        segs.append(np.c_[i, np.roll(i, -1)])
        off += len(g)
    holes = [np.asarray(Polygon(g).representative_point().coords)[0] for g, _ in gl[1:]]
    tin = {'vertices': bverts, 'segments': np.concatenate(segs)}
    if holes:
        tin['holes'] = np.array(holes)
    tb = tr.triangulate(tin, 'pQ')
    assert len(tb['vertices']) == len(bverts), 'bottom triangulation added vertices'
    bot_faces = tb['triangles'][:, ::-1] + nt + nwall
    allv = np.concatenate(verts)
    wf = np.concatenate(wall_faces)
    cf = np.concatenate(cham_faces)
    allf = np.concatenate([T, wf, cf, bot_faces])
    groups = {'top': (0, len(T))}
    s_ = len(T)
    groups['wall'] = (s_, s_ + len(wf))
    s_ += len(wf)
    groups['chamfer'] = (s_, s_ + len(cf))
    s_ += len(cf)
    groups['bottom'] = (s_, s_ + len(bot_faces))
    # drop vertices triangle left unused (duplicate Steiner candidates)
    usedv = np.zeros(len(allv), bool)
    usedv[allf.ravel()] = True
    remap = np.cumsum(usedv) - 1
    allv = allv[usedv]
    allf = remap[allf]
    cvals = np.concatenate([ci for _, ci in gl])
    return allv, allf, [g for g, _ in gl], groups, cvals


# --------------------------------------------------------------------------------------------
# 7. checks
# --------------------------------------------------------------------------------------------
def skeleton_end_distance(sk, px, limit):
    """Geodesic distance (mm) along an 8-connected skeleton from the nearest skeleton end point."""
    import scipy.sparse as sp
    from scipy.sparse.csgraph import dijkstra
    ys, xs = np.nonzero(sk)
    n = len(ys)
    idx = -np.ones(sk.shape, np.int64)
    idx[ys, xs] = np.arange(n)
    rows, cols, wts = [], [], []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            y2, x2 = ys + dy, xs + dx
            ok = (y2 >= 0) & (x2 >= 0) & (y2 < sk.shape[0]) & (x2 < sk.shape[1])
            j = np.full(n, -1)
            j[ok] = idx[y2[ok], x2[ok]]
            ok = j >= 0
            rows.append(np.nonzero(ok)[0])
            cols.append(j[ok])
            wts.append(np.full(ok.sum(), math.hypot(dy, dx) * px))
    G = sp.csr_matrix((np.concatenate(wts), (np.concatenate(rows), np.concatenate(cols))), shape=(n + 1, n + 1))
    deg = np.diff(G.indptr)[:n]
    ends = np.nonzero(deg <= 1)[0]
    # virtual source node n linked to every end point
    G = G + sp.csr_matrix((np.full(len(ends), 1e-9), (np.full(len(ends), n), ends)), shape=(n + 1, n + 1))
    d = dijkstra(G, directed=True, indices=n, limit=limit)[:n]
    return ys, xs, d


def width_histogram(poly, px=0.02, end_skip=1.2):
    """Local width along the medial axis of a polygon (2 x inscribed radius), in mm. Skeleton points
    within `end_skip` mm (along the skeleton) of a free end are left out: tips are not stems (their
    width goes to 0 at the end; they are rounded to tip_radius instead). Geodesic distance is used, not
    spur peeling, so a small fork at a tip cannot shorten the excluded length."""
    from skimage.morphology import medial_axis
    b = poly.bounds
    pad = 4
    shp = (int((b[3] - b[1]) / px) + 2 * pad, int((b[2] - b[0]) / px) + 2 * pad)
    from shapely import affinity
    q = affinity.translate(poly, -b[0] + pad * px, -b[3] - pad * px)
    img = raster_polygon(q, shp, px)
    # true medial axis (ridge of the EDT; a thinning skeleton such as skeletonize() drifts off the ridge
    # in asymmetric places and then under-reports the width by up to 0.1 mm) + EDT for the radius
    sk, dist = medial_axis(img, return_distance=True, rng=0)
    rr, cc, dend = skeleton_end_distance(sk, px, end_skip + 0.1)
    keep = ~(dend < end_skip)
    rr, cc = rr[keep], cc[keep]
    # EDT reaches the centre of the first outside pixel, on average half a pixel beyond the outline
    wv = (2 * dist[rr, cc] - 1) * px
    bins = [0, 0.4, 0.6, 0.8, 0.9, 1.0, 1.1, 1.2, 1.5, 2.0, 3.0, 5.0, 100]
    h, _ = np.histogram(wv, bins)
    o = np.argsort(wv)[:8]
    where = [[round(cc[i] * px + b[0] - pad * px, 2), round(-(rr[i] * px) + b[3] + pad * px, 2),
              round(float(wv[i]), 3)] for i in o]
    return {'bins_mm': bins, 'counts': h.tolist(), 'end_skip_mm': end_skip, 'min_mm': round(float(wv.min()), 3),
            'p1_mm': round(float(np.percentile(wv, 1)), 3), 'thinnest_xy_w': where,
            'method': f'medial axis on a {px} mm raster, points within {end_skip} mm (geodesic) of a free end excluded'}


def convex_radius_stats(poly, step=0.01, k=20):
    """Radius of the outline at its convex corners (leaf tips etc.): circumradius of points k*step
    apart along the resampled ring, convex = left turn (solid on the left for every ring)."""
    import shapely
    radii = []
    where = []
    for ring in [poly.exterior] + list(poly.interiors):
        r = shapely.segmentize(ring, step)
        xy = np.asarray(r.coords)[:-1]
        a_ = np.roll(xy, k, axis=0)
        c_ = np.roll(xy, -k, axis=0)
        ab = np.linalg.norm(xy - a_, axis=1)
        bc = np.linalg.norm(c_ - xy, axis=1)
        ca = np.linalg.norm(c_ - a_, axis=1)
        cross = (xy[:, 0] - a_[:, 0]) * (c_[:, 1] - a_[:, 1]) - (xy[:, 1] - a_[:, 1]) * (c_[:, 0] - a_[:, 0])
        R = ab * bc * ca / np.maximum(2 * np.abs(cross), 1e-12)
        conv = cross > 1e-9
        radii.append(R[conv])
        where.append(xy[conv])
    R = np.concatenate(radii)
    W_ = np.concatenate(where)
    o = np.argsort(R)[:5]
    return {'min_mm': round(float(R.min()), 3), 'p1_mm': round(float(np.percentile(R, 1)), 3),
            'tightest_xy_r': [[round(float(W_[i, 0]), 2), round(float(W_[i, 1]), 2), round(float(R[i]), 3)] for i in o],
            'method': f'circumradius over {k * step:.2f} mm arcs of the {step} mm resampled outline'}


def opening_residual(poly, r):
    """What a disc of radius r cannot reach from inside poly (exact shapely opening): total area and
    largest single piece, in mm2. About 0 means every convex corner has radius >= r and no part is
    thinner than 2r."""
    op = poly.buffer(-r, quad_segs=32).buffer(r, quad_segs=32)
    res = poly.difference(op)
    parts = [g for g in getattr(res, 'geoms', [res]) if g.geom_type == 'Polygon']
    return float(res.area), float(max([g.area for g in parts], default=0.0))


def pinch_widths(top, centres, half=0.9):
    """Exact wall-to-wall distance of each widened pinch: in a window around it, the smallest distance
    between two pieces of the outline whose connecting segment runs through open space (a gap, not a stem)."""
    from shapely.geometry import box, LineString
    from shapely.ops import nearest_points
    out = []
    for cx, cy in centres:
        b = top.boundary.intersection(box(cx - half, cy - half, cx + half, cy + half))
        parts = [g for g in getattr(b, 'geoms', [b]) if g.length > 0.05]
        best = None
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                p_, q_ = nearest_points(parts[i], parts[j])
                seg = LineString([p_, q_])
                if top.contains(seg.interpolate(0.5, normalized=True)):
                    continue                                   # across a stem
                if best is None or seg.length < best[0]:
                    best = (seg.length, seg.interpolate(0.5, normalized=True))
        out.append({'xy': [round(cx, 2), round(cy, 2)], 'wall_distance_mm': round(best[0], 4) if best else None,
                    'at': [round(best[1].x, 3), round(best[1].y, 3)] if best else None})
    return out


def gap_check(poly, px=0.02):
    """Minimum open gap between parts: local width of the complement inside the hull."""
    hull = poly.convex_hull.buffer(1.0)
    comp = hull.difference(poly)
    return width_histogram(comp, px)


def run_checks(mesh_v, mesh_f, groups, chamfer):
    import trimesh
    import manifold3d
    m = trimesh.Trimesh(mesh_v, mesh_f, process=False)
    res = {}
    res['is_watertight'] = bool(m.is_watertight)
    res['is_winding_consistent'] = bool(m.is_winding_consistent)
    res['body_count'] = int(m.body_count)
    res['volume_mm3'] = float(m.volume)
    res['euler_number'] = int(m.euler_number)
    mm = manifold3d.Manifold(manifold3d.Mesh(vert_properties=np.asarray(mesh_v, np.float32),
                                             tri_verts=np.asarray(mesh_f, np.uint32)))
    res['manifold3d_status'] = str(mm.status())
    res['manifold3d_volume_mm3'] = float(mm.volume())
    res['manifold3d_genus'] = int(mm.genus())
    n = m.face_normals
    a0, a1 = groups['top']
    res['top_min_normal_z'] = float(n[a0:a1, 2].min())
    w0, w1 = groups['wall']
    res['wall_max_abs_normal_z'] = float(np.abs(n[w0:w1, 2]).max())
    c0, c1 = groups['chamfer']
    res['chamfer_normal_z_range'] = [float(n[c0:c1, 2].min()), float(n[c0:c1, 2].max())]
    # chamfer face angle from the horizontal (45 deg = normal z -0.707), area weighted
    ang = np.degrees(np.arccos(np.clip(-n[c0:c1, 2], -1, 1)))
    ar = m.area_faces[c0:c1]
    o = np.argsort(ang)
    cw = np.cumsum(ar[o]) / ar.sum()
    res['chamfer_angle_from_horizontal_deg'] = {
        'p1': float(ang[o][np.searchsorted(cw, 0.01)]), 'median': float(ang[o][np.searchsorted(cw, 0.5)]),
        'p99': float(ang[o][min(np.searchsorted(cw, 0.99), len(o) - 1)]),
        'min': float(ang.min()), 'max': float(ang.max()),
        'area_outside_40_50_mm2': float(ar[(ang < 40) | (ang > 50)].sum()),
        'note': 'angle of the chamfer faces from the bed plane; < 45 deg would be a flatter overhang'}
    b0, b1 = groups['bottom']
    res['bottom_normal_z_max'] = float(n[b0:b1, 2].max())
    bot_vz = mesh_v[np.unique(mesh_f[b0:b1])][:, 2]
    res['bottom_vertex_z_absmax'] = float(np.abs(bot_vz).max())
    down = n[:, 2] < -1e-6
    down_other = down.copy()
    down_other[c0:c1] = False
    down_other[b0:b1] = False
    res['downward_faces_outside_bottom_and_chamfer'] = int(down_other.sum())
    res['min_vertex_z'] = float(mesh_v[:, 2].min())
    res['degenerate_faces'] = int((m.area_faces < 1e-12).sum())
    res['triangles'] = int(len(mesh_f))
    res['vertices'] = int(len(mesh_v))
    return res, m


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--glb', default=DEFAULT_GLB)
    ap.add_argument('--depth', default=DEFAULT_DEPTH,
                    help='depth-map cache (q16 .npz, committed). If missing it is regenerated from --glb (needs node/npx)')
    ap.add_argument('--regen', action='store_true',
                    help='re-decode the GLB, re-cast the depth maps and overwrite --depth (needs node/npx)')
    ap.add_argument('--float-cache', default=None,
                    help='read full-precision tree_front.npz / tree_back.npz from this dir instead of --depth (checks)')
    ap.add_argument('--res', type=float, default=0.0003, help='depth map resolution for --regen (model units/px)')
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    ap.add_argument('--work', default=DEFAULT_WORK, help='scratch dir (regenerated GLB data, height grid)')
    ap.add_argument('--scale', type=float, default=110.7, help='mm per model unit')
    ap.add_argument('--cut-world-y', type=float, default=-0.1540, help='trunk cut height (model y); -0.154 = depth row 1478')
    # thickness mapping
    ap.add_argument('--base', type=float, default=1.30, help='thickness (mm) at the source rim relief level')
    ap.add_argument('--rim-relief', type=float, default=0.90, help='source relief (mm above the flat back) mapped to --base')
    ap.add_argument('--gain', type=float, default=1.50, help='relief gain above the rim level')
    ap.add_argument('--max-thick', type=float, default=3.55, help='soft cap of the total thickness (mm)')
    ap.add_argument('--cap-soft', type=float, default=0.45, help='width of the soft-cap knee (mm)')
    ap.add_argument('--floor-in', type=float, default=1.4,
                    help='min thickness inside --plaque-rect (mm); default = --floor-out, i.e. no zoning, so the '
                         'tree does not depend on where the card puts it')
    ap.add_argument('--floor-out', type=float, default=1.4, help='min thickness where leaves hang beyond the plaque (mm)')
    ap.add_argument('--floor-soft', type=float, default=0.3, help='knee of the exponential soft floor (mm)')
    ap.add_argument('--rim-band', type=float, default=2.5, help='outer band (px) whose heights are extrapolated from inside')
    ap.add_argument('--rim-blend', type=float, default=3.0, help='pixels over which the rim extrapolation blends into the surface')
    ap.add_argument('--rim-ext', type=float, default=4.0, help='pixels beyond the outline that get the smooth rim extrapolation')
    ap.add_argument('--rim-max-drop', type=float, default=0.25, help='max extrapolated drop across the rim band (mm of relief)')
    ap.add_argument('--plaque-rect', type=float, nargs=4, default=[-4.6, -4.5, 90.4, 30.5],
                    help='plaque rectangle in the tree frame (x0 y0 x1 y1); only used when --floor-in differs '
                         'from --floor-out, and for the over/beyond-plaque statistics')
    # outline
    ap.add_argument('--min-width', type=float, default=1.05, help='min stem width in XY (mm)')
    ap.add_argument('--width-soft', type=float, default=0.06, help='smooth-max knee for the thickening (mm)')
    ap.add_argument('--spur-prune', type=float, default=1.2, help='medial-axis length peeled from free ends (mm)')
    ap.add_argument('--outline-iters', type=int, default=4, help='thicken/close/open rounds')
    ap.add_argument('--width-res', type=float, default=0.02, help='raster res of the width analysis (mm)')
    ap.add_argument('--min-gap', type=float, default=0.55, help='narrower gaps are closed (mm)')
    ap.add_argument('--gap-piece-min', type=float, default=2e-4,
                    help='narrow-gap pieces smaller than this (mm^2) are buffer noise and left alone')
    ap.add_argument('--pinch-detect', type=float, default=1.3,
                    help='pinches are found as the bridges of a closing with this times min_gap')
    ap.add_argument('--tip-radius', type=float, default=0.42, help='min convex corner radius (mm)')
    ap.add_argument('--hole-area', type=float, default=0.3, help='holes smaller than this are filled (mm^2)')
    ap.add_argument('--chamfer', type=float, default=0.3, help='45 deg bed chamfer on wide parts (mm)')
    ap.add_argument('--chamfer-min', type=float, default=0.15, help='chamfer on the thinnest stems (mm)')
    ap.add_argument('--chamfer-w0', type=float, default=1.0, help='feature width that gets --chamfer-min (mm)')
    ap.add_argument('--chamfer-w1', type=float, default=1.6, help='feature width from which --chamfer applies (mm)')
    ap.add_argument('--chamfer-slope', type=float, default=0.5, help='max change of the chamfer per mm of outline')
    ap.add_argument('--root-radius', type=float, default=0.55,
                    help='parts of the trunk base thinner than 2x this are trimmed to a round cap (0 = off)')
    ap.add_argument('--root-zone', type=float, default=1.5, help='height above the cut where the root trim applies (mm)')
    ap.add_argument('--base-band', type=float, nargs=2, default=[1.0, 3.0], metavar=('Y_LO', 'Y_HI'),
                    help='trunk base (mm above the cut): below Y_LO the relief of the row midway between Y_LO and Y_HI '
                         'is continued straight down along the flutes, warped back into the source by Y_HI '
                         '(0 0 = off: the drooping source heights)')
    ap.add_argument('--base-dir-sigma', type=float, default=0.8, help='smoothing of the measured flute direction along x (mm)')
    ap.add_argument('--base-halfwidth', type=float, default=8.0, help='half width of the trunk-base zone (mm)')
    ap.add_argument('--base-below', type=float, default=1.5, help='virtual trunk continued this far below the cut (mm)')
    ap.add_argument('--flare-left', type=float, default=0.9, help='left root flare width at the cut (mm, 0 = off)')
    ap.add_argument('--flare-height', type=float, default=3.0, help='left root flare height (mm)')
    ap.add_argument('--flare-slope', type=float, default=0.35, help='height drop per mm across the flare')
    ap.add_argument('--contour-sigma', type=float, default=0.8, help='mask smoothing before contouring (px)')
    ap.add_argument('--outline-tol', type=float, default=0.001, help='outline simplification tolerance (mm)')
    # smoothing / detail
    ap.add_argument('--spike-thresh', type=float, default=0.12, help='median outlier threshold (mm)')
    ap.add_argument('--bilateral-ss', type=float, default=2.0, help='bilateral spatial sigma (px)')
    ap.add_argument('--bilateral-sr', type=float, default=0.05, help='bilateral range sigma (mm)')
    ap.add_argument('--bilateral-iters', type=int, default=2)
    ap.add_argument('--detail-gain', type=float, default=0.7, help='band-pass boost (0 = off)')
    ap.add_argument('--detail-s1', type=float, default=0.08, help='band-pass inner sigma (mm)')
    ap.add_argument('--detail-s2', type=float, default=0.45, help='band-pass outer sigma (mm)')
    ap.add_argument('--detail-edge', type=float, default=0.35, help='fade the boost within this distance of the outline (mm)')
    # meshing
    ap.add_argument('--tol', type=float, default=0.015, help='max vertical error of the top surface (mm)')
    ap.add_argument('--target-tris', type=int, default=600000, help='stop refining at this many top triangles')
    ap.add_argument('--min-angle', type=float, default=28.0, help='triangle quality min angle (deg, 0 = off)')
    ap.add_argument('--seed-spacing', type=float, default=1.0, help='initial interior grid (mm)')
    ap.add_argument('--max-iters', type=int, default=40)
    ap.add_argument('--max-seg', type=float, default=0.15, help='max outline segment length (mm)')
    ap.add_argument('--density', type=float, default=1.24, help='g/cm^3 for the mass estimate')
    ap.add_argument('--ray-grid', type=float, default=0.05, help='grid of the vertical-ray heightfield test (mm)')
    ap.add_argument('--no-previews', action='store_true')
    ap.add_argument('--no-report', action='store_true', help='do not (re)write out/report.md')
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    # depth maps: the committed q16 cache; regenerated from the GLB on request or when it is missing
    if a.float_cache:
        F, B, ys, res = load_depth_float(a.float_cache)
    else:
        if a.regen or not os.path.exists(a.depth):
            log(f'depth cache {a.depth}: ' + ('regenerating (--regen)' if a.regen else 'missing, regenerating from the GLB'))
            save_depth_q16(a.depth, regen_depth(a.glb, a.work, a.res))
        F, B, ys, res = load_depth(a.depth)
    px = res * a.scale
    r_cut = (ys[0] - a.cut_world_y) / res
    log(f'depth {F.shape}, {px:.4f} mm/px, cut row {r_cut:.1f}')
    m_raw = ~np.isnan(F)
    plane = fit_back_plane(B, m_raw)
    m, flare, mask_info = clean_mask(F, r_cut, px, a)
    log('mask', mask_info)
    sk_info = thin_skeleton(np.where(np.arange(F.shape[0])[:, None] <= r_cut, m, False), px, a)
    P0, P1, P3, gapfill, oinfo = build_outline(m, px, r_cut, sk_info, a)
    log('outline', oinfo)
    # canonical frame: origin = centre of the straight bottom edge of the trunk
    ycut = -r_cut * px
    from shapely.geometry import LineString
    from shapely import affinity
    bx = P3.bounds
    edge = P3.intersection(LineString([(bx[0] - 1, ycut + 1e-3), (bx[2] + 1, ycut + 1e-3)]))
    exs = np.asarray([c for g in getattr(edge, 'geoms', [edge]) for c in g.coords])
    x_left, x_right = float(exs[:, 0].min()), float(exs[:, 0].max())
    x0 = 0.5 * (x_left + x_right)
    # rasters of the final region on the source grid
    region = raster_polygon(P3, F.shape, px)
    gapmask = raster_polygon(gapfill, F.shape, px) if gapfill.area > 0 else np.zeros_like(region)
    Zext, Rext, hstats = build_heights(F, plane, m, region, flare, gapmask, sk_info, px, a.scale, r_cut, x0, a)
    log('heights', hstats)
    # mesh
    rings = refine_rings(P3, Zext, px, a.tol, a.max_seg)
    log(f'outline rings: {[len(r) for r in rings][:6]}... total {sum(len(r) for r in rings)} verts')
    V, T, Zv, nb, rhist, err = triangulate_top(P3, rings, Zext, region, px, a)
    cimg, cX0, cY1, cres = chamfer_field(P3, a)
    allv, allf, bot_rings, groups, cvals = assemble(V, T, Zv, rings, a, cimg, cX0, cY1, cres)
    log(f'chamfer per wall vertex: min {cvals.min():.3f} p5 {np.percentile(cvals, 5):.3f} '
        f'median {np.median(cvals):.3f} max {cvals.max():.3f} mm')
    # shift to the canonical frame
    allv[:, 0] -= x0
    allv[:, 1] -= ycut
    shift = (-x0, -ycut)
    checks, tm = run_checks(allv, allf, groups, a.chamfer)
    log('checks', {k: v for k, v in checks.items() if not isinstance(v, dict)})
    # footprint (glue face = bottom rings)
    from shapely.geometry import Polygon
    import shapely
    glue = Polygon(bot_rings[0], bot_rings[1:])
    glue = affinity.translate(glue, *shift)
    top_outline = affinity.translate(P3, *shift)
    # glue inset actually achieved: distance of every glue vertex to the top outline
    gpts = np.concatenate([np.asarray(r.coords)[:-1] for r in [glue.exterior] + list(glue.interiors)])
    dists = shapely.distance(shapely.points(gpts), top_outline.boundary)
    sd = {'glue_vertex_inset_mm': {'min': round(float(dists.min()), 4), 'max': round(float(dists.max()), 4)},
          'chamfer_per_wall_vertex_mm': {'min': round(float(cvals.min()), 4),
                                         'p5': round(float(np.percentile(cvals, 5)), 4),
                                         'median': round(float(np.median(cvals)), 4),
                                         'max': round(float(cvals.max()), 4),
                                         'fraction_full': round(float(np.mean(cvals > a.chamfer - 0.005)), 3)}}
    log('glue inset', sd)
    a._x_left, a._x_right = x_left - x0, x_right - x0
    write_outputs(a, allv, allf, groups, glue, top_outline, checks, tm, oinfo, mask_info, hstats, rhist,
                  x_left - x0, x_right - x0, sd, px, plane, Zext, region, shift, err)
    if not a.no_previews:
        import tree_previews
        # crop of the source hillshade covering the same region as the top view (+0.5 mm margin)
        bx = P3.bounds
        m_ = 0.5 / px
        hill_crop = (bx[0] / px - m_, -bx[3] / px - m_, bx[2] / px + m_, -bx[1] / px + m_)
        hill = tree_previews.source_hillshade(F, px, a.scale)
        tree_previews.make_all(a.out, allv, allf, groups, top_outline, glue, hill_crop=hill_crop, hill_img=hill)
        tree_previews.layer_preview(a.out, Zext, region, px)
        log('previews written')
    if not a.no_report:
        import make_report
        make_report.main(a.out)
    log('done')


def section(mm, z):
    """Exact planar section of a manifold3d solid at height z, as a shapely (Multi)Polygon."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    polys = [np.asarray(p) for p in mm.slice(z).to_polygons()]
    # rings come without explicit nesting: even-odd combine (symmetric difference)
    g = Polygon()
    for p in polys:
        if len(p) >= 3:
            g = g.symmetric_difference(Polygon(p).buffer(0))
    return g


def rim_roughness(tm, step=0.008, wins=((41, 28.6), (-26, 28.5), (0, 44), (10, 25), (-17, 15), (30, 36))):
    """Fine bumps of the top surface along the outline (the judge's metric): in six 5 x 4 mm windows, the
    top height is sampled by vertical rays every `step` mm, followed along contours at fixed distances
    inside the top edge, and high-passed with a 0.3 mm running mean. Returns (std, p99 of |high-pass|) in
    mm per distance; the rim should be no rougher than the interior (0.25 mm)."""
    from skimage.measure import find_contours
    res = {}
    hs = []
    for cx, cy in wins:
        xs = np.arange(cx - 2.5, cx + 2.5, step)
        ys = np.arange(cy + 2, cy - 2, -step)
        X, Y = np.meshgrid(xs, ys)
        O = np.c_[X.ravel(), Y.ravel(), np.full(X.size, 10.0)]
        loc, idx, _ = tm.ray.intersects_location(O, np.tile([0, 0, -1.0], (len(O), 1)), multiple_hits=False)
        h = np.zeros(X.size)
        h[idx] = loc[:, 2]
        h = h.reshape(X.shape)
        hs.append((h, ndi.distance_transform_edt(h > 0.35) * step))
    for delta in (0.03, 0.06, 0.12, 0.25):
        hp = []
        for h, d in hs:
            for c in find_contours(d, delta):
                if len(c) < 80:
                    continue
                z = ndi.map_coordinates(h, c.T, order=1)
                seg = np.r_[0, np.cumsum(np.hypot(*np.diff(c, axis=0).T))] * step
                sg = np.arange(0, seg[-1], 0.01)
                zz = np.interp(sg, seg, z)
                if len(zz) < 60:
                    continue
                hp.append((zz - ndi.uniform_filter1d(zz, 31, mode='nearest'))[15:-15])
        hp = np.concatenate(hp)
        res[f'{delta}'] = {'std': round(float(hp.std()), 4), 'p99': round(float(np.percentile(np.abs(hp), 99)), 4)}
    return {'mm_inside_edge': res, 'windows_xy': [list(w) for w in wins]}


def trunk_base_profile(tm, x_left, x_right, floor, rows=(0.02, 0.5, 1.0, 2.0, 3.0), step=0.02):
    """Thickness of the finished solid along horizontal lines just above the trunk cut (vertical rays): does
    the trunk keep its relief down to the cut? Per row: extent hit, min / p25 / median / max of the top z,
    and the share of the row within 0.2 mm of the thickness floor."""
    out = {}
    for y in rows:
        xs = np.arange(x_left - 2.0, x_right + 2.0, step)
        O = np.c_[xs, np.full(len(xs), y), np.full(len(xs), 10.0)]
        loc, idx, _ = tm.ray.intersects_location(O, np.tile([0, 0, -1.0], (len(O), 1)), multiple_hits=False)
        z = np.full(len(xs), np.nan)
        z[idx] = loc[:, 2]
        hit = ~np.isnan(z)
        # the trunk run: the hit run that contains x = 0 at the cut, or the one nearest to it
        runs = np.split(np.nonzero(hit)[0], np.nonzero(np.diff(np.nonzero(hit)[0]) > 1)[0] + 1)
        run = min(runs, key=lambda r_: abs(xs[r_].mean() - (x_left + x_right) / 2) if len(r_) else 1e9)
        zz = z[run]
        out[f'{y}'] = {'x_from': round(float(xs[run[0]]), 2), 'x_to': round(float(xs[run[-1]]), 2),
                       'min': round(float(zz.min()), 3), 'p25': round(float(np.percentile(zz, 25)), 3),
                       'median': round(float(np.median(zz)), 3), 'max': round(float(zz.max()), 3),
                       'share_within_0.2_of_floor': round(float(np.mean(zz < floor + 0.2)), 3)}
    return out


def solid_checks(tm, glue, top, a):
    """Checks on the finished solid that do not trust the construction:
    * vertical rays on a grid: each column must cross the surface exactly 0 or 2 times (a heightfield,
      so no overhang anywhere), and the column thickness (top z) must respect the floor;
    * exact manifold3d sections at several z: area, widths, and the z ~ 0 section vs the footprint."""
    import manifold3d
    out = {}
    g = a.ray_grid
    b = tm.bounds
    X, Y = np.meshgrid(np.arange(b[0, 0] + 0.37 * g, b[1, 0], g), np.arange(b[0, 1] + 0.61 * g, b[1, 1], g))
    O = np.c_[X.ravel(), Y.ravel(), np.full(X.size, -1.0)]
    import shapely
    n_all = len(O)
    O = O[shapely.contains_xy(top.buffer(0.05), O[:, 0], O[:, 1])]     # rays elsewhere cannot hit
    locs, ray, _ = tm.ray.intersects_location(O, np.tile([0, 0, 1.0], (len(O), 1)), multiple_hits=True)
    cnt = np.bincount(ray, minlength=len(O))
    ztop = np.full(len(O), -np.inf)
    np.maximum.at(ztop, ray, locs[:, 2])
    zbot = np.full(len(O), np.inf)
    np.minimum.at(zbot, ray, locs[:, 2])
    hit2 = cnt == 2
    # columns inside the glue face (not over the chamfer band): bottom hit is the bed
    inside = np.zeros(len(O), bool)
    inside[hit2] = shapely.contains_xy(glue, O[hit2, 0], O[hit2, 1])
    tz = ztop[hit2]
    out['vertical_ray_test'] = {
        'grid_mm': g, 'rays_cast': int(len(O)), 'grid_points_in_bbox': n_all, 'rays_hitting': int((cnt > 0).sum()),
        'hit_count_histogram': np.bincount(cnt).tolist(),
        'non_heightfield_columns_area_mm2': float((cnt > 2).sum() * g * g),
        'odd_hit_columns': int((cnt % 2 == 1).sum()),
        'thickness_top_z_mm': {'min': round(float(tz.min()), 4), 'p1': round(float(np.percentile(tz, 1)), 4),
                               'area_below_1.4_mm2': round(float((tz < 1.4 - 1e-4).sum() * g * g), 4),
                               'area_below_floor_mm2': round(float((tz < min(a.floor_in, a.floor_out) - 1e-4).sum() * g * g), 4)},
        'bed_contact_columns_bottom_z_absmax': round(float(np.abs(zbot[inside]).max()), 6) if inside.any() else None,
        'note': 'thickness = height of the top surface above the bed in each column (the chamfer band '
                'columns are included: their top is the relief, their bottom the chamfer)'}
    out['rim_roughness'] = rim_roughness(tm)
    out['trunk_base_profile'] = trunk_base_profile(tm, a._x_left, a._x_right, a.floor_out)
    mm = manifold3d.Manifold(manifold3d.Mesh(vert_properties=np.asarray(tm.vertices, np.float32),
                                             tri_verts=np.asarray(tm.faces, np.uint32)))
    secs = {}
    for z in (0.0005, 0.1, 0.2, 1.0):
        sg = section(mm, z)
        w = width_histogram(sg, end_skip=a.spur_prune) if not sg.is_empty else None
        secs[f'{z:.4g}'] = {'area_mm2': round(float(sg.area), 3),
                            'parts': len(getattr(sg, 'geoms', [sg])),
                            'width_min_mm': w['min_mm'] if w else None, 'width_p1_mm': w['p1_mm'] if w else None,
                            'thinnest_xy_w': w['thinnest_xy_w'][:4] if w else None}
        if z < 0.001:
            # the section at z is the glue face grown by z (45 deg chamfer): compare boundaries
            secs[f'{z:.4g}']['hausdorff_vs_footprint_mm'] = round(float(sg.boundary.hausdorff_distance(glue.boundary)), 5)
        if abs(z - 1.0) < 1e-9:
            secs[f'{z:.4g}']['symdiff_vs_outline_full_mm2'] = round(float(sg.symmetric_difference(top).area), 5)
    out['exact_sections'] = secs
    return out


def write_outputs(a, V, Fc, groups, glue, top, checks, tm, oinfo, minfo, hstats, rhist,
                  xl, xr, sd, px, plane, Zext, region, shift, err):
    out = a.out
    tm.export(os.path.join(out, 'tree.stl'))
    log('wrote tree.stl')

    def ring_list(r):
        return [[round(float(x), 4), round(float(y), 4)] for x, y in np.asarray(r.coords)[:-1]]

    def poly_list(P):
        return [{'exterior': ring_list(q.exterior), 'holes': [ring_list(h) for h in q.interiors]}
                for q in getattr(P, 'geoms', [P])]
    from shapely.geometry import box
    gb = glue.bounds
    base_band = glue.intersection(box(-100, -1, 100, a.chamfer + 0.5)).bounds
    fp = {
        'units': 'mm',
        'frame': 'canonical tree frame: origin = bottom-centre of trunk base, +x right, +y up (front view), '
                 'polygons are the bottom (glue) face outline',
        'scale_mm_per_model_unit': a.scale,
        'polygons': poly_list(glue),
        'bbox': [round(v, 3) for v in gb],
        'area_mm2': round(glue.area, 2),
        'trunk_base': {'x_left': round(xl, 3), 'x_right': round(xr, 3), 'y': 0.0,
                       'flat_cut_x_left': round(xl, 3), 'flat_cut_x_right': round(xr, 3),
                       'glue_face_x_left': round(base_band[0], 3), 'glue_face_x_right': round(base_band[2], 3),
                       'glue_face_y_min': round(gb[1], 3)},
        'chamfer_mm': {'max': a.chamfer, 'min': a.chamfer_min,
                       'rule': f'{a.chamfer_min} mm on stems {a.chamfer_w0} mm wide, rising linearly to {a.chamfer} mm '
                               f'from {a.chamfer_w1} mm wide features up (45 deg)'},
        'outline_full': {'z_mm': f'z >= {a.chamfer} (walls are vertical above the chamfer)',
                         'area_mm2': round(top.area, 2), 'bbox': [round(v, 3) for v in top.bounds],
                         'polygons': poly_list(top)},
        'notes': 'polygons = exact outline of the z = 0 face of tree.stl (the glue face, inset by the bed chamfer: '
                 f'{a.chamfer_min} to {a.chamfer} mm). outline_full = exact outline of the vertical walls above the '
                 'chamfer, i.e. the visible silhouette and the largest section of the tree. Size the pocket from '
                 'outline_full + clearance so the tree drops in to the full pocket depth (a pocket sized from the '
                 'glue face would only let the chamfer seat on its rim). trunk_base x_left/x_right = ends of the '
                 'straight trunk-bottom edge at y = 0 (the lowest line of the solid); the glue face starts at '
                 'glue_face_y_min. Exteriors CCW, holes CW.',
    }
    with open(os.path.join(out, 'tree_footprint.json'), 'w') as f:
        json.dump(fp, f)
    # thickness stats on the final region grid
    zz = Zext[region]
    tb = [0, 1.0, 1.2, 1.3, 1.4, 1.6, 2.0, 2.5, 3.0, 3.3, 3.6, 4.0]
    th, _ = np.histogram(zz, tb)
    widths_top = width_histogram(top, end_skip=a.spur_prune)
    widths_glue = width_histogram(glue, end_skip=a.spur_prune)
    gaps = gap_check(top)
    tips = convex_radius_stats(top)
    comp = top.convex_hull.buffer(2.0).difference(top)
    t_tot, t_max = opening_residual(top, a.tip_radius - 0.01)
    g_tot, g_max = opening_residual(comp, a.min_gap / 2 - 0.01)
    pinches = [g['xy_image_mm'] for g in oinfo.get('gap_fix', []) if g['action'].startswith('pinch')]
    exact = {
        'pinch_wall_distance_mm': pinch_widths(top, [(x + shift[0], y + shift[1]) for x, y in pinches]),
        'tip_opening_residual_mm2': {'total': round(t_tot, 5), 'largest_piece': round(t_max, 5)},
        'gap_opening_residual_mm2': {'total': round(g_tot, 5), 'largest_piece': round(g_max, 5)},
        'tip_radius_tested_mm': round(a.tip_radius - 0.01, 3),
        'gap_tested_mm': round(a.min_gap - 0.02, 3),
        'note': 'exact shapely openings. Tips: residual ~0 means every convex corner of the top outline has '
                'radius >= tip_radius. Gaps: residual ~0 means no slot or notch of the open space is narrower '
                'than min_gap (the final closing fills them with min_gap/2 fillets).',
    }
    solid = solid_checks(tm, glue, top, a)
    log('solid checks', json.dumps(solid['vertical_ray_test']))
    # thickness where leaves hang beyond the plaque rectangle (floor_out applies there)
    rr_, cc_ = np.nonzero(region)
    Xc = cc_ * px + shift[0]
    Yc = -rr_ * px + shift[1]
    pr = a.plaque_rect
    outside = ~((Xc > pr[0]) & (Xc < pr[2]) & (Yc > pr[1]) & (Yc < pr[3]))
    z_out = Zext[rr_[outside], cc_[outside]]
    z_in = Zext[rr_[~outside], cc_[~outside]]
    vol = checks['volume_mm3']
    b = V.min(0), V.max(0)
    meta = {
        'name': 'gold tree relief (final, from variant B + judge fixes)',
        'units': 'mm',
        'frame': fp['frame'],
        'scale_mm_per_model_unit': a.scale,
        'pixel_mm': px,
        'bbox_3d': {'min': [round(float(v), 4) for v in b[0]], 'max': [round(float(v), 4) for v in b[1]],
                    'size': [round(float(v), 3) for v in (b[1] - b[0])]},
        'thickness_mm': {'min': round(float(zz.min()), 3), 'p1': round(float(np.percentile(zz, 1)), 3),
                         'p5': round(float(np.percentile(zz, 5)), 3), 'median': round(float(np.median(zz)), 3),
                         'mean': round(float(zz.mean()), 3), 'p99': round(float(np.percentile(zz, 99)), 3),
                         'max': round(float(zz.max()), 3), 'hist_bins': tb, 'hist_counts_px': th.tolist()},
        'volume_mm3': round(vol, 2),
        'mass_solid_g': round(vol / 1000 * a.density, 2),
        'density_g_cm3': a.density,
        'triangles': checks['triangles'],
        'vertices': checks['vertices'],
        'trunk_base': fp['trunk_base'],
        'outline_area_mm2': round(top.area, 2),
        'outline_holes_mm2': [round(Polygon_area(h), 3) for h in top.interiors],
        'glue_face_area_mm2': round(glue.area, 2),
        'glue_face_holes_mm2': [round(Polygon_area(h), 3) for h in glue.interiors],
        'chamfer': sd,
        'min_width_top_outline': widths_top,
        'min_width_glue_face': widths_glue,
        'min_gap_top_outline': gaps,
        'convex_corner_radius_top_outline': tips,
        'exact_outline_checks': exact,
        'solid_checks': solid,
        'thickness_outside_plaque_mm': {'area_mm2': round(float(outside.sum() * px * px), 1),
                                        'min': round(float(z_out.min()), 3) if len(z_out) else None,
                                        'p1': round(float(np.percentile(z_out, 1)), 3) if len(z_out) else None,
                                        'plaque_rect_tree_frame': list(pr)},
        'thickness_over_plaque_mm': {'min': round(float(z_in.min()), 3), 'p1': round(float(np.percentile(z_in, 1)), 3)},
        'surface_error_mm': {'max': round(float(err.max()), 4), 'p99': round(float(np.percentile(err, 99)), 4),
                             'mean': round(float(err.mean()), 5), 'refine_history': rhist,
                             'note': 'vertical error of the triangulated top surface against the height grid '
                                     '(every pixel centre at least one pixel inside the outline)'},
        'checks': checks,
        'outline_info': oinfo,
        'mask_info': minfo,
        'height_info': hstats,
        'back_plane_coef': [float(c) for c in plane],
        'parameters': {k: rel_path(v) if isinstance(v, str) else v for k, v in vars(a).items() if not k.startswith('_')},
        'shift_image_to_canonical_mm': list(shift),
    }
    with open(os.path.join(out, 'tree_meta.json'), 'w') as f:
        json.dump(meta, f, indent=1)
    # keep the height grid for previews / slice checks
    os.makedirs(a.work, exist_ok=True)
    np.savez_compressed(os.path.join(a.work, 'zgrid.npz'), Z=Zext, region=region, px=px, shift=np.array(shift))
    log('wrote footprint/meta')


def rel_path(p):
    """Paths inside place-cards/ as relative paths (meta stays the same wherever the repo is cloned)."""
    if os.path.isabs(p) and p.startswith(REPO_PC + os.sep):
        return os.path.relpath(p, REPO_PC)
    return p


def Polygon_area(ring):
    from shapely.geometry import Polygon
    return float(Polygon(ring).area)


if __name__ == '__main__':
    sys.path.insert(0, HERE)
    main()
