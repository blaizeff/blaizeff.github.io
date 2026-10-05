#!/usr/bin/env python3
"""Preview renders for the gold tree (pure numpy z-buffer rasterizer, no GPU needed).

  python3 tree_previews.py [--stl out/tree.stl] [--out out/]

Writes preview_top.png (hillshade, true proportions), preview_oblique.png, preview_side.png,
closeup_leaf_cluster.png, closeup_trunk_base.png, closeup_pinch.png, preview_footprint.png,
compare_hillshade.png and preview_layers.png
(top view of the relief quantised to 0.1 mm layers: what survives printing).
"""
import argparse
import math
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
GOLD = np.array([0.83, 0.66, 0.26])


def rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def _candidates(xy, depth, F, W, H, sel):
    """Yield (pixel index, depth, face, l1, l2) for pixel centres covered by faces `sel`."""
    tri = xy[F[sel]]
    x0 = np.floor(tri[:, :, 0].min(1)).astype(np.int64)
    x1 = np.ceil(tri[:, :, 0].max(1)).astype(np.int64)
    y0 = np.floor(tri[:, :, 1].min(1)).astype(np.int64)
    y1 = np.ceil(tri[:, :, 1].max(1)).astype(np.int64)
    span = np.maximum(x1 - x0, y1 - y0) + 1
    lev = np.ceil(np.log2(np.maximum(span, 1))).astype(int)
    for L in np.unique(lev):
        s = 1 << L
        idx = np.nonzero(lev == L)[0]
        dy, dx = np.mgrid[0:s, 0:s]
        dx = dx.ravel()
        dy = dy.ravel()
        nchunk = max(1, len(idx) * s * s // 3_000_000 + 1)
        for ch in np.array_split(idx, nchunk):
            if len(ch) == 0:
                continue
            cx = x0[ch][:, None] + dx[None, :]
            cy = y0[ch][:, None] + dy[None, :]
            t = tri[ch]
            ax, ay = t[:, 0, 0:1], t[:, 0, 1:2]
            bx, by = t[:, 1, 0:1], t[:, 1, 1:2]
            qx, qy = t[:, 2, 0:1], t[:, 2, 1:2]
            det = (by - qy) * (ax - qx) + (qx - bx) * (ay - qy)
            det = np.where(np.abs(det) < 1e-12, 1e-12, det)
            px_ = cx + 0.5
            py_ = cy + 0.5
            l1 = ((by - qy) * (px_ - qx) + (qx - bx) * (py_ - qy)) / det
            l2 = ((qy - ay) * (px_ - qx) + (ax - qx) * (py_ - qy)) / det
            l3 = 1 - l1 - l2
            inside = (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
            inside &= (cx >= 0) & (cx < W) & (cy >= 0) & (cy < H)
            k, j = np.nonzero(inside)
            f = sel[ch[k]]
            d = depth[F[f, 0]] * l1[k, j] + depth[F[f, 1]] * l2[k, j] + depth[F[f, 2]] * l3[k, j]
            yield cy[k, j] * W + cx[k, j], d, f, l1[k, j], l2[k, j]


def rasterize(V, F, R, ppmm, margin=10, size=None, center=None, clip=None):
    """Orthographic render. R maps world -> camera (camera looks down -z, +y up).
    Returns face id, barycentrics, camera-space points and the pixel grid geometry."""
    P = V @ R.T
    if clip is not None:                      # clip = (xmin, xmax, ymin, ymax) in camera coords
        lo = np.array([clip[0], clip[2]])
        hi = np.array([clip[1], clip[3]])
    else:
        lo, hi = P[:, :2].min(0), P[:, :2].max(0)
    W = int(math.ceil((hi[0] - lo[0]) * ppmm)) + 2 * margin
    H = int(math.ceil((hi[1] - lo[1]) * ppmm)) + 2 * margin
    xy = np.empty((len(P), 2))
    xy[:, 0] = (P[:, 0] - lo[0]) * ppmm + margin
    xy[:, 1] = (hi[1] - P[:, 1]) * ppmm + margin
    depth = P[:, 2]
    # cull faces outside the frame
    tri = xy[F]
    vis = (tri[:, :, 0].max(1) >= 0) & (tri[:, :, 0].min(1) < W) & (tri[:, :, 1].max(1) >= 0) & (tri[:, :, 1].min(1) < H)
    sel = np.nonzero(vis)[0]
    zbuf = np.full(W * H, -np.inf)
    for pix, d, f, l1, l2 in _candidates(xy, depth, F, W, H, sel):
        np.maximum.at(zbuf, pix, d)
    fid = np.full(W * H, -1, np.int64)
    B1 = np.zeros(W * H)
    B2 = np.zeros(W * H)
    for pix, d, f, l1, l2 in _candidates(xy, depth, F, W, H, sel):
        win = d >= zbuf[pix] - 1e-7
        fid[pix[win]] = f[win]
        B1[pix[win]] = l1[win]
        B2[pix[win]] = l2[win]
    return fid.reshape(H, W), B1.reshape(H, W), B2.reshape(H, W), P, (W, H)


def smooth_normals(V, F, smooth_mask):
    """Vertex normals accumulated separately per smooth group (1 = top relief, 2 = walls), so the
    sharp top edge stays sharp; group 0 faces (chamfer, bottom) are flat-shaded."""
    fn = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    grp = np.asarray(smooth_mask).astype(int)
    vns = {}
    for g in np.unique(grp[grp > 0]):
        vn = np.zeros_like(V)
        sel = grp == g
        for k in range(3):
            np.add.at(vn, F[sel, k], fn[sel])
        vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-12)
        vns[g] = vn
    fnn = fn / np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-12)
    return vns, fnn, grp


def shade(V, F, smooth_mask, R, ppmm, lights, base=GOLD, bg=(255, 255, 255), spec=0.55, shin=40.0,
          clip=None, margin=10, ambient=0.18):
    fid, B1, B2, P, (W, H) = rasterize(V, F, R, ppmm, margin=margin, clip=clip)
    vns, fn, grp = smooth_normals(V, F, smooth_mask)
    hit = fid >= 0
    f = fid[hit]
    l1, l2 = B1[hit], B2[hit]
    l3 = 1 - l1 - l2
    n = fn[f].copy()
    for g, vn in vns.items():
        sel = grp[f] == g
        ff = f[sel]
        n[sel] = vn[F[ff, 0]] * l1[sel, None] + vn[F[ff, 1]] * l2[sel, None] + vn[F[ff, 2]] * l3[sel, None]
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    n = n @ R.T                                   # to camera space
    view = np.array([0, 0, 1.0])
    col = np.zeros((len(f), 3)) + ambient * base
    for Ld, inten in lights:
        Ld = np.asarray(Ld, float)
        Ld /= np.linalg.norm(Ld)
        diff = np.clip(n @ Ld, 0, 1)
        h = Ld + view
        h /= np.linalg.norm(h)
        sp = np.clip(n @ h, 0, 1) ** shin
        col += inten * (diff[:, None] * base + spec * sp[:, None] * np.array([1.0, 0.92, 0.7]))
    img = np.zeros((H * W, 3)) + np.array(bg) / 255.0
    img[hit.ravel()] = np.clip(col, 0, 1)
    return (img.reshape(H, W, 3) * 255).astype(np.uint8), fid


def label(img, text, xy=(12, 8), size=22):
    im = Image.fromarray(img) if isinstance(img, np.ndarray) else img
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', size)
    except Exception:
        font = ImageFont.load_default()
    d.text(xy, text, fill=(40, 40, 40), font=font)
    return im


def scale_bar(im, ppmm, mm=10, xy=None):
    d = ImageDraw.Draw(im)
    W, H = im.size
    x0, y0 = xy or (20, H - 30)
    d.rectangle([x0, y0, x0 + mm * ppmm, y0 + 6], fill=(40, 40, 40))
    try:
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
    except Exception:
        font = ImageFont.load_default()
    d.text((x0, y0 - 20), f'{mm} mm', fill=(40, 40, 40), font=font)
    return im


def source_hillshade(depth, px, scale):
    """Gold hillshade of the source front depth map (front view, NaN = background), light from the upper
    left like preview_top.png: the reference the relief is compared with."""
    m = ~np.isnan(depth)
    Z = np.where(m, depth, np.nanmin(depth)) * scale                 # mm toward the viewer
    gy, gx = np.gradient(Z, px)                                        # rows run down the image
    n = np.dstack([-gx, gy, np.ones_like(Z)])
    n /= np.linalg.norm(n, axis=2, keepdims=True)
    L = np.array([-1.0, 1.0, 1.4]) / np.linalg.norm([-1.0, 1.0, 1.4])  # from the upper left
    sh = np.clip(n @ L, 0, 1)
    img = np.where(m[..., None], (0.12 + 0.88 * sh[..., None]) * GOLD * 255, 255)
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))


def make_all(out, V, F, groups, top_outline=None, glue=None, hill_crop=None, hill_img=None):
    V = np.asarray(V, float)
    F = np.asarray(F, np.int64)
    smooth = np.zeros(len(F), np.int8)
    smooth[groups['top'][0]:groups['top'][1]] = 1
    if 'wall' in groups:
        smooth[groups['wall'][0]:groups['wall'][1]] = 2
    # 1. top view, hillshade-like (light from the upper left, like tree_hill.png)
    R_top = np.eye(3)
    az, alt = math.radians(135), math.radians(45)      # from upper-left in image space
    Ltop = [np.cos(alt) * np.cos(az), np.cos(alt) * np.sin(az), np.sin(alt)]
    ppmm = 20
    img, _ = shade(V, F, smooth, R_top, ppmm, [(Ltop, 0.95)], spec=0.25, shin=25, ambient=0.12)
    im = scale_bar(label(img, 'gold tree - top view (true proportions), light from upper left'), ppmm)
    im.save(os.path.join(out, 'preview_top.png'))
    top_img = im
    # 2. oblique: tree tilted back 35 deg, turned 25 deg
    R_ob = rot_x(math.radians(-38)) @ rot_z(math.radians(-18))
    R_ob = rot_x(math.radians(-50)) @ rot_z(math.radians(-15))
    lights = [((-0.4, 0.6, 0.7), 0.85), ((0.6, 0.2, 0.5), 0.35)]
    img, _ = shade(V, F, smooth, R_ob, 18, lights)
    label(img, 'gold tree - oblique render').save(os.path.join(out, 'preview_oblique.png'))
    # 3. side profiles: from below (x across, z up) and from the right (y across, z up)
    R_bot = rot_x(math.radians(-90))                   # seen from below: x right, thickness up
    imgA, _ = shade(V, F, smooth, R_bot, 25, [((-0.3, 0.6, 0.75), 0.9)], spec=0.3, margin=12)
    R_side = np.array([[0, 1.0, 0], [0, 0, 1.0], [1.0, 0, 0]])   # seen from the right: y right, thickness up
    imgB, _ = shade(V, F, smooth, R_side, 25, [((-0.3, 0.6, 0.75), 0.9)], spec=0.3, margin=12)
    Wm = max(imgA.shape[1], imgB.shape[1])
    pad = lambda x: np.pad(x, ((40, 10), (0, Wm - x.shape[1]), (0, 0)), constant_values=255)
    side = np.concatenate([pad(imgA), pad(imgB)], 0)
    im = Image.fromarray(side)
    label(im, 'side profile seen from below (x across, thickness up) - 25 px/mm', (12, 8), 18)
    label(im, 'side profile seen from the right (y across, thickness up)', (12, imgA.shape[0] + 58), 18)
    im.save(os.path.join(out, 'preview_side.png'))
    # 4. close-ups (about 40 px/mm, i.e. a 1:1 print at arm's length on a laptop screen x10)
    R_cl = rot_x(math.radians(-40)) @ rot_z(math.radians(-12))
    for name, box_, cpp in (('closeup_leaf_cluster.png', (22.0, 44.0, 26.0, 46.0), 40),
                            ('closeup_trunk_base.png', (-10.0, 12.0, -1.0, 14.0), 40),
                            ('closeup_pinch.png', (33.0, 40.0, 31.5, 36.5), 100)):
        x0, x1, y0, y1 = box_
        cen = np.array([(x0 + x1) / 2, (y0 + y1) / 2, 1.5])
        sel_v = (V[:, 0] > x0 - 8) & (V[:, 0] < x1 + 8) & (V[:, 1] > y0 - 8) & (V[:, 1] < y1 + 8)
        selF = sel_v[F].all(1)
        Fs = F[selF]
        sm = smooth[selF]
        hw = (x1 - x0) / 2
        hh = (y1 - y0) / 2 * math.cos(math.radians(40)) + 2.0
        img, _ = shade(V - cen, Fs, sm, R_cl, cpp, lights, clip=(-hw, hw, -hh, hh), margin=0)
        im = label(img, name.replace('.png', '').replace('_', ' ') + f' - {cpp} px/mm, oblique', (12, 8), 20)
        scale_bar(im, cpp, 2 if cpp < 80 else 1).save(os.path.join(out, name))
    # 4b. footprint: visible outline (pocket) vs glue face (z = 0), and the chamfer width between them
    if top_outline is not None and glue is not None:
        footprint_figure(os.path.join(out, 'preview_footprint.png'), top_outline, glue)
    # 5. side-by-side with the source hillshade
    if hill_img is not None:
        src = hill_img.convert('RGB')
        if hill_crop is not None:                      # same region as the top view (scale match)
            x0, y0, x1, y1 = (int(round(v)) for v in hill_crop)
            canvas = Image.new('RGB', (x1 - x0, y1 - y0), (255, 255, 255))
            canvas.paste(src.crop((max(x0, 0), max(y0, 0), min(x1, src.size[0]), min(y1, src.size[1]))),
                         (max(-x0, 0), max(-y0, 0)))
            src = canvas
        h = top_img.size[1]
        src = src.resize((int(src.size[0] * h / src.size[1]), h))
        both = Image.new('RGB', (top_img.size[0] + src.size[0] + 20, h), (255, 255, 255))
        both.paste(top_img, (0, 0))
        both.paste(src, (top_img.size[0] + 20, 0))
        label(both, 'source: front depth map of the Tripo tree, same scale', (top_img.size[0] + 32, 8))
        both.save(os.path.join(out, 'compare_hillshade.png'))


def footprint_figure(path, top, glue):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from shapely.geometry import box

    def draw(ax, g, **kw):
        for q in getattr(g, 'geoms', [g]):
            if q.geom_type != 'Polygon':
                continue
            ax.fill(*q.exterior.xy, **kw)
            for h in q.interiors:
                ax.fill(*h.xy, color='white')
    fig = plt.figure(figsize=(16, 7.5))
    ax = fig.add_axes([0.03, 0.06, 0.6, 0.88])
    draw(ax, top, color=(0.83, 0.66, 0.26))
    draw(ax, glue, color=(0.55, 0.42, 0.12))
    ax.set_aspect('equal')
    ax.set_title('outline_full (gold, the pocket shape) and glue face at z = 0 (dark), tree frame, mm')
    ax.grid(alpha=0.3)
    zooms = [((-7, 7, -0.5, 6), 'trunk base'), ((33, 40, 31.5, 37.5), 'thin stems near (36, 35)')]
    for i, ((x0, x1, y0, y1), t) in enumerate(zooms):
        az = fig.add_axes([0.66, 0.53 - 0.47 * i, 0.32, 0.41])
        draw(az, top.intersection(box(x0 - 1, y0 - 1, x1 + 1, y1 + 1)), color=(0.83, 0.66, 0.26))
        draw(az, glue.intersection(box(x0 - 1, y0 - 1, x1 + 1, y1 + 1)), color=(0.55, 0.42, 0.12))
        az.set_xlim(x0, x1)
        az.set_ylim(y0, y1)
        az.set_aspect('equal')
        az.set_title(t + ': chamfer 0.15 mm on 1 mm stems, 0.3 mm on wide parts', fontsize=9)
        az.grid(alpha=0.3)
    fig.savefig(path, dpi=90)
    plt.close(fig)


def layer_preview(out, Zgrid, region, px, layer=0.1, name='preview_layers.png'):
    """Hillshade of the relief quantised to print layers (what a 0.1 mm layer print can show)."""
    Zq = np.ceil(Zgrid / layer) * layer
    Zq = np.where(region, Zq, 0)
    gy, gx = np.gradient(Zq, px)
    az, alt = math.radians(315), math.radians(45)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    sh = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    sh = np.clip(sh, 0, 1)
    img = np.where(region[..., None], (0.2 + 0.8 * sh[..., None]) * GOLD * 255, 255).astype(np.uint8)
    rows, cols = np.nonzero(region)
    img = img[max(rows.min() - 10, 0):rows.max() + 10, max(cols.min() - 10, 0):cols.max() + 10]
    im = label(img, f'top view of the relief quantised to {layer} mm layers (terraces = layer steps)')
    im.save(os.path.join(out, name))


if __name__ == '__main__':
    import trimesh
    ap = argparse.ArgumentParser()
    ap.add_argument('--stl', default=os.path.join(HERE, 'out', 'tree.stl'))
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    a = ap.parse_args()
    m = trimesh.load(a.stl)
    V, F = np.asarray(m.vertices), np.asarray(m.faces)
    n = m.face_normals
    # recover the top group: faces pointing up that are not on the bottom
    # recover the face groups from the normals: top relief (up), walls (vertical), rest flat
    top = n[:, 2] > 1e-6
    wall = np.abs(n[:, 2]) <= 1e-6
    key = np.where(top, 0, np.where(wall, 1, 2))
    order = np.argsort(key, kind='stable')
    F = F[order]
    nt, nw = int(top.sum()), int(wall.sum())
    groups = {'top': (0, nt), 'wall': (nt, nt + nw)}
    make_all(a.out, V, F, groups)
