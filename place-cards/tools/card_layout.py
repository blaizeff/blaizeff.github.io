#!/usr/bin/env python3
"""Fit the place card plaque to the glued gold tree and write tree_data.scad.

The SCAD (wedding_place_cards.scad) includes the generated tree_data.scad. This script
computes everything the SCAD cannot work out by itself:
  * tree_pos_auto: where the tree origin (trunk base centre) sits in the plaque frame.
      x: the plaque's left end hides `--hide` mm inside the trunk.
      y: real 3D check with manifold3d against the foot exported from the SCAD, so the
         trunk bottom clears the foot by `trunk_gap` (read from the SCAD).
  * the tree outline (tree frame), the pocket (glue face offset by pocket_clear,
    slivers closed), the backing added to the plaque behind the trunk and inner canopy,
    the small white bits cut back so none peek out between leaves, the region where the
    gold border is cut so it ends behind a solid part of the tree,
  * the name table: ink extents of every guest name (measured from OpenSCAD's own text
    rendering) and the left-most position that keeps each name name_gap clear of the tree.

Frames: plaque frame = x from the plaque's left end (rounded rectangle, before the
backing), y = 0 on the plaque's centre line, z = 0 on the bed. Tree frame (canonical) =
origin at the bottom centre of the trunk, +y up, z = 0 on the glue face. All polygons in
tree_data.scad are in the tree frame; the SCAD moves them by tree_pos.

Run from anywhere (Python 3.11, shapely, trimesh, manifold3d, OpenSCAD on PATH):
  python3 place-cards/tools/card_layout.py              # final tree if present, else provisional
  python3 place-cards/tools/card_layout.py --footprint some/tree_footprint.json
  python3 place-cards/tools/card_layout.py --check      # also verify every name (OpenSCAD 2D export)
Re-run it whenever the tree, the names list or a plaque/pocket/border parameter changes.
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


def tree_to_foot(P, tx, ty, contact=0.0):
    """4x4: tree frame -> foot frame, for the plaque seated in the slot.
    contact shifts the plaque inside the slot clearance (-fit/2 = against the front wall)."""
    t, h, sd = P["plaque_t"], P["plaque_h"], P["slot_depth"]
    A = np.eye(4)
    A[:3, 3] = [tx, ty, t - P["pocket_depth"]]                       # tree -> plaque
    c = t / 2 + contact
    B = np.array([[1, 0, 0, 0], [0, 0, -1, c], [0, 1, 0, h / 2 - sd], [0, 0, 0, 1]], float)  # plaque -> slot
    a = math.radians(-P["lean"])
    C = np.array([[1, 0, 0, 0], [0, math.cos(a), -math.sin(a), P["slot_y"]],
                  [0, math.sin(a), math.cos(a), P["foot_h"]], [0, 0, 0, 1]])                  # slot -> foot
    return C @ B @ A


def solve_tree_y(P, tree_m, foot_m, tx, foot_x, target, lo=-17.0, hi=-5.0):
    """Lowest tree_pos.y whose minimum 3D gap to the foot is `target` (bisection, 0.005 mm)."""
    foot_m = foot_m.translate([foot_x, 0, 0])

    def gap(ty):
        g = []
        for contact in (-P["slot_fit"] / 2, 0.0, P["slot_fit"] / 2):
            t = tree_m.transform(tree_to_foot(P, tx, ty, contact)[:3, :])
            if (t ^ foot_m).volume() > 1e-6:
                return -1.0
            g.append(t.min_gap(foot_m, 5.0))
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


def layout(P, F, args, tx, ty):
    """All 2D geometry in the plaque frame for the tree at (tx, ty). Returns a dict of shapely
    geometries; the right end of the plaque is taken at min_w (wider plaques only differ on the right)."""
    h, w = P["plaque_h"], P["min_w"]
    y_bot = -h / 2
    res = {"tree_pos": (tx, ty)}
    y0 = F.bounds[1]
    Fp = affinity.translate(F, tx, ty)
    res["tree"] = Fp
    trunk_bottom = ty + y0
    R = rrect(0, y_bot, w, h / 2, P["corner"])

    # ---- backing: smooth superellipse behind trunk and inner canopy, blended into the plaque
    cx, cy, a, b, n = args.backing
    E = superellipse(tx + cx, ty + cy, a, b, n)
    # keep the backing's lower-left edge behind the trunk: nothing left of the plaque end
    # below the point where the backing leaves the trunk
    E = E.difference(box(-100, y_bot - 1, 0, trunk_bottom + args.backing_low))
    P0 = closing(unary_union([R, E]), args.blend)
    P0 = P0.intersection(box(-200, y_bot, 1000, 200))           # straight bottom edge
    P0 = polys(P0)[0] if len(polys(P0)) == 1 else max(polys(P0), key=lambda q: q.area)

    # ---- cut back white bits that would peek out between leaves near the plaque edge
    Pc = P0
    for _ in range(3):
        V = Pc.difference(Fp)                                    # visible ivory
        main = max(polys(V), key=lambda q: q.area)
        thin = V.difference(opening(V, args.thin))               # white necks narrower than 2*thin
        small = unary_union([q for q in polys(V) if q is not main and q.area < args.small])
        cand = unary_union([thin, small])
        edge = Pc.exterior.buffer(0.05)
        # only on the backing: the rectangle keeps its straight edges, leaves may hang over them
        cand = denoise(cand.difference(R), 0.02, 0.05)
        bits = [q for q in polys(cand) if q.intersects(edge) and q.area > 0.02]
        # small white islands on the backing read as odd specks even when the gold encloses them
        # (tiny ones stay: cutting them would only punch pinholes through the plaque)
        bits += [q for q in polys(V) if q is not main and 2.0 <= q.area < args.small
                 and q.difference(R).area > 0.5 * q.area]
        if not bits:
            break
        bits = unary_union(bits)
        # pull the cut edge `tuck` mm under the gold so it stays hidden
        cut = bits.buffer(0.02).union(bits.buffer(args.tuck, quad_segs=QS).intersection(Fp))
        Pc = Pc.difference(cut)
        Pc = opening(closing(Pc, 0.3), 0.3)
        Pc = Pc.intersection(box(-200, y_bot, 1000, 200))
        Pc = max(polys(Pc), key=lambda q: q.area)
    # keep the bottom edge straight and full where it enters the slot (cut never touches it)
    Pc = unary_union([Pc, R.intersection(box(-1, y_bot, 1000, trunk_bottom - 0.5))])
    Pc = max(polys(Pc.buffer(0)), key=lambda q: q.area)
    res["outline"] = Pc
    # The SCAD rebuilds the outline as (rounded rect - plaque_cut) + backing. Overlap the pieces so
    # rounding and simplification never leave hairline gaps or slivers at their seams: the backing
    # reaches 0.1 into the rectangle, the cut grows 0.03 (its edges are tucked under the gold anyway).
    res["backing"] = clean(denoise(Pc.difference(R.buffer(-0.1, quad_segs=QS))))
    res["plaque_cut"] = clean(denoise(R.difference(Pc)).buffer(0.03, quad_segs=4).intersection(R.buffer(0.1)))

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
        raise SystemExit("border never meets the tree: check tree_pos / backing")

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
    band = rrect(P["border_in"], y_bot + P["border_in"], w - P["border_in"], h / 2 - P["border_in"], P["border_r"]).difference(
        rrect(P["border_in"] + P["border_w"], y_bot + P["border_in"] + P["border_w"], w - P["border_in"] - P["border_w"],
              h / 2 - P["border_in"] - P["border_w"], P["border_r"] - P["border_w"]))
    # round off the sharp tips where the cut meets the band at a shallow angle (unprintable slivers)
    kept = opening(band.difference(cut), args.tip)
    res["border_cut"] = clean(unary_union([cut, denoise(band.difference(kept), 0.005, 0.001)]))
    res["border"] = band.difference(res["border_cut"])

    # ---- name keep-out
    res["name_keepout"] = unary_union([Fp, res["pocket"]]).buffer(P["name_gap"] + 0.02, quad_segs=QS)
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
    """Same rule as the SCAD: centre in the free area when it fits, else grow to the right."""
    x0, x1 = n_ink
    wtxt = x1 - x0
    right = P["min_w"] - P["border_in"] - P["border_w"] - P["name_gap"]
    free = right - left_min
    ink_left = left_min + (free - wtxt) / 2 if wtxt <= free else left_min
    width = max(P["min_w"], ink_left + wtxt + P["name_gap"] + P["border_w"] + P["border_in"])
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
    ap.add_argument("--hide", type=float, default=1.2, help="plaque left end, mm inside the trunk's left edge")
    ap.add_argument("--foot-left", type=float, default=0.2,
                    help="assembly: foot's left end, mm right of the trunk's left edge (about flush, as in the photo)")
    ap.add_argument("--backing", type=float, nargs=5, default=[-1.0, 24.0, 16.5, 10.2, 2.6],
                    metavar=("CX", "CY", "A", "B", "N"),
                    help="backing superellipse in the tree frame: centre, half-width, half-height, exponent")
    ap.add_argument("--backing-low", type=float, default=4.0,
                    help="backing may leave the trunk leftwards only this far above the trunk bottom")
    ap.add_argument("--blend", type=float, default=1.0, help="fillet radius where backing meets the plaque")
    ap.add_argument("--thin", type=float, default=0.9, help="white necks narrower than 2x this are cut back")
    ap.add_argument("--small", type=float, default=10.0, help="white islands on the backing smaller than this (mm2) are cut")
    ap.add_argument("--tuck", type=float, default=0.8, help="cut edges sit this far under the gold")
    ap.add_argument("--sliver", type=float, default=0.4, help="pocket: close gaps / ivory walls thinner than 2x this")
    ap.add_argument("--tip", type=float, default=0.3, help="border ends: tips thinner than 2x this are rounded off")
    ap.add_argument("--slab", type=float, default=3.6, help="tree thickness assumed when no tree STL exists")
    ap.add_argument("--check", action="store_true", help="verify every name with OpenSCAD 2D exports")
    ap.add_argument("--plot", help="write a debug PNG of the layout here")
    ap.add_argument("--json", help="write a summary JSON here")
    return ap


def main():
    args = build_parser().parse_args()

    P = parse_scad(args.scad)
    need = ["plaque_h", "plaque_t", "min_w", "corner", "border_in", "border_w", "border_r", "name_gap", "cap_h",
            "cap_ratio", "font", "lean", "slot_y", "foot_h", "slot_fit", "slot_depth", "foot_len",
            "pocket_depth", "pocket_clear", "border_gap", "trunk_gap", "names"]
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
        # x: the plaque's left end (x = 0) sits `hide` mm inside the trunk's left edge
        tx = round(-(trunk_left_edge(F, y0) + args.hide), 2)
        foot_x = tx + trunk_left_edge(F, y0) + args.foot_left + P["foot_len"] / 2
        ty, gap = solve_tree_y(P, tm, foot_m, tx, foot_x, P["trunk_gap"])
        print("tree_pos = [%.2f, %.2f]  (trunk-to-foot gap %.3f mm, target %.2f)" % (tx, ty, gap, P["trunk_gap"]))

        R = layout(P, F, args, tx, ty)

        # rough masses for the sideways tipping estimate in --check (PLA 1.24 g/cm3, the user's
        # 2 walls / 3 bottom / 5 top layers / 15 % infill): foot shell + infill, tree nearly solid
        rho = 1.24e-3
        shell = min(foot.volume, foot.area * 0.8)
        foot_g = rho * (shell + 0.15 * (foot.volume - shell))
        if tree_mesh is not None:
            tree_g, tree_cx = rho * 0.95 * tree_mesh.volume, tx + tree_mesh.center_mass[0]
        else:
            tree_g, tree_cx = rho * 0.95 * 2.7 * F.area, tx + F.centroid.x
        masses = {"foot": (foot_g, foot_x), "tree": (tree_g, tree_cx),
                  "foot_x0": foot_x - P["foot_len"] / 2 + 0.5, "foot_x1": foot_x + P["foot_len"] / 2 - 0.5,
                  "trunk_x1": tx + F.intersection(box(-30, y0, 30, y0 + 1)).bounds[2]}

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
            table.append((n, x0, x1, lm, ink_left, width))
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
        lines.append("assembly_foot_x = %.2f;   // foot centre (plaque frame): its left end sits under the trunk" % foot_x)
        lines.append("trunk_gap_measured = %.3f;  // 3D gap trunk -> foot at tree_pos_auto" % gap)
        lines.append("outline_bbox = [%.2f, %.2f, %.2f, %.2f];  // plaque outline at min_w (plaque frame)" % bb)
        lines.append("")
        lines.append("// Border run ends (plaque frame), for reference")
        e0, e1 = R["border_ends"]
        lines.append("border_end_bottom = [%.2f, %.2f];" % e0)
        lines.append("border_end_top = [%.2f, %.2f];" % e1)
        lines.append("")
        lines.append("// Names: [name, ink x0, ink x1 (text origin, halign left), left-most ink x clear of the tree (tree frame)]")
        lines.append("name_table = [")
        for n, x0, x1, lm, il, wd in table:
            lines.append("  [%s, %.3f, %.3f, %.3f],   // plaque %.1f mm" % (json.dumps(n, ensure_ascii=False), x0, x1, lm - tx, wd))
        lines.append("];")
        lines.append("name_clear_x = %.3f;   // fallback for names not in the table: left-most ink x (tree frame)" % (clear_x - tx))
        lines.append("plaque_w_max = %.2f;   // widest plaque in the table (%s)" % (widest[5], widest[0]))
        lines.append("")
        lines.append("tree_outline = %s;" % scad_poly(tf(R["tree"])))
        lines.append("pocket = %s;" % scad_poly(tf(R["pocket"])))
        lines.append("backing = %s;" % scad_poly(tf(R["backing"])))
        lines.append("plaque_cut = %s;" % scad_poly(tf(R["plaque_cut"])) if not R["plaque_cut"].is_empty
                     else "plaque_cut = [[[0,0],[0,0.001],[0.001,0]],[[0,1,2]]];")
        lines.append("border_cut = %s;" % scad_poly(tf(R["border_cut"])))
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("wrote %s" % args.out)
        print("widest plaque: %s %.1f mm" % (widest[0], widest[5]))

        summary = {
            "footprint": fp, "provisional": provisional, "tree_stl_found": tree_stl_found,
            "tree_pos": [tx, ty], "trunk_gap": gap, "assembly_foot_x": foot_x,
            "outline_bbox": bb, "border_ends": R["border_ends"],
            "names": {n: {"ink": [x0, x1], "ink_left": il, "width": wd} for n, x0, x1, lm, il, wd in table},
            "widest": [widest[0], widest[5]],
        }
        if args.plot:
            plot(args.plot, P, R, table, G)
        if args.check:
            ok = check(P, R, table, tmp, args, masses)
            summary["check_ok"] = ok
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
def check(P, R, table, tmp, args, masses):
    """Export outline, pocket, border and name of every name in 2D from the SCAD and test them."""
    names = [r[0] for r in table]
    spacing = 200.0
    geo = {}
    for layer in ("outline", "pocket", "border", "name"):
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
    print("\nwidth = plaque width; tree/pocket = name clearance; right = name to border; border = border to pocket;")
    print("tip = centre of mass to the foot's right end (foot left end under the trunk); shift = slide the foot")
    print("right by this much for a 5 mm margin (the trunk stays over the foot up to %.1f mm)" % (
        masses["trunk_x1"] - 2 - masses["foot_x0"]))
    print("\n%-14s %7s %7s %7s %7s %6s %6s %6s" % ("name", "width", "tree", "pocket", "right", "border", "tip", "shift"))
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
        bad = []
        if d_pocket < P["name_gap"] - 0.02 or d_tree < P["name_gap"] - 0.02:
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
        # sideways tipping: plaque ~72 % solid, gold solid, about its 2D centroid
        rho = 1.24e-3
        parts = [masses["foot"], masses["tree"], (rho * 0.72 * P["plaque_t"] * o.area, o.centroid.x),
                 (rho * P["gold_h"] * (bd.area + nm.area), unary_union([bd, nm]).centroid.x)]
        cx = sum(m * x for m, x in parts) / sum(m for m, x in parts)
        tip = masses["foot_x1"] - cx
        shift = max(0.0, 5.0 - tip) * sum(m for m, x in parts) / (sum(m for m, x in parts) - masses["foot"][0])
        worst["tip"] = min(worst.get("tip", 1e9), tip)
        worst["name_gap"] = min(worst["name_gap"], d_tree, d_pocket)
        worst["border_gap"] = min(worst["border_gap"], d_border)
        worst["right_gap"] = min(worst["right_gap"], d_right)
        print("%-14s %7.2f %7.2f %7.2f %7.2f %6.2f %6.1f %6.1f %s" % (
            n, width, d_tree, d_pocket, d_right, d_border, tip, shift, "OK" if not bad else "FAIL: " + ", ".join(bad)))
        ok &= not bad
    print("worst: name-tree %.2f, name-right border %.2f, border-pocket %.2f, tip margin %.1f -> %s" % (
        worst["name_gap"], worst["right_gap"], worst["border_gap"], worst["tip"], "ALL OK" if ok else "FAILURES"))
    return ok


if __name__ == "__main__":
    main()
