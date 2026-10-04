#!/usr/bin/env python3
"""Mesh sanity and printability report for STL files (tree, plaque parts, foot).

For every mesh given:
  * topology: watertight, consistent winding, outward normals (positive volume), manifold
    (manifold3d accepts it), non-manifold edges, degenerate and duplicate faces, body count
  * size: volume, area, triangle count, bounding box, sits on z = 0, fits the bed
    (256 x 256 x 256, also tried turned 90 degrees)
  * overhangs: area of downward faces steeper than --overhang (default 45 deg from
    vertical), not counting the bed face at z = 0; faces entirely within --bed-band of the
    bed (the bed chamfer, printed in the first layers) are reported apart; flat ceilings too
  * flat relief parts (--relief on, or auto = every part under --relief-max-h tall):
    thickness map by ray casting (top surface minus bottom surface, ignoring the chamfer
    band along the outline), local XY width along the medial axis (free ends skipped) of
    the outline, the glue face and a mid-height section, and the spots a disc of
    0.9 x --min-width cannot reach (morphological opening): a spot joined to wide material
    on two sides is a thin neck (a stem too thin: WARN / FAIL), on one side a sharp tip or
    corner (listed only). Positions are in the mesh's frame.

Prints a PASS / WARN / FAIL table per mesh and writes a JSON report (--json) and, with
--png-dir, a thickness / width map per relief part.

Run:
  python3 place-cards/tools/check_mesh.py place-cards/tree/out/tree.stl --bodies 1 --relief on \
      --json place-cards/print/check_mesh_tree.json --png-dir place-cards/renders/checks
  python3 place-cards/tools/check_mesh.py place-cards/stl/*.stl --allow-float
Exit code: 0 when nothing FAILs, 1 otherwise.
"""
import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_common as C  # noqa: E402


# ---------------------------------------------------------------- topology / size
def topology(mesh):
    import trimesh
    out = {}
    out["triangles"] = int(len(mesh.faces))
    out["vertices"] = int(len(mesh.vertices))
    out["watertight"] = bool(mesh.is_watertight)
    out["winding_consistent"] = bool(mesh.is_winding_consistent)
    out["volume_mm3"] = float(mesh.volume)
    out["area_mm2"] = float(mesh.area)
    out["outward_normals"] = bool(mesh.volume > 0)
    # edges used by more than two faces
    edges = np.sort(mesh.edges, axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    out["nonmanifold_edges"] = int((counts > 2).sum())
    out["boundary_edges"] = int((counts == 1).sum())
    areas = mesh.area_faces
    out["degenerate_faces"] = int((areas < 1e-9).sum())
    out["duplicate_faces"] = int(len(mesh.faces) - len(np.unique(np.sort(mesh.faces, axis=1), axis=0)))
    labels = trimesh.graph.connected_component_labels(mesh.face_adjacency, node_count=len(mesh.faces))
    out["bodies"] = int(labels.max() + 1) if len(labels) else 0
    try:
        man = C.to_manifold(mesh)
        st = str(man.status())
        out["manifold3d_status"] = st.split(".")[-1]
        out["manifold3d_ok"] = st.endswith("NoError")
        if out["manifold3d_ok"]:
            out["manifold3d_volume_mm3"] = float(man.volume())
            out["manifold3d_genus"] = int(man.genus())
    except Exception as e:  # noqa: BLE001
        out["manifold3d_status"] = f"error: {e}"
        out["manifold3d_ok"] = False
    return out


def bed_fit(mesh, bed, margin):
    lo, hi = mesh.bounds
    size = hi - lo
    fit0 = size[0] <= bed[0] - 2 * margin and size[1] <= bed[1] - 2 * margin
    fit90 = size[1] <= bed[0] - 2 * margin and size[0] <= bed[1] - 2 * margin
    return {
        "bbox_min": [round(float(v), 4) for v in lo],
        "bbox_max": [round(float(v), 4) for v in hi],
        "size": [round(float(v), 4) for v in size],
        "on_bed": bool(abs(lo[2]) < 1e-3),
        "fits_bed": bool((fit0 or fit90) and size[2] <= bed[2]),
        "fits_bed_turned": bool(fit90 and not fit0),
    }


def overhangs(mesh, angle_deg, bed_band=0.4, bed_eps=0.01):
    """Area of faces that face down more steeply than angle_deg from vertical (45 deg = the
    usual no-support limit), excluding the bed face. 1 deg of slack so exact 45 deg chamfers pass.
    Faces entirely within bed_band of the bed (the bed chamfer, printed in the first one or
    two layers, which sit on the bed anyway) are reported apart and do not count."""
    n = mesh.face_normals
    a = mesh.area_faces
    zmin = mesh.bounds[0, 2]
    fz = mesh.vertices[mesh.faces][:, :, 2]
    on_bed = (fz.max(axis=1) < zmin + bed_eps)
    limit = -math.sin(math.radians(angle_deg + 1.0))
    low = fz.max(axis=1) <= zmin + bed_band
    steep = (n[:, 2] < limit) & ~on_bed
    over = steep & ~low
    ceil = (n[:, 2] < -0.995) & ~on_bed & ~low
    res = {
        "threshold_deg": angle_deg,
        "overhang_area_mm2": float(a[over].sum()),
        "overhang_faces": int(over.sum()),
        "flat_ceiling_area_mm2": float(a[ceil].sum()),
        "bed_band_mm": bed_band,
        "bed_band_overhang_area_mm2": float(a[steep & low].sum()),
        "bed_face_area_mm2": float(a[on_bed & (n[:, 2] < -0.99)].sum()),
    }
    if over.any():
        c = mesh.triangles_center[over]
        w = a[over]
        res["overhang_centroid"] = [round(float(v), 3) for v in (c * w[:, None]).sum(0) / w.sum()]
        res["overhang_z_range"] = [round(float(c[:, 2].min()), 3), round(float(c[:, 2].max()), 3)]
        # worst faces, for locating the problem
        order = np.argsort(-w)[:5]
        res["largest_overhang_faces"] = [{"centre": [round(float(v), 2) for v in c[i]],
                                          "area": round(float(w[i]), 4)} for i in order]
    return res


# ---------------------------------------------------------------- relief analysis
def height_maps(mesh, grid):
    """Top and bottom surface z on the grid by ray casting (NaN = no material)."""
    xs, ys = grid.centres()
    X, Y = np.meshgrid(xs, ys)
    pts = np.c_[X.ravel(), Y.ravel()]
    lo, hi = mesh.bounds
    top = np.full(len(pts), np.nan)
    bot = np.full(len(pts), np.nan)
    inter = mesh.ray
    for sign, out in ((-1, top), (1, bot)):
        z0 = hi[2] + 1 if sign < 0 else lo[2] - 1
        origins = np.c_[pts, np.full(len(pts), z0)]
        dirs = np.tile([0, 0, sign], (len(pts), 1)).astype(float)
        step = 400000
        for s in range(0, len(pts), step):
            loc, idx, _ = inter.intersects_location(origins[s:s + step], dirs[s:s + step], multiple_hits=False)
            out[s + idx] = loc[:, 2]
    return top.reshape(grid.shape), bot.reshape(grid.shape)


def width_stats(mask, res, min_width, end_skip, label):
    """Local width along the medial axis (2 x distance to the edge), skipping skeleton points
    near free ends (leaf tips taper to zero by design), and the thin spots that a disc of
    diameter 0.9 * min_width cannot reach."""
    import cv2
    from scipy import ndimage as ndi
    from skimage.morphology import medial_axis
    out = {"layer": label, "area_mm2": float(mask.sum() * res * res)}
    if not mask.any():
        return out, None
    skel, dist = medial_axis(mask, return_distance=True)
    width = 2 * dist * res
    # skeleton end points: exactly one neighbour
    nb = ndi.convolve(skel.astype(np.uint8), np.ones((3, 3), np.uint8), mode="constant") - 1
    ends = skel & (nb == 1)
    d_end = ndi.distance_transform_edt(~ends) * res
    keep = skel & (d_end > end_skip)
    w = width[keep]
    if len(w):
        bins = [0, 0.4, 0.6, 0.8, 0.9, 1.0, 1.1, 1.2, 1.5, 2.0, 3.0, 5.0, 1000]
        hist, _ = np.histogram(w, bins=bins)
        out.update({
            "skeleton_px": int(keep.sum()),
            "width_min_mm": round(float(w.min()), 3),
            "width_p1_mm": round(float(np.percentile(w, 1)), 3),
            "width_p5_mm": round(float(np.percentile(w, 5)), 3),
            "width_median_mm": round(float(np.median(w)), 3),
            "width_hist_bins_mm": bins, "width_hist_px": hist.tolist(),
            "skeleton_below_min_frac": round(float((w < min_width).mean()), 4),
        })
        iy, ix = np.nonzero(keep)
        j = int(np.argmin(w))
        out["width_min_at_px"] = [int(iy[j]), int(ix[j])]
    # thin spots: opening with a disc of diameter 0.9 * min_width
    r_px = max(1, int(round(0.45 * min_width / res)))
    disc = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r_px + 1, 2 * r_px + 1))
    opened = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, disc).astype(bool)
    thin = mask & ~opened
    lab, n = ndi.label(thin, structure=np.ones((3, 3)))
    necks, tips = [], []
    ring = np.ones((5, 5), bool)
    if n:
        sl = ndi.find_objects(lab)
        for i, s in enumerate(sl):
            # local window around the spot, a little larger than it
            r0, r1 = max(s[0].start - 3, 0), min(s[0].stop + 3, mask.shape[0])
            c0, c1 = max(s[1].start - 3, 0), min(s[1].stop + 3, mask.shape[1])
            spot = lab[r0:r1, c0:c1] == i + 1
            a = float(spot.sum()) * res * res
            ext = max(s[0].stop - s[0].start, s[1].stop - s[1].start) * res
            if ext < 0.5 or a < 0.02:
                continue          # crescents at rounded tips: harmless
            # a neck (too thin stem) joins the wide material on two or more sides;
            # a tip or a sharp corner touches it on one side only
            touch = ndi.binary_dilation(spot, ring) & opened[r0:r1, c0:c1]
            _, sides = ndi.label(touch, structure=np.ones((3, 3)))
            d = {"area_mm2": round(a, 4), "extent_mm": round(float(ext), 3),
                 "px": [int((s[0].start + s[0].stop) / 2), int((s[1].start + s[1].stop) / 2)]}
            (necks if sides >= 2 else tips).append(d)
    necks.sort(key=lambda d: -d["extent_mm"])
    tips.sort(key=lambda d: -d["extent_mm"])
    out["thin_necks_count"] = len(necks)
    out["thin_necks_area_mm2"] = round(float(sum(d["area_mm2"] for d in necks)), 4)
    out["thin_necks"] = necks[:40]
    out["sharp_tips_count"] = len(tips)
    out["sharp_tips"] = tips[:40]
    return out, (width, keep, thin)


def relief(mesh, args):
    lo, hi = mesh.bounds
    grid = C.Grid(lo[0], lo[1], hi[0], hi[1], args.res, pad=0.3)
    top, bot = height_maps(mesh, grid)
    mask = ~np.isnan(top)
    thick = top - bot
    res = grid.res
    from scipy import ndimage as ndi
    inner = ndi.distance_transform_edt(mask) * res > args.edge_skip
    out = {"grid_res_mm": res, "projected_area_mm2": float(mask.sum() * res * res)}

    def stats(v):
        v = v[~np.isnan(v)]
        if not len(v):
            return {}
        return {"min": round(float(v.min()), 3), "p1": round(float(np.percentile(v, 1)), 3),
                "p5": round(float(np.percentile(v, 5)), 3), "median": round(float(np.median(v)), 3),
                "max": round(float(v.max()), 3)}

    out["thickness_all_mm"] = stats(thick[mask])
    out["thickness_inner_mm"] = stats(thick[inner])       # beyond the bed chamfer band
    out["thickness_inner_below_min_mm2"] = round(float((inner & (thick < args.min_thick)).sum() * res * res), 4)
    out["max_height_mm"] = round(float(np.nanmax(top)), 3)
    # height of the outline wall, just inside the edge (the leaf rim)
    band = mask & (ndi.distance_transform_edt(mask) * res <= args.edge_skip + 2 * res) & inner
    out["rim_thickness_mm"] = stats(thick[band])

    layers = {}
    masks = {"outline (projection)": mask}
    man = C.to_manifold(mesh)
    for z in sorted({0.05, args.mid_z}):
        rings = C.section_rings(man, lo[2] + z)
        masks[f"section z={z:g}"] = C.raster_rings(rings, grid)
    vis = {}
    for name, m in masks.items():
        s, extra = width_stats(m, res, args.min_width, args.end_skip, name)
        # px -> mm positions
        for key in ("width_min_at_px",):
            if key in s:
                r, c = s.pop(key)
                s["width_min_at_mm"] = [round(float(grid.x0 + (c + 0.5) * res), 2),
                                         round(float(grid.y0 + (r + 0.5) * res), 2)]
        for sp in s.get("thin_necks", []) + s.get("sharp_tips", []):
            r, c = sp.pop("px")
            sp["at_mm"] = [round(float(grid.x0 + (c + 0.5) * res), 2), round(float(grid.y0 + (r + 0.5) * res), 2)]
        layers[name] = s
        vis[name] = (extra + (s,)) if extra else None
    out["widths"] = layers
    return out, (grid, top, thick, mask, vis)


def relief_png(path, title, grid, top, thick, mask, vis, args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ext = [grid.x0, grid.x0 + grid.w * grid.res, grid.y0, grid.y0 + grid.h * grid.res]
    fig, axs = plt.subplots(1, 2, figsize=(16, 6.2), dpi=110)
    ax = axs[0]
    t = np.where(mask, thick, np.nan)
    im = ax.imshow(t, origin="lower", extent=ext, cmap="viridis", vmin=0, vmax=max(3.6, float(np.nanmax(t))))
    from scipy import ndimage as ndi
    inner = ndi.distance_transform_edt(mask) * grid.res > args.edge_skip
    lowm = inner & (thick < args.min_thick)
    if lowm.any():
        ax.contour(lowm.astype(float), levels=[0.5], colors="red", linewidths=0.6, origin="lower", extent=ext)
    fig.colorbar(im, ax=ax, fraction=0.03, label="thickness (mm)")
    ax.set_title(f"thickness (red: < {args.min_thick} mm, more than {args.edge_skip} mm inside the outline)")
    ax.set_aspect("equal")
    ax = axs[1]
    w, keep, thin, stats = vis["outline (projection)"]
    base = np.where(mask, 0.85, 1.0)
    ax.imshow(base, origin="lower", extent=ext, cmap="gray", vmin=0, vmax=1)
    iy, ix = np.nonzero(keep)
    xs = grid.x0 + (ix + 0.5) * grid.res
    ys = grid.y0 + (iy + 0.5) * grid.res
    sc = ax.scatter(xs, ys, c=np.clip(w[keep], 0, 3), s=0.3, cmap="turbo_r", vmin=0.6, vmax=3)
    if thin.any():
        ax.contour(thin.astype(float), levels=[0.5], colors="magenta", linewidths=0.8, origin="lower", extent=ext)
    for d in stats.get("thin_necks", []):
        ax.add_patch(plt.Circle(d["at_mm"], 1.2, fill=False, color="red", lw=1.2))
    for d in stats.get("sharp_tips", []):
        ax.add_patch(plt.Circle(d["at_mm"], 0.8, fill=False, color="orange", lw=0.8))
    fig.colorbar(sc, ax=ax, fraction=0.03, label="local width (mm)")
    ax.set_title(f"outline width on the medial axis (magenta: narrower than {0.9 * args.min_width:.2f} mm;"
                 " red circle: thin neck, orange: sharp tip/corner)", fontsize=9)
    ax.set_aspect("equal")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ---------------------------------------------------------------- verdicts
def verdicts(name, r, args):
    rows = []

    def add(check, status, value, note=""):
        rows.append({"check": check, "status": status, "value": value, "note": note})

    t = r["topology"]
    add("watertight", "PASS" if t["watertight"] else "FAIL", t["watertight"], f"{t['boundary_edges']} open edges")
    add("winding consistent", "PASS" if t["winding_consistent"] else "FAIL", t["winding_consistent"])
    add("outward normals", "PASS" if t["outward_normals"] else "FAIL", f"{t['volume_mm3']:.1f} mm3")
    add("manifold (manifold3d)", "PASS" if t["manifold3d_ok"] else "FAIL", t["manifold3d_status"])
    add("non-manifold edges", "PASS" if t["nonmanifold_edges"] == 0 else "FAIL", t["nonmanifold_edges"])
    add("degenerate faces", "PASS" if t["degenerate_faces"] == 0 else "WARN", t["degenerate_faces"])
    add("duplicate faces", "PASS" if t["duplicate_faces"] == 0 else "WARN", t["duplicate_faces"])
    if args.bodies is not None:
        add("body count", "PASS" if t["bodies"] == args.bodies else "FAIL", t["bodies"], f"expected {args.bodies}")
    else:
        add("body count", "INFO", t["bodies"])
    if args.max_tris:
        add("triangle count", "PASS" if t["triangles"] <= args.max_tris else "WARN", t["triangles"],
            f"limit {args.max_tris}")
    else:
        add("triangle count", "INFO", t["triangles"])
    b = r["bed"]
    add("sits on z = 0", "PASS" if b["on_bed"] else ("INFO" if args.allow_float else "WARN"), b["bbox_min"][2],
        "" if b["on_bed"] else "allowed (part of a multi-part object)" if args.allow_float else "")
    add("fits the bed", "PASS" if b["fits_bed"] else "FAIL", "x".join(f"{v:.1f}" for v in b["size"]),
        "turned 90 deg" if b["fits_bed_turned"] else "")
    o = r["overhangs"]
    lim = args.max_overhang
    add(f"overhangs > {o['threshold_deg']:g} deg", "PASS" if o["overhang_area_mm2"] <= lim else "WARN",
        f"{o['overhang_area_mm2']:.2f} mm2", f"flat ceilings {o['flat_ceiling_area_mm2']:.2f} mm2; in the bed band "
        f"(z < {o['bed_band_mm']:g}) {o['bed_band_overhang_area_mm2']:.1f} mm2, ignored")
    rl = r.get("relief")
    if rl:
        ti = rl["thickness_inner_mm"]
        add("thickness min (inner)", "PASS" if ti.get("min", 0) >= args.min_thick else
            ("WARN" if ti.get("min", 0) >= 0.85 * args.min_thick else "FAIL"),
            f"{ti.get('min')} mm", f"p1 {ti.get('p1')}, median {ti.get('median')}, below-min area "
            f"{rl['thickness_inner_below_min_mm2']} mm2")
        mx = ti.get("max", 0)
        ok = args.total_range[0] <= mx <= args.total_range[1]
        add("total thickness", "PASS" if ok else "WARN", f"{mx} mm",
            f"target {args.total_range[0]}..{args.total_range[1]}")
        rim = rl["rim_thickness_mm"]
        add("rim height (outline wall)", "INFO", f"{rim.get('min')} mm", f"median {rim.get('median')}")
        for lname, s in rl["widths"].items():
            if "width_min_mm" not in s:
                continue
            st = "PASS" if s["thin_necks_count"] == 0 else ("WARN" if s["thin_necks_area_mm2"] < 0.3 else "FAIL")
            if lname.startswith("section z=0.05"):
                st = "INFO"      # the glue face is narrowed by the bed chamfer on purpose
            add(f"min width, {lname}", st, f"{s['width_min_mm']} mm",
                f"p1 {s['width_p1_mm']}, necks < {0.9 * args.min_width:.2f}: {s['thin_necks_count']} "
                f"({s['thin_necks_area_mm2']} mm2)"
                + (f" worst at {s['thin_necks'][0]['at_mm']}" if s["thin_necks"] else "")
                + f"; sharp tips/corners {s['sharp_tips_count']}")
    return rows


def print_table(name, rows):
    print(f"\n== {name}")
    w = max(len(r["check"]) for r in rows)
    for r in rows:
        print(f"  {r['status']:<5} {r['check']:<{w}}  {str(r['value']):<18} {r['note']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("meshes", nargs="+", help="STL / 3MF / PLY / OBJ files")
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--png-dir", help="write <name>_relief.png maps here (relief parts)")
    ap.add_argument("--bed", default="256x256x256", help="bed size X x Y x Z in mm")
    ap.add_argument("--bed-margin", type=float, default=2.0, help="keep this far from the bed edge")
    ap.add_argument("--allow-float", action="store_true",
                    help="parts that start above z = 0 by design (the card's gold part) are not a warning")
    ap.add_argument("--bodies", type=int, help="expected number of separate bodies (tree: 1)")
    ap.add_argument("--max-tris", type=int, default=600000, help="warn above this triangle count (0 = no limit)")
    ap.add_argument("--overhang", type=float, default=45.0, help="overhang angle from vertical (deg)")
    ap.add_argument("--bed-band", type=float, default=0.4,
                    help="overhangs entirely below this height (bed chamfer, first layers) do not count (mm)")
    ap.add_argument("--max-overhang", type=float, default=1.0, help="overhang area (mm2) still counted as PASS")
    ap.add_argument("--relief", choices=["auto", "on", "off"], default="off",
                    help="thickness / width analysis for flat relief parts like the tree (auto: any part "
                         "under --relief-max-h tall)")
    ap.add_argument("--relief-max-h", type=float, default=6.0)
    ap.add_argument("--res", type=float, default=0.04, help="raster resolution for the relief analysis (mm/px)")
    ap.add_argument("--min-thick", type=float, default=1.0, help="minimum part thickness (mm)")
    ap.add_argument("--total-range", type=float, nargs=2, default=[2.5, 3.6], help="target total thickness (mm)")
    ap.add_argument("--min-width", type=float, default=1.0, help="minimum stem / twig width in XY (mm)")
    ap.add_argument("--end-skip", type=float, default=1.0,
                    help="ignore the medial axis this close to a free end (tips taper by design) (mm)")
    ap.add_argument("--edge-skip", type=float, default=0.4,
                    help="thickness stats ignore this band along the outline (bed chamfer) (mm)")
    ap.add_argument("--mid-z", type=float, default=1.0, help="extra width section at this height above the bed")
    args = ap.parse_args()
    bed = [float(v) for v in args.bed.lower().split("x")]

    report = {}
    worst = "PASS"
    bases = [os.path.basename(p) for p in args.meshes]
    for path in args.meshes:
        name = os.path.basename(path)
        if bases.count(name) > 1:          # e.g. several tree.stl variants: keep the folders
            parts = os.path.normpath(os.path.abspath(path)).split(os.sep)
            name = "_".join(parts[-4:]) if parts[-2] == "out" else "_".join(parts[-3:])
        mesh = C.load_mesh(path)
        r = {"path": os.path.abspath(path)}
        r["topology"] = topology(mesh)
        r["bed"] = bed_fit(mesh, bed, args.bed_margin)
        r["overhangs"] = overhangs(mesh, args.overhang, args.bed_band)
        do_relief = args.relief == "on" or (args.relief == "auto" and r["bed"]["size"][2] <= args.relief_max_h)
        if do_relief and r["topology"]["watertight"]:
            rl, vis = relief(mesh, args)
            r["relief"] = rl
            if args.png_dir:
                os.makedirs(args.png_dir, exist_ok=True)
                png = os.path.join(args.png_dir, os.path.splitext(name)[0] + "_relief.png")
                relief_png(png, name, *vis, args)
                r["relief_png"] = os.path.abspath(png)
        rows = verdicts(name, r, args)
        r["verdicts"] = rows
        print_table(name, rows)
        for row in rows:
            if row["status"] == "FAIL":
                worst = "FAIL"
            elif row["status"] == "WARN" and worst == "PASS":
                worst = "WARN"
        report[name] = r
    print(f"\nOVERALL: {worst}")
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump({"overall": worst, "meshes": report}, f, indent=1)
        print("report:", args.json)
    sys.exit(1 if worst == "FAIL" else 0)


if __name__ == "__main__":
    main()
