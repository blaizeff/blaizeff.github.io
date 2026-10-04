#!/usr/bin/env python3
"""Gold tree relief for the wedding place cards - variant B ("vector outline + adaptive triangulation").

Turns the Tripo tree (part 0 of place-cards/source/tree_tripo_meshopt.glb) into a print-ready FDM
heightfield relief: flat glue face on the bed at z = 0, relief up, one watertight body.

Pipeline
  1. Orthographic depth maps of the tree front/back (cached .npz; --regen rebuilds them from the GLB:
     meshopt decode with gltf-transform, node tripo_part_0 in world transform, embree ray casting).
  2. Raster clean-up of the silhouette: horizontal trunk cut, largest body only, pin-holes filled,
     optional left root flare.
  3. Vector outline: sub-pixel contour of a smoothed mask -> shapely. Thin stems (medial-axis width
     below --min-width) are thickened by a union of discs on their medial axis only (tapers elsewhere
     are kept), gaps narrower than --min-gap are closed (filled as low grooves), tiny holes are
     dropped and every convex tip is rounded by an opening of radius --tip-radius.
  4. Heights: source relief above the flat back, edge-preserving denoise (bilateral), thickened
     stems get their round profile stretched instead of a flat bulge, a band-pass detail boost keeps
     midribs / fluting legible at 0.1 mm layers, then base + gain mapping, thickness floors and a
     soft cap.
  5. Constrained Delaunay triangulation (triangle) of the exact outline, refined adaptively by
     greedy insertion of the worst pixel until the max vertical error is below --tol.
  6. Vertical walls, a 45 deg bed chamfer and a planar bottom from the same outline rings, so the
     glue face outline written to tree_footprint.json is exactly the mesh's z = 0 face.
  7. Checks (trimesh / manifold3d / overhangs / widths / thickness) and previews.

Run (from anywhere):
  python3 place-cards/tree/variants/B/build_tree.py            # full build, about 4 min
  python3 place-cards/tree/variants/B/build_tree.py --regen    # also re-decode the GLB first
  python3 place-cards/tree/variants/B/slice_check.py           # PrusaSlicer extrusion coverage check
Outputs go to place-cards/tree/variants/B/out/ (override with --out).
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
REPO_PC = os.path.abspath(os.path.join(HERE, '..', '..', '..'))          # place-cards/
DEFAULT_GLB = os.path.join(REPO_PC, 'source', 'tree_tripo_meshopt.glb')
SHARED_WORK = '/tmp/claude-0/-home-user-blaizeff-github-io/24db3fd0-37aa-5abe-8f21-849f3f5e2464/scratchpad/work'
T0 = time.time()


def log(*a):
    print(f'[{time.time() - T0:6.1f}s]', *a, flush=True)


# --------------------------------------------------------------------------------------------
# 1. source depth maps
# --------------------------------------------------------------------------------------------
def regen_cache(glb, cache, res):
    """Decode the meshopt GLB, extract tripo_part_0 in world coordinates, ray-cast depth maps."""
    import trimesh
    os.makedirs(cache, exist_ok=True)
    dec = os.path.join(cache, 'tree_decoded.glb')
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
    mesh.export(os.path.join(cache, 'tree_part0_raw.ply'))
    b = mesh.bounds
    zs = np.arange(b[0, 2], b[1, 2], res)
    ys = np.arange(b[1, 1], b[0, 1], -res)
    Z, Y = np.meshgrid(zs, ys)
    n = Z.size
    for name, x0, dx in (('front', b[1, 0] + 0.01, -1.0), ('back', b[0, 0] - 0.01, 1.0)):
        O = np.stack([np.full(n, x0), Y.ravel(), Z.ravel()], 1)
        D = np.tile([dx, 0.0, 0.0], (n, 1))
        loc, idx, _ = mesh.ray.intersects_location(O, D, multiple_hits=False)
        depth = np.full(n, np.nan)
        depth[idx] = loc[:, 0]
        np.savez_compressed(os.path.join(cache, f'tree_{name}.npz'), depth=depth.reshape(Z.shape),
                            zs=zs, ys=ys, res=res)
        log(f'  depth map {name}: {Z.shape}, coverage {np.mean(~np.isnan(depth)):.3f}')


def load_depth(cache):
    f = np.load(os.path.join(cache, 'tree_front.npz'))
    b = np.load(os.path.join(cache, 'tree_back.npz'))
    # flip columns: front view the right way round (tree on the left, canopy sweeping right)
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
    info['iterations'] = hist
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
        Rext = np.where(fz, masked_blur(Rext, region, 4.0), Rext)
    # small blend across every filled area so no step remains at the seams
    added = region & ~orig
    if added.any():
        band = ndi.binary_dilation(added | stretch_zone, iterations=2) & region
        Rb = masked_blur(Rext, region, 1.0)
        Rext = np.where(band, Rb, Rext)
    # rim: the outermost pixels of the depth map are grazing-angle samples (noisy); take the rim
    # height from just inside so the top edge of the walls runs clean
    din_px = ndi.distance_transform_edt(region)
    if a.rim_band > 0:
        # first-order normalized convolution from a smoothed surface further in: every band pixel
        # gets the Gaussian-weighted average of the tangent-plane predictions of the good pixels
        # around it (smooth along the edge, continues the rim slope, no lip and no streaks)
        deep = (region & (din_px >= a.rim_band + 1.5)).astype(np.float64)
        band = region & (din_px < a.rim_band)
        Gs = masked_blur(Rext, region & (din_px >= a.rim_band), 1.0).astype(np.float64)
        gy, gx = np.gradient(Gs)
        sg = 2.0
        Wt = ndi.gaussian_filter(deep, sg)
        A = ndi.gaussian_filter(deep * (Gs - gx * cc - gy * rr), sg)
        Bx = ndi.gaussian_filter(deep * gx, sg)
        By = ndi.gaussian_filter(deep * gy, sg)
        A0 = ndi.gaussian_filter(deep * Gs, sg) / np.maximum(Wt, 1e-9)
        v = (A + cc * Bx + rr * By) / np.maximum(Wt, 1e-9)
        v = np.clip(v, A0 - a.rim_max_drop, A0)
        Rext = np.where(band & (Wt > 1e-3), v, Rext).astype(np.float32)
    # band-pass detail boost (midribs, fluting, leaf rims) computed inside the final region only
    s1 = a.detail_s1 / px
    s2 = a.detail_s2 / px
    G1 = masked_blur(Rext, region, s1) if s1 > 0.2 else Rext
    G2 = masked_blur(Rext, region, s2)
    detail = G1 - G2
    # attenuate the boost right at the silhouette (the rim drop is already strong there)
    din = ndi.distance_transform_edt(region) * px
    w_edge = np.clip(din / a.detail_edge, 0, 1) ** 2
    Renh = Rext + a.detail_gain * detail * w_edge
    # map to thickness: base at the rim level, gain above it
    Z = a.base + a.gain * (Renh - a.rim_relief)
    # thickness floors: thicker where leaves hang beyond the plaque
    X = cc * px - x0
    Y = (r_cut - rr) * px
    pr = a.plaque_rect
    inside = (X > pr[0]) & (X < pr[2]) & (Y > pr[1]) & (Y < pr[3])
    fl = np.where(inside, a.floor_in, a.floor_out).astype(np.float32)
    fl = ndi.gaussian_filter(fl, 1.0 / px)
    # soft floor: identity above floor + d, exponential approach to the floor below it, so low
    # areas (root flare, petiole ends) keep a gentle shape instead of a flat clamp
    d_ = a.floor_soft
    Z = np.where(Z >= fl + d_, Z, fl + d_ * np.exp(np.minimum(Z - fl - d_, 0) / d_))
    # soft cap toward max_thick
    c0 = a.max_thick - a.cap_soft
    over = Z > c0
    Z = np.where(over, c0 + a.cap_soft * np.tanh((Z - c0) / a.cap_soft), Z)
    Z = Z.astype(np.float32)
    Zext = nearest_fill(Z, region).astype(np.float32)
    stats = {'spikes_replaced_px': int(spike.sum()), 'stretch_zone_px': int(stretch_zone.sum()),
             'added_px': int(added.sum())}
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
def inset_rings(rings, c, tol=0.001, max_seg=0.15):
    """Glue-face rings: the exact inward offset (by c) of the top outline, matched to each top ring.
    Also returns, for every top-ring vertex, its projection parameter on the matching inset ring,
    which drives the chamfer stitching."""
    import shapely
    from shapely.geometry import Polygon, LineString, Point
    P = Polygon(rings[0], rings[1:])
    Bx = P.buffer(-c, quad_segs=32, join_style='round')
    if Bx.geom_type != 'Polygon' or len(Bx.interiors) != len(rings) - 1:
        raise RuntimeError('chamfer inset changes the topology (a part is thinner than 2 x chamfer)')
    Bx = Bx.simplify(tol, preserve_topology=True)
    Bx = shapely.segmentize(Bx, max_seg)                 # keep the chamfer strip triangles local
    Bx = shapely.geometry.polygon.orient(Bx, 1.0)
    exact = [np.asarray(Bx.exterior.coords)[:-1]] + [np.asarray(h.coords)[:-1] for h in Bx.interiors]
    out = []
    used = set()
    for ring in rings:
        d = [LineString(np.r_[e, e[:1]]).distance(Point(ring[0])) if k not in used else 1e9
             for k, e in enumerate(exact)]
        j = int(np.argmin(d))
        used.add(j)
        b = exact[j]
        line = LineString(np.r_[b, b[:1]])
        L = line.length
        s = shapely.line_locate_point(line, shapely.points(ring))
        t = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(b, axis=0), axis=1))]
        ds = np.diff(np.r_[s, s[:1]])
        ds = np.where(ds < -0.5 * L, ds + L, ds)
        if (ds < -1e-9).any():
            raise RuntimeError(f'chamfer projection folds back at {int((ds < -1e-9).sum())} vertices')
        out.append((b, s, t, L))
    return out


def stitch(W, s, B, t, L):
    """Triangle strip between ring W (params s) and ring B (params t), both running the same way.
    Returns triangles (W_i, B_j, X) with X = B_{j+1} or W_{i+1}: outward-down for the chamfer."""
    n, m = len(W), len(B)
    s0 = s[0]
    sp = np.mod(s - s0, L)
    sp[0] = 0.0
    j0 = int(np.searchsorted(t, s0)) % m
    tp = np.mod(t[(j0 + np.arange(m)) % m] - s0, L)
    Bi = B[(j0 + np.arange(m)) % m]
    tris = []
    i = j = 0
    while i < n or j < m:
        ns = sp[i + 1] if i + 1 < n else L + sp[0]
        nt = tp[j + 1] if j + 1 < m else L + tp[0]
        if (ns <= nt and i < n) or j >= m:
            tris.append((W[i % n], Bi[j % m], W[(i + 1) % n]))
            i += 1
        else:
            tris.append((W[i % n], Bi[j % m], Bi[(j + 1) % m]))
            j += 1
    return np.array(tris)


def assemble(V, T, Z, rings, c):
    import triangle as tr
    from shapely.geometry import Polygon
    nt = len(V)
    insets = inset_rings(rings, c)
    nwall = sum(len(r) for r in rings)
    verts = [np.c_[V, Z]]
    wall_faces, cham_faces, bot_rings = [], [], []
    w_off = nt
    b_off = nt + nwall
    top_off = 0
    for ring, (b, s, t, L) in zip(rings, insets):
        n = len(ring)
        ti = np.arange(n) + top_off                    # top boundary vertices = first nb of V
        wi = np.arange(n) + w_off
        top_off += n
        w_off += n
        verts.append(np.c_[ring, np.full(n, c)])
        wall_faces.append(np.c_[ti, wi, np.roll(wi, -1)])
        wall_faces.append(np.c_[ti, np.roll(wi, -1), np.roll(ti, -1)])
    for ring, (b, s, t, L), k in zip(rings, insets, range(len(rings))):
        m = len(b)
        bi = np.arange(m) + b_off
        b_off += m
        wi = np.arange(len(ring)) + nt + sum(len(r) for r in rings[:k])
        cham_faces.append(stitch(wi, s, bi, t, L))
        bot_rings.append(b)
    bverts = np.concatenate(bot_rings)
    verts.append(np.c_[bverts, np.zeros(len(bverts))])
    segs, off = [], 0
    for r_ in bot_rings:
        i = np.arange(len(r_)) + off
        segs.append(np.c_[i, np.roll(i, -1)])
        off += len(r_)
    holes = [np.asarray(Polygon(r_).representative_point().coords)[0] for r_ in bot_rings[1:]]
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
    return allv, allf, bot_rings, groups


# --------------------------------------------------------------------------------------------
# 7. checks
# --------------------------------------------------------------------------------------------
def width_histogram(poly, px=0.02, end_skip=1.2):
    """Local width along the medial axis of a polygon (2 x inscribed radius), in mm. The last
    `end_skip` mm toward each free end are left out: like in the thickening step, tips are not stems
    (their width goes to 0 at the end; they are rounded to tip_radius instead)."""
    from skimage.morphology import skeletonize
    b = poly.bounds
    pad = 4
    shp = (int((b[3] - b[1]) / px) + 2 * pad, int((b[2] - b[0]) / px) + 2 * pad)
    from shapely import affinity
    q = affinity.translate(poly, -b[0] + pad * px, -b[3] - pad * px)
    img = raster_polygon(q, shp, px)
    # 1-px skeleton (clean topology for the end peeling) + exact EDT for the inscribed radius
    sk = skeletonize(img)
    dist = ndi.distance_transform_edt(img)
    s2 = prune_spurs(sk, int(round(end_skip / px)))
    # EDT reaches the centre of the first outside pixel, on average half a pixel beyond the outline
    w = (2 * dist[s2] - 1) * px
    bins = [0, 0.4, 0.6, 0.8, 0.9, 1.0, 1.1, 1.2, 1.5, 2.0, 3.0, 5.0, 100]
    h, _ = np.histogram(w, bins)
    # where are the thinnest places
    rr, cc = np.nonzero(s2)
    wv = (2 * dist[rr, cc] - 1) * px
    o = np.argsort(wv)[:8]
    where = [[round(cc[i] * px + b[0] - pad * px, 2), round(-(rr[i] * px) + b[3] + pad * px, 2),
              round(float(wv[i]), 3)] for i in o]
    return {'bins_mm': bins, 'counts': h.tolist(), 'end_skip_mm': end_skip, 'min_mm': round(float(w.min()), 3),
            'p1_mm': round(float(np.percentile(w, 1)), 3), 'thinnest_xy_w': where}


def convex_radius_stats(poly, step=0.01, k=12):
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
    ap.add_argument('--cache', default=None, help='dir with tree_front/back.npz (default: shared work dir, else ./cache)')
    ap.add_argument('--regen', action='store_true', help='re-decode the GLB and re-cast the depth maps')
    ap.add_argument('--res', type=float, default=0.0003, help='depth map resolution for --regen (model units/px)')
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    ap.add_argument('--work', default=os.path.join(SHARED_WORK, 'agents', 'B') if os.path.isdir(SHARED_WORK)
                    else os.path.join(HERE, 'cache'), help='scratch dir for intermediate grids')
    ap.add_argument('--scale', type=float, default=110.7, help='mm per model unit')
    ap.add_argument('--cut-world-y', type=float, default=-0.1540, help='trunk cut height (model y); -0.154 = depth row 1478')
    # thickness mapping
    ap.add_argument('--base', type=float, default=1.30, help='thickness (mm) at the source rim relief level')
    ap.add_argument('--rim-relief', type=float, default=0.90, help='source relief (mm above the flat back) mapped to --base')
    ap.add_argument('--gain', type=float, default=1.50, help='relief gain above the rim level')
    ap.add_argument('--max-thick', type=float, default=3.55, help='soft cap of the total thickness (mm)')
    ap.add_argument('--cap-soft', type=float, default=0.45, help='width of the soft-cap knee (mm)')
    ap.add_argument('--floor-in', type=float, default=1.2, help='min thickness over the plaque (mm)')
    ap.add_argument('--floor-out', type=float, default=1.4, help='min thickness where leaves hang beyond the plaque (mm)')
    ap.add_argument('--floor-soft', type=float, default=0.3, help='knee of the exponential soft floor (mm)')
    ap.add_argument('--rim-band', type=float, default=2.5, help='outer band (px) whose heights are extrapolated from inside')
    ap.add_argument('--rim-max-drop', type=float, default=0.25, help='max extrapolated drop across the rim band (mm of relief)')
    ap.add_argument('--plaque-rect', type=float, nargs=4, default=[-4.6, -4.5, 90.4, 30.5],
                    help='plaque rectangle in the tree frame (x0 y0 x1 y1) for the floors')
    # outline
    ap.add_argument('--min-width', type=float, default=1.05, help='min stem width in XY (mm)')
    ap.add_argument('--width-soft', type=float, default=0.06, help='smooth-max knee for the thickening (mm)')
    ap.add_argument('--spur-prune', type=float, default=1.2, help='medial-axis length peeled from free ends (mm)')
    ap.add_argument('--outline-iters', type=int, default=4, help='thicken/close/open rounds')
    ap.add_argument('--width-res', type=float, default=0.02, help='raster res of the width analysis (mm)')
    ap.add_argument('--min-gap', type=float, default=0.55, help='narrower gaps are closed (mm)')
    ap.add_argument('--tip-radius', type=float, default=0.4, help='min convex corner radius (mm)')
    ap.add_argument('--hole-area', type=float, default=0.3, help='holes smaller than this are filled (mm^2)')
    ap.add_argument('--chamfer', type=float, default=0.3, help='45 deg bed chamfer (mm)')
    ap.add_argument('--flare-left', type=float, default=0.9, help='left root flare width at the cut (mm, 0 = off)')
    ap.add_argument('--flare-height', type=float, default=3.0, help='left root flare height (mm)')
    ap.add_argument('--flare-slope', type=float, default=0.35, help='height drop per mm across the flare')
    ap.add_argument('--contour-sigma', type=float, default=0.8, help='mask smoothing before contouring (px)')
    ap.add_argument('--outline-tol', type=float, default=0.002, help='outline simplification tolerance (mm)')
    # smoothing / detail
    ap.add_argument('--spike-thresh', type=float, default=0.12, help='median outlier threshold (mm)')
    ap.add_argument('--bilateral-ss', type=float, default=2.0, help='bilateral spatial sigma (px)')
    ap.add_argument('--bilateral-sr', type=float, default=0.05, help='bilateral range sigma (mm)')
    ap.add_argument('--bilateral-iters', type=int, default=2)
    ap.add_argument('--detail-gain', type=float, default=1.0, help='band-pass boost (0 = off)')
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
    ap.add_argument('--no-previews', action='store_true')
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    cache = a.cache or (SHARED_WORK if os.path.exists(os.path.join(SHARED_WORK, 'tree_front.npz')) and not a.regen
                        else os.path.join(HERE, 'cache'))
    if a.regen or not os.path.exists(os.path.join(cache, 'tree_front.npz')):
        regen_cache(a.glb, cache, a.res)
    F, B, ys, res = load_depth(cache)
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
    allv, allf, bot_rings, groups = assemble(V, T, Zv, rings, a.chamfer)
    # shift to the canonical frame
    allv[:, 0] -= x0
    allv[:, 1] -= ycut
    shift = (-x0, -ycut)
    checks, tm = run_checks(allv, allf, groups, a.chamfer)
    log('checks', checks)
    # footprint (glue face = bottom rings)
    from shapely.geometry import Polygon
    glue = Polygon(bot_rings[0], bot_rings[1:])
    glue = affinity.translate(glue, *shift)
    top_outline = affinity.translate(P3, *shift)
    expected = top_outline.buffer(-a.chamfer, quad_segs=16)
    sd = glue.symmetric_difference(expected).area
    write_outputs(a, allv, allf, groups, glue, top_outline, checks, tm, oinfo, mask_info, hstats, rhist,
                  x_left - x0, x_right - x0, sd, px, plane, Zext, region, shift, err)
    if not a.no_previews:
        import tree_previews
        # crop of the source hillshade covering the same region as the top view (+0.5 mm margin)
        bx = P3.bounds
        m_ = 0.5 / px
        hill_crop = (bx[0] / px - m_, -bx[3] / px - m_, bx[2] / px + m_, -bx[1] / px + m_)
        tree_previews.make_all(a.out, allv, allf, groups, top_outline, glue, hill_crop=hill_crop)
        tree_previews.layer_preview(a.out, Zext, region, px)
        log('previews written')
    import make_report
    make_report.main(a.out)
    log('done')


def write_outputs(a, V, Fc, groups, glue, top, checks, tm, oinfo, minfo, hstats, rhist,
                  xl, xr, sd, px, plane, Zext, region, shift, err):
    out = a.out
    tm.export(os.path.join(out, 'tree.stl'))
    log('wrote tree.stl')

    def ring_list(r):
        return [[round(float(x), 4), round(float(y), 4)] for x, y in np.asarray(r.coords)[:-1]]
    gb = glue.bounds
    fp = {
        'units': 'mm',
        'frame': 'canonical tree frame: origin = bottom-centre of trunk base, +x right, +y up (front view), '
                 'polygons are the bottom (glue) face outline',
        'scale_mm_per_model_unit': a.scale,
        'polygons': [{'exterior': ring_list(glue.exterior), 'holes': [ring_list(h) for h in glue.interiors]}],
        'bbox': [round(v, 3) for v in gb],
        'area_mm2': round(glue.area, 2),
        'trunk_base': {'x_left': round(xl, 3), 'x_right': round(xr, 3), 'y': 0.0},
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
    exact = {
        'tip_opening_residual_mm2': {'total': round(t_tot, 5), 'largest_piece': round(t_max, 5)},
        'gap_opening_residual_mm2': {'total': round(g_tot, 5), 'largest_piece': round(g_max, 5)},
        'tip_radius_tested_mm': round(a.tip_radius - 0.01, 3),
        'gap_tested_mm': round(a.min_gap - 0.02, 3),
        'note': 'exact shapely openings. Tips: residual ~0 means every convex corner of the top outline has '
                'radius >= tip_radius. Gaps: the residual is the sharp inner corners the last opening leaves '
                'where two discs meet (harmless V-notches) plus any gap narrower than min_gap.',
    }
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
        'variant': 'B (vector outline + adaptive constrained Delaunay)',
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
        'trunk_base': {'x_left': round(xl, 3), 'x_right': round(xr, 3), 'y': 0.0,
                       'note': 'ends of the straight bottom edge of the top outline (y = 0); the glue face '
                               'is inset by the chamfer'},
        'outline_area_mm2': round(top.area, 2),
        'glue_face_area_mm2': round(glue.area, 2),
        'glue_vs_exact_inset_symdiff_mm2': round(sd, 4),
        'min_width_top_outline': widths_top,
        'min_width_glue_face': widths_glue,
        'min_gap_top_outline': gaps,
        'convex_corner_radius_top_outline': tips,
        'exact_outline_checks': exact,
        'thickness_outside_plaque_mm': {'area_mm2': round(float(outside.sum() * px * px), 1),
                                        'min': round(float(z_out.min()), 3) if len(z_out) else None,
                                        'p1': round(float(np.percentile(z_out, 1)), 3) if len(z_out) else None},
        'thickness_over_plaque_mm': {'min': round(float(z_in.min()), 3), 'p1': round(float(np.percentile(z_in, 1)), 3)},
        'surface_error_mm': {'max': round(float(err.max()), 4), 'p99': round(float(np.percentile(err, 99)), 4),
                             'mean': round(float(err.mean()), 5), 'refine_history': rhist},
        'checks': checks,
        'outline_info': oinfo,
        'mask_info': minfo,
        'height_info': hstats,
        'back_plane_coef': [float(c) for c in plane],
        'parameters': {k: v for k, v in vars(a).items()},
        'shift_image_to_canonical_mm': list(shift),
    }
    with open(os.path.join(out, 'tree_meta.json'), 'w') as f:
        json.dump(meta, f, indent=1)
    # keep the height grid for previews / slice checks
    os.makedirs(a.work, exist_ok=True)
    np.savez_compressed(os.path.join(a.work, 'zgrid.npz'), Z=Zext, region=region, px=px, shift=np.array(shift))
    log('wrote footprint/meta')


if __name__ == '__main__':
    sys.path.insert(0, HERE)
    main()
