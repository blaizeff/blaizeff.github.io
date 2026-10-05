#!/usr/bin/env python3
"""Write an OrcaSlicer (Bambu-format) 3MF project for the place cards, trees and feet.

Modelled on the user's own project (source/previous/Place cards - TEST 2 cards, 2 feet.3mf):
  * Metadata/project_settings.config is copied from the template untouched (printer Elegoo
    Centauri Carbon 2, the three filaments, the process), except that the per-plate prime
    tower position list is extended to the number of plates.
  * Per object and per part metadata are copied from the template by role: the card object
    settings, the "Ivory plaque" part (extruder 1, speeds, ironing) and the "Gold name +
    border" part (extruder 2, slow walls, concentric top, 3 walls), the feet (extruder 3,
    lightning infill, speeds). The card's gold Z range prints at 0.1 mm
    (Metadata/layer_config_ranges.xml), measured from the gold mesh.
  * Trees: extruder 2 (silk gold), 0.1 mm layers, the gold part's settings plus silk tweaks
    (outer wall 30 mm/s, concentric top and solid infill, 100 % infill so no sparse pattern
    shows through the thin top skins of the relief, no brim, no ironing).
  * Plates are filled automatically (shelf packing, prime tower corner kept free on
    multi-filament plates) and spill onto further plates when full, so the same tool makes
    the TEST project and the production plates. Identical meshes become one object with
    several instances (one mesh stored, much smaller file); --no-instances writes a separate
    object per copy, exactly like the template does.

Validation (always run after writing, or alone with --validate-only FILE): zip entries and
XML parse, every cross reference (rels, components, model_settings objects / parts / plate
instances, layer ranges), triangle counts and volumes round-trip against the source meshes,
every instance inside its plate. Then each plate is flattened to a plain 3MF and loaded with
PrusaSlicer (--info), which cannot read Bambu's split-file format directly (it fails on the
user's own project too); --slice-check also slices each flattened plate.

Run (from place-cards/):
  python3 tools/make_3mf.py --out print/place_cards_TEST.3mf \
      --card "Sophie=stl/plaque_Sophie_base.stl,stl/plaque_Sophie_gold.stl" \
      --card "Max-Antoine=stl/plaque_Max-Antoine_base.stl,stl/plaque_Max-Antoine_gold.stl" \
      --tree tree/out/tree.stl --trees 2 --foot stl/foot.stl --feet 2 --plate-prefix TEST --card-suffix " (test card)"
  Production: --card-dir stl/ (every plaque_<name>_base.stl + _gold.stl), --trees 52, --feet 55.
  Options: --template FILE.3mf|DIR, --no-instances, --tree-layer 0.1, --gap 6, --rotate auto|0|90,
           --plate-prefix NAME, --slice-check, --validate-only FILE.3mf
"""
import argparse
import glob
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_common as C  # noqa: E402

TEMPLATE = os.path.join(C.ROOT, "source", "previous", "Place cards - TEST 2 cards, 2 feet.3mf")
NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
      "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"}
PLATE_GAP = 0.2            # OrcaSlicer lays plates out with a 20 % gap (256 -> 307.2 mm pitch)
SKIP_META = {"name", "matrix", "source_file", "source_object_id", "source_volume_id",
             "source_offset_x", "source_offset_y", "source_offset_z"}

# Silk tree settings on top of the template's gold part settings
TREE_OBJECT_META = [("extruder", "2"), ("layer_height", "0.1"), ("seam_position", "back"),
                    ("wall_generator", "arachne"), ("precise_outer_wall", "1"), ("brim_type", "no_brim")]
TREE_PART_EXTRA = [("outer_wall_speed", "30"), ("inner_wall_speed", "50"), ("top_surface_speed", "35"),
                   ("sparse_infill_density", "100%"), ("top_surface_pattern", "concentric"),
                   ("internal_solid_infill_pattern", "concentric"), ("ironing_type", "no ironing"),
                   ("wall_loops", "3"), ("only_one_wall_top", "0"), ("filter_out_gap_fill", "0")]
DEFAULT_CARD_OBJECT = [("extruder", "1"), ("layer_height", "0.2"), ("seam_position", "back"),
                       ("wall_generator", "arachne"), ("precise_outer_wall", "1")]
DEFAULT_FOOT_OBJECT = [("extruder", "3"), ("layer_height", "0.2"), ("sparse_infill_pattern", "lightning")]


# ---------------------------------------------------------------- template
def read_template(path):
    """Files of the template 3MF (zip or unzipped folder) as {name: bytes}."""
    files = {}
    if os.path.isdir(path):
        for root, _, names in os.walk(path):
            for n in names:
                full = os.path.join(root, n)
                files[os.path.relpath(full, path).replace(os.sep, "/")] = open(full, "rb").read()
    else:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                files[n] = z.read(n)
    return files


def template_roles(files):
    """Object / part metadata from the template's model_settings.config, by role."""
    root = ET.fromstring(files["Metadata/model_settings.config"])
    roles = {"card_object": None, "ivory_part": None, "gold_part": None, "foot_object": None, "foot_part": None,
             "plate": None}

    def meta(el):
        return [(m.get("key"), m.get("value")) for m in el.findall("metadata") if m.get("key") not in SKIP_META]
    for obj in root.findall("object"):
        parts = obj.findall("part")
        ext = dict(meta(obj)).get("extruder")
        if len(parts) >= 2 and roles["card_object"] is None:
            roles["card_object"] = meta(obj)
            for p in parts:
                pe = dict(meta(p)).get("extruder")
                if pe == "1" or "ivory" in (dict((m.get("key"), m.get("value")) for m in p.findall("metadata"))
                                            .get("name", "").lower()):
                    roles["ivory_part"] = roles["ivory_part"] or meta(p)
                else:
                    roles["gold_part"] = roles["gold_part"] or meta(p)
        elif ext == "3" and roles["foot_object"] is None:
            roles["foot_object"] = meta(obj)
            roles["foot_part"] = meta(parts[0]) if parts else []
    pl = root.find("plate")
    if pl is not None:
        roles["plate"] = [(m.get("key"), m.get("value")) for m in pl.findall("metadata")
                          if m.get("key") in ("bed_type", "locked", "filament_map_mode", "gcode_file")]
    roles["card_object"] = roles["card_object"] or DEFAULT_CARD_OBJECT
    roles["foot_object"] = roles["foot_object"] or DEFAULT_FOOT_OBJECT
    roles["ivory_part"] = roles["ivory_part"] or [("extruder", "1")]
    roles["gold_part"] = roles["gold_part"] or [("extruder", "2")]
    roles["foot_part"] = roles["foot_part"] or []
    roles["plate"] = roles["plate"] or [("bed_type", "High Temp Plate"), ("locked", "false"),
                                        ("filament_map_mode", "Auto For Flush"), ("gcode_file", "")]
    return roles


def merge_meta(base, extra):
    d = dict(base)
    for k, v in extra:
        d[k] = v
    return list(d.items())


# ---------------------------------------------------------------- items
class Item:
    """One printable object: parts = [(name, mesh, part_meta)], placed on a plate."""

    def __init__(self, key, name, kind, parts, obj_meta, filaments, layer_ranges=None):
        self.key, self.name, self.kind = key, name, kind
        self.parts = parts
        self.obj_meta = obj_meta
        self.filaments = filaments
        self.layer_ranges = layer_ranges or []
        lo = np.min([m.bounds[0] for _, m, _, _ in parts], axis=0)
        hi = np.max([m.bounds[1] for _, m, _, _ in parts], axis=0)
        self.lo, self.hi = lo, hi
        self.size = hi - lo


def load_items(args, roles):
    items = []
    cards = list(args.card or [])
    if args.card_dir:
        for b in sorted(glob.glob(os.path.join(args.card_dir, "plaque_*_base.stl"))):
            n = os.path.basename(b)[len("plaque_"):-len("_base.stl")]
            g = b.replace("_base.stl", "_gold.stl")
            if os.path.exists(g):
                cards.append(f"{n}={b},{g}")
    for spec in cards:
        name, _, files = spec.partition("=")
        base, gold = [f.strip() for f in files.split(",")]
        mb, mg = C.load_mesh(base), C.load_mesh(gold)
        z0, z1 = float(mg.bounds[0, 2]), float(mg.bounds[1, 2])
        items.append(Item(
            key=f"card:{name}", name=name + args.card_suffix, kind="card",
            parts=[("Ivory plaque", mb, roles["ivory_part"], base), ("Gold name + border", mg, roles["gold_part"], gold)],
            obj_meta=roles["card_object"], filaments={1, 2},
            layer_ranges=[(z0, z1, args.gold_layer)]))
    if args.tree:
        mt = C.load_mesh(args.tree)
        part_meta = merge_meta(roles["gold_part"], TREE_PART_EXTRA)
        obj_meta = merge_meta([], TREE_OBJECT_META + [("layer_height", f"{args.tree_layer:g}")])
        for i in range(args.trees):
            items.append(Item(key="tree", name=f"Tree {i + 1:02d}", kind="tree",
                              parts=[("Gold tree", mt, part_meta, args.tree)], obj_meta=obj_meta, filaments={2}))
    if args.foot:
        mf = C.load_mesh(args.foot)
        foot_meta = roles["foot_object"]
        if args.foot_infill != "template":
            # a heavy foot keeps the leaning card from tipping backwards (lightning cannot do 100 %)
            foot_meta = merge_meta(foot_meta, [("sparse_infill_density", args.foot_infill),
                                               ("sparse_infill_pattern", "zig-zag")])
        for i in range(args.feet):
            items.append(Item(key="foot", name=f"Foot {i + 1:02d}", kind="foot",
                              parts=[(f"Foot {i + 1:02d}", mf, roles["foot_part"], args.foot)],
                              obj_meta=foot_meta, filaments={3}))
    return items


# ---------------------------------------------------------------- packing
def shelf_pack(sizes, W, H, gap, keepout=None):
    """Place rectangles (w, h) row by row from the top-left of a W x H area, then centre each
    row. Returns the centres of the rectangles that fit (in order). keepout = a rectangle
    (x0, y0, x1, y1) to avoid (the prime tower)."""
    pos, rows = [], []
    x, y_top, row_h, row = 0.0, H, 0.0, 0
    for (w, h) in sizes:
        placed = False
        for _ in range(3):
            if x + w > W:
                x, y_top, row_h, row = 0.0, y_top - row_h - gap, 0.0, row + 1
            if y_top - h < 0:
                break
            r = (x, y_top - h, x + w, y_top)
            if keepout and not (r[2] <= keepout[0] or r[0] >= keepout[2] or r[3] <= keepout[1] or r[1] >= keepout[3]):
                # skip past the keep-out area on this row
                x = keepout[2] + gap
                if x + w > W:
                    x = W + 1
                continue
            pos.append([(r[0] + r[2]) / 2, (r[1] + r[3]) / 2])
            rows.append(row)
            x += w + gap
            row_h = max(row_h, h)
            placed = True
            break
        if not placed:
            break
    # centre each row that stays clear of the keep-out area
    for rw in set(rows):
        idx = [i for i, r in enumerate(rows) if r == rw]
        x0 = min(pos[i][0] - sizes[i][0] / 2 for i in idx)
        x1 = max(pos[i][0] + sizes[i][0] / 2 for i in idx)
        y0 = min(pos[i][1] - sizes[i][1] / 2 for i in idx)
        y1 = max(pos[i][1] + sizes[i][1] / 2 for i in idx)
        dx = (W - (x1 - x0)) / 2 - x0
        if keepout and (y1 > keepout[1] and y0 < keepout[3]) and x1 + dx > keepout[0]:
            continue
        for i in idx:
            pos[i][0] += dx
    return [tuple(p) for p in pos]


def plan_plates(items, args, bed):
    """Group items by kind, fill plates in order. Returns [(plate_name, [(item, x, y, rot)])]."""
    margin = args.margin
    W, H = bed[0] - 2 * margin, bed[1] - 2 * margin
    tower = (214 - 5 - margin, 200 - 5 - margin, W, H)      # prime tower + brim, in the packing area frame
    plates = []
    kinds = []
    for it in items:
        if it.kind not in kinds:
            kinds.append(it.kind)
    labels = {"card": "cards", "tree": "trees", "foot": "feet"}
    for kind in kinds:
        group = [it for it in items if it.kind == kind]
        gap = args.gap if kind != "foot" else args.foot_gap
        multi = any(len(it.filaments) > 1 for it in group)
        while group:
            best = None
            for rot in ((0, 90) if args.rotate == "auto" else (int(args.rotate),)):
                sizes = [((it.size[0], it.size[1]) if rot == 0 else (it.size[1], it.size[0])) for it in group]
                pos = shelf_pack(sizes, W, H, gap, tower if multi else None)
                if best is None or len(pos) > len(best[1]):
                    best = (rot, pos)
            rot, pos = best
            if not pos:
                raise SystemExit(f"{group[0].name} does not fit on the bed")
            n = len(pos)
            if args.max_per_plate:
                n = min(n, args.max_per_plate)
            # centre the block of placed items on the bed (keeping clear of the tower)
            sizes = [((it.size[0], it.size[1]) if rot == 0 else (it.size[1], it.size[0])) for it in group[:n]]
            xs0 = min(p[0] - s[0] / 2 for p, s in zip(pos[:n], sizes))
            xs1 = max(p[0] + s[0] / 2 for p, s in zip(pos[:n], sizes))
            ys0 = min(p[1] - s[1] / 2 for p, s in zip(pos[:n], sizes))
            ys1 = max(p[1] + s[1] / 2 for p, s in zip(pos[:n], sizes))
            dx = (W - (xs1 - xs0)) / 2 - xs0
            dy = (H - (ys1 - ys0)) / 2 - ys0
            if multi:
                # keep the tower corner free after centring
                over_x = xs1 + dx - tower[0]
                over_y = ys1 + dy - tower[1]
                if over_x > 0 and over_y > 0:
                    dx -= min(over_x, max(0.0, xs0 + dx))
            placed = [(it, p[0] + dx + margin, p[1] + dy + margin, rot) for it, p in zip(group[:n], pos[:n])]
            plates.append((labels.get(kind, kind), placed))
            group = group[n:]
    out = []
    for i, (label, placed) in enumerate(plates):
        count = len(placed)
        words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
        nm = f"{args.plate_prefix} {i + 1} - {words.get(count, str(count))} {label if count != 1 else label.rstrip('s')}"
        if label == "feet" and count == 1:
            nm = f"{args.plate_prefix} {i + 1} - one foot"
        out.append((nm.strip(), placed))
    return out


def plate_origin(index, n_plates, bed):
    """OrcaSlicer's plate grid: columns = ceil-ish sqrt, rows go towards -y."""
    v = math.sqrt(n_plates)
    cols = int(round(v)) + (1 if v > round(v) else 0)
    cols = max(cols, 1)
    r, c = divmod(index, cols)
    return c * bed[0] * (1 + PLATE_GAP), -r * bed[1] * (1 + PLATE_GAP)


# ---------------------------------------------------------------- writing
def fmt(v):
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def mesh_xml(obj_id, mesh, offset, uid):
    v = np.asarray(mesh.vertices, float) - offset
    f = np.asarray(mesh.faces, np.int64)
    out = io.StringIO()
    out.write(f'  <object id="{obj_id}" p:UUID="{uid}" type="model">\n   <mesh>\n    <vertices>\n')
    out.write("".join(f'     <vertex x="{fmt(a)}" y="{fmt(b)}" z="{fmt(c)}"/>\n' for a, b, c in v))
    out.write("    </vertices>\n    <triangles>\n")
    out.write("".join(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in f))
    out.write("    </triangles>\n   </mesh>\n  </object>\n")
    return out.getvalue()


def tf(M):
    """3MF transform string (column-major 3x4) from a 4x4 matrix."""
    vals = [M[0, 0], M[1, 0], M[2, 0], M[0, 1], M[1, 1], M[2, 1], M[0, 2], M[1, 2], M[2, 2], M[0, 3], M[1, 3], M[2, 3]]
    return " ".join(fmt(x) for x in vals)


def matrix_meta(M):
    return " ".join(fmt(x) for x in M.reshape(-1))


def rotz(deg):
    a = math.radians(deg)
    M = np.eye(4)
    M[0, 0], M[0, 1], M[1, 0], M[1, 1] = math.cos(a), -math.sin(a), math.sin(a), math.cos(a)
    return M


def trans(x, y, z):
    M = np.eye(4)
    M[:3, 3] = [x, y, z]
    return M


def uid(prefix, n):
    return f"{prefix:08x}-{n:04x}-4c03-9d28-80fed5dfa1dc"


MODEL_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
              'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
              'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
              'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n')


def build(args):
    bed = C.BED
    tpl = read_template(args.template)
    roles = template_roles(tpl)
    items = load_items(args, roles)
    if not items:
        raise SystemExit("nothing to pack: give --card / --card-dir / --tree / --foot")
    plates = plan_plates(items, args, bed)
    n_plates = len(plates)

    # objects: one per item, or one per distinct item key with instances
    objects = []            # dicts: id, item(s), instances [(plate_idx, x, y, rot)]
    by_key = {}
    for pi, (_, placed) in enumerate(plates):
        for it, x, y, rot in placed:
            share = (not args.no_instances) and it.kind in ("tree", "foot")
            if share and it.key in by_key:
                by_key[it.key]["instances"].append((pi, x, y, rot, it))
                continue
            o = {"item": it, "instances": [(pi, x, y, rot, it)]}
            objects.append(o)
            if share:
                by_key[it.key] = o
                o["shared"] = True
    next_id = 1
    files = {}
    rels = []
    resources = []
    build_items = []
    settings_objects = []
    layer_ranges = []
    plate_instances = [[] for _ in plates]
    identify = 100
    report_meshes = []
    for oi, o in enumerate(objects):
        it = o["item"]
        shared = o.get("shared", False)
        if shared:
            it_name = {"tree": "Tree", "foot": "Foot"}[it.kind] + f" x{len(o['instances'])}"
        else:
            it_name = it.name
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", it_name).strip("_") or "object"
        path = f"/3D/Objects/{safe}_{oi + 1}.model"
        cx, cy = (it.lo[0] + it.hi[0]) / 2, (it.lo[1] + it.hi[1]) / 2
        part_ids = []
        body = io.StringIO()
        for k, (pname, mesh, pmeta, src) in enumerate(it.parts):
            zc = (mesh.bounds[0, 2] + mesh.bounds[1, 2]) / 2
            pid = next_id
            next_id += 1
            body.write(mesh_xml(pid, mesh, np.array([cx, cy, zc]), uid(0x10000 + oi, k)))
            part_ids.append((pid, pname, pmeta, src, zc, mesh))
            report_meshes.append({"object": it_name, "part": pname, "source": os.path.abspath(src),
                                  "triangles": int(len(mesh.faces)), "volume": float(mesh.volume), "path": path,
                                  "id": pid})
        oid = next_id
        next_id += 1
        files[path.lstrip("/")] = (MODEL_HEAD + ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
                                   + body.getvalue() + " </resources>\n <build/>\n</model>\n").encode()
        rels.append(path)
        # placement: single instance -> like the template (components carry the bed position,
        # the build item only the plate offset); shared -> the build items carry each instance
        if not shared:
            pi, x, y, rot, _ = o["instances"][0]
            px, py = plate_origin(pi, n_plates, bed)
            place = trans(x, y, 0) @ rotz(rot)
            item_M = trans(px, py, 0)
        else:
            place = np.eye(4)
        comp_xml = []
        part_mats = []
        for k, (pid, pname, pmeta, src, zc, mesh) in enumerate(part_ids):
            Mc = place @ trans(0, 0, zc - it.lo[2])
            part_mats.append(Mc)
            comp_xml.append(f'    <component p:path="{path}" objectid="{pid}" p:UUID="{uid(0x20000 + oi, k)}" '
                            f'transform="{tf(Mc)}"/>')
        resources.append(f'  <object id="{oid}" p:UUID="{uid(oi + 1, 0)}" type="model">\n   <components>\n'
                         + "\n".join(comp_xml) + "\n   </components>\n  </object>")
        for ii, (pi, x, y, rot, inst_item) in enumerate(o["instances"]):
            px, py = plate_origin(pi, n_plates, bed)
            M = item_M if not shared else trans(px + x, py + y, 0) @ rotz(rot)
            build_items.append(f'  <item objectid="{oid}" p:UUID="{uid(0x30000 + oi, ii)}" transform="{tf(M)}" '
                               'printable="1" auto_drop="1"/>')
            identify += 1
            plate_instances[pi].append((oid, ii, identify))
        # model_settings object
        so = [f'  <object id="{oid}">', f'    <metadata key="name" value="{xml_esc(it_name)}"/>']
        so += [f'    <metadata key="{k}" value="{xml_esc(v)}"/>' for k, v in it.obj_meta]
        for k, (pid, pname, pmeta, src, zc, mesh) in enumerate(part_ids):
            Mc = part_mats[k]
            so.append(f'    <part id="{pid}" subtype="normal_part">')
            so.append(f'      <metadata key="name" value="{xml_esc(pname)}"/>')
            so.append(f'      <metadata key="matrix" value="{matrix_meta(Mc)}"/>')
            so.append(f'      <metadata key="source_file" value="{xml_esc(os.path.basename(src))}"/>')
            so.append(f'      <metadata key="source_object_id" value="{oi}"/>')
            so.append('      <metadata key="source_volume_id" value="0"/>')
            so.append(f'      <metadata key="source_offset_x" value="{fmt(cx)}"/>')
            so.append(f'      <metadata key="source_offset_y" value="{fmt(cy)}"/>')
            so.append(f'      <metadata key="source_offset_z" value="{fmt(zc)}"/>')
            so += [f'      <metadata key="{kk}" value="{xml_esc(vv)}"/>' for kk, vv in pmeta]
            so.append('      <mesh_stat edges_fixed="0" degenerate_facets="0" facets_removed="0" '
                      'facets_reversed="0" backwards_edges="0"/>')
            so.append("    </part>")
        so.append("  </object>")
        settings_objects.append("\n".join(so))
        if it.layer_ranges:
            layer_ranges.append((oi + 1, [(z0 - it.lo[2], z1 - it.lo[2], h) for z0, z1, h in it.layer_ranges]))
        o["oid"] = oid

    # ---- assemble the package
    model = [MODEL_HEAD]
    meta = [("Application", "BambuStudio-02.06.00.51"), ("OrcaSlicer", "2.4.2"), ("BambuStudio:3mfVersion", "1"),
            ("Copyright", ""), ("CreationDate", ""), ("Description", ""), ("Designer", ""), ("DesignerCover", ""),
            ("DesignerUserId", ""), ("License", ""), ("ModificationDate", ""), ("Origin", ""), ("Title", args.title)]
    try:
        r = ET.fromstring(tpl["3D/3dmodel.model"])
        tm = {m.get("name"): (m.text or "") for m in r.findall("m:metadata", NS)}
        meta = [(k, tm.get(k, v) if k != "Title" else v) for k, v in meta]
    except Exception:  # noqa: BLE001
        pass
    model += [f' <metadata name="{k}">{xml_esc(v)}</metadata>\n' for k, v in meta]
    model.append(" <resources>\n" + "\n".join(resources) + "\n </resources>\n")
    model.append(' <build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">\n'
                 + "\n".join(build_items) + "\n </build>\n</model>\n")
    files["3D/3dmodel.model"] = "".join(model).encode()
    files["3D/_rels/3dmodel.model.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "".join(f' <Relationship Target="{p}" Id="rel-{i + 1}" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
                  for i, p in enumerate(rels)) + "</Relationships>").encode()
    files["[Content_Types].xml"] = tpl.get("[Content_Types].xml") or (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        ' <Default Extension="png" ContentType="image/png"/>\n <Default Extension="gcode" ContentType="text/x.gcode"/>\n'
        '</Types>').encode()
    files["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        ' <Relationship Target="/Metadata/plate_1.png" Id="rel-2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"/>\n'
        ' <Relationship Target="/Metadata/plate_1.png" Id="rel-4" Type="http://schemas.bambulab.com/package/2021/cover-thumbnail-middle"/>\n'
        ' <Relationship Target="/Metadata/plate_1_small.png" Id="rel-5" Type="http://schemas.bambulab.com/package/2021/cover-thumbnail-small"/>\n'
        '</Relationships>').encode()
    # project settings: the user's, with the per-plate prime tower lists sized to the plates
    ps = json.loads(tpl["Metadata/project_settings.config"])
    for k in ("wipe_tower_x", "wipe_tower_y"):
        if isinstance(ps.get(k), list) and ps[k]:
            ps[k] = (ps[k] + [ps[k][-1]] * n_plates)[:max(n_plates, len(ps[k]))]
    files["Metadata/project_settings.config"] = json.dumps(ps, indent=4).encode()
    for keep in ("Metadata/slice_info.config",):
        if keep in tpl:
            files[keep] = tpl[keep]
    # model settings
    ms = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"] + settings_objects
    for pi, (pname, placed) in enumerate(plates):
        ms.append("  <plate>")
        pm = dict(roles["plate"])
        ms.append(f'    <metadata key="bed_type" value="{xml_esc(pm.get("bed_type", "High Temp Plate"))}"/>')
        ms.append(f'    <metadata key="plater_id" value="{pi + 1}"/>')
        ms.append(f'    <metadata key="plater_name" value="{xml_esc(pname)}"/>')
        ms.append(f'    <metadata key="locked" value="{xml_esc(pm.get("locked", "false"))}"/>')
        ms.append(f'    <metadata key="filament_map_mode" value="{xml_esc(pm.get("filament_map_mode", "Auto For Flush"))}"/>')
        ms.append('    <metadata key="gcode_file" value=""/>')
        for oid, ii, ident in plate_instances[pi]:
            ms += ["    <model_instance>", f'      <metadata key="object_id" value="{oid}"/>',
                   f'      <metadata key="instance_id" value="{ii}"/>',
                   f'      <metadata key="identify_id" value="{ident}"/>', "    </model_instance>"]
        ms.append("  </plate>")
    ms += ["  <assemble>", "  </assemble>", "</config>", ""]
    files["Metadata/model_settings.config"] = "\n".join(ms).encode()
    lr = ['<?xml version="1.0" encoding="utf-8"?>', "<objects>"]
    for ordinal, ranges in layer_ranges:
        lr.append(f' <object id="{ordinal}">')
        for z0, z1, h in ranges:
            lr += [f'  <range min_z="{z0:.10g}" max_z="{z1:.10g}">', f'   <option opt_key="layer_height">{h:g}</option>',
                   "  </range>"]
        lr.append(" </object>")
    lr += ["</objects>", ""]
    files["Metadata/layer_config_ranges.xml"] = "\n".join(lr).encode()
    files["Metadata/filament_sequence.json"] = json.dumps(
        {f"plate_{i + 1}": {"nozzle_sequence": [], "optimal_assignment": [], "sequence": []} for i in range(n_plates)},
        separators=(",", ":")).encode()
    # thumbnails
    for pi, (pname, placed) in enumerate(plates):
        big, small = plate_thumbnail(placed, bed)
        files[f"Metadata/plate_{pi + 1}.png"] = big
        files[f"Metadata/plate_{pi + 1}_small.png"] = small

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    order = ["[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "3D/_rels/3dmodel.model.rels"]
    order += sorted(k for k in files if k.startswith("3D/Objects/"))
    order += sorted(k for k in files if k.startswith("Metadata/"))
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for k in order:
            zi = zipfile.ZipInfo(k, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, files[k])
    summary = {"out": os.path.abspath(args.out), "plates": [
        {"name": n, "objects": [{"name": it.name, "kind": it.kind, "x": round(x, 2), "y": round(y, 2), "rot": rot}
                                for it, x, y, rot in placed]} for n, placed in plates],
        "objects": len(objects), "instances": sum(len(o["instances"]) for o in objects),
        "meshes": report_meshes, "bytes": os.path.getsize(args.out)}
    return summary


def xml_esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def plate_thumbnail(placed, bed):
    """Top view of the plate (512 px and 128 px PNG)."""
    import cv2
    S = 512
    img = np.full((S, S, 3), 62, np.uint8)                 # dark plate, like the slicer's
    cv2.rectangle(img, (0, 0), (S - 1, S - 1), (110, 110, 110), 2)
    sc = S / bed[0]
    colours = {1: (230, 240, 244), 2: (55, 175, 212), 3: (46, 79, 122)}      # BGR
    for it, x, y, rot in placed:
        for k, (pname, mesh, pmeta, src) in enumerate(it.parts):
            ext = int(dict(pmeta).get("extruder", dict(it.obj_meta).get("extruder", "1")))
            v = mesh.vertices[:, :2] - [(it.lo[0] + it.hi[0]) / 2, (it.lo[1] + it.hi[1]) / 2]
            if rot:
                a = math.radians(rot)
                v = v @ np.array([[math.cos(a), math.sin(a)], [-math.sin(a), math.cos(a)]])
            v = v + [x, y]
            tri = v[mesh.faces]
            n = mesh.face_normals[:, 2]
            tri = tri[n > 0.05]
            pts = np.round(np.stack([tri[:, :, 0] * sc, (bed[1] - tri[:, :, 1]) * sc], -1) * 4).astype(np.int32)
            col = colours.get(ext, (128, 128, 128))
            cv2.fillPoly(img, list(pts), col, lineType=cv2.LINE_AA, shift=2)
    ok, big = cv2.imencode(".png", img)
    ok2, small = cv2.imencode(".png", cv2.resize(img, (128, 128), interpolation=cv2.INTER_AREA))
    return big.tobytes(), small.tobytes()


# ---------------------------------------------------------------- validation
def parse_mesh_file(data):
    root = ET.fromstring(data)
    meshes = {}
    for obj in root.find("m:resources", NS).findall("m:object", NS):
        mesh = obj.find("m:mesh", NS)
        if mesh is None:
            continue
        vs = np.array([[float(v.get("x")), float(v.get("y")), float(v.get("z"))]
                       for v in mesh.find("m:vertices", NS)], float)
        ts = np.array([[int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))]
                       for t in mesh.find("m:triangles", NS)], np.int64)
        meshes[obj.get("id")] = (vs, ts)
    return meshes


def parse_tf(s):
    v = [float(x) for x in s.split()]
    M = np.eye(4)
    M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = v[0:3], v[3:6], v[6:9], v[9:12]
    return M


def write_plain_3mf(path, meshes):
    """Core-spec 3MF, one object per mesh, meshes inline (what PrusaSlicer reads)."""
    body = io.StringIO()
    body.write('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
               'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n <resources>\n')
    for i, m in enumerate(meshes):
        body.write(f'  <object id="{i + 1}" type="model">\n   <mesh>\n    <vertices>\n')
        body.write("".join(f'     <vertex x="{fmt(a)}" y="{fmt(b)}" z="{fmt(c)}"/>\n' for a, b, c in m.vertices))
        body.write("    </vertices>\n    <triangles>\n")
        body.write("".join(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in m.faces))
        body.write("    </triangles>\n   </mesh>\n  </object>\n")
    body.write(" </resources>\n <build>\n")
    body.write("".join(f'  <item objectid="{i + 1}"/>\n' for i in range(len(meshes))))
    body.write(" </build>\n</model>\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/'
                   'vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/'
                   'vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" '
                   'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", body.getvalue())


def validate(path, expected=None, slice_check=False, work=None):
    """Structure, cross references, round trip and a PrusaSlicer load of every plate."""
    import trimesh
    res = {"file": os.path.abspath(path), "checks": []}

    def add(name, ok, detail=""):
        res["checks"].append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})
    z = zipfile.ZipFile(path)
    names = set(z.namelist())
    need = ["[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "3D/_rels/3dmodel.model.rels",
            "Metadata/project_settings.config", "Metadata/model_settings.config"]
    add("required entries", all(n in names for n in need), ", ".join(n for n in need if n not in names))
    bad = []
    for n in names:
        if n.endswith((".model", ".rels", ".config", ".xml")) and n != "Metadata/project_settings.config":
            try:
                ET.fromstring(z.read(n))
            except ET.ParseError as e:
                bad.append(f"{n}: {e}")
    try:
        ps = json.loads(z.read("Metadata/project_settings.config"))
    except Exception as e:  # noqa: BLE001
        bad.append(f"project_settings.config: {e}")
        ps = {}
    add("XML / JSON parse", not bad, "; ".join(bad))
    add("printer and filaments kept", ps.get("printer_settings_id", "").startswith("Elegoo")
        and len(ps.get("filament_settings_id", [])) == 3,
        f"{ps.get('printer_settings_id')} / {ps.get('filament_settings_id')}")
    rels = ET.fromstring(z.read("3D/_rels/3dmodel.model.rels"))
    targets = [r.get("Target").lstrip("/") for r in rels]
    add("rels targets exist", all(t in names for t in targets), ", ".join(t for t in targets if t not in names))
    top = ET.fromstring(z.read("3D/3dmodel.model"))
    sub = {t: parse_mesh_file(z.read(t)) for t in targets}
    objs = {}
    comp_bad = []
    for o in top.find("m:resources", NS).findall("m:object", NS):
        comps = []
        for c in o.find("m:components", NS):
            pth = c.get(f"{{{NS['p']}}}path").lstrip("/")
            oid = c.get("objectid")
            if pth not in sub or oid not in sub[pth]:
                comp_bad.append(f"{pth}#{oid}")
                continue
            comps.append((pth, oid, parse_tf(c.get("transform", "1 0 0 0 1 0 0 0 1 0 0 0"))))
        objs[o.get("id")] = comps
    add("components resolve", not comp_bad, ", ".join(comp_bad))
    items = [(it.get("objectid"), parse_tf(it.get("transform", "1 0 0 0 1 0 0 0 1 0 0 0")))
             for it in top.find("m:build", NS).findall("m:item", NS)]
    add("build items reference objects", all(i in objs for i, _ in items))
    ms = ET.fromstring(z.read("Metadata/model_settings.config"))
    so = {o.get("id"): o for o in ms.findall("object")}
    add("model_settings objects match", set(so) == set(objs), f"{sorted(set(so) ^ set(objs))}")
    part_bad = []
    for oid, o in so.items():
        pids = {p.get("id") for p in o.findall("part")}
        cids = {c[1] for c in objs.get(oid, [])}
        if pids != cids:
            part_bad.append(oid)
    add("model_settings parts match components", not part_bad, str(part_bad))
    # instances per object, in build order
    inst_count = {}
    for oid, _ in items:
        inst_count[oid] = inst_count.get(oid, 0) + 1
    plates = ms.findall("plate")
    seen = set()
    pl_bad = []
    for pl in plates:
        for mi in pl.findall("model_instance"):
            d = {m.get("key"): m.get("value") for m in mi.findall("metadata")}
            key = (d.get("object_id"), int(d.get("instance_id", 0)))
            if key[0] not in objs or key[1] >= inst_count.get(key[0], 0) or key in seen:
                pl_bad.append(str(key))
            seen.add(key)
    add("plate instances valid and complete", not pl_bad and len(seen) == len(items),
        f"{len(seen)} assigned of {len(items)}; bad {pl_bad}")
    # layer ranges point at card objects (ordinal) and sit inside them
    lr_bad = []
    if "Metadata/layer_config_ranges.xml" in names:
        lr = ET.fromstring(z.read("Metadata/layer_config_ranges.xml"))
        order = [o.get("id") for o in top.find("m:resources", NS).findall("m:object", NS)]
        for o in lr.findall("object"):
            k = int(o.get("id"))
            if not 1 <= k <= len(order):
                lr_bad.append(f"ordinal {k}")
                continue
            oid = order[k - 1]
            zs = []
            for pth, cid, M in objs[oid]:
                vs, _ = sub[pth][cid]
                zz = vs[:, 2] + M[2, 3]
                zs += [zz.min(), zz.max()]
            for r in o.findall("range"):
                z0, z1 = float(r.get("min_z")), float(r.get("max_z"))
                if not (min(zs) - 1e-3 <= z0 < z1 <= max(zs) + 1e-3):
                    lr_bad.append(f"{oid}: {z0}-{z1} outside {min(zs):.3f}-{max(zs):.3f}")
    add("layer ranges inside their objects", not lr_bad, "; ".join(lr_bad))
    # round trip of every stored mesh
    rt = []
    if expected:
        for e in expected:
            vs, ts = sub[e["path"].lstrip("/")][str(e["id"])]
            m = trimesh.Trimesh(vs, ts, process=False)
            ok = len(ts) == e["triangles"] and abs(m.volume - e["volume"]) < 1e-3 * max(1.0, e["volume"])
            rt.append((e["object"], e["part"], len(ts), e["triangles"], round(float(m.volume), 2),
                       round(e["volume"], 2), ok))
        add("meshes round-trip (triangles, volume)", all(r[-1] for r in rt),
            "; ".join(f"{a}/{b}: {c} tris (src {d}), {e_} mm3 (src {f})" for a, b, c, d, e_, f, _ in rt))
    # instances inside their plate's bed, and not overlapping each other
    bed = C.BED
    n_pl = len(plates)
    inst_world = []
    k_by_obj = {}
    for oid, M in items:
        k = k_by_obj.get(oid, 0)
        k_by_obj[oid] = k + 1
        pts = []
        for pth, cid, Mc in objs[oid]:
            vs, _ = sub[pth][cid]
            pts.append(C.transform_points(M @ Mc, vs))
        inst_world.append((oid, k, np.vstack(pts)))
    plate_of = {}
    for pi, pl in enumerate(plates):
        for mi in pl.findall("model_instance"):
            d = {m.get("key"): m.get("value") for m in mi.findall("metadata")}
            plate_of[(d["object_id"], int(d["instance_id"]))] = pi
    out_bad = []
    boxes = {}
    for oid, k, P in inst_world:
        pi = plate_of.get((oid, k), -1)
        ox, oy = plate_origin(pi, n_pl, bed)
        lo, hi = P.min(0), P.max(0)
        if lo[0] < ox - 1e-3 or hi[0] > ox + bed[0] + 1e-3 or lo[1] < oy - 1e-3 or hi[1] > oy + bed[1] + 1e-3:
            out_bad.append(f"{oid}#{k} plate {pi + 1}")
        if lo[2] < -1e-3 or lo[2] > 1e-3:
            out_bad.append(f"{oid}#{k} not on the bed (z {lo[2]:.3f})")
        boxes.setdefault(pi, []).append((lo, hi, f"{oid}#{k}"))
    overlap = []
    for pi, bl in boxes.items():
        for i in range(len(bl)):
            for j in range(i + 1, len(bl)):
                a, b = bl[i], bl[j]
                if (a[0][0] < b[1][0] and b[0][0] < a[1][0] and a[0][1] < b[1][1] and b[0][1] < a[1][1]):
                    overlap.append(f"{a[2]}/{b[2]}")
    add("instances on their plate, on the bed", not out_bad, "; ".join(out_bad))
    # multi-filament plates get a prime tower: keep its corner free
    try:
        tw_x = [float(v) for v in ps.get("wipe_tower_x", ["214"])]
        tw_y = [float(v) for v in ps.get("wipe_tower_y", ["200"])]
        tw_w = float(ps.get("prime_tower_width", "35"))
    except (TypeError, ValueError):
        tw_x, tw_y, tw_w = [214.0], [200.0], 35.0
    ext_of = {}
    for oid, o in so.items():
        exts = {m.get("value") for m in o.iter("metadata") if m.get("key") == "extruder"}
        ext_of[oid] = exts
    tower_bad = []
    for pi in boxes:
        multi = set().union(*[ext_of.get(b[2].split("#")[0], set()) for b in boxes[pi]])
        if len(multi) < 2:
            continue
        ox, oy = plate_origin(pi, n_pl, bed)
        x0 = ox + tw_x[min(pi, len(tw_x) - 1)] - 3
        y0 = oy + tw_y[min(pi, len(tw_y) - 1)] - 3
        x1, y1 = x0 + tw_w + 6, y0 + tw_w + 6
        for lo, hi, name in boxes[pi]:
            if lo[0] < x1 and hi[0] > x0 and lo[1] < y1 and hi[1] > y0:
                tower_bad.append(f"{name} on plate {pi + 1}")
    add("prime tower corner free (multi-filament plates)", not tower_bad, "; ".join(tower_bad))
    add("no overlapping bounding boxes", not overlap, "; ".join(overlap))
    # PrusaSlicer: flatten each plate to a plain 3MF and load it
    exe = shutil.which("prusa-slicer")
    if exe:
        work = work or tempfile.mkdtemp(prefix="3mf_")
        for pi in range(n_pl):
            ox, oy = plate_origin(pi, n_pl, bed)
            meshes = []
            for (oid, k, _), (oid2, M) in zip(inst_world, items):
                if plate_of.get((oid, k)) != pi:
                    continue
                parts = []
                for pth, cid, Mc in objs[oid]:
                    vs, ts = sub[pth][cid]
                    parts.append(trimesh.Trimesh(C.transform_points(trans(-ox, -oy, 0) @ M @ Mc, vs), ts, process=False))
                meshes.append(trimesh.util.concatenate(parts))
            flat = os.path.join(work, f"plate_{pi + 1}_flat.3mf")
            write_plain_3mf(flat, meshes)
            r = subprocess.run([exe, "--info", flat], capture_output=True, text=True, timeout=600)
            info = r.stdout
            vols = [float(v) for v in re.findall(r"volume\s*=\s*([\d.]+)", info)]
            okl = r.returncode == 0 and "Loading of a model file failed" not in (r.stdout + r.stderr)
            detail = f"{len(meshes)} objects, PrusaSlicer volumes {[round(v, 1) for v in vols]}"
            if slice_check and okl:
                g = os.path.join(work, f"plate_{pi + 1}.gcode")
                r2 = subprocess.run([exe, "--export-gcode", "--dont-arrange", "--load",
                                     C.write_ini(C.prusa_profile("plaque"), os.path.join(work, "p.ini")),
                                     "--output", g, flat], capture_output=True, text=True, timeout=1800)
                okl = okl and r2.returncode == 0 and os.path.exists(g)
                detail += "; sliced" if okl else "; slicing failed: " + (r2.stdout + r2.stderr)[-300:]
            add(f"PrusaSlicer loads plate {pi + 1} (flattened)", okl, detail)
    res["overall"] = "PASS" if all(c["status"] == "PASS" for c in res["checks"]) else "FAIL"
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="3MF to write")
    ap.add_argument("--template", default=TEMPLATE, help="the user's OrcaSlicer 3MF (or its unzipped folder)")
    ap.add_argument("--card", action="append", help='"Name=base.stl,gold.stl" (repeatable)')
    ap.add_argument("--card-dir", help="folder with plaque_<name>_base.stl / plaque_<name>_gold.stl pairs")
    ap.add_argument("--card-suffix", default="", help='appended to card object names, e.g. " (test card)"')
    ap.add_argument("--tree", help="tree STL")
    ap.add_argument("--trees", type=int, default=0)
    ap.add_argument("--foot", help="foot STL")
    ap.add_argument("--feet", type=int, default=0)
    ap.add_argument("--tree-layer", type=float, default=0.1, help="tree layer height (mm)")
    ap.add_argument("--gold-layer", type=float, default=0.1, help="layer height in the card's gold Z range")
    ap.add_argument("--foot-infill", default="100%",
                    help='feet sparse infill, rectilinear (default 100%%: a heavy foot keeps the card from tipping '
                         'backwards); "template" keeps the test print\'s 15%% lightning')
    ap.add_argument("--no-instances", action="store_true", help="one object (and mesh copy) per tree / foot")
    ap.add_argument("--gap", type=float, default=4.0, help="gap between cards / trees (mm)")
    ap.add_argument("--foot-gap", type=float, default=4.0, help="gap between feet (mm)")
    ap.add_argument("--margin", type=float, default=3.0, help="keep this far from the bed edge (mm)")
    ap.add_argument("--rotate", default="auto", choices=["auto", "0", "90"], help="turn parts to fit more per plate")
    ap.add_argument("--max-per-plate", type=int, default=0)
    ap.add_argument("--plate-prefix", default="Plate")
    ap.add_argument("--title", default="Wedding place cards")
    ap.add_argument("--slice-check", action="store_true", help="also slice every flattened plate with PrusaSlicer")
    ap.add_argument("--validate-only", help="only validate this 3MF")
    ap.add_argument("--json", help="write the summary + validation here (default: <out>.json)")
    args = ap.parse_args()

    if args.validate_only:
        res = validate(args.validate_only, slice_check=args.slice_check)
        print_validation(res)
        sys.exit(0 if res["overall"] == "PASS" else 1)
    if not args.out:
        ap.error("--out is required")
    summary = build(args)
    work = tempfile.mkdtemp(prefix="3mf_check_")
    res = validate(args.out, summary["meshes"], args.slice_check, work)
    summary["validation"] = res
    js = args.json or os.path.splitext(args.out)[0] + ".json"
    with open(js, "w") as f:
        json.dump(summary, f, indent=1)
    print(f"wrote {args.out} ({summary['bytes'] / 1e6:.1f} MB): {summary['objects']} objects, "
          f"{summary['instances']} instances, {len(summary['plates'])} plates")
    for p in summary["plates"]:
        print(f"  {p['name']}: " + ", ".join(f"{o['name']} @ ({o['x']}, {o['y']}){' rot 90' if o['rot'] else ''}"
                                            for o in p["objects"]))
    print_validation(res)
    print("summary:", js)
    sys.exit(0 if res["overall"] == "PASS" else 1)


def print_validation(res):
    w = max(len(c["check"]) for c in res["checks"])
    for c in res["checks"]:
        d = c["detail"]
        print(f"  {c['status']:<5} {c['check']:<{w}}  {d[:240]}")
    print(f"VALIDATION: {res['overall']}")


if __name__ == "__main__":
    main()
