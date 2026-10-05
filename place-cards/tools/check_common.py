#!/usr/bin/env python3
"""Shared helpers for the place card checks, the 3MF packager and the renders.

Not a command by itself. Imported by check_mesh.py, check_slice.py, check_assembly.py,
make_3mf.py and render_scene.py (all in this folder).

What lives here:
  * parse_scad / card_params: the SCAD's top-level parameters (plus the generated
    tree_data.scad), so every check uses the real numbers instead of copies.
  * plaque_to_world / tree_to_plaque / foot_to_world: the assembled pose, exactly as
    wedding_place_cards.scad's assembly() module builds it.
  * PrusaSlicer helpers: a profile that mirrors the user's OrcaSlicer settings
    (0.4 nozzle, 0.42 lines, arachne, 2 walls, 5 top / 3 bottom, 15 % infill, the
    per-part tweaks from the user's 3MF), a slice() wrapper and a G-code summary parser.
"""
import math
import os
import re
import shutil
import subprocess

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                         # place-cards/
DEFAULT_SCAD = os.path.join(ROOT, "wedding_place_cards.scad")

BED = (256.0, 256.0, 256.0)                          # Elegoo Centauri Carbon 2

# The user's three filaments (colours and densities from their OrcaSlicer project)
FILAMENTS = {
    "ivory": {"extruder": 1, "colour": "#F4F0E6", "density": 1.25, "name": "Elegoo Rapid PLA+ ivory"},
    "gold":  {"extruder": 2, "colour": "#D4AF37", "density": 1.32, "name": "Elegoo PLA Silk gold"},
    "wood":  {"extruder": 3, "colour": "#7A4F2E", "density": 1.25, "name": "Elegoo PLA Wood"},
}


# ---------------------------------------------------------------- SCAD parameters
def parse_scad(path, env=None):
    """Top-level `name = value;` assignments of a SCAD file that evaluate as Python
    (numbers, strings, lists, simple arithmetic on earlier names). Blocks are skipped."""
    txt = open(path, encoding="utf-8").read()
    txt = re.sub(r"//[^\n]*", "", txt)
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    stmts, buf, depth = [], "", 0
    for ch in txt:
        if ch == "{":
            depth += 1
            buf = ""
        elif ch == "}":
            depth -= 1
            buf = ""
        elif depth:
            continue
        elif ch == ";":
            stmts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    env = dict(env or {})
    env.update({"true": True, "false": False, "undef": None,
                "sqrt": math.sqrt, "max": max, "min": min, "abs": abs, "round": round,
                "ceil": math.ceil, "floor": math.floor})
    for s in stmts:
        m = re.match(r"^\$?([A-Za-z_]\w*)\s*=\s*(.*)$", s, re.S)
        if not m or s.startswith(("module", "function", "include", "use")):
            continue
        expr = m.group(2).strip()
        expr = re.sub(r"\bpow\(", "math.pow(", expr)
        try:
            env[m.group(1)] = eval(expr, {"__builtins__": {}, "math": math}, env)
        except Exception:
            pass
    return env


def card_params(scad=DEFAULT_SCAD, tree_data=None, overrides=None, name=None):
    """Plaque, foot and assembly parameters as one dict. tree_data.scad (the generated
    include next to the SCAD) is read first, as OpenSCAD does, so tree_pos etc. resolve.
    With a guest `name`, assembly_foot_x is that card's own foot position (name_table)."""
    env = {}
    td = tree_data or os.path.join(os.path.dirname(os.path.abspath(scad)), "tree_data.scad")
    if os.path.exists(td) and "include <tree_data.scad>" in open(scad, encoding="utf-8").read():
        env = parse_scad(td)
    P = parse_scad(scad, env)
    P.setdefault("pocket_depth", 0.0)
    P.setdefault("tree_pos", P.get("tree_pos_auto", [0.0, 0.0]))
    P.setdefault("assembly_foot_x", None)
    for k, v in (overrides or {}).items():
        P[k] = v
    if name:
        P["assembly_foot_x"] = foot_x_for(P, name)
    P["_scad"] = os.path.abspath(scad)
    return P


def name_row(P, name):
    """This guest's name_table row (tree_data.scad); "Sophie_final" finds "Sophie"."""
    table = P.get("name_table") or []
    for key in (name, (name or "").rsplit("_", 1)[0]):
        for r in table:
            if r and r[0] == key:
                return r
    return None


def foot_x_for(P, name=None):
    """Foot centre x (plaque frame) for this guest: name_table's 5th column (tree frame, written by
    card_layout.py: the foot's right end goes on the tick) moved by tree_pos, else assembly_foot_x."""
    r = name_row(P, name) if name else None
    if r is not None and len(r) > 4 and r[4] is not None:
        return float(P["tree_pos"][0]) + float(r[4])
    return P.get("assembly_foot_x")


# ---------------------------------------------------------------- assembled pose
def _rx(deg):
    a = math.radians(deg)
    return np.array([[1, 0, 0, 0], [0, math.cos(a), -math.sin(a), 0],
                     [0, math.sin(a), math.cos(a), 0], [0, 0, 0, 1]], float)


def _t(x, y, z):
    M = np.eye(4)
    M[:3, 3] = [x, y, z]
    return M


def plaque_to_world(P, contact=0.0):
    """Plaque frame (x from the left end, y up the card, z = 0 on the back face) -> world
    (the SCAD's assembly frame: foot bottom on z = 0, viewer at -y, plaque front faces -y).
    Mirrors assembly(): translate([0, slot_y, foot_h]) rotate([-lean, 0, 0])
    translate([0, plaque_t / 2, plaque_h / 2 - slot_depth]) rotate([90, 0, 0]).
    `contact` slides the plaque inside the slot clearance (+-slot_fit / 2)."""
    return (_t(0, P["slot_y"], P["foot_h"]) @ _rx(-P["lean"]) @
            _t(0, P["plaque_t"] / 2 + contact, P["plaque_h"] / 2 - P["slot_depth"]) @ _rx(90))


def tree_to_plaque(P, tree_pos=None):
    """Tree frame (origin = trunk base centre, z = 0 glue face) -> plaque frame: the glue
    face sits on the pocket floor at tree_pos."""
    tx, ty = tree_pos if tree_pos is not None else P["tree_pos"]
    return _t(tx, ty, P["plaque_t"] - P.get("pocket_depth", 0.0))


def foot_to_world(foot_x):
    return _t(foot_x, 0, 0)


def transform_points(M, pts):
    pts = np.asarray(pts, float)
    return pts @ M[:3, :3].T + M[:3, 3]


# ---------------------------------------------------------------- meshes
def load_mesh(path):
    import trimesh
    m = trimesh.load(path, force="mesh", process=True)
    return m


def to_manifold(mesh):
    import manifold3d as m3
    return m3.Manifold(m3.Mesh(vert_properties=np.asarray(mesh.vertices, np.float32),
                               tri_verts=np.asarray(mesh.faces, np.uint32)))


def manifold_to_trimesh(man):
    import trimesh
    mm = man.to_mesh()
    return trimesh.Trimesh(np.asarray(mm.vert_properties)[:, :3], np.asarray(mm.tri_verts), process=False)


# ---------------------------------------------------------------- PrusaSlicer
# PrusaSlicer stands in for OrcaSlicer (not installable here). Keys are PrusaSlicer's names;
# values copy the user's OrcaSlicer project (process "0.20mm Standard @Elegoo CC2 0.4 nozzle"
# plus their per-part overrides).
PRUSA_COMMON = {
    "nozzle_diameter": 0.4, "filament_diameter": 1.75, "gcode_flavor": "klipper",
    "use_relative_e_distances": 1, "bed_shape": "0x0,256x0,256x256,0x256", "max_print_height": 256,
    "layer_height": 0.2, "first_layer_height": 0.2,
    "perimeter_generator": "arachne", "perimeters": 2,
    "extrusion_width": 0.42, "external_perimeter_extrusion_width": 0.42,
    "perimeter_extrusion_width": 0.45, "infill_extrusion_width": 0.45,
    "solid_infill_extrusion_width": 0.42, "top_infill_extrusion_width": 0.42,
    "first_layer_extrusion_width": 0.5,
    "top_solid_layers": 5, "bottom_solid_layers": 3,
    "top_solid_min_thickness": 1.0, "bottom_solid_min_thickness": 0.6,
    "fill_density": "15%", "fill_pattern": "rectilinear",
    "top_fill_pattern": "monotonic", "bottom_fill_pattern": "monotonic", "solid_fill_pattern": "monotonic",
    "infill_overlap": "15%", "min_bead_width": "85%", "min_feature_size": "25%",
    "wall_transition_angle": 10, "wall_distribution_count": 1,
    "elefant_foot_compensation": 0.1, "seam_position": "rear",
    "skirts": 0, "brim_width": 0, "support_material": 0, "arc_fitting": "disabled",
    "ensure_vertical_shell_thickness": "enabled", "resolution": 0.012,
    "external_perimeters_first": 0, "gcode_resolution": 0.0125,
    "temperature": 220, "first_layer_temperature": 220, "bed_temperature": 60,
    "first_layer_bed_temperature": 60,
}

# Per-part tweaks (the user's 3MF part settings, and the tree's silk settings)
PRUSA_KIND = {
    "plaque": {"filament_density": FILAMENTS["ivory"]["density"]},
    "gold": {"filament_density": FILAMENTS["gold"]["density"], "layer_height": 0.1, "perimeters": 3,
             "top_fill_pattern": "concentric", "solid_fill_pattern": "concentric", "extrusion_multiplier": 1.03},
    "tree": {"filament_density": FILAMENTS["gold"]["density"], "layer_height": 0.1, "perimeters": 3,
             "fill_density": "100%", "top_fill_pattern": "concentric", "solid_fill_pattern": "concentric",
             "extrusion_multiplier": 1.03},
    "foot": {"filament_density": FILAMENTS["wood"]["density"], "fill_pattern": "lightning"},
}


def prusa_profile(kind="plaque", overrides=None):
    cfg = dict(PRUSA_COMMON)
    cfg.update(PRUSA_KIND.get(kind, {}))
    for k, v in (overrides or {}).items():
        cfg[k] = v
    return cfg


def parse_set(items):
    """--set key=value pairs -> dict (numbers stay strings, PrusaSlicer parses them)."""
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = v.strip()
    return out


def write_ini(cfg, path):
    with open(path, "w") as f:
        for k in sorted(cfg):
            f.write(f"{k} = {cfg[k]}\n")
    return path


def slice_prusa(model, gcode, cfg, workdir, center=None, timeout=1800):
    """Slice one model file to G-code with the PrusaSlicer CLI. Returns the command list.
    center: put the model's bbox centre there (bed coordinates)."""
    exe = shutil.which("prusa-slicer") or shutil.which("prusa-slicer-console")
    if not exe:
        raise RuntimeError("prusa-slicer not found on PATH")
    ini = write_ini(cfg, os.path.join(workdir, os.path.splitext(os.path.basename(gcode))[0] + ".ini"))
    cmd = [exe, "--export-gcode", "--load", ini, "--output", gcode]
    if center is not None:
        cmd += ["--center", f"{center[0]:.4f},{center[1]:.4f}"]
    cmd.append(model)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not os.path.exists(gcode):
        raise RuntimeError("PrusaSlicer failed:\n" + " ".join(cmd) + "\n" + r.stdout[-3000:] + r.stderr[-3000:])
    return cmd


def gcode_mass_com(path, mesh, center=(128.0, 128.0), density=1.25):
    """Mass (g) and centre of mass (part frame) of the plastic a G-code lays down: each extruding move
    puts its filament at the segment midpoint, half a layer under the nozzle (relative E; retracts and
    unretracts skipped). The part was sliced with its bbox centre at `center`, lowest point on the bed."""
    ax = re.compile(r"([XYZE])(-?\d*\.?\d+)")
    x = y = z = 0.0
    lh, e_tot, acc = 0.2, 0.0, np.zeros(3)
    with open(path) as f:
        for line in f:
            if line.startswith(";HEIGHT:"):
                lh = float(line[8:])
            elif line.startswith(("G1 ", "G0 ")):
                d = dict((k, float(v)) for k, v in ax.findall(line.split(";")[0]))
                nx, ny, z = d.get("X", x), d.get("Y", y), d.get("Z", z)
                e = d.get("E", 0.0)
                if e > 0 and (nx != x or ny != y):
                    acc += e * np.array([(x + nx) / 2, (y + ny) / 2, z - lh / 2])
                    e_tot += e
                x, y = nx, ny
    lo, hi = mesh.bounds
    com = acc / max(e_tot, 1e-9) + [(lo[0] + hi[0]) / 2 - center[0], (lo[1] + hi[1]) / 2 - center[1], lo[2]]
    return e_tot * math.pi * 0.875 ** 2 * density / 1000.0, com


def gcode_summary(path):
    """Filament and time figures PrusaSlicer writes at the end of the G-code."""
    keys = {
        "filament used [mm]": "filament_mm", "filament used [cm3]": "filament_cm3",
        "filament used [g]": "filament_g", "total filament used [g]": "total_filament_g",
        "estimated printing time (normal mode)": "time",
    }
    out = {}
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 200000))
        tail = f.read().decode("utf-8", "replace")
    for line in tail.splitlines():
        if not line.startswith(";"):
            continue
        k, _, v = line[1:].partition("=")
        k = k.strip()
        if k in keys:
            v = v.strip()
            try:
                out[keys[k]] = float(v.split(",")[0])
            except ValueError:
                out[keys[k]] = v
    return out


# ---------------------------------------------------------------- rasters
class Grid:
    """Pixel grid over an XY box: pixel (r, c) centre = (x0 + (c + 0.5) * res, y0 + (r + 0.5) * res).
    Row 0 is at the bottom (y0), so flip rows (img[::-1]) for display."""

    def __init__(self, x0, y0, x1, y1, res, pad=0.5):
        self.res = float(res)
        self.x0 = x0 - pad
        self.y0 = y0 - pad
        self.w = int(math.ceil((x1 - x0 + 2 * pad) / res))
        self.h = int(math.ceil((y1 - y0 + 2 * pad) / res))

    @property
    def shape(self):
        return (self.h, self.w)

    def to_px(self, xy):
        xy = np.asarray(xy, float)
        return np.c_[(xy[:, 0] - self.x0) / self.res, (xy[:, 1] - self.y0) / self.res]

    def centres(self):
        xs = self.x0 + (np.arange(self.w) + 0.5) * self.res
        ys = self.y0 + (np.arange(self.h) + 0.5) * self.res
        return xs, ys


def raster_rings(rings, grid):
    """Even-odd fill of closed rings (list of Nx2 arrays) on the grid -> bool mask.
    Each ring is filled in its own bounding box and XOR-ed in, so holes and islands in
    holes come out right without needing their nesting."""
    import cv2
    mask = np.zeros(grid.shape, np.uint8)
    for ring in rings:
        if len(ring) < 3:
            continue
        p = grid.to_px(ring) - 0.5            # pixel-centre convention for cv2
        c0 = max(int(math.floor(p[:, 0].min())) - 1, 0)
        r0 = max(int(math.floor(p[:, 1].min())) - 1, 0)
        c1 = min(int(math.ceil(p[:, 0].max())) + 2, grid.w)
        r1 = min(int(math.ceil(p[:, 1].max())) + 2, grid.h)
        if c1 <= c0 or r1 <= r0:
            continue
        sub = np.zeros((r1 - r0, c1 - c0), np.uint8)
        pts = np.round((p - [c0, r0]) * 16).astype(np.int32)   # 4 fractional bits
        cv2.fillPoly(sub, [pts], 1, lineType=cv2.LINE_8, shift=4)
        mask[r0:r1, c0:c1] ^= sub
    return mask.astype(bool)


def section_rings(man_or_mesh, z):
    """Closed rings of the cross-section at height z (manifold3d if given a Manifold)."""
    try:
        import manifold3d as m3
        if isinstance(man_or_mesh, m3.Manifold):
            return [np.asarray(r, float) for r in man_or_mesh.slice(z).to_polygons()]
    except ImportError:
        pass
    sec = man_or_mesh.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
    if sec is None:
        return []
    return [np.asarray(d[:, :2], float) for d in sec.discrete]
