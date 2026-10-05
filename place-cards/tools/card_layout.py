#!/usr/bin/env python3
"""Fit the place card plaque to the glued gold tree and write tree_data.scad.

The SCAD (wedding_place_cards.scad) includes the generated tree_data.scad. This script
computes everything the SCAD cannot work out by itself:
  * tree_pos_auto: where the tree origin (trunk base centre) sits in the plaque frame.
      x: the plaque (the user's plain rounded rectangle) has its left end hidden behind the
         trunk: the x where the gold covers the most of the plaque's visible left edge (the
         middle of that plateau), or `--hide` mm inside the trunk's left edge if given.
      y: real 3D check with manifold3d against the foot exported from the SCAD, so the
         trunk bottom clears the foot by `trunk_gap` (read from the SCAD).
  * the tree outline (tree frame), the pocket (glue face offset by pocket_clear,
    slivers closed), the region where the gold border is cut so it ends behind a solid
    part of the tree,
  * the name table: ink extents of every guest name (measured from OpenSCAD's own text
    rendering), the left-most position that keeps each name tree_name_gap clear of the tree
    (the SCAD left-aligns every name there), and where that card's foot goes (see below).
  * the foot position of every card. The foot sits as in the photo (left end flush with the
    trunk's left edge) unless the card would then tip sideways at less than `side_tilt` deg
    (SCAD) with the foot printed as recommended (`--foot-basis`, default "solid": 100 % infill;
    the plain plaque has nothing on the left to balance a long name on a 15 % foot). Long names
    then get the foot slid right just enough, never past the trunk's centre line, so the trunk
    stays over the foot.
    The SCAD engraves a small tick on the plaque's back where the foot's right end goes.
    Masses and centres of mass come from a print model of each part (2D outlines, the user's
    walls / shells / infill, PrusaSlicer-calibrated: see plaque_mass()). The foot prints in
    FOOT_PRINTS are sliced with PrusaSlicer whenever the foot changes (cached in foot_prints.json).

Frames: plaque frame = x from the plaque's left end (rounded rectangle), y = 0 on the plaque's
centre line, z = 0 on the bed. Tree frame (canonical) =
origin at the bottom centre of the trunk, +y up, z = 0 on the glue face. All polygons in
tree_data.scad are in the tree frame; the SCAD moves them by tree_pos.

Run from anywhere (Python 3.11, shapely, scipy, trimesh, manifold3d, OpenSCAD on PATH):
  python3 place-cards/tools/card_layout.py              # final tree if present, else provisional
  python3 place-cards/tools/card_layout.py --footprint some/tree_footprint.json
  python3 place-cards/tools/card_layout.py --check      # also verify every name (OpenSCAD 2D export)
  python3 place-cards/tools/card_layout.py --check --foot-check test,solid,gyroid40   # stability per foot print
Re-run it whenever the tree, the names list or a plaque/pocket/border/foot parameter changes.
--check prints, per name: plaque width, foot shift, tick x, and the tilt (deg) that tips the card
left / right / back for each foot print in --foot-check. It fails when a card is under side_tilt
sideways with the --foot-basis print, or under --back-tilt backwards with the recommended print.
"""
import argparse
import json
import math
import os
import re
import subprocess
import tempfile

import numpy as np
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # place-cards/
SCAD = os.path.join(ROOT, "wedding_place_cards.scad")
OUT = os.path.join(ROOT, "tree_data.scad")
FINAL_FOOTPRINT = os.path.join(ROOT, "tree", "out", "tree_footprint.json")
PROVISIONAL_FOOTPRINT = ("/tmp/claude-0/-home-user-blaizeff-github-io/24db3fd0-37aa-5abe-8f21-849f3f5e2464/"
                         "scratchpad/work/tree_footprint_provisional.json")
TREE_STL_REL = "tree/out/tree.stl"                 # relative to place-cards/ (as the SCAD sees it)
LORA_TTF = "/usr/share/fonts/truetype/lora/Lora-600.ttf"
SIMPLIFY = 0.02
QS = 16                                            # quad segments for shapely buffers

# ---- print model for masses and centres of mass (tipping)
# Filament densities (g/cm3) from the user's OrcaSlicer project.
RHO = {"ivory": 1.25, "gold": 1.32, "wood": 1.25}
# Ivory plaque: the user's 2 walls, 3 bottom / 5 top layers, 15 % infill. `band`: solid ring the
# slicer adds around the pocket step (vertical shell); `flow`: bridges and overlaps. Fitted to the
# extruded filament in PrusaSlicer G-code for every guest name: mass within 0.1 %, CoM within 0.05 mm.
PLAQUE_PRINT = {"wall": 0.87, "bottom": 0.6, "top": 1.0, "infill": 0.15, "band": 1.0, "flow": 1.050, "res": 0.15}
GOLD_FLOW = 0.993          # gold name + border (3 walls, solid): G-code mass / solid volume
TREE_FLOW = 1.037          # silk tree (3 walls, 100 %), same
# Foot prints: [description, PrusaSlicer settings]. Their mass (as a fraction of a solid foot) and
# centre of mass (shift from the solid foot's centroid) are measured on the current foot from the
# G-code (extruded filament per move), and cached in foot_prints.json until the foot changes.
# Lightning infill sits under the top surfaces, so a light foot also has a high centre of mass.
FOOT_PRINTS = {
    "test":     ["15 % lightning, 2 walls (the test print)", {"fill_pattern": "lightning", "fill_density": "15%"}],
    "gyroid15": ["15 % gyroid, 2 walls", {"fill_pattern": "gyroid", "fill_density": "15%"}],
    "gyroid40": ["40 % gyroid, 4 walls", {"fill_pattern": "gyroid", "fill_density": "40%", "perimeters": 4}],
    "bottom64": ["15 % lightning, 2 walls, 6.4 mm solid bottom",
                 {"fill_pattern": "lightning", "fill_density": "15%", "bottom_solid_layers": 32,
                  "bottom_solid_min_thickness": 6.4}],
    "solid":    ["100 % infill (recommended)", {"fill_pattern": "rectilinear", "fill_density": "100%"}],
}
FOOT_CACHE = os.path.join(HERE, "foot_prints.json")


# ---------------------------------------------------------------- helpers
def polys(g):
    """List of the Polygons in any shapely geometry."""
    if g is None or g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    out = []
    for q in getattr(g, "geoms", []):
        out += polys(q)
    return out


def clean(g, tol=SIMPLIFY, min_area=0.01):
    """Valid MultiPolygon, simplified, without crumbs."""
    g = g.buffer(0)
    g = g.simplify(tol, preserve_topology=True).buffer(0)
    keep = []
    for q in polys(g):
        holes = [h for h in q.interiors if Polygon(h).area >= min_area]
        q = Polygon(q.exterior, holes)
        if q.area >= min_area:
            keep.append(orient(q, 1.0))
    return MultiPolygon(keep) if keep else MultiPolygon()


def closing(g, r):
    return g.buffer(r, quad_segs=QS).buffer(-r, quad_segs=QS)


def opening(g, r):
    return g.buffer(-r, quad_segs=QS).buffer(r, quad_segs=QS)


def denoise(g, eps=0.01, min_area=0.05):
    """Drop numerical slivers left by booleans between nearly identical outlines."""
    g = g.buffer(-eps, quad_segs=4).buffer(eps, quad_segs=4)
    return unary_union([q for q in polys(g) if q.area >= min_area])


def rrect(x0, y0, x1, y1, r):
    return box(x0 + r, y0 + r, x1 - r, y1 - r).buffer(r, quad_segs=QS)


def superellipse(cx, cy, a, b, n, k=360):
    t = np.linspace(0, 2 * np.pi, k, endpoint=False)
    c, s = np.cos(t), np.sin(t)
    return Polygon(np.c_[cx + a * np.sign(c) * np.abs(c) ** (2 / n), cy + b * np.sign(s) * np.abs(s) ** (2 / n)])


# ---------------------------------------------------------------- SCAD parameters
def parse_scad(path):
    """Top-level `name = value;` assignments that evaluate as Python (numbers, strings, lists)."""
    txt = open(path, encoding="utf-8").read()
    txt = re.sub(r"//[^\n]*", "", txt)
    stmts, buf, depth = [], "", 0
    for ch in txt:
        if ch == "{":
            depth += 1
            buf = ""
            continue
        if ch == "}":
            depth -= 1
            buf = ""
            continue
        if depth:
            continue
        if ch == ";":
            stmts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    env = {"true": True, "false": False, "undef": None}
    for s in stmts:
        m = re.match(r"^\$?([A-Za-z_]\w*)\s*=\s*(.*)$", s, re.S)
        if not m or s.startswith(("module", "function", "include", "use")):
            continue
        try:
            env[m.group(1)] = eval(m.group(2), {"__builtins__": {}}, env)
        except Exception:
            pass
    return env


# ---------------------------------------------------------------- inputs
def load_footprint(path):
    """Glue-face outline (tree frame). The pocket must take the tree's full outline, so an
    `outline_full` (outline above the bed chamfer) is merged in when the JSON has one."""
    d = json.load(open(path))
    polys_in = list(d["polygons"]) + list(d.get("outline_full", {}).get("polygons", []))
    geoms = [Polygon(p["exterior"], p.get("holes", [])) for p in polys_in]
    return clean(unary_union(geoms)), d


def tree_stl_outline(mesh, depth):
    """Outline of the tree where it sits in the pocket (above the glue-face chamfer)."""
    out = []
    for z in sorted({0.35, max(0.05, depth - 0.05)}):
        sec = mesh.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
        if sec is None:
            continue
        planar, T = sec.to_2D()
        for q in planar.polygons_full:
            out.append(affinity.affine_transform(q, [T[0, 0], T[0, 1], T[1, 0], T[1, 1], T[0, 3], T[1, 3]]))
    return clean(unary_union(out)) if out else MultiPolygon()


def export_foot_stl(scad, tmpdir):
    path = os.path.join(tmpdir, "foot.stl")
    subprocess.run(["openscad", "-o", path, "-D", 'make="foot"', scad], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


# ---------------------------------------------------------------- 3D placement
def to_manifold(vertices, faces):
    import manifold3d as m3
    return m3.Manifold(m3.Mesh(vert_properties=np.asarray(vertices, np.float32),
                               tri_verts=np.asarray(faces, np.uint32)))


def slab_manifold(shape, height):
    import manifold3d as m3
    rings = []
    for q in polys(shape):
        q = orient(q, 1.0)
        rings.append(np.asarray(q.exterior.coords[:-1]))
        rings += [np.asarray(h.coords[:-1]) for h in q.interiors]
    return m3.Manifold.extrude(m3.CrossSection(rings), height)


def plaque_to_foot(P, contact=0.0):
    """4x4: plaque frame -> foot frame (foot centred on x = 0), plaque seated in the slot.
    contact shifts the plaque inside the slot clearance (-fit/2 = against the front wall)."""
    t, h, sd = P["plaque_t"], P["plaque_h"], P["slot_depth"]
    c = t / 2 + contact
    B = np.array([[1, 0, 0, 0], [0, 0, -1, c], [0, 1, 0, h / 2 - sd], [0, 0, 0, 1]], float)  # plaque -> slot
    a = math.radians(-P["lean"])
    C = np.array([[1, 0, 0, 0], [0, math.cos(a), -math.sin(a), P["slot_y"]],
                  [0, math.sin(a), math.cos(a), P["foot_h"]], [0, 0, 0, 1]])                  # slot -> foot
    return C @ B


def tree_to_foot(P, tx, ty, contact=0.0):
    """4x4: tree frame -> foot frame, for the plaque seated in the slot."""
    A = np.eye(4)
    A[:3, 3] = [tx, ty, P["plaque_t"] - P["pocket_depth"]]           # tree -> plaque
    return plaque_to_foot(P, contact) @ A


def solve_tree_y(P, tree_m, foot_m, tx, foot_xs, target, lo=-17.0, hi=-5.0):
    """Lowest tree_pos.y whose minimum 3D gap to the foot is `target` (bisection, 0.005 mm),
    for the foot at every position in foot_xs (where the trunk meets the foot's flat top or its end)."""
    feet = [foot_m.translate([fx, 0, 0]) for fx in foot_xs]

    def gap(ty):
        g = []
        for contact in (-P["slot_fit"] / 2, 0.0, P["slot_fit"] / 2):
            t = tree_m.transform(tree_to_foot(P, tx, ty, contact)[:3, :])
            for f in feet:
                if (t ^ f).volume() > 1e-6:
                    return -1.0
                g.append(t.min_gap(f, 5.0))
        return min(g)

    for _ in range(40):
        mid = (lo + hi) / 2
        if gap(mid) < target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 0.005:
            break
    return round(math.ceil(hi * 100) / 100, 2), gap(math.ceil(hi * 100) / 100)


# ---------------------------------------------------------------- stability (print model, tipping)
def plaque_mass(P, outline, pocket):
    """Ivory plaque as printed: (g, CoM in the plaque frame). Column model on a 2D raster: walls and
    the edge profile (bed chamfer, rounded top) solid, bottom and top shells solid, sparse infill
    between, the pocket floor with its own top shell and the solid ring around the pocket step."""
    from scipy import ndimage as ndi
    from shapely import contains_xy, prepare
    k = PLAQUE_PRINT
    res, t, pd = k["res"], P["plaque_t"], P["pocket_depth"]
    c, r = P.get("edge_chamfer", 0.4), P.get("edge_fillet", 0.8)
    x0, y0, x1, y1 = outline.bounds
    X, Y = np.meshgrid(np.arange(x0 - 0.5, x1 + 0.5, res) + res / 2, np.arange(y0 - 0.5, y1 + 0.5, res) + res / 2)
    prepare(outline)
    m = contains_xy(outline, X, Y)
    p = np.zeros_like(m)
    if pd > 0 and pocket is not None and not pocket.is_empty:
        prepare(pocket)
        p = contains_xy(pocket, X, Y) & m
    d = ndi.distance_transform_edt(m) * res - res / 2           # distance to the plaque edge
    dp = ndi.distance_transform_edt(~p) * res - res / 2         # distance to the pocket
    ttop = np.where(p, t - pd, t)
    zlo = np.clip(c - d, 0, None)
    zhi = np.minimum(np.where(d < r, t - r + np.sqrt(np.clip(r * r - (r - np.clip(d, 0, r)) ** 2, 0, None)), t), ttop)
    solid = d < k["wall"]
    bot, top, inf = k["bottom"], k["top"], k["infill"]
    core = np.clip(ttop - top - bot, 0, None)
    v_in = (ttop - core) + inf * core
    z_in = (bot * bot / 2 + top * (ttop - top / 2) + inf * core * (bot + core / 2)) / v_in
    ring = (~p) & (dp < k["band"]) & ~solid & (pd > 0)
    v_ring = np.where(ring, (1 - inf) * min(pd, max(t - top - bot, 0)), 0.0)
    vol = (np.where(solid, zhi - zlo, v_in) + v_ring) * m
    zc = np.where(solid, (zhi + zlo) / 2, (v_in * z_in + v_ring * (t - pd / 2 - top)) / np.maximum(v_in + v_ring, 1e-9))
    w = vol.sum()
    com = np.array([(vol * X).sum() / w, (vol * Y).sum() / w, (vol * zc).sum() / w])
    return w * res * res * RHO["ivory"] * k["flow"] / 1000.0, com


def gold_mass(P, gold):
    """Raised gold name + border (solid, stepped bevel on top): (g, CoM in the plaque frame)."""
    steps = int(round(P.get("gold_chamfer", 0.2) / P.get("gold_step", 0.1)))
    s = P.get("gold_step", 0.1)
    vol = gold.area * P["gold_h"] - gold.length * s * s * steps * (steps + 1) / 2
    c = gold.centroid
    return vol * RHO["gold"] * GOLD_FLOW / 1000.0, np.array([c.x, c.y, P["plaque_t"] + P["gold_h"] / 2 - 0.02])


def tree_mass(tree_mesh, F, slab):
    """Glued tree: (g, CoM in the tree frame). Without the STL: a slab of the footprint."""
    if tree_mesh is not None:
        vol, com = float(tree_mesh.volume), np.asarray(tree_mesh.center_mass, float)
    else:
        vol, com = F.area * slab, np.array([F.centroid.x, F.centroid.y, slab / 2])
    return vol * RHO["gold"] * TREE_FLOW / 1000.0, com


def foot_key(foot_mesh):
    """Geometry fingerprint of the foot (OpenSCAD's facet order varies between runs, so no file hash)."""
    return "vol %.1f bbox %s" % (foot_mesh.volume, " ".join("%.2f" % v for v in np.asarray(foot_mesh.bounds).ravel()))


def measure_foot_prints(foot_mesh, tmpdir):
    """Slice the foot with every FOOT_PRINTS setting: {key: [mass fraction of a solid foot, CoM shift]}.
    Cached in foot_prints.json for this foot geometry."""
    key = foot_key(foot_mesh)
    try:
        cache = json.load(open(FOOT_CACHE))
    except (OSError, ValueError):
        cache = {}
    if cache.get("foot") == key and set(cache.get("prints", {})) == set(FOOT_PRINTS):
        return cache["prints"]
    import check_common as C
    stl = os.path.join(tmpdir, "foot_for_prints.stl")
    foot_mesh.export(stl)
    solid_g = foot_mesh.volume * RHO["wood"] / 1000.0
    centroid = np.asarray(foot_mesh.center_mass, float)
    prints = {}
    for k, (desc, settings) in FOOT_PRINTS.items():
        cfg = C.prusa_profile("foot", dict(settings, filament_density=RHO["wood"]))
        gcode = os.path.join(tmpdir, "foot_%s.gcode" % k)
        C.slice_prusa(stl, gcode, cfg, tmpdir, center=(128, 128))
        g, com = C.gcode_mass_com(gcode, foot_mesh, center=(128, 128), density=RHO["wood"])
        prints[k] = [round(g / solid_g, 4), [round(float(v), 2) for v in np.asarray(com) - centroid]]
        print("  foot print %-9s %s: %.2f g (%.1f %% of solid), CoM shift %s" % (k, desc, g, 100 * g / solid_g, prints[k][1]))
    json.dump({"foot": key, "prints": prints}, open(FOOT_CACHE, "w"), indent=1)
    return prints


def foot_mass(foot_mesh, key, prints):
    """Foot printed with FOOT_PRINTS[key]: (g, CoM in the foot frame)."""
    frac, shift = prints[key]
    return foot_mesh.volume * frac * RHO["wood"] / 1000.0, np.asarray(foot_mesh.center_mass, float) + shift


def contact_box(foot_mesh):
    """The foot's bed contact: (x range from the foot centre, y range). Its ends are straight
    across the middle, where the centre of mass of every card falls."""
    sec = foot_mesh.section(plane_origin=[0, 0, 0.02], plane_normal=[0, 0, 1])
    v = np.asarray(sec.vertices)
    return (float(v[:, 0].min()), float(v[:, 0].max())), (float(v[:, 1].min()), float(v[:, 1].max()))


def card_parts(P, tx, ty, outline, pocket, gold, tree):
    """Mass and CoM (foot frame, foot centred on x = 0) of the plaque, its gold and the tree."""
    M = plaque_to_foot(P)

    def world(c):
        return M[:3, :3] @ c + M[:3, 3]
    mb, cb = plaque_mass(P, outline, pocket)
    mg, cg = gold_mass(P, gold)
    mt, ct = tree
    return [(mb, world(cb)), (mg, world(cg)), (mt, world(ct + [tx, ty, P["plaque_t"] - P["pocket_depth"]]))]


def tilts(parts, foot, foot_x, box):
    """Tilt (deg) that tips the card over each edge of the foot's bed contact: atan(distance from the
    centre of mass to that edge / CoM height)."""
    mf, cf = foot
    mass = sum(m for m, _ in parts) + mf
    com = (sum(m * c for m, c in parts) + mf * (cf + [foot_x, 0, 0])) / mass
    (x0, x1), (y0, y1) = box

    def a(d):
        return math.degrees(math.atan2(d, com[2]))
    return {"left": a(com[0] - foot_x - x0), "right": a(foot_x + x1 - com[0]), "back": a(y1 - com[1]),
            "front": a(com[1] - y0), "com": com, "mass": mass}


def place_foot(parts, foot, box, x_photo, x_cap, side):
    """Foot centre x: the photo position, slid right just enough that the card needs `side` deg to tip
    over the foot's right end (long names lean that way; the left side has far more), never past
    x_cap. The CoM moves linearly with the foot, so the right edge's margin has a closed form."""
    t = tilts(parts, foot, x_photo, box)
    if t["right"] >= side:
        return x_photo
    need = math.tan(math.radians(side)) * t["com"][2]
    have = x_photo + box[0][1] - t["com"][0]
    x = x_photo + (need - have) / (1 - foot[0] / t["mass"])
    return min(math.ceil(x * 10 - 1e-6) / 10, x_cap)


# ---------------------------------------------------------------- 2D layout (plaque frame)
def trunk_left_edge(F, y0, height=3.0):
    """Right-most left edge of the trunk over its bottom `height` mm (tree frame)."""
    lefts = []
    for y in np.linspace(y0 + 0.1, y0 + height, 16):
        row = F.intersection(box(-30, y - 0.02, 30, y + 0.02))
        segs = [q for q in polys(row) if q.bounds[0] <= 0 <= q.bounds[2]]
        if segs:
            lefts.append(segs[0].bounds[0])
    return max(lefts)


def border_centre_path(P, w):
    """Border centre line as a closed path, starting on the bottom run at x = w/2 and running
    clockwise seen from the front: left along the bottom, up the left side, right along the top."""
    i = P["border_in"] + P["border_w"] / 2
    r = P["border_r"] - P["border_w"] / 2
    h = P["plaque_h"]
    ring = orient(rrect(i, -h / 2 + i, w - i, h / 2 - i, r), -1.0).exterior     # clockwise
    pts = np.asarray(ring.coords[:-1])
    start = np.argmin(np.hypot(pts[:, 0] - w / 2, pts[:, 1] - (-h / 2 + i)))
    pts = np.roll(pts, -start, axis=0)
    return LineString(np.vstack([pts, pts[:1]]))


def covered_intervals(path, keep_out, band_half, step=0.05):
    """Arc-length intervals of `path` where any point across the border band is inside keep_out."""
    L = path.length
    s = np.arange(0, L, step)
    pts = np.array([path.interpolate(v).coords[0] for v in s])
    nxt = np.array([path.interpolate(min(v + step, L)).coords[0] for v in s])
    tang = nxt - pts
    tang /= np.maximum(np.linalg.norm(tang, axis=1, keepdims=True), 1e-9)
    nrm = np.c_[-tang[:, 1], tang[:, 0]]
    from shapely import contains_xy
    full = np.ones(len(s), bool)
    anyc = np.zeros(len(s), bool)
    for off in np.linspace(-band_half, band_half, 5):
        q = pts + nrm * off
        c = contains_xy(keep_out, q[:, 0], q[:, 1])
        anyc |= c
        full &= c
    return s, anyc, full


def runs(mask):
    """[start, end) index runs where mask is True."""
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def border_band(P, w):
    """The closed border ring of a plaque `w` wide (before it is cut open behind the tree)."""
    h, bi, bw, br = P["plaque_h"], P["border_in"], P["border_w"], P["border_r"]
    return rrect(bi, -h / 2 + bi, w - bi, h / 2 - bi, br).difference(
        rrect(bi + bw, -h / 2 + bi + bw, w - bi - bw, h / 2 - bi - bw, br - bw))


def card_geometry(P, R, w):
    """Outline, pocket and border of a plaque `w` wide: the plain rounded rectangle, with the
    pocket and border cut of the layout at min_w (they are all on the left)."""
    h = P["plaque_h"]
    outline = rrect(0, -h / 2, w, h / 2, P["corner"])
    border = border_band(P, w).difference(R["border_cut"])
    return outline, R["pocket"].intersection(outline), border


def left_edge_cover(P, F, tx, ty, margin):
    """Length (mm) of the plaque's visible left end (the rounded rectangle's boundary left of the
    corner radius, above the trunk bottom) hidden behind the gold, `margin` mm inside its edge."""
    h, r = P["plaque_h"], P["corner"]
    ring = LineString(rrect(0, -h / 2, 1000, h / 2, r).exterior.coords)
    left = ring.intersection(box(-1, ty + F.bounds[1], r, h / 2 + 1))
    return left.intersection(affinity.translate(F, tx, ty).buffer(-margin)).length, left.length


def hide_left_end(P, F, ty, margin, lo, hi, step=0.05):
    """Tree x that hides the plaque's left end best: the middle of the run of x values that cover
    the most of it (within 0.3 mm of the best), searched from lo to hi."""
    xs = np.round(np.arange(lo, hi + step / 2, step), 3)
    cov = np.array([left_edge_cover(P, F, x, ty, margin)[0] for x in xs])
    good = np.nonzero(cov >= cov.max() - 0.3)[0]
    run = max(runs(np.isin(np.arange(len(xs)), good)), key=lambda r: r[1] - r[0])
    tx = float(round(xs[(run[0] + run[1] - 1) // 2], 2))
    return tx, cov.max(), left_edge_cover(P, F, tx, ty, margin)[1], (float(xs[run[0]]), float(xs[run[1] - 1]))


def layout(P, F, args, tx, ty):
    """All 2D geometry in the plaque frame for the tree at (tx, ty). Returns a dict of shapely
    geometries; the right end of the plaque is taken at min_w (wider plaques only differ on the right)."""
    h, w = P["plaque_h"], P["min_w"]
    y_bot = -h / 2
    res = {"tree_pos": (tx, ty)}
    Fp = affinity.translate(F, tx, ty)
    res["tree"] = Fp
    Pc = rrect(0, y_bot, w, h / 2, P["corner"])                 # the user's plain plaque
    res["outline"] = Pc

    # ---- pocket: glue face on the plaque, offset by the clearance, slivers closed
    c = P["pocket_clear"]
    pk = Fp.intersection(Pc.buffer(c)).buffer(c, quad_segs=8)
    pk = closing(pk, args.sliver)                               # no thin ivory ribs inside the pocket
    ivory = Pc.difference(pk)
    thin_ivory = denoise(ivory.difference(opening(ivory, args.sliver)))  # no thin ivory walls left at the edge
    thin_ivory = unary_union([q for q in polys(thin_ivory) if q.distance(pk) < 0.01])
    pk = unary_union([pk, thin_ivory]).intersection(Pc.buffer(0.5))
    res["pocket"] = clean(pk)

    # ---- border: one open run, ends where the tree covers it
    bg = P["border_gap"]
    keep_out = res["pocket"].buffer(bg + 0.02, quad_segs=QS)
    path = border_centre_path(P, w)
    s, anyc, full = covered_intervals(path, keep_out, P["border_w"] / 2)
    cov = runs(anyc)
    if not cov:
        raise SystemExit("border never meets the tree: check tree_pos")

    def cut_from(run, forward):
        """Arc length where the dropped part starts inside the first contact `run`: where the tree
        covers the full band width (the end then follows the leaf), else at the first touch (square end,
        so a leaf that only grazes the band never leaves a notch)."""
        a_, b_ = run
        fr = runs(full[a_:b_])
        touch = s[a_] if forward else s[min(b_, len(s) - 1)]
        if fr:
            st, en = fr[0] if forward else fr[-1]
            mid = s[a_ + (st + en) // 2]                 # middle of the stretch the tree covers fully
            edge = s[a_ + st] if forward else s[a_ + en - 1]
            if abs(edge - touch) <= 1.0:                 # the tree takes the whole band at once
                return mid
        return touch

    # bottom run, walking left from x = w/2; top run, walking left from the top-right corner
    s_bot = cut_from(cov[0], True)
    s_top = cut_from(cov[-1], False)
    res["border_ends"] = (path.interpolate(s[cov[0][0]]).coords[0],
                          path.interpolate(s[min(cov[-1][1], len(s) - 1)]).coords[0])
    # region to drop: everything between the two ends (through the left side), plus the keep-out
    seg = LineString([path.interpolate(v).coords[0] for v in np.arange(s_bot, s_top, 0.1)] + [path.interpolate(s_top).coords[0]])
    drop = seg.buffer(P["border_w"] / 2 + 0.3, cap_style="flat", quad_segs=8)
    cut = unary_union([keep_out, drop])
    band = border_band(P, w)
    # round off the sharp tips where the cut meets the band at a shallow angle (unprintable slivers)
    kept = opening(band.difference(cut), args.tip)
    res["border_cut"] = clean(unary_union([cut, denoise(band.difference(kept), 0.005, 0.001)]))
    res["border"] = band.difference(res["border_cut"])

    # ---- name keep-out
    res["name_keepout"] = unary_union([Fp, res["pocket"]]).buffer(P["tree_name_gap"] + 0.02, quad_segs=QS)
    return res


# ---------------------------------------------------------------- names (measured from OpenSCAD)
def svg_rings(path):
    txt = open(path, encoding="utf-8").read()
    rings = []
    for d in re.findall(r'd="([^"]*)"', txt):
        for sub in re.split(r"(?=M )", d.strip()):
            pts = re.findall(r"(-?[\d.]+(?:e[+-]?\d+)?),(-?[\d.]+(?:e[+-]?\d+)?)", sub)
            if len(pts) >= 3:
                rings.append([(float(x), -float(y)) for x, y in pts])
    return rings


def rings_to_geom(rings):
    """Even-odd fill of SVG rings -> shapely geometry."""
    g = Polygon()
    for r in sorted(rings, key=lambda r: -abs(Polygon(r).area)):
        g = g.symmetric_difference(Polygon(r).buffer(0))
    return g


def measure_names(P, names, tmpdir, spacing=60.0):
    """Exact glyph geometry of each name as OpenSCAD renders it (halign left, baseline y=0)."""
    size = P["cap_h"] / P["cap_ratio"]
    src = os.path.join(tmpdir, "names.scad")
    with open(src, "w", encoding="utf-8") as f:
        f.write("ns = %s;\n" % json.dumps(names, ensure_ascii=False))
        f.write('for (i = [0 : len(ns) - 1]) translate([0, -%g * i]) text(ns[i], size = %r, font = "%s", '
                'halign = "left", valign = "baseline");\n' % (spacing, size, P["font"]))
    out = os.path.join(tmpdir, "names.svg")
    subprocess.run(["openscad", "-o", out, src], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    groups = {}
    for r in svg_rings(out):
        cy = (min(p[1] for p in r) + max(p[1] for p in r)) / 2
        k = int(round(-cy / spacing + 0.05))
        groups.setdefault(k, []).append([(x, y + spacing * k) for x, y in r])
    return {n: rings_to_geom(groups[i]) for i, n in enumerate(names)}


def font_band(P):
    """Vertical extent of any Latin name on the plaque (accented capitals to descenders)."""
    from fontTools.ttLib import TTFont
    try:
        path = subprocess.run(["fc-match", "-f", "%{file}", P["font"]],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        path = ""
    if not path.lower().endswith((".ttf", ".otf")):
        path = LORA_TTF
    size = P["cap_h"] / P["cap_ratio"]
    f = TTFont(path)
    cmap, glyf = f.getBestCmap(), f["glyf"]
    k = size / 720.0 * 1000.0 / f["head"].unitsPerEm      # OpenSCAD: font units * size / 720 (1000 upem)
    lo, hi = 0, 0
    for c in list(range(0x41, 0x5B)) + list(range(0x61, 0x7B)) + list(range(0xC0, 0x100)) + [0x2D, 0x27]:
        g = cmap.get(c)
        if g and hasattr(glyf[g], "yMin"):
            lo, hi = min(lo, glyf[g].yMin), max(hi, glyf[g].yMax)
    return (-P["cap_h"] / 2 + lo * k, -P["cap_h"] / 2 + hi * k)


def name_left_min(G, keepout, x_lo, x_hi, y_shift):
    """Left-most ink x such that the name, moved there, stays out of keepout (bisection)."""
    x0 = G.bounds[0]

    def hits(x):
        return affinity.translate(G, x - x0, y_shift).intersects(keepout)

    if hits(x_hi):
        raise SystemExit("name cannot clear the tree even at x=%g" % x_hi)
    lo, hi = x_lo, x_hi
    if not hits(lo):
        return lo
    while hi - lo > 0.01:
        mid = (lo + hi) / 2
        if hits(mid):
            lo = mid
        else:
            hi = mid
    return hi


def name_layout(P, n_ink, left_min):
    """Same rule as the SCAD: the name starts right after the tree, the plaque grows to the right
    when the name needs it (name_gap to the border)."""
    x0, x1 = n_ink
    ink_left = left_min
    width = max(P["min_w"], ink_left + (x1 - x0) + P["name_gap"] + P["border_w"] + P["border_in"])
    return ink_left, width


# ---------------------------------------------------------------- output
def scad_poly(g, d=3):
    """[points, paths] literal for polygon(): every ring of every polygon, holes included."""
    pts, paths = [], []
    for q in polys(g):
        q = orient(q, 1.0)
        for ring in [q.exterior] + list(q.interiors):
            c = list(ring.coords)[:-1]
            paths.append(list(range(len(pts), len(pts) + len(c))))
            pts += c
    fmt = "[%%.%df,%%.%df]" % (d, d)
    rows = [",".join(fmt % (x, y) for x, y in pts[i:i + 8]) for i in range(0, len(pts), 8)]
    return ("[[\n  " + ",\n  ".join(rows) + "],\n [" +
            ",".join("[" + ",".join(map(str, p)) + "]" for p in paths) + "]]")


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--footprint", help="tree footprint JSON (default: final tree, else provisional)")
    ap.add_argument("--tree-stl", default=os.path.join(ROOT, TREE_STL_REL), help="tree STL (used if present)")
    ap.add_argument("--scad", default=SCAD)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--hide", type=float,
                    help="plaque left end, mm inside the trunk's left edge (default: placed where the gold "
                         "hides the plaque's left end best)")
    ap.add_argument("--hide-margin", type=float, default=0.3,
                    help="the plaque's left end counts as hidden this far inside the gold silhouette")
    ap.add_argument("--foot-left", type=float, default=0.2,
                    help="assembly: foot's left end, mm right of the trunk's left edge (about flush, as in the photo)")
    ap.add_argument("--sliver", type=float, default=0.4, help="pocket: close gaps / ivory walls thinner than 2x this")
    ap.add_argument("--tip", type=float, default=0.3, help="border ends: tips thinner than 2x this are rounded off")
    ap.add_argument("--slab", type=float, default=3.6, help="tree thickness assumed when no tree STL exists")
    ap.add_argument("--side-tilt", type=float, help="sideways tilt every card must survive, deg (default: SCAD side_tilt)")
    ap.add_argument("--foot-basis", default="solid", choices=sorted(FOOT_PRINTS),
                    help="foot print the foot positions are worked out for (the recommended 100 %% infill; "
                         "\"test\" = the light 15 %% lightning test print)")
    ap.add_argument("--foot-recommended", default="solid", choices=sorted(FOOT_PRINTS),
                    help="recommended foot print, checked against --back-tilt")
    ap.add_argument("--foot-check", default="test,solid", help="--check: foot prints to report (comma list)")
    ap.add_argument("--back-tilt", type=float, default=15.0,
                    help="--check: backward tilt every card must survive with the recommended foot, deg")
    ap.add_argument("--check", action="store_true", help="verify every name with OpenSCAD 2D exports")
    ap.add_argument("--plot", help="write a debug PNG of the layout here")
    ap.add_argument("--json", help="write a summary JSON here")
    return ap


def main():
    args = build_parser().parse_args()

    P = parse_scad(args.scad)
    need = ["plaque_h", "plaque_t", "min_w", "corner", "border_in", "border_w", "border_r", "name_gap", "cap_h",
            "cap_ratio", "font", "lean", "slot_y", "foot_h", "slot_fit", "slot_depth", "foot_len",
            "pocket_depth", "pocket_clear", "border_gap", "trunk_gap", "tree_name_gap", "names"]
    miss = [k for k in need if k not in P]
    if miss:
        raise SystemExit("missing SCAD parameters: %s" % miss)

    fp = args.footprint or (FINAL_FOOTPRINT if os.path.exists(FINAL_FOOTPRINT) else PROVISIONAL_FOOTPRINT)
    F, meta = load_footprint(fp)
    tree_mesh = None
    if args.tree_stl and os.path.exists(args.tree_stl):
        import trimesh
        tree_mesh = trimesh.load(args.tree_stl, force="mesh")
        F = clean(unary_union([F, tree_stl_outline(tree_mesh, P["pocket_depth"])]))
    provisional = bool(meta.get("provisional", False))
    print("footprint: %s%s, %d polygons, area %.1f mm2, bbox %s" % (
        fp, " (PROVISIONAL)" if provisional else "", len(polys(F)), F.area, tuple(round(v, 2) for v in F.bounds)))
    print("tree STL: %s" % (args.tree_stl if tree_mesh is not None else "not found, slab %.1f mm thick" % args.slab))

    with tempfile.TemporaryDirectory() as tmp:
        # ---- 3D: trunk bottom vs foot
        import trimesh
        foot = trimesh.load(export_foot_stl(args.scad, tmp), force="mesh")
        foot_m = to_manifold(foot.vertices, foot.faces)
        y0 = F.bounds[1]
        if tree_mesh is not None:
            tm = to_manifold(tree_mesh.vertices, tree_mesh.faces)
            import manifold3d as m3
            tm = tm ^ m3.Manifold.cube([400, 12, 40]).translate([-200, y0 - 1, -5])   # trunk zone only
        else:
            tm = slab_manifold(F.intersection(box(-100, y0 - 1, 100, y0 + 10)), args.slab)
        tle = trunk_left_edge(F, y0)
        tb = F.intersection(box(-30, y0, 30, y0 + 1)).bounds

        def place_y(tx):
            # foot: photo position (left end flush with the trunk's left edge); it may slide right
            # for balance until its left end reaches the trunk's centre line (trunk still over the foot)
            fx = tx + tle + args.foot_left + P["foot_len"] / 2
            cap = tx + (tb[0] + tb[2]) / 2 + P["foot_len"] / 2
            return (fx, cap) + solve_tree_y(P, tm, foot_m, tx, [fx, cap], P["trunk_gap"])

        if args.hide is not None:
            # x: the plaque's left end (x = 0) sits `hide` mm inside the trunk's left edge
            tx = round(-(tle + args.hide), 2)
        else:
            # x: hide the plaque's left end behind the trunk. The height comes first (it barely
            # depends on x), then the x that covers the most of the left end, then the height again.
            _, _, ty, _ = place_y(round(-(tle + 1.0), 2))
            tx, cov, length, plateau = hide_left_end(P, F, ty, args.hide_margin, -tle - 3.0, -tle + 1.0)
            print("plaque left end hidden behind the tree: %.1f of %.1f mm at x %.2f (plateau %.2f to %.2f, "
                  "%.2f mm inside the trunk's left edge)" % (cov, length, tx, plateau[0], plateau[1], -tle - tx))
        foot_x, foot_x_cap, ty, gap = place_y(tx)
        print("tree_pos = [%.2f, %.2f]  (trunk-to-foot gap %.3f mm, target %.2f, foot anywhere from the photo "
              "position to +%.1f mm)" % (tx, ty, gap, P["trunk_gap"], foot_x_cap - foot_x))

        R = layout(P, F, args, tx, ty)

        # print model for the tipping checks: tree and foot masses, the foot's bed contact
        side = args.side_tilt if args.side_tilt is not None else float(P.get("side_tilt", 10.0))
        prints = measure_foot_prints(foot, tmp)
        stab = {"tree": tree_mass(tree_mesh, F, args.slab), "box": contact_box(foot), "side": side,
                "feet": {k: foot_mass(foot, k, prints) for k in FOOT_PRINTS}, "basis": args.foot_basis,
                "recommended": args.foot_recommended, "back": args.back_tilt,
                "foot_x": foot_x, "foot_x_cap": foot_x_cap}

        # ---- names
        names = []
        for n in list(P["names"]) + list(P.get("test_names", [])) + [P.get("assembly_name", "Sophie")]:
            if isinstance(n, str) and n not in names:
                names.append(n)
        G = measure_names(P, names, tmp)
        y_shift = -P["cap_h"] / 2
        table = []
        for n in names:
            g = G[n]
            x0, _, x1, _ = g.bounds
            lm = name_left_min(g, R["name_keepout"], 0.0, P["min_w"], y_shift)
            ink_left, width = name_layout(P, (x0, x1), lm)
            # foot position for this card (the plaque, gold and tree as printed)
            outline, pocket, border = card_geometry(P, R, width)
            gold = unary_union([border, affinity.translate(g, ink_left - x0, y_shift)])
            parts = card_parts(P, tx, ty, outline, pocket, gold, stab["tree"])
            fx = place_foot(parts, stab["feet"][args.foot_basis], stab["box"], foot_x, foot_x_cap, side)
            table.append((n, x0, x1, lm, ink_left, width, fx))
        # fallback for names that are not in the table: clear of the keep-out over the whole font band
        band = font_band(P)
        row = R["name_keepout"].intersection(box(-200, band[0], 1000, band[1]))
        clear_x = row.bounds[2] if not row.is_empty else 0.0

        # ---- write tree_data.scad (tree frame)
        def tf(g):
            return clean(affinity.translate(g, -tx, -ty))

        tree_stl_found = tree_mesh is not None
        bb = R["outline"].bounds
        widest = max(table, key=lambda r: r[5])
        lines = []
        lines.append("// GENERATED by tools/card_layout.py - do not edit by hand, re-run the script instead.")
        lines.append("// Footprint: %s%s" % (os.path.relpath(fp, ROOT) if fp.startswith(ROOT) else fp,
                                             "  (PROVISIONAL tree)" if provisional else ""))
        lines.append("// Polygons are in the tree frame (origin = bottom centre of the trunk, +y up); the SCAD")
        lines.append("// moves them by tree_pos. Format: [points, paths] for polygon().")
        lines.append("")
        lines.append("tree_provisional = %s;" % ("true" if provisional else "false"))
        lines.append("tree_pos_auto = [%.2f, %.2f];   // plaque frame: x from the plaque's left end, y from its centre line" % (tx, ty))
        stl = os.path.abspath(args.tree_stl)
        stl_ref = os.path.relpath(stl, ROOT) if stl.startswith(ROOT + os.sep) else stl
        lines.append("tree_stl = \"%s\";" % stl_ref)
        lines.append("tree_stl_found = %s;   // false: previews show the flat outline instead" % ("true" if tree_stl_found else "false"))
        lines.append("assembly_foot_x = %.2f;   // foot centre (plaque frame) as in the photo: left end flush with the trunk"
                     % foot_x)
        lines.append("trunk_gap_measured = %.3f;  // 3D gap trunk -> foot at tree_pos_auto" % gap)
        lines.append("outline_bbox = [%.2f, %.2f, %.2f, %.2f];  // plaque outline at min_w (plaque frame)" % bb)
        lines.append("")
        lines.append("// Border run ends (plaque frame), for reference")
        e0, e1 = R["border_ends"]
        lines.append("border_end_bottom = [%.2f, %.2f];" % e0)
        lines.append("border_end_top = [%.2f, %.2f];" % e1)
        lines.append("")
        lines.append("// Names: [name, ink x0, ink x1 (text origin, halign left), left-most ink x clear of the tree (tree frame),")
        lines.append("//         foot centre x (tree frame): the photo position, or slid right so the card stands at least")
        lines.append("//         %g deg sideways on the foot printed %s]" % (side, FOOT_PRINTS[args.foot_basis][0]))
        lines.append("name_table = [")
        for n, x0, x1, lm, il, wd, fx in table:
            lines.append("  [%s, %.3f, %.3f, %.3f, %.2f],   // plaque %.1f mm%s" % (
                json.dumps(n, ensure_ascii=False), x0, x1, lm - tx, fx - tx, wd,
                ", foot +%.1f mm" % (fx - foot_x) if fx > foot_x + 0.01 else ""))
        lines.append("];")
        lines.append("name_clear_x = %.3f;   // fallback for names not in the table: left-most ink x (tree frame)" % (clear_x - tx))
        lines.append("plaque_w_max = %.2f;   // widest plaque in the table (%s)" % (widest[5], widest[0]))
        lines.append("")
        lines.append("tree_outline = %s;" % scad_poly(tf(R["tree"])))
        lines.append("pocket = %s;" % scad_poly(tf(R["pocket"])))
        lines.append("border_cut = %s;" % scad_poly(tf(R["border_cut"])))
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("wrote %s" % args.out)
        print("widest plaque: %s %.1f mm" % (widest[0], widest[5]))

        summary = {
            "footprint": fp, "provisional": provisional, "tree_stl_found": tree_stl_found,
            "tree_pos": [tx, ty], "trunk_gap": gap, "assembly_foot_x": foot_x,
            "outline_bbox": bb, "border_ends": R["border_ends"],
            "names": {n: {"ink": [x0, x1], "ink_left": il, "width": wd, "foot_x": fx, "foot_shift": round(fx - foot_x, 2)}
                      for n, x0, x1, lm, il, wd, fx in table},
            "widest": [widest[0], widest[5]],
            "foot_x_cap": foot_x_cap, "side_tilt": side,
        }
        if args.plot:
            plot(args.plot, P, R, table, G)
        if args.check:
            ok, rows = check(P, R, table, tmp, args, stab)
            summary["check_ok"] = ok
            summary["stability"] = rows
        if args.json:
            json.dump(summary, open(args.json, "w"), indent=1)


# ---------------------------------------------------------------- debug plot
def plot(path, P, R, table, G):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def draw(ax, g, **kw):
        for q in polys(g):
            x, y = q.exterior.xy
            ax.fill(x, y, **kw)
            for hh in q.interiors:
                x, y = hh.xy
                ax.fill(x, y, color="white")

    n = [r for r in table if r[0] == "Sophie"] or table[:1]
    n = n[0]
    fig, ax = plt.subplots(figsize=(16, 8))
    draw(ax, R["outline"], color="#f4f0e6", ec="k", lw=0.6)
    draw(ax, R["pocket"], color="#d9d2bf")
    draw(ax, R["name_keepout"], color="none", ec="r", lw=0.4, ls="--")
    draw(ax, R["border"], color="#b8901f")
    draw(ax, R["tree"], color="#d4af37", alpha=0.75)
    g = affinity.translate(G[n[0]], n[4] - G[n[0]].bounds[0], -P["cap_h"] / 2)
    draw(ax, g, color="#b8901f")
    ax.set_aspect("equal")
    ax.set_xlim(-35, 100)
    ax.set_ylim(-20, 40)
    ax.grid(True, alpha=0.3)
    plt.savefig(path, dpi=90, bbox_inches="tight")


# ---------------------------------------------------------------- verification (OpenSCAD 2D exports)
def check(P, R, table, tmp, args, stab):
    """Export outline, pocket, border, name and foot tick of every name in 2D from the SCAD and test them:
    layout clearances, then stability with the print model on the SCAD's own geometry."""
    names = [r[0] for r in table]
    spacing = 200.0
    geo = {}
    layers = ["outline", "pocket", "border", "name"] + (["tick"] if P.get("tick_depth", 0) > 0 else [])
    for layer in layers:
        out = os.path.join(tmp, "chk_%s.svg" % layer)
        cmd = ["openscad", "-o", out, "-D", 'make="check2d"', "-D", 'check_layer="%s"' % layer,
               "-D", "check_names=%s" % json.dumps(names, ensure_ascii=False), "-D", "check_spacing=%g" % spacing, args.scad]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        groups = {}
        for r in svg_rings(out):
            cy = (min(p[1] for p in r) + max(p[1] for p in r)) / 2
            k = int(round(-cy / spacing))
            groups.setdefault(k, []).append([(x, y + spacing * k) for x, y in r])
        geo[layer] = {n: rings_to_geom(groups.get(i, [])) for i, n in enumerate(names)}
    ok = True
    worst = {"name_gap": 1e9, "border_gap": 1e9, "right_gap": 1e9}
    tx, ty = R["tree_pos"]
    tree = R["tree"]
    feet = [k.strip() for k in args.foot_check.split(",") if k.strip()]
    for k in (stab["basis"], stab["recommended"]):
        if k not in feet:
            feet.append(k)
    bad_by = {n: [] for n in names}
    print("\nwidth = plaque width; tree/pocket = name clearance; right = name to border; border = border to pocket")
    print("\n%-14s %7s %7s %7s %7s %6s" % ("name", "width", "tree", "pocket", "right", "border"))
    for n in names:
        o, pk, bd, nm = (geo[k][n] for k in ("outline", "pocket", "border", "name"))
        width = o.bounds[2]
        d_tree = nm.distance(tree)
        d_pocket = nm.distance(pk)
        # free space between the name and the border's inner edge on the right
        right_inner = width - P["border_in"] - P["border_w"]
        d_right = right_inner - nm.bounds[2]
        d_border = bd.distance(pk)
        pieces = len(polys(bd))
        thin = bd.difference(opening(bd, 0.3)).area
        bad = bad_by[n]
        if d_pocket < P["tree_name_gap"] - 0.02 or d_tree < P["tree_name_gap"] - 0.02:
            bad.append("name too close to tree")
        if d_right < P["name_gap"] - 0.05:
            bad.append("name too close to border")
        if bd.intersects(pk) or d_border < P["border_gap"] - 0.02:
            bad.append("border touches pocket")
        if pieces != 1:
            bad.append("border in %d pieces" % pieces)
        if thin > 0.05:
            bad.append("border slivers %.2f mm2" % thin)
        if not pk.within(o.buffer(0.01)):
            bad.append("pocket outside plaque")
        if len(polys(o)) != 1 or any(Polygon(hh).area < 1.0 for q in polys(o) for hh in q.interiors):
            bad.append("outline in %d pieces / pinholes" % len(polys(o)))
        ivory_top = o.buffer(-0.01).difference(pk)
        if ivory_top.difference(opening(ivory_top, 0.2)).area > 0.5:
            bad.append("ivory slivers %.2f mm2" % ivory_top.difference(opening(ivory_top, 0.2)).area)
        worst["name_gap"] = min(worst["name_gap"], d_tree, d_pocket)
        worst["border_gap"] = min(worst["border_gap"], d_border)
        worst["right_gap"] = min(worst["right_gap"], d_right)
        print("%-14s %7.2f %7.2f %7.2f %7.2f %6.2f %s" % (
            n, width, d_tree, d_pocket, d_right, d_border, "OK" if not bad else "FAIL: " + ", ".join(bad)))

    # ---- stability: the SCAD's own outline / pocket / gold, the print model, the per-name foot
    fl = P["foot_len"]
    x_photo = stab["foot_x"]
    print("\nTilt (deg) that tips each card over the foot's left / right / back edge, foot at its name_table "
          "position\n(shift = mm right of the photo position, max %.1f; tick = x of the foot's right end on the "
          "plaque's back).\nOK needs left and right >= %g with the %s foot and back >= %g with the %s foot." % (
              stab["foot_x_cap"] - x_photo, stab["side"], stab["basis"], stab["back"], stab["recommended"]))
    for k in feet:
        m, c = stab["feet"][k]
        print("  %-9s %s: foot %.2f g, CoM height %.2f mm" % (k, FOOT_PRINTS[k][0], m, c[2]))
    hdr = "%-14s %7s %5s %6s" % ("name", "width", "shift", "tick")
    for k in feet:
        hdr += " | %-17s" % ("%s L / R / B" % k)
    print("\n" + hdr)
    rows = {}
    lows = {k: {"side": 1e9, "back": 1e9} for k in feet}
    for n, x0, x1, lm, il, wd, fx in table:
        o, pk, bd, nm = (geo[k][n] for k in ("outline", "pocket", "border", "name"))
        parts = card_parts(P, tx, ty, o, pk, unary_union([bd, nm]), stab["tree"])
        line = "%-14s %7.2f %5.1f %6.2f" % (n, o.bounds[2], fx - x_photo, fx + fl / 2)
        rows[n] = {"width": round(o.bounds[2], 2), "foot_x": round(fx, 2), "foot_shift": round(fx - x_photo, 2),
                   "tick_x": round(fx + fl / 2, 2), "tilt_deg": {}}
        for k in feet:
            t = tilts(parts, stab["feet"][k], fx, stab["box"])
            line += " | %5.1f %5.1f %5.1f" % (t["left"], t["right"], t["back"])
            rows[n]["tilt_deg"][k] = {s: round(t[s], 1) for s in ("left", "right", "back", "front")}
            rows[n]["tilt_deg"][k]["mass_g"] = round(t["mass"], 2)
            lows[k]["side"] = min(lows[k]["side"], t["left"], t["right"])
            lows[k]["back"] = min(lows[k]["back"], t["back"])
            if k == stab["basis"] and min(t["left"], t["right"]) < stab["side"] - 0.05:
                bad_by[n].append("tips sideways at %.1f deg (%s foot)" % (min(t["left"], t["right"]), k))
            if k == stab["recommended"] and t["back"] < stab["back"] - 0.05:
                bad_by[n].append("tips backwards at %.1f deg (%s foot)" % (t["back"], k))
        if "tick" in geo:
            tk = geo["tick"][n]
            if tk.is_empty or abs((tk.bounds[0] + tk.bounds[2]) / 2 - (fx + fl / 2)) > 0.05:
                bad_by[n].append("tick not at the foot's right end")
            elif not (P["corner"] <= tk.bounds[0] and tk.bounds[2] <= o.bounds[2] - P["corner"]):
                bad_by[n].append("tick off the straight bottom edge")
        rows[n]["ok"] = not bad_by[n]
        print(line + "  " + ("OK" if not bad_by[n] else "FAIL: " + ", ".join(bad_by[n][-3:])))
        ok &= not bad_by[n]
    print("worst: name-tree %.2f, name-right border %.2f, border-pocket %.2f; " % (
        worst["name_gap"], worst["right_gap"], worst["border_gap"]) +
        "; ".join("%s side %.1f / back %.1f deg" % (k, lows[k]["side"], lows[k]["back"]) for k in feet) +
        " -> %s" % ("ALL OK" if ok else "FAILURES"))
    return ok, rows


if __name__ == "__main__":
    main()
