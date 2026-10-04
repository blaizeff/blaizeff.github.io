#!/usr/bin/env python3
"""Gold tree relief for the wedding place cards, variant A ("faithful SDF heightfield").

Turns part 0 of the Tripo scene (the tree) into a print-ready FDM relief: flat glue face on
the bed (z = 0), sculpted relief up, no overhangs, one watertight manifold body.

Method
  1. Orthographic front/back depth maps of the tree (embree ray casts). Cached .npz files are
     used when present; --regen decodes the meshopt GLB with gltf-transform and re-casts them.
  2. Relief r(x, y) = front surface height above the tree's flat back plane, in mm.
     Edge-preserving bilateral denoise, optional gentle unsharp detail boost.
  3. Outline as a 2D signed distance field d(x, y) (mm, negative inside) from a smoothed
     sub-pixel mask: straight trunk cut with a concave root flare, specks and pin-holes removed,
     narrow gaps either joined (only if the join is >= min width) or opened to the min gap,
     concave corners filleted, thin stems thickened along the pruned medial axis (only where
     too thin; free ends get a smaller rounded cap), convex corners rounded (opening).
     Relief in added areas: stems re-mapped across their widened profile, flare and fills
     solved as harmonic (Laplace) patches.
  4. Height h = base + gain * (r - r_ref), soft floor and soft cap.
  5. 3D field f = max(K * (d + max(0, c - z)), z - h, -z), with a width-adaptive bed chamfer
     c(x, y) (0.15 mm on 1 mm stems, 0.3 mm from 1.6 mm features up), meshed with marching cubes
     in row tiles on z levels that are fine through the chamfer and coarse above (the field is
     linear in z there, K keeps the rim crisp), then error-bounded simplification (manifold3d:
     a subset of the vertices, every surface moves less than --tolerance) and the checks.
  6. Footprint = outline of the z = 0 faces of the final mesh; previews ray-cast from the mesh.

Run (from anywhere):
  python3 build_tree.py                 # default parameters, outputs in ./out
  python3 build_tree.py --regen         # rebuild the depth maps from the GLB first
  python3 build_tree.py --help          # all parameters
Optional: python3 slice_check.py (PrusaSlicer G-code coverage check of out/tree.stl).
"""
import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np
import scipy.ndimage as ndi
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from scipy.sparse.csgraph import dijkstra

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_PC = os.path.abspath(os.path.join(HERE, '..', '..', '..'))  # place-cards/
WORK = '/tmp/claude-0/-home-user-blaizeff-github-io/24db3fd0-37aa-5abe-8f21-849f3f5e2464/scratchpad/work'
PAD = 48  # px of empty margin around the depth maps (EDT, marching cubes need outside space)
T0 = time.time()


def log(*a):
    print(f'[{time.time() - T0:7.1f}s]', *a, flush=True)


# ----------------------------------------------------------------------------- depth maps
def regen_depth(glb, cache_dir, res):
    """Decode the meshopt GLB, take node tripo_part_0 in world space, cast front/back depth maps.
    Same conventions as $WORK/depthmap.py: column -> world z, row -> world y (top = max y)."""
    import trimesh
    os.makedirs(cache_dir, exist_ok=True)
    dec = os.path.join(cache_dir, 'decoded.glb')
    log('decoding GLB (meshopt) with gltf-transform ...')
    subprocess.run(['npx', '-y', '@gltf-transform/cli@4', 'copy', glb, dec], check=True,
                   stdout=subprocess.DEVNULL)
    scene = trimesh.load(dec)
    T, gname = scene.graph.get('tripo_part_0')
    mesh = scene.geometry[gname].copy()
    mesh.apply_transform(T)
    mesh.export(os.path.join(cache_dir, 'tree_part0_raw.ply'))
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
        np.savez_compressed(os.path.join(cache_dir, f'tree_{name}.npz'),
                            depth=depth.reshape(Z.shape), zs=zs, ys=ys, res=res)
        log(f'  {name} depth map', Z.shape, 'coverage', float(np.mean(~np.isnan(depth))))


def load_depth(a):
    d = a.depth_dir
    need = [os.path.join(d, f'tree_{k}.npz') for k in ('front', 'back')]
    if a.regen or not all(os.path.exists(p) for p in need):
        d = a.regen_dir
        regen_depth(a.glb, d, a.res)
    f = np.load(os.path.join(d, 'tree_front.npz'))
    b = np.load(os.path.join(d, 'tree_back.npz'))
    # flip columns: tree on the left, canopy sweeping right (front view)
    front = f['depth'][:, ::-1].astype(np.float64)
    back = b['depth'][:, ::-1].astype(np.float64)
    ys = f['ys']
    res = float(f['res'])
    front = np.pad(front, PAD, constant_values=np.nan)
    back = np.pad(back, PAD, constant_values=np.nan)
    log(f'depth maps from {d}: {front.shape} (padded), res {res} units/px')
    return front, back, ys, res


# ----------------------------------------------------------------------------- 2D helpers
def sdf_from_mask(m, px):
    """Signed distance (mm, negative inside) of a binary mask, half-pixel corrected."""
    out = ndi.distance_transform_edt(~m)
    inn = ndi.distance_transform_edt(m)
    return np.where(m, -(inn - 0.5), out - 0.5) * px


def redistance(d, px):
    """Make d a true signed distance again after unions/intersections (which leave stale values
    away from the new boundary). Keeps the sub-pixel field right at the boundary, EDT elsewhere."""
    m = d < 0
    s = ndi.gaussian_filter(sdf_from_mask(m, px), 0.7)
    w = np.clip((np.abs(s) - 1.5 * px) / (1.5 * px), 0, 1)
    out = (1 - w) * d + w * s
    return np.where(m, np.minimum(out, -1e-6), np.maximum(out, 1e-6))


def nearest_fill(v, known):
    """Extend v from the known pixels to the whole grid by nearest neighbour."""
    idx = ndi.distance_transform_edt(~known, return_distances=False, return_indices=True)
    return v[idx[0], idx[1]]


def norm_gauss(v, m, sigma):
    """Gaussian blur of v restricted to mask m (normalised convolution)."""
    w = ndi.gaussian_filter(m.astype(np.float64), sigma)
    g = ndi.gaussian_filter(np.where(m, v, 0.0), sigma)
    return g / np.maximum(w, 1e-9)


def largest_component(m):
    lab, n = ndi.label(m, structure=np.ones((3, 3)))
    if n <= 1:
        return m, n
    sz = ndi.sum(m, lab, range(1, n + 1))
    return lab == (int(np.argmax(sz)) + 1), n


def fill_small_holes(m, max_px):
    bg, n = ndi.label(~m)  # 4-connected background
    if n == 0:
        return m, 0
    sz = ndi.sum(~m, bg, range(1, n + 1))
    border = np.unique(np.r_[bg[0], bg[-1], bg[:, 0], bg[:, -1]])
    small = [i + 1 for i in range(n) if sz[i] <= max_px and (i + 1) not in border]
    if small:
        m = m | np.isin(bg, small)
    return m, len(small)


def harmonic_fill(v, known, domain, outer=None, outer_value=0.0):
    """Solve Laplace(v) = 0 on domain & ~known, Dirichlet from known pixels (and from the pixels of
    `outer` just outside the domain, at outer_value), Neumann elsewhere."""
    unk = domain & ~known
    ys, xs = np.nonzero(unk)
    n = len(ys)
    if n == 0:
        return v
    idx = -np.ones(v.shape, np.int64)
    idx[ys, xs] = np.arange(n)
    rows, cols, vals = [], [], []
    rhs = np.zeros(n)
    diag = np.zeros(n)
    for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        y2, x2 = ys + dy, xs + dx
        inside = domain[y2, x2]
        diag += inside
        nb_unk = inside & unk[y2, x2]
        rows.append(np.nonzero(nb_unk)[0])
        cols.append(idx[y2[nb_unk], x2[nb_unk]])
        nb_kn = inside & ~unk[y2, x2]
        rhs[nb_kn] += v[y2[nb_kn], x2[nb_kn]]
        if outer is not None:
            ob = ~inside & outer[y2, x2]
            diag += ob
            rhs[ob] += outer_value
    rows = np.concatenate(rows)
    cols = np.concatenate(cols)
    diag = np.maximum(diag, 1)
    A = sp.csr_matrix((np.r_[diag, -np.ones(len(rows))], (np.r_[np.arange(n), rows], np.r_[np.arange(n), cols])),
                      shape=(n, n))
    sol = spla.spsolve(A.tocsc(), rhs)
    out = v.copy()
    out[ys, xs] = sol
    return out


def skeleton_graph(sk):
    """Pixel skeleton -> (ys, xs, csr graph). Diagonal links are dropped when a 4-path exists,
    so plain curves have degree 2 and junction/endpoint detection works."""
    H, W = sk.shape
    ys, xs = np.nonzero(sk)
    n = len(ys)
    idx = -np.ones((H, W), np.int64)
    idx[ys, xs] = np.arange(n)
    R, C, Wt = [], [], []
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        y2, x2 = ys + dy, xs + dx
        ok = sk[y2, x2]
        if dy and dx:
            ok &= ~(sk[ys, xs + dx] | sk[ys + dy, xs])
        R.append(np.nonzero(ok)[0])
        C.append(idx[y2[ok], x2[ok]])
        Wt.append(np.full(ok.sum(), np.hypot(dy, dx)))
    R, C, Wt = np.concatenate(R), np.concatenate(C), np.concatenate(Wt)
    G = sp.coo_matrix((np.r_[Wt, Wt], (np.r_[R, C], np.r_[C, R])), shape=(n, n)).tocsr()
    return ys, xs, G


def prune_spurs(G, dt_nodes, px, ratio, extra):
    """Remove terminal skeleton branches that barely leave the inscribed disk at their junction
    (boundary noise). Returns keep mask over nodes and the branch count removed."""
    n = G.shape[0]
    deg = np.diff(G.indptr)
    keep = np.ones(n, bool)
    removed = 0
    for e in np.nonzero(deg == 1)[0]:
        path = [e]
        prev, cur, L = -1, e, 0.0
        while True:
            s, t = G.indptr[cur], G.indptr[cur + 1]
            nb = G.indices[s:t]
            wt = G.data[s:t]
            k = np.nonzero(nb != prev)[0]
            if len(k) == 0:
                break
            prev, cur = cur, nb[k[0]]
            L += wt[k[0]] * px
            path.append(cur)
            if deg[cur] != 2:
                break
        if deg[cur] >= 3 and L < ratio * dt_nodes[cur] + extra:
            keep[path[:-1]] = False
            removed += 1
    return keep, removed


# ----------------------------------------------------------------------------- outline
def close_gaps(d, px, ic, a, carve):
    """Closing by gap/2. Fills that touch the outline along one arc are concave-corner fillets:
    kept. Fills bridging two separate parts are kept only when the bridge is at least min_width
    wide (a deliberate join). With carve=True the remaining gaps narrower than 0.8 * gap are opened
    to gap + 2 px by carving disks on the background medial axis."""
    from skimage.morphology import medial_axis
    H, W = d.shape
    eps = px
    info = {}
    d = redistance(d, px)
    rg = a.gap / 2 + eps
    c = sdf_from_mask(d < rg, px) + rg
    newd = np.minimum(d, c + eps)
    Mo = d < 0
    F = (newd < 0) & ~Mo
    lab, n = ndi.label(F, structure=np.ones((3, 3)))
    sk1 = medial_axis(newd < 0, rng=0)
    bnd = Mo & ~ndi.binary_erosion(Mo)
    rejected, bridges = [], 0
    for k, sl in enumerate(ndi.find_objects(lab)):
        sl = tuple(slice(max(s_.start - 4, 0), s_.stop + 4) for s_ in sl)
        Fk = lab[sl] == k + 1
        contact = ndi.binary_dilation(Fk, iterations=2) & bnd[sl]
        if ndi.label(contact, structure=np.ones((3, 3)))[1] < 2:
            continue
        bridges += 1
        nodes = sk1[sl] & ndi.binary_dilation(Fk, iterations=2)
        neck = 2 * float((-newd[sl][nodes]).min()) if nodes.any() else 0.0
        if neck < a.min_width:
            rejected.append(k + 1)
    if rejected:
        rej = ndi.binary_dilation(np.isin(lab, rejected), iterations=3)
        newd = np.where(rej, d, newd)
    info['bridges'] = bridges
    info['bridges_rejected'] = len(rejected)
    info['closed'] = (d >= 0) & (newd < 0)
    info['closed_area'] = float(info['closed'].sum() * px * px)
    d = redistance(newd, px)
    if carve:
        rr_, cc_ = np.nonzero(d < 0)
        box = (slice(rr_.min() - 20, rr_.max() + 20), slice(cc_.min() - 20, cc_.max() + 20))
        skb = medial_axis(np.pad(d[box] >= 0, 1), rng=0)[1:-1, 1:-1]
        Q = np.zeros((H, W), bool)
        Q[box] = skb & (d[box] < 0.8 * a.gap / 2) & (d[box] > 0)
        Q &= (np.arange(H)[:, None] < ic - 2)
        info['gap_nodes_opened'] = int(Q.sum())
        if Q.any():
            cv = rg - ndi.distance_transform_edt(~Q) * px
            info['opened_gap_area'] = float(((d < 0) & (cv >= 0)).sum() * px * px)
            d = redistance(np.maximum(d, cv), px)
    return d, info


def build_outline(r_nn, m0, px, ic, a, dbg):
    """Returns the final signed distance field d (mm) and a dict of masks used to rebuild r."""
    H, W = m0.shape
    rows = np.arange(H)[:, None].astype(np.float64)
    cut_sdf = np.broadcast_to((rows - ic) * px, (H, W))  # > 0 below the cut line
    info = {}

    d = ndi.gaussian_filter(sdf_from_mask(m0, px), a.outline_smooth / px)
    d = np.maximum(d, cut_sdf)

    # --- root flare on the left of the trunk: widen the last flare_height mm with a concave curve.
    # The outer edge is analytic (fitted trunk edge minus a smooth offset), so it blends without kinks.
    flare = np.zeros((H, W), bool)
    if a.flare_left > 0 and a.flare_height > 0:
        nfl = int(np.ceil(a.flare_height / px))
        i_bot = int(np.floor(ic))
        jc = int(np.median(np.nonzero(d[i_bot - 2] < 0)[0]))  # a column inside the trunk near the bottom
        fit_rows, fit_x = [], []
        for i in range(i_bot - int(0.9 * nfl), i_bot - 2):
            row = d[i]
            j = jc
            if row[j] >= 0:
                continue
            while j > 0 and row[j - 1] < 0:
                j -= 1
            t = row[j - 1] / (row[j - 1] - row[j])  # sub-pixel zero crossing
            fit_rows.append(i)
            fit_x.append(j - 1 + t)
        fit_rows, fit_x = np.array(fit_rows, float), np.array(fit_x)
        pc = np.polyfit(fit_rows, fit_x, 1)
        ok = np.abs(np.polyval(pc, fit_rows) - fit_x) < 3  # drop rows that run into a petiole
        pc = np.polyfit(fit_rows[ok], fit_x[ok], 1)
        info['flare_fit_resid_px'] = float(np.abs(np.polyval(pc, fit_rows[ok]) - fit_x[ok]).max())
        ii = np.arange(H, dtype=np.float64)
        y = (ic - ii) * px
        xl = np.polyval(pc, ii)
        o = a.flare_left * np.clip(1.0 - y / a.flare_height, 0, 1) ** a.flare_power
        e = xl + 1.0 - o / px  # outer flare edge (px); 1 px inside the trunk where o -> 0
        de = np.gradient(e)
        cos_t = 1.0 / np.sqrt(1.0 + de ** 2)
        jj = np.arange(W, dtype=np.float64)[None, :]
        g = (e[:, None] - jj) * px * cos_t[:, None]           # < 0 right of the flare edge
        g = np.maximum(g, (jj - (xl[:, None] + 6)) * px)        # only a strip along the trunk edge
        band = (y >= -1.0) & (y <= a.flare_height)
        g[~band] = np.inf
        g = np.maximum(g, (y - a.flare_height)[:, None])        # nothing above the flare top
        d_before = d
        d = np.minimum(d, np.maximum(g, cut_sdf))
        flare = (d < 0) & (d_before >= 0)
        info['flare_cols'] = xl
    info['flare'] = flare & ~m0

    # --- gaps on the source outline: fillet concave corners, join or open narrow gaps
    if a.gap > 0:
        d, ci = close_gaps(d, px, ic, a, carve=True)
        d = np.maximum(d, cut_sdf)
        info.update({k + '_src': v for k, v in ci.items() if k != 'closed'})

    # --- thicken thin stems along the pruned medial axis
    from skimage.morphology import medial_axis
    M = d < 0
    # extrude the bottom row downwards so the cut corners do not read as thin tips
    i_bot = int(np.floor(ic))
    n_ext = int(np.ceil(3.0 / px))
    # the whole bottom row is extruded: the root flare tips run into the cut, they are not twigs
    core = M[i_bot] | M[i_bot - 1] | M[i_bot - 2]
    M_ext = M.copy()
    M_ext[i_bot + 1:i_bot + 1 + n_ext] = core[None, :]
    M_ext[i_bot - 1:i_bot + 1] = core[None, :] | M[i_bot - 1:i_bot + 1]
    sk, dist = medial_axis(M_ext, return_distance=True, rng=0)
    ys, xs, G = skeleton_graph(sk)
    dt_nodes = (dist[ys, xs] - 0.5) * px
    keep, nrem = prune_spurs(G, dt_nodes, px, a.prune_ratio, a.prune_extra)
    ys, xs, dt_nodes = ys[keep], xs[keep], dt_nodes[keep]
    G = G[keep][:, keep]
    deg = np.diff(G.indptr)
    ends = np.nonzero(deg <= 1)[0]
    s_end = dijkstra(G, indices=ends, min_only=True) * px if len(ends) else np.full(len(ys), 1e9)
    r_min = a.min_width / 2 + a.width_margin
    r_req = a.tip_radius + (r_min - a.tip_radius) * np.clip(s_end / a.tip_taper, 0, 1)
    thin = (dt_nodes < r_req) & (ys < ic)  # nodes of the helper extrusion below the cut never count
    info['skeleton_nodes'] = int(len(ys))
    info['spurs_pruned'] = int(nrem)
    info['thin_nodes'] = int(thin.sum())
    tube = np.full((H, W), np.inf)
    if thin.any():
        ty, tx, tr, tdt = ys[thin], xs[thin], r_req[thin], dt_nodes[thin]
        # tubes along the cut line (the right root flare) are lifted so the cut keeps their full width
        lift = np.ceil(np.maximum(0, tr - (ic - ty) * px) / px).astype(int)
        ty = ty - lift
        levels = np.round(tr / 0.025) * 0.025
        for lv in np.unique(levels):
            pts = np.zeros((H, W), bool)
            sel = levels == lv
            pts[ty[sel], tx[sel]] = True
            tube = np.minimum(tube, ndi.distance_transform_edt(~pts) * px - lv)
        # nearest thin skeleton node per pixel, for re-mapping the relief across widened stems
        pts = np.zeros((H, W), bool)
        pts[ty, tx] = True
        dist_t, (ny, nx) = ndi.distance_transform_edt(~pts, return_indices=True)
        node_id = -np.ones((H, W), np.int64)
        node_id[ty, tx] = np.arange(len(ty))
        nid = node_id[ny, nx]
        scale = np.clip(tdt[nid] / tr[nid], 0.3, 1.0)
        region = (tube < d + 0.5 * px) & (dist_t * px < tr[nid] + px)
        src_y = ny + (np.arange(H)[:, None] - ny) * scale
        src_x = nx + (np.arange(W)[None, :] - nx) * scale
        info['tube_region'] = region
        info['tube_src'] = (src_y, src_x)
        d = np.minimum(d, tube)
        d = np.maximum(d, cut_sdf)
    else:
        info['tube_region'] = np.zeros((H, W), bool)
    dbg['thin_pts'] = (ys[thin], xs[thin]) if thin.any() else ([], [])

    # --- opening: round convex corners and drop slivers thinner than 2 * open_radius
    eps = px
    d = redistance(d, px)
    if a.open_radius > 0:
        er = d < -a.open_radius
        o = sdf_from_mask(er, px) - a.open_radius
        info['opened_area'] = float(((d < 0) & (np.maximum(d, o - eps) >= 0)).sum() * px * px)
        d = np.maximum(d, o - eps)
    # --- closing: fill gaps narrower than 2 * gap/2 and round concave corners
    if a.gap > 0:
        d, ci = close_gaps(d, px, ic, a, carve=False)
        info.update({k + '_final': v for k, v in ci.items() if k != 'closed'})
        info['closed'] = ci['closed']
    d = np.maximum(d, cut_sdf)
    d = ndi.gaussian_filter(d, a.final_smooth / px)
    d = redistance(np.maximum(d, cut_sdf), px)

    # --- specks and pin-holes on the final mask
    M = d < 0
    M2, ncomp = largest_component(M)
    M2, nholes = fill_small_holes(M2, a.hole_area / (px * px))
    info['components_before'] = int(ncomp)
    info['holes_filled'] = int(nholes)
    if (M2 != M).any():
        # removed specks: push outside; filled holes: pull inside
        d = np.where(M2 & ~M, -0.5 * px, d)
        d = np.where(~M2 & M, np.maximum(d, 0.5 * px), d)
        d = np.where(~M2, np.maximum(d, sdf_from_mask(M2, px)), d)
        d = redistance(d, px)
    return d, info


# ----------------------------------------------------------------------------- meshing
def z_levels(cmax, zmax, dz_fine, dz_top):
    """Marching-cubes z levels: fine through the chamfer zone, coarse above. z = 0 sits exactly
    mid-cell (levels -dz/2, +dz/2), so the bottom vertices interpolate to z = 0 exactly.
    The field is linear in z inside every cell except at the chamfer/wall crease, so coarse
    levels above the chamfer lose nothing on the top surface or the walls."""
    zl = list(np.arange(-0.5, np.ceil((cmax + 0.15) / dz_fine) + 0.5) * dz_fine)
    while zl[-1] < zmax + dz_top:
        zl.append(zl[-1] + dz_top)
    return np.array(zl)


def marching_tiles(d, h, c, px, zl, tile_rows, wall_k=25.0):
    """Tiled marching cubes of f = max(K * (d + max(0, c - z)), z - h, -z) on z levels zl.
    The wall term is scaled by K (same zero set) so that along tall z cells the top-surface
    term wins everywhere except within dz/K of the wall: the rim stays crisp with coarse levels.
    Returns verts as (row, col, z_mm) and faces."""
    from skimage.measure import marching_cubes
    H, W = d.shape
    nz = len(zl)
    # K = 1 through the chamfer zone (keeps the bed/chamfer corner exact), K above it
    kz_from = float(np.nanmax(c)) + 0.1
    inside = d < 0
    rr = np.nonzero(inside.any(1))[0]
    cc = np.nonzero(inside.any(0))[0]
    r0, r1 = rr[0] - 3, rr[-1] + 4
    c0, c1 = cc[0] - 3, cc[-1] + 4
    V, F = [], []
    nv = 0
    for a in range(r0, r1 - 1, tile_rows):
        b = min(a + tile_rows, r1 - 1)
        dd = d[a:b + 1, c0:c1, None].astype(np.float32)
        hh = h[a:b + 1, c0:c1, None].astype(np.float32)
        ch = c[a:b + 1, c0:c1, None].astype(np.float32)
        z = zl[None, None, :].astype(np.float32)
        kz = np.where(zl > kz_from, wall_k, 1.0)[None, None, :].astype(np.float32)
        f = np.maximum(np.maximum(kz * (dd + np.maximum(0, ch - z)), z - hh), -z)
        if f.min() >= 0:
            continue
        v, fc, _, _ = marching_cubes(f, 0.0, allow_degenerate=False)
        v[:, 0] += a
        v[:, 1] += c0
        k = np.clip(np.floor(v[:, 2]).astype(int), 0, nz - 2)
        v[:, 2] = zl[k] + (v[:, 2] - k) * (zl[k + 1] - zl[k])
        V.append(v)
        F.append(fc + nv)
        nv += len(v)
    V = np.concatenate(V)
    F = np.concatenate(F)
    # tiles share their boundary row: identical vertices -> merge exactly
    uq, inv = np.unique(V, axis=0, return_inverse=True)
    F = inv.reshape(-1)[F]
    F = F[(F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])]
    return uq, F


def bed_snap(V, F, snap):
    """Exact planar glue face: vertices below `snap` go to z = 0. Only z moves, so a face keeps the
    orientation of its xy projection; the few faces that would still fold or collapse onto the bed
    get their vertices back (iterated until none is left)."""
    V = np.asarray(V, np.float64).copy()
    V0 = V.copy()

    def nz(P):
        T = P[F]
        return np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])[:, 2]
    nz0 = nz(V0)
    low = V[:, 2] < snap
    V[low, 2] = 0.0
    for _ in range(50):
        n1 = nz(V)
        flat = (V[F][:, :, 2] == 0).all(1)
        bad = (np.sign(n1) != np.sign(nz0)) & (np.abs(nz0) > 1e-14)
        bad |= flat & (n1 >= 0) & ~((V0[F][:, :, 2] == 0).all(1))
        if not bad.any():
            break
        vs = np.unique(F[bad])
        vs = vs[low[vs]]
        if len(vs) == 0:
            break
        V[vs, 2] = V0[vs, 2]
        low[vs] = False
    V[np.abs(V[:, 2]) < 1e-9, 2] = 0.0
    return V


# ----------------------------------------------------------------------------- previews
GOLD = np.array([0.83, 0.66, 0.22])


def shade_render(mesh, view_dir, up, px_mm, lights, bounds2d=None, shadows=True, bg=(1, 1, 1),
                 smooth_angle=np.radians(35)):
    """Orthographic ray-cast render with interpolated normals, Blinn-Phong gold, cast shadows."""
    import trimesh
    vd = np.asarray(view_dir, float)
    vd /= np.linalg.norm(vd)
    upv = np.asarray(up, float)
    rgt = np.cross(vd, upv)
    rgt /= np.linalg.norm(rgt)
    upv = np.cross(rgt, vd)
    V = mesh.vertices
    pu, pv = V @ rgt, V @ upv
    if bounds2d is None:
        bounds2d = (pu.min() - 1, pu.max() + 1, pv.min() - 1, pv.max() + 1)
    u0, u1, v0, v1 = bounds2d
    Wp = int(round((u1 - u0) / px_mm))
    Hp = int(round((v1 - v0) / px_mm))
    U, Vv = np.meshgrid(u0 + (np.arange(Wp) + 0.5) * px_mm, v1 - (np.arange(Hp) + 0.5) * px_mm)
    back = (V @ vd).min() - 5
    O = U.reshape(-1, 1) * rgt + Vv.reshape(-1, 1) * upv + back * vd
    D = np.tile(vd, (len(O), 1))
    tri, ray, loc = mesh.ray.intersects_id(O, D, multiple_hits=False, return_locations=True)
    img = np.tile(np.asarray(bg, float), (Hp * Wp, 1))
    if len(tri) == 0:
        return img.reshape(Hp, Wp, 3)
    # Phong normals: per-corner vertex normal, or the face normal across sharp creases (rim, chamfer)
    fn = mesh.face_normals[tri]
    vn = mesh.vertex_normals[mesh.faces[tri]]
    cos_lim = np.cos(smooth_angle)
    vn = np.where((vn * fn[:, None, :]).sum(2, keepdims=True) > cos_lim, vn, fn[:, None, :])
    bary = trimesh.triangles.points_to_barycentric(mesh.triangles[tri], loc)
    n = (vn * bary[:, :, None]).sum(1)
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    col = np.zeros((len(tri), 3)) + 0.18 * GOLD
    eye = -vd
    for L, inten in lights:
        L = np.asarray(L, float)
        L /= np.linalg.norm(L)
        ndl = np.clip(n @ L, 0, 1)
        lit = np.ones(len(tri))
        if shadows:
            o2 = loc + n * 0.01 + L * 0.02
            hit = mesh.ray.intersects_any(o2, np.tile(L, (len(o2), 1)))
            lit = np.where(hit, 0.15, 1.0)
        hv = L + eye
        hv /= np.linalg.norm(hv)
        spec = np.clip(n @ hv, 0, 1) ** 45
        col += inten * lit[:, None] * (0.75 * ndl[:, None] * GOLD + 0.55 * spec[:, None] * np.array([1.0, 0.92, 0.7]))
    img[ray] = np.clip(col, 0, 1)
    return img.reshape(Hp, Wp, 3)


def save_png(img, path):
    from PIL import Image
    Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8)).save(path)


def make_previews(mesh, out, a):
    from PIL import Image, ImageDraw
    b = mesh.bounds
    top_lights = [((-1, 1, 1.4), 1.0), ((1, -0.3, 0.8), 0.25)]
    obl_lights = [((-0.5, 0.8, 0.7), 1.0), ((1, 0.1, 0.6), 0.3)]
    # 1. top view (true proportions), lit from the upper left like a hillshade
    top = shade_render(mesh, (0, 0, -1), (0, 1, 0), 0.04, top_lights,
                       bounds2d=(b[0, 0] - 1, b[1, 0] + 1, b[0, 1] - 1, b[1, 1] + 1))
    save_png(top, os.path.join(out, 'preview_top.png'))
    # 2. oblique: as the tree stands on the card, seen from the front, a little left and above
    vd = -np.array([-0.35, 0.45, 1.0])
    obl = shade_render(mesh, vd, (0, 1, 0), 0.045, obl_lights)
    save_png(obl, os.path.join(out, 'preview_oblique.png'))
    # 3. side profile: elevation from below the trunk (looking +y), true scale and z x4
    side = shade_render(mesh, (0, 1, 0), (0, 0, 1), 0.03, [((-0.5, -1, 0.8), 1.0)], shadows=False)
    Hs, Ws = side.shape[:2]
    big = np.array(Image.fromarray((side * 255).astype(np.uint8)).resize((Ws, Hs * 4), Image.NEAREST)) / 255
    canvas = np.ones((Hs + 24 + Hs * 4 + 24, Ws, 3))
    canvas[20:20 + Hs] = side
    canvas[Hs + 44:Hs + 44 + Hs * 4] = big
    im = Image.fromarray((canvas * 255).astype(np.uint8))
    dr = ImageDraw.Draw(im)
    dr.text((5, 4), f'side elevation looking +y (trunk side), true scale; height {b[1, 2]:.2f} mm, width {b[1, 0] - b[0, 0]:.1f} mm',
            fill=(0, 0, 0))
    dr.text((5, Hs + 28), 'same, z exaggerated 4x', fill=(0, 0, 0))
    im.save(os.path.join(out, 'preview_side.png'))
    make_profiles(mesh, out, a)
    # 4. close-ups (25 px/mm): a leaf cluster at the canopy's right end, and the trunk base
    x1, y1 = b[1, 0], b[1, 1]
    closeup_render(mesh, out, 'leaf_cluster', (x1 - 19, x1 + 0.5, y1 - 25, y1 - 11), px=0.04)
    closeup_render(mesh, out, 'trunk_base', (-10, 10, -0.8, 11), px=0.04)
    # 5. side by side with the source hillshade
    hill_p = os.path.join(a.depth_dir, 'tree_hill.png')
    if os.path.exists(hill_p):
        hill = Image.open(hill_p).convert('RGB')
        topi = Image.open(os.path.join(out, 'preview_top.png'))
        hgt = 900
        hill_r = hill.resize((int(hill.width * hgt / hill.height), hgt))
        top_r = topi.resize((int(topi.width * hgt / topi.height), hgt))
        sbs = Image.new('RGB', (hill_r.width + top_r.width + 30, hgt + 30), 'white')
        sbs.paste(hill_r, (0, 30))
        sbs.paste(top_r, (hill_r.width + 30, 30))
        dr = ImageDraw.Draw(sbs)
        dr.text((10, 8), 'source: Tripo front depth hillshade (uncut)', fill=(0, 0, 0))
        dr.text((hill_r.width + 40, 8), 'variant A: tree.stl top view render', fill=(0, 0, 0))
        sbs.save(os.path.join(out, 'compare_hillshade.png'))


def make_profiles(mesh, out, a):
    """Cross-section profiles of the top surface (ray cast), z exaggerated 3x."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    b = mesh.bounds
    cuts = [('trunk + root flare, y = 5 mm', (-11, 5), (13, 5)),
            ('across the canopy, y = 40 mm', (b[0, 0], 40), (b[1, 0], 40)),
            ('right leaf cluster, x = 40 mm', (40, 22), (40, b[1, 1]))]
    fig, axs = plt.subplots(len(cuts), 1, figsize=(13, 7.5))
    for ax, (title, p0, p1) in zip(axs, cuts):
        p0, p1 = np.array(p0, float), np.array(p1, float)
        L = np.linalg.norm(p1 - p0)
        t = np.arange(0, L, 0.01)
        P = p0 + np.outer(t / L, p1 - p0)
        O = np.c_[P, np.full(len(P), 20.0)]
        loc, ray, _ = mesh.ray.intersects_location(O, np.tile([0, 0, -1.0], (len(O), 1)), multiple_hits=False)
        z = np.full(len(O), np.nan)
        z[ray] = loc[:, 2]
        ax.fill_between(t, 0, np.nan_to_num(z), where=~np.isnan(z), color='#D4AF37', lw=0, step='mid')
        ax.plot(t, z, color='#7a5c10', lw=0.8)
        ax.axhline(a.floor, color='#888888', lw=0.8, ls='--')
        ax.axhline(a.max_thickness, color='#888888', lw=0.8, ls=':')
        ax.set_aspect(3.0)
        ax.set_ylim(0, 4.2)
        ax.set_xlim(0, L)
        ax.set_title(f'{title}  (z exaggerated 3x; dashed = floor {a.floor} mm, dotted = max {a.max_thickness} mm)',
                     fontsize=9, loc='left', color='#333333')
        ax.tick_params(labelsize=8, colors='#555555')
        for sp_ in ax.spines.values():
            sp_.set_color('#bbbbbb')
        ax.set_ylabel('z (mm)', fontsize=8, color='#555555')
    axs[-1].set_xlabel('distance along the cut (mm)', fontsize=8, color='#555555')
    fig.tight_layout()
    fig.savefig(os.path.join(out, 'preview_profiles.png'), dpi=130)
    plt.close(fig)


def closeup_render(mesh, out, name, box, px=0.04):
    """Top and oblique close-up of a region box = (x0, x1, y0, y1) in mm."""
    from PIL import Image, ImageDraw
    x0, x1, y0, y1 = box
    cen = mesh.triangles_center
    sel = (cen[:, 0] > x0 - 12) & (cen[:, 0] < x1 + 12) & (cen[:, 1] > y0 - 12) & (cen[:, 1] < y1 + 12)
    sub = mesh.submesh([np.nonzero(sel)[0]], append=True)
    top = shade_render(sub, (0, 0, -1), (0, 1, 0), px, [((-1, 1, 1.4), 1.0), ((1, -0.3, 0.8), 0.25)],
                       bounds2d=(x0, x1, y0, y1))
    vd = -np.array([-0.35, 0.5, 1.0])
    vd /= np.linalg.norm(vd)
    rgt = np.cross(vd, [0, 1, 0])
    rgt /= np.linalg.norm(rgt)
    upv = np.cross(rgt, vd)
    corners = np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (0, 3.6)])
    pu, pv = corners @ rgt, corners @ upv
    obl = shade_render(sub, vd, (0, 1, 0), px, [((-0.5, 0.8, 0.7), 1.0), ((1, 0.1, 0.6), 0.3)],
                       bounds2d=(pu.min(), pu.max(), pv.min(), pv.max()))
    t = Image.fromarray((top * 255).astype(np.uint8))
    o = Image.fromarray((obl * 255).astype(np.uint8))
    hgt = max(t.height, o.height)
    can = Image.new('RGB', (t.width + o.width + 20, hgt + 44), 'white')
    can.paste(t, (0, 20))
    can.paste(o, (t.width + 20, 20))
    dr = ImageDraw.Draw(can)
    # 1 mm and 5 mm scale bars
    sb = int(round(5 / px))
    dr.rectangle([10, hgt + 30, 10 + sb, hgt + 34], fill=(0, 0, 0))
    dr.text((14 + sb, hgt + 26), '5 mm', fill=(0, 0, 0))
    dr.text((10, 4), f'{name}: top view ({1 / px:.0f} px/mm) | oblique', fill=(0, 0, 0))
    can.save(os.path.join(out, f'closeup_{name}.png'))


# ----------------------------------------------------------------------------- checks
def mesh_checks(mesh, chamfer_max, tol=1e-6):
    import trimesh
    import manifold3d
    res = {}
    res['is_watertight'] = bool(mesh.is_watertight)
    res['is_winding_consistent'] = bool(mesh.is_winding_consistent)
    res['body_count'] = int(mesh.body_count)
    res['volume_mm3'] = float(mesh.volume)
    res['euler_number'] = int(mesh.euler_number)
    try:
        mf = manifold3d.Manifold(manifold3d.Mesh(np.asarray(mesh.vertices, np.float32),
                                                 np.asarray(mesh.faces, np.uint32)))
        res['manifold3d_status'] = str(mf.status())
        res['manifold3d_volume_mm3'] = float(mf.volume())
        res['manifold3d_genus'] = int(mf.genus())
    except Exception as e:  # noqa
        res['manifold3d_status'] = f'ERROR {e}'
    V, F = mesh.vertices, mesh.faces
    z = V[:, 2]
    res['z_min'] = float(z.min())
    fz = z[F]
    fn = mesh.face_normals
    bottom = (np.abs(fz) <= tol).all(1)
    res['bottom_faces'] = int(bottom.sum())
    res['bottom_faces_normal_ok'] = bool((fn[bottom, 2] < -0.999999).all())
    res['vertices_below_zero'] = int((z < -tol).sum())
    res['degenerate_faces'] = int((mesh.area_faces < 1e-10).sum())
    down = (fn[:, 2] < -1e-3) & ~bottom
    in_chamfer = fz.max(1) <= chamfer_max + 0.06
    od = down & ~in_chamfer
    res['downward_faces_chamfer_band'] = int((down & in_chamfer).sum())
    res['steepest_chamfer_face_nz'] = float(fn[down & in_chamfer, 2].min()) if (down & in_chamfer).any() else 0.0
    res['downward_faces_outside_chamfer_band'] = int(od.sum())
    # physical size of those: projected (xy) area and the horizontal depth of each overhang
    A = mesh.area_faces
    proj = A * np.abs(fn[:, 2])
    tri = mesh.triangles[:, :, :2]
    lmax = np.max(np.linalg.norm(tri - np.roll(tri, 1, axis=1), axis=2), axis=1)
    depth = 2 * proj / np.maximum(lmax, 1e-12)  # xy height of the projected triangle
    res['outside_band_area_mm2'] = float(A[od].sum())
    res['outside_band_projected_xy_area_mm2'] = float(proj[od].sum())
    res['outside_band_max_overhang_depth_mm'] = float(depth[od].max()) if od.any() else 0.0
    res['outside_band_faces_nz_below_-0.5'] = int((od & (fn[:, 2] < -0.5)).sum())
    nf = down & in_chamfer & (fn[:, 2] < -0.95)
    res['chamfer_band_near_flat_faces'] = int(nf.sum())
    res['chamfer_band_near_flat_area_mm2'] = float(A[nf].sum())
    # heightfield test: vertical rays on a 0.05 mm grid must cross the surface exactly 0 or 2 times
    b = mesh.bounds
    g = 0.05
    X, Y = np.meshgrid(np.arange(b[0, 0] + 0.37 * g, b[1, 0], g), np.arange(b[0, 1] + 0.61 * g, b[1, 1], g))
    O = np.c_[X.ravel(), Y.ravel(), np.full(X.size, -1.0)]
    _, ray = mesh.ray.intersects_id(O, np.tile([0, 0, 1.0], (len(O), 1)), multiple_hits=True)[:2]
    cnt = np.bincount(ray, minlength=len(O))
    res['vertical_ray_test'] = {'grid_mm': g, 'rays_hitting': int((cnt > 0).sum()),
                                'hit_count_histogram': np.bincount(cnt).tolist(),
                                'non_heightfield_columns_area_mm2': float((cnt > 2).sum() * g * g)}
    return res


def skeleton_widths(mask, dist_mm, px, end_skip=0.8):
    """Local widths (2 x inscribed radius, from the signed field dist_mm >= 0 inside the mask) along
    the medial axis. Nodes within end_skip mm (geodesic) of a free end are reported separately:
    that is where rounded tips taper by design. Tip radius = largest inscribed radius within
    0.5 mm of each free end."""
    from skimage.morphology import medial_axis
    mask = np.pad(mask, 2)
    dist_mm = np.pad(dist_mm, 2, mode='edge')
    sk = medial_axis(mask, rng=0)
    ys, xs, G = skeleton_graph(sk)
    w = 2 * dist_mm[ys, xs]
    deg = np.diff(G.indptr)
    ends = np.nonzero(deg <= 1)[0]
    s = dijkstra(G, indices=ends, min_only=True) * px if len(ends) else np.full(len(ys), np.inf)
    keep = s >= end_skip
    tip_r = []
    if len(ends):
        dmat = dijkstra(G, indices=ends, limit=0.5 / px)
        for k in range(len(ends)):
            near = np.isfinite(dmat[k])
            tip_r.append(float((w[near] / 2).max()))
    bins = [0, 0.4, 0.6, 0.8, 0.9, 1.0, 1.1, 1.2, 1.5, 2.0, 3.0, 5.0, 100]
    hist, _ = np.histogram(w[keep], bins=bins)
    out = {'bins_mm': bins, 'counts_nodes': hist.tolist(), 'min_mm': float(w[keep].min()),
           'p1_mm': float(np.percentile(w[keep], 1)), 'nodes': int(keep.sum()),
           'end_skip_mm': end_skip, 'free_ends': int(len(ends)),
           'tip_radius_min_mm': float(min(tip_r)) if tip_r else None,
           'tip_radius_p10_mm': float(np.percentile(tip_r, 10)) if tip_r else None}
    thin = keep & (w < 1.0)
    return out, (ys[thin] - 2, xs[thin] - 2, w[thin])


# ----------------------------------------------------------------------------- main
def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--glb', default=os.path.join(REPO_PC, 'source', 'tree_tripo_meshopt.glb'))
    ap.add_argument('--depth-dir', default=WORK if os.path.isdir(WORK) else os.path.join(HERE, 'cache'),
                    help='folder with tree_front.npz / tree_back.npz (+ tree_hill.png for the comparison)')
    ap.add_argument('--regen', action='store_true', help='decode the GLB and re-cast the depth maps')
    ap.add_argument('--regen-dir', default=os.path.join(HERE, 'cache'))
    ap.add_argument('--res', type=float, default=0.0003, help='depth map resolution for --regen (model units/px)')
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    ap.add_argument('--scale', type=float, default=110.7, help='mm per model unit')
    ap.add_argument('--cut-y', type=float, default=-0.15415, help='trunk cut, world y (model units)')
    # relief
    ap.add_argument('--base', type=float, default=1.45, help='thickness (mm) where relief = r_ref')
    ap.add_argument('--gain', type=float, default=1.4, help='relief gain (source mm -> print mm)')
    ap.add_argument('--r-ref', type=float, default=0.7, help='reference relief (mm at --scale): typical leaf rim')
    ap.add_argument('--floor', type=float, default=1.4, help='minimum thickness (mm), soft')
    ap.add_argument('--max-thickness', type=float, default=3.6, help='maximum thickness (mm), soft')
    ap.add_argument('--denoise-space', type=float, default=0.07, help='bilateral spatial sigma (mm)')
    ap.add_argument('--denoise-range', type=float, default=0.035, help='bilateral range sigma (mm of relief)')
    ap.add_argument('--denoise-passes', type=int, default=2)
    ap.add_argument('--detail', type=float, default=0.35, help='unsharp detail boost amount (0 = off)')
    ap.add_argument('--detail-sigma', type=float, default=0.45, help='unsharp radius (mm)')
    # outline
    ap.add_argument('--outline-smooth', type=float, default=0.05, help='mask SDF smoothing sigma (mm)')
    ap.add_argument('--final-smooth', type=float, default=0.025, help='final SDF smoothing sigma (mm)')
    ap.add_argument('--min-width', type=float, default=1.0, help='minimum stem width (mm)')
    ap.add_argument('--width-margin', type=float, default=0.03, help='extra radius on thickened stems (mm)')
    ap.add_argument('--tip-radius', type=float, default=0.42, help='minimum tip radius (mm)')
    ap.add_argument('--tip-taper', type=float, default=1.0, help='length over which tips grow to min width (mm)')
    ap.add_argument('--prune-ratio', type=float, default=1.0)
    ap.add_argument('--prune-extra', type=float, default=0.15)
    ap.add_argument('--open-radius', type=float, default=0.4, help='convex corner rounding (mm)')
    ap.add_argument('--gap', type=float, default=0.5, help='gaps narrower than this are closed (mm)')
    ap.add_argument('--hole-area', type=float, default=0.3, help='fill holes smaller than this (mm^2)')
    ap.add_argument('--flare-left', type=float, default=1.5, help='root flare on the trunk left side (mm)')
    ap.add_argument('--flare-height', type=float, default=3.0, help='root flare height (mm)')
    ap.add_argument('--flare-power', type=float, default=2.0)
    # bed chamfer
    ap.add_argument('--chamfer', type=float, default=0.3, help='45 deg bed-edge chamfer (mm)')
    ap.add_argument('--chamfer-min', type=float, default=0.15, help='chamfer on the thinnest stems (mm)')
    # meshing
    ap.add_argument('--upsample', type=int, default=1, help='grid upsampling of the depth map')
    ap.add_argument('--dz', type=float, default=0.05, help='marching cubes z step through the chamfer (mm)')
    ap.add_argument('--dz-top', type=float, default=0.4, help='marching cubes z step above the chamfer (mm)')
    ap.add_argument('--tile-rows', type=int, default=160)
    ap.add_argument('--wall-k', type=float, default=25.0, help='wall term scale in the 3D field (crisp rims)')
    ap.add_argument('--tolerance', type=float, default=0.01, help='simplification max surface move (mm)')
    ap.add_argument('--target-faces', type=int, default=450000, help='tolerance is raised until below this')
    ap.add_argument('--bed-snap', type=float, default=0.035,
                    help='after simplification, vertices below this z go onto the bed (mm)')
    ap.add_argument('--no-previews', action='store_true')
    ap.add_argument('--previews-only', action='store_true', help='re-render previews of out/tree.stl')
    ap.add_argument('--debug-dir', default=None)
    ap.add_argument('--stop-after', default=None, choices=[None, 'outline'], help='stop after the 2D stage (debug)')
    return ap.parse_args()


def main():
    a = parse_args()
    os.makedirs(a.out, exist_ok=True)
    import trimesh
    import manifold3d
    if a.previews_only:
        make_previews(trimesh.load(os.path.join(a.out, 'tree.stl')), a.out, a)
        log('previews written')
        return

    front, back, ys_world, res = load_depth(a)
    S = a.scale
    px = res * S
    H, W = front.shape
    m_raw = ~np.isnan(front)
    # back plane: robust plane fit to the flat back of the source relief
    yy, xx = np.nonzero(m_raw & ~np.isnan(back))
    B = back[yy, xx] * S
    sel = np.abs(B - np.median(B)) < 0.05
    coef = np.linalg.lstsq(np.c_[xx[sel], yy[sel], np.ones(sel.sum())], B[sel], rcond=None)[0]
    plane = coef[0] * np.arange(W)[None, :] + coef[1] * np.arange(H)[:, None] + coef[2]
    r = front * S - plane  # relief above the back plane, mm (at this scale)
    # cut row (fractional, padded grid): world y -> row
    ic = PAD + (ys_world[0] - a.cut_y) / res
    log(f'px = {px:.4f} mm, back plane fit on {sel.mean():.0%} of pixels, cut row {ic:.2f}')

    if a.upsample > 1:
        k = a.upsample
        rn = nearest_fill(r, m_raw)
        sd = sdf_from_mask(m_raw, px)
        r = ndi.zoom(rn, k, order=1)
        m_raw = ndi.zoom(sd, k, order=1) < 0
        r[~m_raw] = np.nan
        px /= k
        ic = (ic + 0.5) * k - 0.5
        H, W = r.shape

    rows = np.arange(H)[:, None]
    m0 = m_raw & (rows < ic)
    m0, ncomp0 = largest_component(m0)
    m0, nh0 = fill_small_holes(m0, a.hole_area / (px * px))
    log(f'raw mask: {ncomp0} components (kept largest), {nh0} pin-holes filled')

    # ---- relief denoise (inside the raw mask, extended outside by nearest neighbour)
    import cv2
    valid = m0 & ~np.isnan(r)
    r_nn = nearest_fill(np.where(valid, r, 0.0), valid)
    r_dn = r_nn.astype(np.float32)
    for _ in range(a.denoise_passes):
        r_dn = cv2.bilateralFilter(r_dn, d=-1, sigmaColor=a.denoise_range, sigmaSpace=a.denoise_space / px)
    r_dn = r_dn.astype(np.float64)
    hp = (r - r_dn)[valid & (ndi.distance_transform_edt(m0) > 4)]
    log(f'denoise removed {np.std(hp):.4f} mm rms of relief (p99 {np.percentile(np.abs(hp), 99):.4f})')

    # ---- outline
    dbg = {}
    d, info = build_outline(r_dn, m0, px, ic, a, dbg)
    M = d < 0
    log('outline ops:', {k: (round(v, 3) if isinstance(v, float) else v) for k, v in info.items() if isinstance(v, (int, float))})
    log(f'outline: area {M.sum() * px * px:.1f} mm2, skeleton {info["skeleton_nodes"]} nodes, '
        f'{info["spurs_pruned"]} spurs pruned, {info["thin_nodes"]} thin nodes thickened, '
        f'opened {info.get("opened_area", 0):.3f} mm2, closed {info.get("closed_area", 0):.3f} mm2, '
        f'holes filled {info["holes_filled"]}, components before {info["components_before"]}')

    # ---- relief on the final domain
    rr = np.where(m0, r_dn, np.nan)
    known = m0 & M
    if info['tube_region'].any():
        reg = info['tube_region'] & M
        sy, sx = info['tube_src']
        vals = ndi.map_coordinates(r_dn, [sy[reg], sx[reg]], order=1)
        rr[reg] = vals
        known = known | reg
    fl = info['flare'] & M & ~known
    outer = None
    if fl.any():
        # root flare: harmonic ramp from the trunk's relief down to rim height at the flare's edge
        outer = ndi.binary_dilation(fl, iterations=2) & ~M & (np.arange(H)[:, None] < ic - 1)
    rr = np.where(known, rr, 0.0)
    rr = harmonic_fill(rr, known, M, outer, a.r_ref + 0.05)
    # soften the seams between original, widened and filled regions
    seam = ndi.binary_dilation(M & ~(m0 & ~info['tube_region']), iterations=3) & M
    rs = norm_gauss(rr, M, 1.2)
    rr = np.where(seam, rs, rr)
    # gentle detail boost (unsharp mask, normalised inside the outline)
    if a.detail > 0:
        rr = rr + a.detail * (rr - norm_gauss(rr, M, a.detail_sigma / px))
    rr = nearest_fill(np.where(M, rr, 0.0), M)

    # ---- heights
    hraw = a.base + a.gain * (rr - a.r_ref)
    k = 0.15
    hfl = 0.5 * (hraw + a.floor + np.sqrt((hraw - a.floor) ** 2 + k * k))
    hcap = 0.5 * (hfl + a.max_thickness - np.sqrt((hfl - a.max_thickness) ** 2 + k * k))
    h = hcap
    hin = h[M]
    log(f'height: min {hin.min():.3f} p1 {np.percentile(hin, 1):.3f} median {np.median(hin):.3f} '
        f'p99 {np.percentile(hin, 99):.3f} max {hin.max():.3f} mm')

    # ---- width-adaptive bed chamfer
    # chamfer grows with the local half width lt: 0.15 on 1.0 mm stems (glue face 0.7 wide),
    # the full chamfer from 1.6 mm features up (glue face = width - 2 * chamfer)
    # local half width lt = inscribed radius at the nearest medial-axis point
    from skimage.morphology import medial_axis
    skc = medial_axis(M, rng=0)
    lt = nearest_fill(np.where(skc, -d, 0.0), skc)
    cfield = np.clip(a.chamfer_min + (lt - 0.5) * (a.chamfer - a.chamfer_min) / 0.3, a.chamfer_min, a.chamfer)
    # smooth without letting wide-part chamfers bleed into thin stems; the wide smoothing keeps
    # |grad c| small so the chamfer stays close to 45 deg where its width changes
    cfield = ndi.gaussian_filter(ndi.grey_erosion(cfield, size=int(0.9 / px) | 1), 0.3 / px)
    cfield = np.clip(cfield, a.chamfer_min, a.chamfer)
    bottom = d < -cfield
    log(f'chamfer: {cfield[M].min():.3f}..{cfield[M].max():.3f} mm; glue-face area {bottom.sum() * px * px:.1f} mm2, '
        f'components {ndi.label(bottom, structure=np.ones((3, 3)))[1]}')

    # ---- origin (bottom-centre of trunk base) and frame
    irow = int(np.floor(ic))
    js = np.nonzero(M[irow])[0]
    # sub-pixel ends of the bottom row (full outline above the chamfer)
    jl, jr = js.min(), js.max()
    tl = d[irow, jl - 1] / (d[irow, jl - 1] - d[irow, jl])
    tr = d[irow, jr] / (d[irow, jr] - d[irow, jr + 1])
    xl_px, xr_px = jl - 1 + tl, jr + tr
    j0 = 0.5 * (xl_px + xr_px)
    trunk_w = (xr_px - xl_px) * px
    log(f'trunk base width {trunk_w:.2f} mm at the cut')

    if a.debug_dir:
        os.makedirs(a.debug_dir, exist_ok=True)
        from PIL import Image
        img = np.zeros((H, W, 3), np.uint8)
        img[m0] = (150, 150, 150)
        img[M & ~m0] = (0, 200, 0)
        img[m0 & ~M] = (220, 0, 0)
        ty, tx = dbg['thin_pts']
        if len(ty):
            img[ty, tx] = (255, 255, 0)
        Image.fromarray(img).save(os.path.join(a.debug_dir, 'outline_changes.png'))
        np.savez_compressed(os.path.join(a.debug_dir, 'fields.npz'), d=d.astype(np.float32),
                            h=h.astype(np.float32), c=cfield.astype(np.float32), m0=m0, M=M,
                            r_dn=r_dn.astype(np.float32), rr=rr.astype(np.float32))

    # ---- width / thickness stats
    skip = a.tip_taper + 0.4
    whist, thin_top = skeleton_widths(M, -d, px, end_skip=skip)
    whist_bot, _ = skeleton_widths(bottom, -(d + cfield), px, end_skip=skip)
    # gaps: medial axis of the background inside the tree's box (narrowest open gap)
    rr_, cc_ = np.nonzero(M)
    mg = PAD - 4
    box = (slice(rr_.min() - mg, rr_.max() + mg), slice(cc_.min() - mg, cc_.max() + mg))
    ghist, thin_gap = skeleton_widths(~M[box], d[box], px, end_skip=0.5)
    thin_gap = (thin_gap[0] + box[0].start, thin_gap[1] + box[1].start, thin_gap[2])
    th = h[M]
    th_hist, th_bins = np.histogram(th, bins=[0, 1.0, 1.2, 1.3, 1.4, 1.6, 2.0, 2.5, 3.0, 3.3, 3.6, 4.0])
    bg_lab, nbg = ndi.label(~M)
    hole_areas = ndi.sum(~M, bg_lab, range(1, nbg + 1)) * px * px
    bnd = np.unique(np.r_[bg_lab[0], bg_lab[-1], bg_lab[:, 0], bg_lab[:, -1]])
    holes = sorted(float(x) for i, x in enumerate(hole_areas) if (i + 1) not in bnd)

    if a.debug_dir:
        np.savez(os.path.join(a.debug_dir, 'thin.npz'), top=np.array(thin_top[:2]), topw=thin_top[2],
                 gap=np.array(thin_gap[:2]), gapw=thin_gap[2])
    log('min width (top, tips excluded)', whist['min_mm'], 'p1', whist['p1_mm'], 'tip r min', whist['tip_radius_min_mm'],
        '| glue face min', whist_bot['min_mm'], '| narrowest gap', ghist['min_mm'], 'p1', ghist['p1_mm'])
    if a.stop_after == 'outline':
        return
    # ---- marching cubes
    zl = z_levels(a.chamfer, float(h[M].max()), a.dz, a.dz_top)
    hfield = np.where(M | (d < 3 * px), h, 0.0)
    V, F = marching_tiles(d, hfield, cfield, px, zl, a.tile_rows, a.wall_k)
    log(f'marching cubes on {len(zl)} z levels: {len(V)} verts, {len(F)} faces')
    X = (V[:, 1] - j0) * px
    Y = (ic - V[:, 0]) * px
    Z = V[:, 2]
    Z[np.abs(Z) < 1e-9] = 0.0
    P = np.c_[X, Y, Z]
    dense = trimesh.Trimesh(P, F, process=False)
    if dense.volume < 0:
        dense.invert()
    log(f'dense mesh volume {dense.volume:.1f} mm3')

    # ---- error-bounded simplification (manifold3d keeps a subset of the original vertices)
    mf = manifold3d.Manifold(manifold3d.Mesh(np.asarray(dense.vertices, np.float32),
                                             np.asarray(dense.faces, np.uint32)))
    log(f'manifold3d status {mf.status()}, {mf.num_tri()} tris')
    tol = a.tolerance
    while True:
        ms = mf.simplify(tol)
        log(f'  simplify tol {tol:.4f} mm -> {ms.num_tri()} tris')
        if ms.num_tri() <= a.target_faces or tol > 0.05:
            break
        tol *= 1.25
    mo = ms.to_mesh()
    vp = np.asarray(mo.vert_properties)[:, :3].astype(np.float64)
    vp = bed_snap(vp, np.asarray(mo.tri_verts), max(2 * tol, a.bed_snap))
    # round trip through manifold3d once more: it collapses any degenerate triangles
    mf2 = manifold3d.Manifold(manifold3d.Mesh(np.ascontiguousarray(vp, np.float32),
                                              np.ascontiguousarray(mo.tri_verts, np.uint32)))
    mo = mf2.to_mesh()
    vp = np.asarray(mo.vert_properties)[:, :3].astype(np.float64)
    vp[np.abs(vp[:, 2]) < 1e-6, 2] = 0.0
    mesh = trimesh.Trimesh(vp, np.asarray(mo.tri_verts), process=True)
    parts = mesh.split(only_watertight=False)
    if len(parts) > 1:
        vols = [abs(p.volume) for p in parts]
        log(f'dropping {len(parts) - 1} sliver bodies (volumes {sorted(vols)[:-1]})')
        mesh = parts[int(np.argmax(vols))]
    mesh.remove_unreferenced_vertices()
    if mesh.volume < 0:
        mesh.invert()
    stl = os.path.join(a.out, 'tree.stl')
    mesh.export(stl)
    log(f'wrote {stl}: {len(mesh.faces)} faces')

    # ---- checks
    chk = mesh_checks(mesh, a.chamfer)
    log('mesh checks:', json.dumps(chk))
    # simplification error vs the dense mesh: vertical ray casts on the top surface
    Mi = M & (d < -0.1)
    iy, ix = np.nonzero(Mi)
    stride = max(1, len(iy) // 300000)
    iy, ix = iy[::stride], ix[::stride]
    # rays exactly through grid nodes hit mesh vertices (degenerate for embree): offset them
    O = np.c_[(ix + 0.37 - j0) * px, (ic - iy - 0.21) * px, np.full(len(iy), 20.0)]
    Dn = np.tile([0, 0, -1.0], (len(O), 1))

    def top_z(m):
        loc, ray, _ = m.ray.intersects_location(O, Dn, multiple_hits=False)
        z = np.full(len(O), np.nan)
        z[ray] = loc[:, 2]
        return z
    z_simpl = top_z(mesh)
    z_dense = top_z(dense)
    err = np.abs(z_simpl - z_dense)
    err_h = np.abs(z_dense - ndi.map_coordinates(h, [iy + 0.21, ix + 0.37], order=1))
    # perpendicular error ~ vertical error x |n_z| of the true (heightfield) surface
    gy, gx = np.gradient(h, px)
    nzh = 1 / np.sqrt(1 + gx[iy, ix] ** 2 + gy[iy, ix] ** 2)
    errn = err * nzh
    chk['simplify_tolerance_mm'] = tol
    chk['top_err_vs_dense_mm'] = {'rays': int(len(err)), 'missed': int(np.isnan(err).sum()),
                                  'vertical_p50': float(np.nanpercentile(err, 50)),
                                  'vertical_p99': float(np.nanpercentile(err, 99)),
                                  'vertical_max': float(np.nanmax(err)),
                                  'normal_p99': float(np.nanpercentile(errn, 99)),
                                  'normal_p999': float(np.nanpercentile(errn, 99.9)),
                                  'normal_max': float(np.nanmax(errn))}
    chk['dense_vs_heightmap_mm'] = {'p50': float(np.nanpercentile(err_h, 50)), 'p99': float(np.nanpercentile(err_h, 99)),
                                    'max': float(np.nanmax(err_h))}
    log('top surface error simplified vs dense', chk['top_err_vs_dense_mm'], 'dense vs h', chk['dense_vs_heightmap_mm'])

    # ---- glue-face footprint: exact z = 0 outline of the final mesh
    import shapely.geometry as sg
    from shapely.ops import unary_union
    import shapely
    bot = np.nonzero((mesh.vertices[mesh.faces][:, :, 2] == 0).all(1))[0]
    # union of the bed triangles = the exact glue face (robust to loops touching at a vertex)
    glue = shapely.union_all(shapely.polygons(mesh.triangles[bot][:, :, :2]))
    chk['glue_face_parts'] = 1 if glue.geom_type == 'Polygon' else len(glue.geoms)
    chk['glue_face_area_vs_bed_triangles_mm2'] = [float(glue.area), float(mesh.area_faces[bot].sum())]
    glue = sg.polygon.orient(glue, 1.0) if glue.geom_type == 'Polygon' else sg.MultiPolygon(
        [sg.polygon.orient(g, 1.0) for g in glue.geoms])
    gl = [glue] if glue.geom_type == 'Polygon' else list(glue.geoms)

    def rnd(cs):
        return [[round(float(x), 4), round(float(y), 4)] for x, y in cs]

    # full outline above the chamfer (the tree's visible silhouette), for clearance checks
    zc = float(np.nanmax(cfield)) + 0.1
    sec = mesh.section(plane_origin=[0, 0, zc], plane_normal=[0, 0, 1])
    full = unary_union(list(sec.to_2D(to_2D=np.eye(4), check=False)[0].polygons_full))
    full_l = [full] if full.geom_type == 'Polygon' else list(full.geoms)
    full_l = [sg.polygon.orient(g, 1.0) for g in full_l]
    # glue face extent along the trunk base
    # trunk base: x extent of the solid in the bottom 0.5 mm (corners are rounded by r_open, so the
    # flat cut at y = 0 itself is a little shorter: reported as flat_cut_*)
    fb = full.intersection(sg.box(-100, 0, 100, 0.5)).bounds
    gb = glue.intersection(sg.box(-100, 0, 100, a.chamfer + 0.5)).bounds
    tb = {'x_left': round(fb[0], 3), 'x_right': round(fb[2], 3), 'y': 0.0,
          'flat_cut_x_left': round(-trunk_w / 2, 3), 'flat_cut_x_right': round(trunk_w / 2, 3),
          'glue_face_x_left': round(gb[0], 3), 'glue_face_x_right': round(gb[2], 3)}
    fp = {
        'units': 'mm',
        'frame': 'canonical tree frame: origin = bottom-centre of trunk base, +x right, +y up (front view), '
                 'polygons are the bottom (glue) face outline',
        'scale_mm_per_model_unit': S,
        'polygons': [{'exterior': rnd(g.exterior.coords[:-1]), 'holes': [rnd(h_.coords[:-1]) for h_ in g.interiors]}
                     for g in gl],
        'bbox': [round(v, 3) for v in glue.bounds],
        'area_mm2': round(glue.area, 2),
        'trunk_base': tb,
        'chamfer_mm': a.chamfer,
        'outline_full': {'z_mm': round(zc, 3), 'area_mm2': round(full.area, 2),
                         'bbox': [round(v, 3) for v in full.bounds],
                         'polygons': [{'exterior': rnd(g.exterior.coords[:-1]),
                                       'holes': [rnd(h_.coords[:-1]) for h_ in g.interiors]} for g in full_l]},
        'notes': 'polygons = outline of the z=0 faces of tree.stl (the glue face, after the bed chamfer); '
                 'outline_full = section above the chamfer (visible silhouette, up to chamfer_mm larger). '
                 'Exteriors CCW, holes CW. trunk_base x_left/x_right = extent of the solid in its bottom 0.5 mm '
                 '(origin = centre of the flat cut at y=0, the lowest point of the tree); the glue face '
                 'itself starts about chamfer_mm higher. A pocket sized from the glue face + 0.15 mm lets '
                 'the chamfer seat on the pocket rim; size it from outline_full + clearance for a full drop-in.',
    }
    with open(os.path.join(a.out, 'tree_footprint.json'), 'w') as f:
        json.dump(fp, f)
    log(f'footprint: {len(gl)} polygon(s), {sum(len(g.interiors) for g in gl)} holes, area {glue.area:.1f} mm2, '
        f'bbox {glue.bounds}')

    vol = float(mesh.volume)
    meta = {
        'variant': 'A (faithful SDF heightfield)',
        'units': 'mm',
        'frame': fp['frame'],
        'scale_mm_per_model_unit': S,
        'pixel_mm': px,
        'bbox_3d': {'min': mesh.bounds[0].round(4).tolist(), 'max': mesh.bounds[1].round(4).tolist(),
                    'size': (mesh.bounds[1] - mesh.bounds[0]).round(3).tolist()},
        'thickness_mm': {'min': float(th.min()), 'p1': float(np.percentile(th, 1)), 'p5': float(np.percentile(th, 5)),
                         'median': float(np.median(th)), 'mean': float(th.mean()), 'p99': float(np.percentile(th, 99)),
                         'max': float(th.max()), 'hist_bins': th_bins.tolist(), 'hist_counts_px': th_hist.tolist()},
        'volume_mm3': vol,
        'mass_solid_g_pla_1.24': round(vol * 1.24e-3, 2),
        'triangles': int(len(mesh.faces)),
        'vertices': int(len(mesh.vertices)),
        'trunk_base': tb,
        'outline_area_mm2': float(M.sum() * px * px),
        'glue_face_area_mm2': float(glue.area),
        'min_width_top_outline': whist,
        'min_width_glue_face': whist_bot,
        'open_gap_widths': ghist,
        'enclosed_holes_mm2': holes,
        'outline_ops': {k: v for k, v in info.items() if isinstance(v, (int, float))},
        'checks': chk,
        'parameters': dict(vars(a)),
        'back_plane_coef_mm_per_px': coef.tolist(),
        'cut_row_padded_px': ic,
        'origin_col_padded_px': j0,
    }
    with open(os.path.join(a.out, 'tree_meta.json'), 'w') as f:
        json.dump(meta, f, indent=1)
    log('meta written')

    if not a.no_previews:
        make_previews(mesh, a.out, a)
        log('previews written')
    log('done')


if __name__ == '__main__':
    main()
