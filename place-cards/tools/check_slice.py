#!/usr/bin/env python3
"""Will every leaf and stem actually print? Slice with PrusaSlicer, rasterise the G-code and
compare it with the model, layer by layer.

Steps:
  1. Slice the model with the PrusaSlicer CLI using the user's settings (0.4 nozzle,
     0.42 mm lines, arachne walls, min bead 85 %, min feature 25 %, elephant foot 0.1,
     first layer 0.2 mm) and the part's profile (--kind: tree = gold silk at 0.1 mm,
     3 walls, solid; plaque; gold; foot). --set key=value overrides any PrusaSlicer option.
     Or analyse an existing G-code with --gcode (registered automatically).
  2. Parse the G-code: every extruding move becomes a stroke whose width comes from the
     extruded volume (PrusaSlicer's flow model), so arachne's variable widths are exact.
  3. For each layer, rasterise the strokes (round caps) and the model's cross-section at the
     layer's mid height (what the slicer samples), at --res mm per pixel.
  4. Lost = model pixels farther than --tol from any extrusion; filled = extrusion farther
     than --tol outside the model. Lost area is split into the outline band (the walls:
     this is where a stem, twig, tip or ridge disappears) and the interior (sparse infill
     gaps, ignored unless the part is solid).
  5. Footprint check: model area that is never extruded on any layer (a whole feature gone).
     Height check: top printed layer vs top model layer per XY pixel.

Outputs in --out-dir (default: the model's folder), prefixed with --tag:
  <tag>_slice.json        per-layer table, totals, the lost spots with positions (model coords)
  <tag>_slice_heatmap.png lost height (mm of model not extruded) over the part, the printed-vs-
                          model top-layer difference, and the per-layer area chart
  <tag>_slice_layers.png  selected layers: extruded inside the model (gold), lost (red),
                          extruded outside the model (blue)
  <tag>_slice_detail.png  close-ups of the worst spots with the real extrusion paths
  <tag>.gcode             the G-code, with --keep-gcode (else it goes to a temp folder)

Run:
  python3 place-cards/tools/check_slice.py place-cards/tree/out/tree.stl --kind tree \
      --out-dir place-cards/renders/checks --tag tree
  python3 place-cards/tools/check_slice.py some.stl --kind tree --layer 0.08 --set perimeters=2
  python3 place-cards/tools/check_slice.py tree.stl --gcode orca_export.gcode   # existing G-code
      (one object per G-code; PrusaSlicer and OrcaSlicer comment tags are both understood)
Verdict: FAIL when footprint area is never printed (over --max-lost mm2) or one spot over
--max-deep mm2 loses 0.3 mm or more of height; WARN for smaller such spots or more than 2 %
of the volume not extruded. Exit code 1 on FAIL.
"""
import argparse
import json
import math
import os
import re
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_common as C  # noqa: E402

FIL_AREA = math.pi * (1.75 / 2) ** 2
BRIDGE_TYPES = {"Bridge infill", "Overhang perimeter", "Internal bridge infill"}
SKIP_TYPES = {"Ironing", "Wipe tower", "Skirt/Brim", "Skirt", "Brim", "Custom", "Support material",
              "Support material interface"}


# ---------------------------------------------------------------- G-code
def parse_gcode(path, extrusion_multiplier=1.0):
    """Layers as dicts {z, h, segs: float array [x0, y0, x1, y1, w], types: list}.
    Width from the extruded volume: A = E * filament area / length, w = A / h + h (1 - pi / 4)
    (PrusaSlicer's rectangle-with-round-ends flow); bridges are round: w = sqrt(4 A / pi)."""
    layers = []
    cur = None
    x = y = 0.0
    rel_e = False
    e_abs = 0.0
    typ = ""
    num = re.compile(r"([XYZEF])(-?\d*\.?\d+)")
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith(";"):
                # PrusaSlicer tags (;Z: ;HEIGHT: ;TYPE:) and OrcaSlicer / Bambu ones (; Z_HEIGHT:
                # ; LAYER_HEIGHT: ; FEATURE:) both work
                t = line[1:].strip()
                if t.startswith("LAYER_CHANGE"):
                    cur = {"z": None, "h": None, "segs": [], "types": []}
                    layers.append(cur)
                elif cur is not None and (t.startswith("Z:") or t.startswith("Z_HEIGHT:")):
                    cur["z"] = float(t.split(":", 1)[1])
                elif cur is not None and (t.startswith("HEIGHT:") or t.startswith("LAYER_HEIGHT:")):
                    cur["h"] = float(t.split(":", 1)[1])
                elif t.startswith("TYPE:") or t.startswith("FEATURE:"):
                    typ = t.split(":", 1)[1].strip()
                continue
            code = line.split(";", 1)[0].strip()
            if not code:
                continue
            if code.startswith("M83"):
                rel_e = True
                continue
            if code.startswith("M82"):
                rel_e = False
                continue
            if code.startswith("G92"):
                for k, v in num.findall(code):
                    if k == "E":
                        e_abs = float(v)
                continue
            if not (code.startswith("G1 ") or code.startswith("G0 ")):
                continue
            vals = dict(num.findall(code))
            nx = float(vals["X"]) if "X" in vals else x
            ny = float(vals["Y"]) if "Y" in vals else y
            de = 0.0
            if "E" in vals:
                ev = float(vals["E"])
                if rel_e:
                    de = ev
                else:
                    de = ev - e_abs
                    e_abs = ev
            if (de > 0 and (nx != x or ny != y) and cur is not None and cur["h"] and typ not in SKIP_TYPES
                    and not any(k in typ.lower() for k in ("tower", "skirt", "brim", "ironing", "support"))):
                L = math.hypot(nx - x, ny - y)
                A = de * FIL_AREA / extrusion_multiplier / L
                h = cur["h"]
                if typ in BRIDGE_TYPES:
                    w = math.sqrt(4 * A / math.pi)
                else:
                    w = A / h + h * (1 - math.pi / 4)
                cur["segs"].append((x, y, nx, ny, w))
                cur["types"].append(typ)
            x, y = nx, ny
    for L in layers:
        L["segs"] = np.asarray(L["segs"], float).reshape(-1, 5)
    return [L for L in layers if len(L["segs"]) and L["z"] is not None]


def raster_strokes(segs, grid):
    """Round-capped strokes of their own width -> bool mask."""
    import cv2
    img = np.zeros(grid.shape, np.uint8)
    if not len(segs):
        return img.astype(bool)
    p0 = grid.to_px(segs[:, 0:2]) - 0.5
    p1 = grid.to_px(segs[:, 2:4]) - 0.5
    th = np.maximum(1, np.round(segs[:, 4] / grid.res)).astype(int)
    a = np.round(p0 * 16).astype(np.int64)
    b = np.round(p1 * 16).astype(np.int64)
    for i in range(len(segs)):
        cv2.line(img, (int(a[i, 0]), int(a[i, 1])), (int(b[i, 0]), int(b[i, 1])), 1, int(th[i]), cv2.LINE_8, 4)
    return img.astype(bool)


def edt(mask_false_is_feature, res):
    """Distance (mm) from each pixel to the nearest False pixel of the given mask."""
    import cv2
    return cv2.distanceTransform(mask_false_is_feature.astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE) * res


# ---------------------------------------------------------------- analysis
def blobs(mask, grid, offset, min_area, layer_z=None, limit=60):
    from scipy import ndimage as ndi
    lab, n = ndi.label(mask, structure=np.ones((3, 3)))
    out = []
    if not n:
        return out
    idx = np.arange(1, n + 1)
    areas = ndi.sum(np.ones(mask.shape), lab, idx) * grid.res ** 2
    big = np.nonzero(areas >= min_area)[0]
    if not len(big):
        return out
    cms = ndi.center_of_mass(mask, lab, idx[big])
    sl = ndi.find_objects(lab)
    for k, i in enumerate(big):
        s = sl[i]
        r, c = cms[k]
        d = {"area_mm2": round(float(areas[i]), 4),
             "at_mm": [round(float(grid.x0 + (c + 0.5) * grid.res - offset[0]), 2),
                       round(float(grid.y0 + (r + 0.5) * grid.res - offset[1]), 2)],
             "size_mm": [round((s[1].stop - s[1].start) * grid.res, 2), round((s[0].stop - s[0].start) * grid.res, 2)]}
        if layer_z is not None:
            d["z"] = layer_z
        out.append(d)
    out.sort(key=lambda d: -d["area_mm2"])
    return out[:limit]


def analyse(mesh, layers, offset, args, cfg):
    res = args.res
    lo, hi = mesh.bounds
    zshift = -lo[2]                                    # the slicer drops the part onto the bed
    grid = C.Grid(lo[0] + offset[0], lo[1] + offset[1], hi[0] + offset[0], hi[1] + offset[1], res, pad=1.0)
    man = C.to_manifold(mesh)
    perims = int(cfg.get("perimeters", 2))
    ext_w = float(cfg.get("external_perimeter_extrusion_width", 0.42))
    per_w = float(cfg.get("perimeter_extrusion_width", 0.45))
    band_w = ext_w + (perims - 1) * per_w + 0.05
    solid = str(cfg.get("fill_density", "15%")).strip() in ("100%", "100", "1")
    if args.solid is not None:
        solid = args.solid

    n = len(layers)
    lost_h = np.zeros(grid.shape, np.float32)          # mm of model height not extruded (wall band)
    void_h = np.zeros(grid.shape, np.float32)          # gaps inside a solid part (hidden, not detail)
    model_top = np.full(grid.shape, -1, np.int16)
    print_top = np.full(grid.shape, -1, np.int16)
    model_any = np.zeros(grid.shape, bool)
    print_any = np.zeros(grid.shape, bool)
    rows, spots, islands = [], [], []
    keep_layers = {}
    pick = set(np.unique(np.linspace(0, n - 1, min(n, args.sheet_layers)).round().astype(int)).tolist())
    for li, L in enumerate(layers):
        z_mid = L["z"] - L["h"] / 2 - zshift            # model coordinates
        rings = [r + offset[:2] for r in C.section_rings(man, z_mid + 0.0)]
        model = C.raster_rings(rings, grid)
        ext = raster_strokes(L["segs"], grid)
        d_ext = edt(~ext, res)                           # distance to the nearest extrusion
        d_mod = edt(~model, res)                         # distance to the model
        inside = edt(model, res)                         # depth inside the model
        lost = model & (d_ext > args.tol)
        extra = ext & (d_mod > args.tol)
        band = model & (inside <= band_w)
        lost_band = lost & band
        lost_int = lost & ~band
        a = res * res
        lost_h[lost_band] += L["h"]
        if solid:
            void_h[lost_int] += L["h"]
        model_top[model] = li
        print_top[ext] = li
        model_any |= model
        print_any |= ext
        row = {"layer": li + 1, "z": round(L["z"], 3), "h": round(L["h"], 3),
               "model_mm2": round(float(model.sum() * a), 3), "extruded_mm2": round(float(ext.sum() * a), 3),
               "lost_band_mm2": round(float(lost_band.sum() * a), 4),
               "lost_interior_mm2": round(float(lost_int.sum() * a), 4),
               "filled_outside_mm2": round(float(extra.sum() * a), 4), "segments": int(len(L["segs"]))}
        rows.append(row)
        for b in blobs(lost_band, grid, offset, args.min_spot, round(L["z"], 3)):
            b["layer"] = li + 1
            spots.append(b)
        # whole islands of the section with (almost) nothing extruded: a leaf tip, a dome top or,
        # worst case, a whole leaf that the slicer dropped
        from scipy import ndimage as ndi
        lab, nl = ndi.label(model, structure=np.ones((3, 3)))
        if nl:
            idx = np.arange(1, nl + 1)
            area_i = ndi.sum(np.ones(model.shape), lab, idx) * a
            cov_i = ndi.sum(~lost, lab, idx) * a
            for k in np.nonzero((cov_i < 0.1 * area_i) & (area_i >= args.min_spot))[0]:
                r_, c_ = ndi.center_of_mass(model, lab, k + 1)
                islands.append({"layer": li + 1, "z": round(L["z"], 3), "area_mm2": round(float(area_i[k]), 4),
                                "at_mm": [round(float(grid.x0 + (c_ + 0.5) * res - offset[0]), 2),
                                          round(float(grid.y0 + (r_ + 0.5) * res - offset[1]), 2)]})
        if li in pick or row["lost_band_mm2"] > 0:
            keep_layers[li] = (model, ext, lost_band, extra, lost_int if solid else None)
        if args.verbose:
            print(f"  layer {li + 1:3d} z={L['z']:.2f} model {row['model_mm2']:8.2f} ext {row['extruded_mm2']:8.2f}"
                  f" lost band {row['lost_band_mm2']:.3f} int {row['lost_interior_mm2']:.3f}"
                  f" filled {row['filled_outside_mm2']:.3f}")
    # never printed at all: model footprint farther than tol from every extrusion of every layer
    never = model_any & (edt(~print_any, res) > args.tol)
    top_diff = np.where(model_top >= 0, model_top - print_top, 0).astype(np.int16)
    spots.sort(key=lambda d: -d["area_mm2"])
    # worst layers to show, in addition to the evenly spaced ones
    worst = sorted(range(n), key=lambda i: -rows[i]["lost_band_mm2"])[:3]
    sheet = sorted(set(pick) | {i for i in worst if rows[i]["lost_band_mm2"] > 0})
    keep_layers = {i: keep_layers[i] for i in sheet if i in keep_layers}
    return {
        "grid": grid, "rows": rows, "spots": spots, "lost_h": lost_h, "void_h": void_h, "never": never,
        "model_any": model_any, "print_any": print_any, "top_diff": top_diff, "model_top": model_top,
        "keep": keep_layers, "band_w": band_w, "solid": solid, "islands": islands,
    }


# ---------------------------------------------------------------- pictures
def extent(grid, offset):
    return [grid.x0 - offset[0], grid.x0 + grid.w * grid.res - offset[0],
            grid.y0 - offset[1], grid.y0 + grid.h * grid.res - offset[1]]


def png_heatmap(path, A, offset, title, args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    g = A["grid"]
    ext = extent(g, offset)
    fig = plt.figure(figsize=(17, 10.5), dpi=110)
    gs = fig.add_gridspec(2, 2, height_ratios=[1.35, 1])
    ax = fig.add_subplot(gs[0, 0])
    base = np.where(A["model_any"], 0.82, 1.0)
    ax.imshow(base, origin="lower", extent=ext, cmap="gray", vmin=0, vmax=1, interpolation="nearest")
    lh = np.where(A["lost_h"] > 0, A["lost_h"], np.nan)
    vmax = max(0.3, float(np.nanmax(lh)) if np.isfinite(lh).any() else 0.3)
    im = ax.imshow(lh, origin="lower", extent=ext, cmap="inferno_r", vmin=0, vmax=vmax, interpolation="nearest")
    if A["never"].any():
        ax.contour(A["never"].astype(float), levels=[0.5], colors="cyan", linewidths=1.0, origin="lower", extent=ext)
    for s in A["spots"][:12]:
        ax.add_patch(plt.Circle(s["at_mm"], 1.0, fill=False, color="red", lw=0.8))
    fig.colorbar(im, ax=ax, fraction=0.03, label="model height not extruded (mm)")
    ax.set_title("lost detail in the wall band, summed over layers (red circles: largest lost spots; "
                 "cyan: never printed)", fontsize=10)
    ax.set_aspect("equal")
    ax = fig.add_subplot(gs[0, 1])
    td = np.where(A["model_top"] >= 0, A["top_diff"], np.nan).astype(float)
    cmap = plt.get_cmap("RdBu_r", 9)
    im = ax.imshow(td, origin="lower", extent=ext, cmap=cmap, vmin=-4.5, vmax=4.5, interpolation="nearest")
    fig.colorbar(im, ax=ax, fraction=0.03, label="model top layer - printed top layer (layers)")
    ax.set_title("relief kept: red = printed lower than the model (lost ridge / tip), blue = higher (filled groove)",
                 fontsize=10)
    ax.set_aspect("equal")
    ax = fig.add_subplot(gs[1, :])
    rows = A["rows"]
    zs = [r["z"] for r in rows]
    ax.plot(zs, [r["model_mm2"] for r in rows], "-", color="0.4", label="model section area")
    ax.plot(zs, [r["extruded_mm2"] for r in rows], "-", color="#b8901c", label="extruded area")
    ax2 = ax.twinx()
    ax2.bar(zs, [r["lost_band_mm2"] for r in rows], width=0.6 * rows[0]["h"], color="#d62728",
            label="lost in the wall band")
    if A["solid"]:
        ax2.bar(zs, [r["lost_interior_mm2"] for r in rows], width=0.3 * rows[0]["h"], color="#ff9896",
                label="lost inside (solid part)")
    ax2.plot(zs, [r["filled_outside_mm2"] for r in rows], ".", color="#1f77b4", label="extruded outside the model")
    ax.set_xlabel("layer top z (mm)")
    ax.set_ylabel("area (mm2)")
    ax2.set_ylabel("lost / outside area (mm2)")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper right", fontsize=8)
    ax.set_title("per layer", fontsize=10)
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def png_layers(path, A, layers, offset, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    g = A["grid"]
    ext = extent(g, offset)
    keys = sorted(A["keep"])
    n = len(keys)
    cols = 3 if n > 4 else max(1, n)
    rws = int(math.ceil(n / cols))
    fig, axs = plt.subplots(rws, cols, figsize=(6.2 * cols, 4.2 * rws), dpi=100, squeeze=False)
    for k, li in enumerate(keys):
        ax = axs[k // cols][k % cols]
        model, ext_m, lost, extra, gaps = A["keep"][li]
        img = np.ones(g.shape + (3,), np.float32)
        img[model] = (0.88, 0.88, 0.88)
        img[ext_m & model] = (0.83, 0.69, 0.22)
        img[ext_m & ~model] = (0.93, 0.80, 0.45)
        img[extra] = (0.12, 0.47, 0.71)
        if gaps is not None:
            img[gaps] = (1.0, 0.6, 0.2)
        img[lost.astype(bool)] = (0.84, 0.15, 0.16)
        ax.imshow(img, origin="lower", extent=ext, interpolation="nearest")
        r = A["rows"][li]
        ax.set_title(f"layer {li + 1}  z={r['z']:.2f}  lost {r['lost_band_mm2']:.2f} mm2", fontsize=9)
        ax.set_aspect("equal")
        ax.tick_params(labelsize=7)
    for k in range(n, rws * cols):
        axs[k // cols][k % cols].axis("off")
    fig.suptitle(title + "\n gold: extruded inside the model, pale: extruded within tol outside, red: model not "
                 "extruded in the wall band (lost), orange: hidden gaps inside a solid part, blue: extruded outside the "
                 "model, grey: model", fontsize=10)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def png_detail(path, A, layers, offset, mesh, title, crops):
    """Close-ups: model section outline (black) with the real extrusion strokes."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    man = C.to_manifold(mesh)
    zshift = -mesh.bounds[0, 2]
    n = len(crops)
    if not n:
        return
    cols = min(3, n)
    rws = int(math.ceil(n / cols))
    fig, axs = plt.subplots(rws, cols, figsize=(5.6 * cols, 5.2 * rws), dpi=110, squeeze=False)
    for k, (cx, cy, li, label) in enumerate(crops):
        ax = axs[k // cols][k % cols]
        L = layers[li]
        half = 2.5
        segs = L["segs"]
        sx = segs[:, [0, 2]] - offset[0]
        sy = segs[:, [1, 3]] - offset[1]
        sel = ((np.abs(sx - cx) < half + 1).any(1)) & ((np.abs(sy - cy) < half + 1).any(1))
        # stroke width in points: data units -> points
        lines = np.stack([np.c_[sx[sel, 0], sy[sel, 0]], np.c_[sx[sel, 1], sy[sel, 1]]], axis=1)
        ax.set_xlim(cx - half, cx + half)
        ax.set_ylim(cy - half, cy + half)
        ax.set_aspect("equal")
        fig.canvas.draw()
        ppd = ax.transData.transform([(1, 0)])[0, 0] - ax.transData.transform([(0, 0)])[0, 0]
        lw = segs[sel, 4] * ppd * 72 / fig.dpi
        lc = LineCollection(lines, linewidths=lw, colors=(0.83, 0.69, 0.22, 0.75), capstyle="round")
        ax.add_collection(lc)
        ax.add_collection(LineCollection(lines, linewidths=0.4, colors="0.25"))
        z_mid = L["z"] - L["h"] / 2 - zshift
        for r in C.section_rings(man, z_mid):
            rr = np.vstack([r, r[:1]])
            ax.plot(rr[:, 0], rr[:, 1], "-", color="k", lw=1.0)
        ax.set_title(f"{label}\nlayer {li + 1} z={L['z']:.2f} (black: model section, gold: extrusion)", fontsize=8)
        ax.tick_params(labelsize=7)
    for k in range(n, rws * cols):
        axs[k // cols][k % cols].axis("off")
    fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def pick_crops(A, layers, mesh, user_crops, deep=None):
    crops = []
    for c in user_crops or []:
        x, y = [float(v) for v in c.split(",")[:2]]
        li = int(c.split(",")[2]) - 1 if len(c.split(",")) > 2 else len(layers) // 2
        crops.append((x, y, li, f"requested ({x:.1f}, {y:.1f})"))
    seen = []
    # tall losses first, shown on the layer that loses the most there
    for d in (deep or [])[:3]:
        near = [s for s in A["spots"] if math.hypot(s["at_mm"][0] - d["at_mm"][0], s["at_mm"][1] - d["at_mm"][1]) < 1.5]
        if not near:
            continue
        s = max(near, key=lambda s: s["area_mm2"])
        seen.append(d["at_mm"])
        crops.append((d["at_mm"][0], d["at_mm"][1], s["layer"] - 1,
                      f"deep loss {d['area_mm2']:.2f} mm2 (>= 0.3 mm of height)"))
    for s in A["spots"]:
        if len(crops) >= 6:
            break
        if any(math.hypot(s["at_mm"][0] - p[0], s["at_mm"][1] - p[1]) < 3 for p in seen):
            continue
        seen.append(s["at_mm"])
        crops.append((s["at_mm"][0], s["at_mm"][1], s["layer"] - 1, f"lost {s['area_mm2']:.3f} mm2"))
    if len(crops) < 3:
        # nothing (much) lost: show the extremities (leaf tips) on the first wall layers and mid height
        v = mesh.vertices
        for i, lab in ((np.argmin(v[:, 0]), "left-most tip"), (np.argmax(v[:, 0]), "right-most tip"),
                       (np.argmax(v[:, 1]), "top-most tip")):
            li = min(len(layers) - 1, max(1, len(layers) // 3))
            crops.append((float(v[i, 0]) + (1.5 if lab.startswith("left") else -1.5 if lab.startswith("right") else 0),
                          float(v[i, 1]) - (1.5 if lab.startswith("top") else 0), li, lab))
    return crops[:6]


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", help="STL of the part, in its print orientation (z = 0 on the bed)")
    ap.add_argument("--kind", default="tree", choices=sorted(C.PRUSA_KIND), help="print profile")
    ap.add_argument("--layer", type=float, help="layer height (default from the profile: tree 0.1)")
    ap.add_argument("--first-layer", type=float, help="first layer height (default 0.2, as the user's profile)")
    ap.add_argument("--set", action="append", default=[], help="extra PrusaSlicer option key=value (repeatable)")
    ap.add_argument("--gcode", help="analyse this G-code instead of slicing (auto-registered to the model)")
    ap.add_argument("--out-dir", help="output folder (default: next to the model)")
    ap.add_argument("--tag", help="output name prefix (default: model name + kind)")
    ap.add_argument("--res", type=float, default=0.02, help="raster resolution (mm/px)")
    ap.add_argument("--tol", type=float, default=0.1, help="distance below which a model pixel counts as extruded (mm)")
    ap.add_argument("--min-spot", type=float, default=0.01, help="list lost spots from this area up (mm2)")
    ap.add_argument("--max-lost", type=float, default=0.5,
                    help="FAIL when the never-printed footprint exceeds this (mm2); WARN above 0")
    ap.add_argument("--max-deep", type=float, default=2.0,
                    help="FAIL when one spot this large (mm2) loses 0.3 mm or more of height; WARN from 0.3 mm2")
    ap.add_argument("--solid", type=lambda s: s.lower() in ("1", "true", "yes"), default=None,
                    help="count interior gaps as lost (default: when the profile is 100%% infill)")
    ap.add_argument("--sheet-layers", type=int, default=6, help="evenly spaced layers on the layer sheet")
    ap.add_argument("--crop", action="append", help="extra close-up at x,y[,layer] (model coordinates)")
    ap.add_argument("--keep-gcode", action="store_true", help="keep <tag>.gcode and its .ini in --out-dir")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    model = os.path.abspath(args.model)
    out_dir = os.path.abspath(args.out_dir or os.path.dirname(model))
    os.makedirs(out_dir, exist_ok=True)
    tag = args.tag or (os.path.splitext(os.path.basename(model))[0] + "_" + args.kind)
    mesh = C.load_mesh(model)
    lo, hi = mesh.bounds

    over = C.parse_set(args.set)
    if args.layer:
        over["layer_height"] = args.layer
    if args.first_layer:
        over["first_layer_height"] = args.first_layer
    cfg = C.prusa_profile(args.kind, over)
    em = float(cfg.get("extrusion_multiplier", 1.0))

    if args.gcode:
        gcode = os.path.abspath(args.gcode)
        cmd = None
    else:
        import tempfile
        gdir = out_dir if args.keep_gcode else tempfile.mkdtemp(prefix="check_slice_")
        gcode = os.path.join(gdir, tag + ".gcode")
        cmd = C.slice_prusa(model, gcode, cfg, gdir, center=(C.BED[0] / 2, C.BED[1] / 2))
    t_slice = time.time() - t0
    layers = parse_gcode(gcode, em)
    if not layers:
        sys.exit("no extrusion found in " + gcode)
    # registration: the slicer centres the part's bounding box on the bed centre
    allseg = np.vstack([L["segs"] for L in layers])
    pts = np.r_[allseg[:, 0:2], allseg[:, 2:4]]
    g_lo, g_hi = pts.min(0), pts.max(0)
    g_c = (g_lo + g_hi) / 2
    m_c = (lo[:2] + hi[:2]) / 2
    nominal = np.array([C.BED[0] / 2, C.BED[1] / 2]) - m_c
    measured = g_c - m_c
    offset = measured if args.gcode else nominal
    reg_err = float(np.hypot(*(measured - nominal))) if not args.gcode else None

    A = analyse(mesh, layers, offset, args, cfg)
    g = A["grid"]
    a = g.res ** 2
    rows = A["rows"]
    never_mm2 = float(A["never"].sum() * a)
    lost_band_total = float(sum(r["lost_band_mm2"] for r in rows))
    lost_int_total = float(sum(r["lost_interior_mm2"] for r in rows))
    model_vol = float(sum(r["model_mm2"] * r["h"] for r in rows))
    lost_vol = float((A["lost_h"] * a).sum())
    void_vol = float((A["void_h"] * a).sum())
    td = A["top_diff"][A["model_top"] >= 0]
    footprint = float(A["model_any"].sum() * a)
    never_spots = blobs(A["never"], g, offset, args.min_spot)
    lowered = float(((td >= 2) * a).sum())
    raised = float(((td <= -2) * a).sum())

    status = "PASS"
    reasons = []
    if never_mm2 > args.max_lost:
        status = "FAIL"
        reasons.append(f"{never_mm2:.2f} mm2 of the footprint is never printed")
    elif never_mm2 > 0:
        status = "WARN"
        reasons.append(f"{never_mm2:.3f} mm2 of the footprint is never printed")
    # tall losses: places where 0.3 mm or more of the model height is not extruded (a ridge, a tip
    # or a leaf that really disappears; single dropped dome-top layers are only 0.1 mm)
    h_layer = float(cfg["layer_height"])
    deep_mask = A["lost_h"] >= 3 * h_layer - 1e-6
    deep = blobs(deep_mask, g, offset, 0.05)
    deep_area = float(deep_mask.sum() * a)
    if any(d["area_mm2"] >= args.max_deep for d in deep):
        status = "FAIL"
        reasons.append(f"{deep[0]['area_mm2']:.2f} mm2 at {deep[0]['at_mm']} loses 0.3 mm or more of height")
    elif any(d["area_mm2"] >= 0.3 for d in deep):
        status = "FAIL" if status == "FAIL" else "WARN"
        reasons.append(f"{sum(1 for d in deep if d['area_mm2'] >= 0.3)} spots over 0.3 mm2 lose 0.3 mm or more of height"
                       f" (largest {deep[0]['area_mm2']:.2f} mm2 at {deep[0]['at_mm']})")
    if lost_vol > 0.02 * model_vol:
        status = "FAIL" if status == "FAIL" else "WARN"
        reasons.append(f"{100 * lost_vol / model_vol:.1f}% of the model volume is not extruded")
    report = {
        "model": model, "gcode": gcode, "kind": args.kind, "slicer_command": cmd,
        "settings": {k: cfg[k] for k in ("layer_height", "first_layer_height", "perimeters", "perimeter_generator",
                                         "extrusion_width", "external_perimeter_extrusion_width", "min_bead_width",
                                         "min_feature_size", "fill_density", "elefant_foot_compensation")},
        "res_mm": g.res, "tol_mm": args.tol, "wall_band_mm": round(A["band_w"], 3), "solid": A["solid"],
        "registration": {"offset_xy": [round(float(v), 4) for v in offset],
                         "bbox_centre_error_mm": None if reg_err is None else round(reg_err, 4),
                         "gcode_bbox": [[round(float(v), 3) for v in g_lo], [round(float(v), 3) for v in g_hi]],
                         "model_bbox_xy": [[round(float(v), 3) for v in lo[:2] + offset],
                                           [round(float(v), 3) for v in hi[:2] + offset]]},
        "layers": len(rows),
        "summary": {
            "status": status, "reasons": reasons,
            "footprint_mm2": round(footprint, 2),
            "never_printed_mm2": round(never_mm2, 4),
            "never_printed_spots": never_spots,
            "lost_band_area_sum_mm2": round(lost_band_total, 3),
            "lost_interior_area_sum_mm2": round(lost_int_total, 3),
            "model_volume_sampled_mm3": round(model_vol, 2),
            "lost_volume_mm3": round(lost_vol, 3),
            "lost_volume_pct": round(100 * lost_vol / max(model_vol, 1e-9), 3),
            "interior_gap_volume_mm3": round(void_vol, 3),
            "printed_top_lower_by_2plus_layers_mm2": round(lowered, 3),
            "printed_top_higher_by_2plus_layers_mm2": round(raised, 3),
            "top_layer_diff_hist": {str(k): int(v) for k, v in zip(*np.unique(np.clip(td, -5, 5), return_counts=True))},
            "filled_outside_area_sum_mm2": round(float(sum(r["filled_outside_mm2"] for r in rows)), 3),
            "deep_loss_area_mm2": round(deep_area, 3),
            "deep_loss_spots": deep[:20],
            "lost_islands_count": len(A["islands"]),
            "lost_islands_largest_mm2": round(max([i["area_mm2"] for i in A["islands"]] or [0.0]), 4),
            "lost_islands_lowest_layer": min([i["layer"] for i in A["islands"]] or [0]),
            "lost_islands_note": "islands of a layer section with under 10% extruded: usually the single top layer "
                                 "of a small dome or ridge (the printed top is then one layer lower there). PrusaSlicer's "
                                 "arachne drops some such islands that classic perimeters keep.",
        },
        "lost_islands": sorted(A["islands"], key=lambda d: -d["area_mm2"])[:60],
        "worst_layers": sorted(rows, key=lambda r: -r["lost_band_mm2"])[:5],
        "lost_spots": A["spots"][:60],
        "per_layer": rows,
        "gcode_summary": C.gcode_summary(gcode),
    }
    title = (f"{os.path.basename(model)} ({args.kind}, {cfg['layer_height']} mm layers, {cfg['perimeters']} arachne walls"
             f", res {g.res} mm, tol {args.tol} mm): {status}")
    png1 = os.path.join(out_dir, tag + "_slice_heatmap.png")
    png2 = os.path.join(out_dir, tag + "_slice_layers.png")
    png3 = os.path.join(out_dir, tag + "_slice_detail.png")
    png_heatmap(png1, A, offset, title, args)
    png_layers(png2, A, layers, offset, title)
    png_detail(png3, A, layers, offset, mesh, title, pick_crops(A, layers, mesh, args.crop, deep))
    report["pngs"] = [png1, png2, png3]
    report["seconds"] = {"slice": round(t_slice, 1), "total": round(time.time() - t0, 1)}
    js = os.path.join(out_dir, tag + "_slice.json")
    with open(js, "w") as f:
        json.dump(report, f, indent=1)

    s = report["summary"]
    print(f"{os.path.basename(model)}: {len(rows)} layers, footprint {s['footprint_mm2']} mm2")
    print(f"  never printed        {s['never_printed_mm2']} mm2 in {len(never_spots)} spots")
    print(f"  lost volume (walls)  {s['lost_volume_mm3']} mm3 ({s['lost_volume_pct']}% of {s['model_volume_sampled_mm3']} mm3);"
          f" interior gaps {s['interior_gap_volume_mm3']} mm3 (hidden)")
    print(f"  lost in wall band    {s['lost_band_area_sum_mm2']} mm2 summed over layers"
          f" (interior {s['lost_interior_area_sum_mm2']} mm2: {'hidden gaps in a solid part' if A['solid'] else 'sparse infill'}, not counted)")
    print(f"  deep losses (>=0.3)  {s['deep_loss_area_mm2']} mm2 in {len(s['deep_loss_spots'])} spots")
    print(f"  lost islands         {s['lost_islands_count']} (largest {s['lost_islands_largest_mm2']} mm2, lowest layer "
          f"{s['lost_islands_lowest_layer']})")
    print(f"  top 2+ layers lower  {s['printed_top_lower_by_2plus_layers_mm2']} mm2, higher "
          f"{s['printed_top_higher_by_2plus_layers_mm2']} mm2")
    if reg_err is not None:
        print(f"  registration error   {reg_err:.3f} mm (G-code vs model bbox centre)")
    gs = report["gcode_summary"]
    if gs:
        print(f"  filament             {gs.get('filament_g', '?')} g, {gs.get('filament_cm3', '?')} cm3, {gs.get('time', '?')}")
    print(f"VERDICT: {status}" + (": " + "; ".join(reasons) if reasons else ""))
    print("report:", js)
    for p in report["pngs"]:
        print("image: ", p)
    sys.exit(1 if status == "FAIL" else 0)


if __name__ == "__main__":
    main()
