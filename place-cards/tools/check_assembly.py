#!/usr/bin/env python3
"""Assembled place card check: foot + plaque leaning in the slot + tree glued in its pocket.

Builds the scene exactly like wedding_place_cards.scad's assembly() (parameters read from the
SCAD and its generated tree_data.scad: lean, slot_y, slot_depth, foot_h, plaque_t,
pocket_depth, tree_pos, assembly_foot_x), with the plaque at both ends of the slot clearance
and in the middle, and reports:
  * interpenetration volume between every pair of parts (manifold3d booleans)
  * the minimum trunk-to-foot gap (target: trunk_gap from the SCAD, about 0.6 mm), the tree's
    side clearance inside its pocket, and whether the tree touches the gold border or name
  * the tree's thickness where it hangs beyond the plaque (target >= 1.4 mm) and over it
  * masses: PrusaSlicer filament weight per part with the user's settings (plaque 15 %
    rectilinear, foot 15 % lightning, tree 100 %, gold name 3 walls) and a shell + infill
    voxel model of each part for its centre of mass (walls 2 x 0.42, top 1.0, bottom 0.6)
  * centre of mass of the assembly and tipping margins front / back / left / right (distance
    from the CoM to the edge of the foot's bed contact, and the tilt angle that tips it) for
    the recommended foot position (left end under the trunk), the foot centred on the plaque,
    and the best position for side balance
Writes a JSON report and a PNG (front, side and top views with the CoM and support area).

Run (from the repo root; every path can be given):
  python3 place-cards/tools/check_assembly.py --name Sophie \
      --base place-cards/stl/plaque_Sophie_base.stl --gold place-cards/stl/plaque_Sophie_gold.stl \
      --foot place-cards/stl/foot.stl --tree place-cards/tree/out/tree.stl \
      --out-dir place-cards/renders/checks
  options: --scad other.scad, --tree-pos X,Y, --foot-x X, --no-slice (voxel masses only),
           --density-ivory/--density-gold/--density-wood, --set-foot / --set-plaque /
           --set-tree / --set-gold key=value (PrusaSlicer overrides per part)
Exit code 1 when parts collide (beyond --max-overlap mm3), the trunk gap is under half the
target, or a tipping margin is negative.
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_common as C  # noqa: E402


# ---------------------------------------------------------------- masses
def shell_model(mesh, layer, res, wall, top, bottom, infill):
    """Voxel model of a sliced part: walls (XY distance to the slice outline <= wall), top and
    bottom shells (vertical distance to the surface), the rest at the infill fraction.
    Returns relative mass volume (mm3 of plastic), CoM, solid volume."""
    import cv2
    man = C.to_manifold(mesh)
    lo, hi = mesh.bounds
    nz = max(1, int(round((hi[2] - lo[2]) / layer)))
    grid = C.Grid(lo[0], lo[1], hi[0], hi[1], res, pad=0.2)
    occ = np.zeros((nz,) + grid.shape, bool)
    zs = lo[2] + (np.arange(nz) + 0.5) * (hi[2] - lo[2]) / nz
    dz = (hi[2] - lo[2]) / nz
    for k, z in enumerate(zs):
        occ[k] = C.raster_rings(C.section_rings(man, z), grid)
    dens = np.zeros(occ.shape, np.float32)
    above = np.zeros(occ.shape, np.int16)
    below = np.zeros(occ.shape, np.int16)
    for k in range(nz - 1, -1, -1):
        above[k] = (above[k + 1] + 1 if k + 1 < nz else 1) * occ[k]
    for k in range(nz):
        below[k] = (below[k - 1] + 1 if k > 0 else 1) * occ[k]
    for k in range(nz):
        if not occ[k].any():
            continue
        d = cv2.distanceTransform(occ[k].astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE) * res
        solid = (d <= wall) | (above[k] * dz <= top + 1e-6) | (below[k] * dz <= bottom + 1e-6)
        dens[k] = np.where(occ[k], np.where(solid, 1.0, infill), 0.0)
    vox = res * res * dz
    xs, ys = grid.centres()
    w = dens.sum()
    com = np.array([(dens.sum(axis=(0, 1)) * xs).sum() / w,
                    (dens.sum(axis=(0, 2)) * ys).sum() / w,
                    (dens.sum(axis=(1, 2)) * zs).sum() / w])
    return {"plastic_mm3": float(w * vox), "solid_mm3": float(occ.sum() * vox), "com": com,
            "fill_fraction": float(w / max(occ.sum(), 1))}


PART_SPEC = {
    # kind for PrusaSlicer, filament, voxel model (layer, wall, top, bottom, infill)
    "base": ("plaque", "ivory", (0.2, 0.87, 1.0, 0.6, 0.15)),
    "gold": ("gold", "gold", (0.1, 1.29, 1.0, 0.6, 1.0)),
    "foot": ("foot", "wood", (0.2, 0.87, 1.0, 0.6, 0.15)),
    "tree": ("tree", "gold", (0.1, 1.29, 1.0, 0.6, 1.0)),
}


def part_masses(paths, meshes, args, work):
    out = {}
    for key, mesh in meshes.items():
        kind, fil, (layer, wall, top, bottom, infill) = PART_SPEC[key]
        dens = {"ivory": args.density_ivory, "gold": args.density_gold, "wood": args.density_wood}[fil]
        res = 0.1 if key in ("tree", "gold") else 0.2
        sm = shell_model(mesh, layer, res, wall, top, bottom, infill)
        r = {"filament": fil, "density_g_cm3": dens, "solid_volume_mm3": round(float(mesh.volume), 1),
             "voxel_plastic_mm3": round(sm["plastic_mm3"], 1), "voxel_fill_fraction": round(sm["fill_fraction"], 3),
             "voxel_mass_g": round(sm["plastic_mm3"] * dens / 1000, 2), "com_part_frame": sm["com"].round(3).tolist()}
        r["mass_g"] = r["voxel_mass_g"]
        r["mass_source"] = "voxel shell model"
        if not args.no_slice:
            over = C.parse_set(getattr(args, f"set_{key}"))
            over["filament_density"] = dens
            cfg = C.prusa_profile(kind, over)
            g = os.path.join(work, f"mass_{key}.gcode")
            try:
                C.slice_prusa(paths[key], g, cfg, work, center=(128, 128))
                s = C.gcode_summary(g)
                r["prusaslicer"] = s
                if "filament_g" in s:
                    r["mass_g"] = round(float(s["filament_g"]), 2)
                    r["mass_source"] = "PrusaSlicer filament used"
            except Exception as e:  # noqa: BLE001
                r["prusaslicer_error"] = str(e)[-500:]
        out[key] = r
    return out


# ---------------------------------------------------------------- tipping
def support_polygon(foot_world):
    from shapely.geometry import MultiPoint, Polygon
    rings = C.section_rings(C.to_manifold(foot_world), 0.02)
    pts = np.vstack(rings) if rings else foot_world.vertices[foot_world.vertices[:, 2] < 0.05][:, :2]
    hull = MultiPoint([tuple(p) for p in pts]).convex_hull
    return hull if isinstance(hull, Polygon) else hull.buffer(1e-3)


def margins(hull, com):
    """Distance from the CoM's ground projection to the support edge, along +-x, +-y, and the
    tilt that tips the card over that edge (atan(margin / CoM height))."""
    from shapely.geometry import LineString, Point
    p = Point(com[0], com[1])
    inside = hull.contains(p)
    out = {}
    for name, d in (("front (-y)", (0, -1)), ("back (+y)", (0, 1)), ("left (-x)", (-1, 0)), ("right (+x)", (1, 0))):
        ray = LineString([(com[0], com[1]), (com[0] + 500 * d[0], com[1] + 500 * d[1])])
        hit = ray.intersection(hull.exterior)
        if hit.is_empty:
            m = -float(hull.exterior.distance(p))
        else:
            pts = [hit] if hit.geom_type == "Point" else list(getattr(hit, "geoms", [hit]))
            m = min(math.hypot(q.x - com[0], q.y - com[1]) for q in pts if q.geom_type == "Point")
            if not inside:
                m = -m
        out[name] = {"margin_mm": round(m, 2), "tip_angle_deg": round(math.degrees(math.atan2(m, com[2])), 1)}
    out["min_edge_distance_mm"] = round(float(hull.exterior.distance(p)) * (1 if inside else -1), 2)
    return out


# ---------------------------------------------------------------- tree thickness vs plaque
def tree_overhang_thickness(tree, base, P, res=0.1):
    """Tree thickness (glue face to top) where its outline lies beyond the plaque, and over it."""
    from scipy import ndimage as ndi
    lo, hi = tree.bounds
    grid = C.Grid(lo[0], lo[1], hi[0], hi[1], res, pad=0.2)
    xs, ys = grid.centres()
    X, Y = np.meshgrid(xs, ys)
    pts = np.c_[X.ravel(), Y.ravel()]
    top = np.full(len(pts), np.nan)
    bot = np.full(len(pts), np.nan)
    for sign, arr in ((-1, top), (1, bot)):
        z0 = hi[2] + 1 if sign < 0 else lo[2] - 1
        o = np.c_[pts, np.full(len(pts), z0)]
        d = np.tile([0, 0, sign], (len(pts), 1)).astype(float)
        loc, idx, _ = tree.ray.intersects_location(o, d, multiple_hits=False)
        arr[idx] = loc[:, 2]
    th = (top - bot).reshape(grid.shape)
    mask = ~np.isnan(th)
    # plaque outline in the tree frame
    tx, ty = P["tree_pos"]
    rings = [r - [tx, ty] for r in C.section_rings(C.to_manifold(base), 0.5 * P["plaque_t"])]
    on_plaque = C.raster_rings(rings, grid)
    inner = ndi.distance_transform_edt(mask) * res > 0.4          # skip the bed chamfer band
    off = mask & ~on_plaque & inner
    over = mask & on_plaque & inner

    def st(v):
        v = v[~np.isnan(v)]
        return {} if not len(v) else {"min": round(float(v.min()), 3), "p1": round(float(np.percentile(v, 1)), 3),
                                      "median": round(float(np.median(v)), 3), "area_mm2": round(len(v) * res * res, 1)}
    return {"beyond_plaque": st(th[off]), "over_plaque": st(th[over]),
            "beyond_below_1.4_mm2": round(float((off & (th < 1.4)).sum() * res * res), 2)}


# ---------------------------------------------------------------- picture
def silhouette(ax, mesh, axes, colour, alpha=1.0, z=1):
    from matplotlib.collections import PolyCollection
    tri = mesh.vertices[mesh.faces][:, :, axes]
    # skip the back-facing half for speed: a silhouette needs only one side
    n = mesh.face_normals
    view = [i for i in range(3) if i not in axes][0]
    keep = n[:, view] <= 0 if view == 1 else n[:, view] >= 0
    pc = PolyCollection(tri[keep], facecolors=colour, edgecolors="none", alpha=alpha, zorder=z, antialiased=False)
    ax.add_collection(pc)


def png(path, scenes, title, coll_geom=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rec = scenes[0]
    fig = plt.figure(figsize=(17, 11), dpi=100)
    gs = fig.add_gridspec(2, 3, width_ratios=[2.2, 1, 1.4])
    col = {"foot": "#7A4F2E", "base": "#E8E2D0", "gold": "#D4AF37", "tree": "#C9A227"}
    # front view (x-z), recommended foot
    ax = fig.add_subplot(gs[0, 0])
    for k in ("base", "gold", "tree", "foot"):
        if k in rec["world"]:
            silhouette(ax, rec["world"][k], [0, 2], col[k], z={"foot": 3, "base": 1, "gold": 2, "tree": 4}[k])
    for s in scenes:
        ax.plot(s["com"][0], s["com"][2], "o", ms=9, mfc="none", mec=s["colour"], mew=2, zorder=10,
                label=f"CoM, {s['label']}")
    for k, m in (coll_geom or {}).items():
        if len(m.faces):
            silhouette(ax, m, [0, 2], "#ff0000", z=12)
            c = m.bounds.mean(axis=0)
            ax.annotate(f"overlap {k}", (c[0], c[2]), color="red", fontsize=8, zorder=13)
    ax.axhline(0, color="k", lw=0.8)
    ax.autoscale()
    ax.set_aspect("equal")
    ax.legend(fontsize=8, loc="upper right")
    ax.set_title("front view (from the guest), recommended foot position", fontsize=10)
    # side view (y-z)
    ax = fig.add_subplot(gs[0, 1])
    for k in ("base", "gold", "tree", "foot"):
        if k in rec["world"]:
            silhouette(ax, rec["world"][k], [1, 2], col[k], alpha=0.85, z={"foot": 3, "base": 1, "gold": 2, "tree": 4}[k])
    ax.plot(rec["com"][1], rec["com"][2], "o", ms=9, mfc="none", mec="r", mew=2, zorder=10)
    hb = rec["hull"].bounds
    ax.plot([hb[1], hb[3]], [0, 0], "-", color="r", lw=3, zorder=11)
    ax.plot([rec["com"][1]] * 2, [0, rec["com"][2]], "r:", lw=1)
    ax.autoscale()
    ax.set_aspect("equal")
    ax.set_title("side view (guest on the left)", fontsize=10)
    # gap close-up, side view around the trunk bottom
    ax = fig.add_subplot(gs[0, 2])
    tw = rec["world"].get("tree", rec["world"]["base"])
    zlo = tw.bounds[0, 2]
    for k in ("base", "tree", "foot"):
        if k in rec["world"]:
            silhouette(ax, rec["world"][k], [1, 2], col[k], alpha=0.8, z={"foot": 3, "base": 1, "tree": 4}[k])
    ax.set_xlim(rec["world"]["foot"].bounds[0, 1] - 1, rec["world"]["foot"].bounds[1, 1] + 1)
    ax.set_ylim(zlo - 4, zlo + 4)
    ax.set_aspect("equal")
    ax.set_title(f"trunk bottom vs foot (side), min gap {rec['trunk_gap_mm']:.2f} mm", fontsize=10)
    # front close-up of the trunk bottom
    ax = fig.add_subplot(gs[1, 2])
    for k in ("tree", "foot"):
        if k in rec["world"]:
            silhouette(ax, rec["world"][k], [0, 2], col[k], alpha=0.8, z={"foot": 3, "tree": 4}[k])
    trunk = tw.vertices[tw.vertices[:, 2] < zlo + 3]
    ax.set_xlim(trunk[:, 0].min() - 4, trunk[:, 0].max() + 4)
    ax.set_ylim(zlo - 5, zlo + 5)
    ax.set_aspect("equal")
    ax.set_title("trunk bottom vs foot (front)", fontsize=10)
    # top view: support polygons and CoM for each foot position
    ax = fig.add_subplot(gs[1, 0:2])
    for k in ("base", "tree"):
        if k in rec["world"]:
            silhouette(ax, rec["world"][k], [0, 1], col[k], alpha=0.35, z=1)
    for i, s in enumerate(scenes):
        hx, hy = s["hull"].exterior.xy
        ax.plot(hx, hy, "-", color=s["colour"], lw=1.5, label=f"foot contact, {s['label']}")
        ax.plot(s["com"][0], s["com"][1], "o", ms=8, color=s["colour"])
        m = s["tipping"]
        ax.annotate(f"L {m['left (-x)']['margin_mm']:.1f} / R {m['right (+x)']['margin_mm']:.1f}\n"
                    f"F {m['front (-y)']['margin_mm']:.1f} / B {m['back (+y)']['margin_mm']:.1f} mm",
                    (s["com"][0], s["com"][1]), textcoords="offset points", xytext=(-40 + 75 * i, -40 - 4 * i),
                    fontsize=7, arrowprops={"arrowstyle": "-", "color": s["colour"], "lw": 0.5},
                    color=s["colour"])
    ax.autoscale()
    ax.set_aspect("equal")
    ax.legend(fontsize=8, loc="lower right")
    ax.set_title("top view: foot bed contact and centre of mass (margins in mm)", fontsize=10)
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scad", default=C.DEFAULT_SCAD, help="SCAD with the card parameters")
    ap.add_argument("--tree-data", help="generated include with tree_pos_auto (default: next to the SCAD)")
    ap.add_argument("--name", default="card", help="label for the outputs")
    ap.add_argument("--base", required=True, help="ivory plaque STL (plaque frame, print pose)")
    ap.add_argument("--gold", help="gold name + border STL (plaque frame, print pose)")
    ap.add_argument("--foot", required=True, help="foot STL (foot frame, print pose)")
    ap.add_argument("--tree", help="tree STL (canonical tree frame)")
    ap.add_argument("--tree-pos", help="x,y of the tree origin on the plaque (default: SCAD tree_pos)")
    ap.add_argument("--foot-x", type=float, help="recommended foot centre x in the plaque frame "
                                                 "(default: assembly_foot_x from tree_data.scad)")
    ap.add_argument("--plaque-x0", type=float, help="left end of the plaque rectangle (default: 0 for the "
                                                     "tree SCAD, the mesh's left end otherwise)")
    ap.add_argument("--no-slice", action="store_true", help="skip PrusaSlicer, masses from the voxel model")
    ap.add_argument("--density-ivory", type=float, default=C.FILAMENTS["ivory"]["density"])
    ap.add_argument("--density-gold", type=float, default=C.FILAMENTS["gold"]["density"])
    ap.add_argument("--density-wood", type=float, default=C.FILAMENTS["wood"]["density"])
    for k in PART_SPEC:
        ap.add_argument(f"--set-{k}", action="append", default=[], help=f"PrusaSlicer override for the {k}")
    ap.add_argument("--min-tip", type=float, default=10.0,
                    help="tilt angle (deg) the card must survive in every direction (PASS); under 5 = FAIL")
    ap.add_argument("--max-overlap", type=float, default=0.5, help="allowed interpenetration per pair (mm3)")
    ap.add_argument("--out-dir", default=os.path.join(C.ROOT, "renders", "checks"))
    ap.add_argument("--work", help="scratch folder for the mass G-code (default: a temp folder)")
    args = ap.parse_args()

    t0 = time.time()
    P = C.card_params(args.scad, args.tree_data)
    if args.tree_pos:
        P["tree_pos"] = [float(v) for v in args.tree_pos.split(",")]
    os.makedirs(args.out_dir, exist_ok=True)
    import tempfile
    work = args.work or tempfile.mkdtemp(prefix=f"check_assembly_{args.name}_")
    os.makedirs(work, exist_ok=True)

    paths = {"base": args.base, "foot": args.foot}
    if args.gold:
        paths["gold"] = args.gold
    if args.tree:
        paths["tree"] = args.tree
    meshes = {k: C.load_mesh(p) for k, p in paths.items()}
    mans = {k: C.to_manifold(m) for k, m in meshes.items()}
    base_lo, base_hi = meshes["base"].bounds
    # rounded rectangle: the tree-era SCAD puts its left end at x = 0 (the backing behind the
    # tree may reach x < 0); the original SCAD centres the plaque on x = 0
    if args.plaque_x0 is not None:
        rect_x0 = args.plaque_x0
    else:
        rect_x0 = 0.0 if ("tree_pos_auto" in P or "outline_bbox" in P) else base_lo[0]
    rect_x1 = base_hi[0]

    # foot positions: recommended (left end under the trunk), centred on the plaque, and best
    foot_rec = args.foot_x if args.foot_x is not None else P.get("assembly_foot_x")
    if foot_rec is None:
        foot_rec = (rect_x0 + rect_x1) / 2
    foot_centre = (rect_x0 + rect_x1) / 2

    contacts = [-P["slot_fit"] / 2, 0.0, P["slot_fit"] / 2]

    def world(contact, foot_x):
        Mp = C.plaque_to_world(P, contact)
        M = {"base": Mp, "gold": Mp, "foot": C.foot_to_world(foot_x)}
        if "tree" in meshes:
            M["tree"] = Mp @ C.tree_to_plaque(P)
        return M

    # ---- collisions and gaps (all slot contacts), recommended foot position
    pairs = [("tree", "base"), ("tree", "gold"), ("tree", "foot"), ("base", "foot"), ("gold", "foot"), ("gold", "base")]
    coll = {}
    coll_geom = {}
    gaps = {"trunk_to_foot_mm": [], "plaque_to_foot_mm": []}
    for c in contacts:
        M = world(c, foot_rec)
        W = {k: mans[k].transform(M[k][:3, :]) for k in mans}
        for a, b in pairs:
            if a in W and b in W:
                inter = W[a] ^ W[b]
                v = inter.volume()
                key = f"{a} x {b}"
                if v > coll.get(key, 0.0) and v > 1e-3:
                    coll_geom[key] = C.manifold_to_trimesh(inter)
                coll[key] = max(coll.get(key, 0.0), float(v))
        if "tree" in W:
            gaps["trunk_to_foot_mm"].append(float(W["tree"].min_gap(W["foot"], 10.0)))
    # tree side clearance in the pocket: the plaque above the pocket floor vs the tree
    pocket = {}
    if "tree" in mans and P.get("pocket_depth", 0) <= 0:
        pocket["tree_to_gold_mm"] = round(float(mans["tree"].transform(C.tree_to_plaque(P)[:3, :]).min_gap(
            mans["gold"], 5.0)), 3) if "gold" in mans else None
        pocket["tree_thickness"] = tree_overhang_thickness(meshes["tree"], meshes["base"], P)
    elif "tree" in mans:
        import manifold3d as m3
        Mt = C.tree_to_plaque(P)
        tr = mans["tree"].transform(Mt[:3, :])
        floor = P["plaque_t"] - P.get("pocket_depth", 0)
        slab = m3.Manifold.cube([1000, 1000, 10]).translate([-500, -500, floor + 0.02])
        walls = mans["base"] ^ slab
        pocket["tree_to_pocket_wall_mm"] = round(float(tr.min_gap(walls, 3.0)), 3)
        pocket["tree_glue_face_z"] = round(float(tr.bounding_box()[2]), 3)
        pocket["pocket_floor_z"] = round(floor, 3)
        if "gold" in mans:
            pocket["tree_to_gold_mm"] = round(float(tr.min_gap(mans["gold"], 5.0)), 3)
        pocket["tree_thickness"] = tree_overhang_thickness(meshes["tree"], meshes["base"], P)

    # ---- masses and CoM
    masses = part_masses(paths, meshes, args, work)
    scenes = []
    best = None
    for label, fx, colour in (("recommended (left end under the trunk)", foot_rec, "#d62728"),
                              ("foot centred on the plaque", foot_centre, "#1f77b4")):
        M = world(0.0, fx)
        Wm = {k: meshes[k].copy().apply_transform(M[k]) for k in meshes}
        tot = 0.0
        acc = np.zeros(3)
        for k, r in masses.items():
            com_w = C.transform_points(M[k], [r["com_part_frame"]])[0]
            acc += r["mass_g"] * com_w
            tot += r["mass_g"]
        com = acc / tot
        hull = support_polygon(Wm["foot"])
        tip = margins(hull, com)
        scenes.append({"label": label, "foot_x": round(float(fx), 2), "com": com, "hull": hull, "tipping": tip,
                       "world": Wm, "colour": colour, "mass_g": tot})
        if best is None:
            # foot centre under the CoM balances left / right (the CoM x does not depend on the foot)
            com_wo_foot = (acc - masses["foot"]["mass_g"] * C.transform_points(M["foot"], [masses["foot"]["com_part_frame"]])[0])
            m_wo = tot - masses["foot"]["mass_g"]
            fcx = masses["foot"]["com_part_frame"][0]
            # solve x: (m_wo * cx_wo + m_f * (x + fcx)) / tot = x  ->  x = (m_wo cx_wo + m_f fcx) / m_wo
            best = float((com_wo_foot[0] + masses["foot"]["mass_g"] * fcx) / m_wo)
    M = world(0.0, best)
    Wm = {k: meshes[k].copy().apply_transform(M[k]) for k in meshes}
    acc = sum(r["mass_g"] * C.transform_points(M[k], [r["com_part_frame"]])[0] for k, r in masses.items())
    com = acc / sum(r["mass_g"] for r in masses.values())
    hull = support_polygon(Wm["foot"])
    scenes.append({"label": "foot under the CoM (best side balance)", "foot_x": round(best, 2), "com": com,
                   "hull": hull, "tipping": margins(hull, com), "world": Wm, "colour": "#2ca02c",
                   "mass_g": sum(r["mass_g"] for r in masses.values())})
    for s in scenes:
        s["trunk_gap_mm"] = min(gaps["trunk_to_foot_mm"]) if gaps["trunk_to_foot_mm"] else float("nan")

    # foot positions that keep the card from tipping sideways: the foot slides along the open slot,
    # so scan its centre x and keep both side tilt angles >= --min-tip
    M0 = world(0.0, 0.0)
    m_f = masses["foot"]["mass_g"]
    f_com = C.transform_points(M0["foot"], [masses["foot"]["com_part_frame"]])[0]
    rest = [(r["mass_g"], C.transform_points(M0[k], [r["com_part_frame"]])[0]) for k, r in masses.items() if k != "foot"]
    m_r = sum(m for m, _ in rest)
    c_r = sum(m * c for m, c in rest) / m_r
    hb = scenes[0]["hull"].bounds
    cx0, cx1 = hb[0] - foot_rec, hb[2] - foot_rec          # contact x range relative to the foot centre
    ok_x = []
    scan = []
    for fx in np.arange(rect_x0 - P["foot_len"] / 2, rect_x1 + P["foot_len"] / 2 + 1e-9, 0.25):
        com = (m_r * c_r + m_f * (f_com + [fx, 0, 0])) / (m_r + m_f)
        left = math.degrees(math.atan2(com[0] - (fx + cx0), com[2]))
        right = math.degrees(math.atan2((fx + cx1) - com[0], com[2]))
        scan.append((round(float(fx), 2), round(left, 1), round(right, 1)))
        if min(left, right) >= args.min_tip:
            ok_x.append(float(fx))
    foot_range = [round(min(ok_x), 2), round(max(ok_x), 2)] if ok_x else None

    # ---- verdicts
    rows = []

    def add(check, status, value, note=""):
        rows.append({"check": check, "status": status, "value": value, "note": note})
    for k, v in coll.items():
        expect_touch = k in ("base x foot", "gold x base", "tree x base")
        st = "PASS" if v <= args.max_overlap else ("WARN" if expect_touch and v < 5 * args.max_overlap else "FAIL")
        add(f"overlap {k}", st, f"{v:.3f} mm3", "max over the slot clearance")
    if gaps["trunk_to_foot_mm"]:
        g = min(gaps["trunk_to_foot_mm"])
        target = P.get("trunk_gap", 0.6)
        st = "PASS" if g >= 0.8 * target else ("WARN" if g >= 0.5 * target else "FAIL")
        add("trunk to foot gap", st, f"{g:.3f} mm", f"target {target}; per slot contact "
            + ", ".join(f"{x:.3f}" for x in gaps["trunk_to_foot_mm"]))
    if pocket:
        if "tree_to_pocket_wall_mm" in pocket:
            w = pocket["tree_to_pocket_wall_mm"]
            add("tree side clearance in pocket", "PASS" if w > 0.02 else "WARN", f"{w:.3f} mm",
                f"pocket_clear {P.get('pocket_clear')}")
        if pocket.get("tree_to_gold_mm") is not None:
            add("tree clear of gold border/name", "PASS" if pocket["tree_to_gold_mm"] > 0.05 else "WARN",
                f"{pocket['tree_to_gold_mm']:.3f} mm")
        tb = pocket["tree_thickness"]["beyond_plaque"]
        if tb:
            add("tree thickness beyond plaque", "PASS" if tb["min"] >= 1.35 else "WARN", f"{tb['min']} mm",
                f"target >= 1.4; {pocket['tree_thickness']['beyond_below_1.4_mm2']} mm2 below; "
                f"area beyond {tb['area_mm2']} mm2")
    for s in scenes:
        m = s["tipping"]
        worst = min(v["tip_angle_deg"] for k, v in m.items() if isinstance(v, dict))
        st = "PASS" if worst >= args.min_tip else ("WARN" if worst >= 5 else "FAIL")
        add(f"tipping, {s['label']}", st, f"min {worst:.1f} deg",
            "F {0} / B {1} / L {2} / R {3} mm, tilt to tip F {4} / B {5} deg".format(
                m["front (-y)"]["margin_mm"], m["back (+y)"]["margin_mm"], m["left (-x)"]["margin_mm"],
                m["right (+x)"]["margin_mm"], m["front (-y)"]["tip_angle_deg"], m["back (+y)"]["tip_angle_deg"])
            + f", L {m['left (-x)']['tip_angle_deg']} / R {m['right (+x)']['tip_angle_deg']} deg")
    add(f"foot x range for side tilt >= {args.min_tip:g} deg", "INFO" if foot_range else "FAIL",
        f"{foot_range}" if foot_range else "none",
        f"foot centre x in the plaque frame; recommended {foot_rec:.2f}; best balance {best:.2f}")

    title = (f"{args.name}: assembled check (lean {P['lean']} deg, tree_pos {P['tree_pos']}, "
             f"foot x {foot_rec:.2f}), total {scenes[0]['mass_g']:.1f} g")
    pngp = os.path.join(args.out_dir, f"assembly_{args.name}.png")
    png(pngp, scenes, title, coll_geom)

    report = {
        "name": args.name, "scad": P["_scad"], "inputs": {k: os.path.abspath(v) for k, v in paths.items()},
        "params": {k: P.get(k) for k in ("lean", "slot_y", "slot_depth", "slot_fit", "foot_h", "foot_len", "plaque_t",
                                         "plaque_h", "pocket_depth", "pocket_clear", "trunk_gap", "tree_pos",
                                         "assembly_foot_x")},
        "plaque_rect_x": [round(rect_x0, 2), round(rect_x1, 2)],
        "overlaps_mm3": {k: round(v, 4) for k, v in coll.items()},
        "gaps": {k: [round(x, 4) for x in v] for k, v in gaps.items()},
        "pocket": pocket,
        "masses": masses,
        "scenes": [{"label": s["label"], "foot_x": s["foot_x"], "mass_g": round(s["mass_g"], 2),
                    "com_world": [round(float(v), 2) for v in s["com"]],
                    "support_bounds": [round(float(v), 2) for v in s["hull"].bounds], "tipping": s["tipping"]}
                   for s in scenes],
        "foot_x_range_side_tilt": {"min_tip_deg": args.min_tip, "range": foot_range,
                                   "contact_x_rel_foot_centre": [round(cx0, 2), round(cx1, 2)],
                                   "scan_foot_x_left_right_deg": scan[::4]},
        "verdicts": rows, "png": pngp, "seconds": round(time.time() - t0, 1),
    }
    overall = "FAIL" if any(r["status"] == "FAIL" for r in rows) else (
        "WARN" if any(r["status"] == "WARN" for r in rows) else "PASS")
    report["overall"] = overall
    js = os.path.join(args.out_dir, f"assembly_{args.name}.json")
    with open(js, "w") as f:
        json.dump(report, f, indent=1)

    print(f"== {args.name}")
    for k, r in masses.items():
        print(f"  mass {k:<5} {r['mass_g']:6.2f} g ({r['mass_source']}; voxel model {r['voxel_mass_g']} g, "
              f"solid {r['solid_volume_mm3']} mm3)")
    for s in scenes:
        print(f"  CoM [{s['label']}] foot x {s['foot_x']}: ({s['com'][0]:.1f}, {s['com'][1]:.1f}, {s['com'][2]:.1f}) mm")
    w = max(len(r["check"]) for r in rows)
    for r in rows:
        print(f"  {r['status']:<5} {r['check']:<{w}}  {r['value']:<14} {r['note']}")
    print(f"OVERALL: {overall}\nreport: {js}\nimage:  {pngp}")
    sys.exit(1 if overall == "FAIL" else 0)


if __name__ == "__main__":
    main()
