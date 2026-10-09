#!/usr/bin/env python3
"""Add cards to one plate of an existing project (for example one the user edited and re-saved in
ElegooSlicer), re-nesting that plate so everything fits, and leave the rest of the file alone.

  python3 tools/add_cards.py --in "their project.3mf" --plate 4 \\
      --card "Blaize=stl/all/plaque_Blaize_base.stl,stl/all/plaque_Blaize_gold.stl" \\
      --card "Katya=stl/all/plaque_Katya_base.stl,stl/all/plaque_Katya_gold.stl" \\
      --remove Alex --remove Melody --card "Mélody=stl/all/plaque_Mélody_base.stl,stl/all/plaque_Mélody_gold.stl" \\
      --out "print/their project - with Blaize and Katya.3mf"

What changes:
  * --remove NAME: that card leaves the project entirely, from whatever plate it was on (object,
    build item, mesh file, rels, settings, plate instance, assemble item, layer ranges; the layer
    ranges of later objects move up one position, since the file counts objects by position);
  * new cards: their object files, resources, build items, rels, model_settings objects (object and
    part settings copied from a card already on that plate, so the user's own tweaks carry over),
    layer ranges (copied from that card too), plate instances and assemble items;
  * the cards already on the plate: moved / turned as whole objects (their build item transform
    only; meshes and components untouched), with the same nesting and 4 mm gap as make_3mf.py;
  * the plate's name ("... - 14 cards") and, only if the nesting needs another corner, its prime
    tower position.
Every other zip entry is copied byte for byte. Plate thumbnails stay as they are until the next
slice in the slicer.
"""
import argparse
import json
import os
import re
import sys
import types
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import trimesh

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_common as C  # noqa: E402
import make_3mf as M  # noqa: E402

NS = M.NS
P_NS = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
CARD_PARTS = ("Ivory plaque", "Gold name + border")


def attr_re(tag, key, value):
    return re.compile(r'<%s\b[^>]*\b%s="%s"[^>]*/?>' % (tag, key, re.escape(value)))


def read_zip(path):
    z = zipfile.ZipFile(path)
    return [(i, z.read(i.filename)) for i in z.infolist()]


def ms_objects(ms_root):
    """model_settings objects: id -> (name, object metadata [(k, v)], parts [(id, name, [(k, v)])])."""
    out = {}
    for o in ms_root.findall("object"):
        meta = [(m.get("key"), m.get("value")) for m in o.findall("metadata")]
        parts = []
        for p in o.findall("part"):
            pm = [(m.get("key"), m.get("value")) for m in p.findall("metadata")]
            parts.append((p.get("id"), dict(pm).get("name"), pm))
        out[o.get("id")] = (dict(meta).get("name"), meta, parts)
    return out


def plates_of(ms_root):
    """[(plater_id, name, [(object_id, instance_id, identify_id)])] in file order."""
    out = []
    for p in ms_root.findall("plate"):
        md = {m.get("key"): m.get("value") for m in p.findall("metadata")}
        inst = []
        for mi in p.findall("model_instance"):
            d = {m.get("key"): m.get("value") for m in mi.findall("metadata")}
            inst.append((d.get("object_id"), d.get("instance_id"), d.get("identify_id")))
        out.append((int(md["plater_id"]), md.get("plater_name", ""), inst))
    return out


def world_parts(model_root, files, oid, item_M):
    """The object's parts as trimeshes in world mm: [(part mesh id, mesh)]."""
    obj = next(o for o in model_root.find("m:resources", NS).findall("m:object", NS) if o.get("id") == oid)
    out = []
    cache = {}
    for c in obj.find("m:components", NS).findall("m:component", NS):
        path = c.get(P_NS + "path").lstrip("/")
        if path not in cache:
            cache[path] = M.parse_mesh_file(files[path])
        v, f = cache[path][c.get("objectid")]
        T = item_M @ M.parse_tf(c.get("transform"))
        vw = (np.c_[v, np.ones(len(v))] @ T.T)[:, :3]
        out.append((c.get("objectid"), trimesh.Trimesh(vw, f, process=False)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--plate", type=int, required=True, help="plate number as shown in the slicer (1 = first)")
    ap.add_argument("--card", action="append", default=[], help="NAME=base.stl,gold.stl (a new card for --plate)")
    ap.add_argument("--remove", action="append", default=[],
                    help="exact name of a card to take out of the project, from whatever plate it is on")
    ap.add_argument("--gap", type=float, default=4.0, help="gap between cards (mm), as make_3mf.py")
    ap.add_argument("--margin", type=float, default=3.0, help="keep this far from the bed edge (mm)")
    args = ap.parse_args(argv)

    entries = read_zip(args.inp)
    files = {i.filename: d for i, d in entries}
    model_txt = files["3D/3dmodel.model"].decode("utf-8")
    ms_txt = files["Metadata/model_settings.config"].decode("utf-8")
    rels_txt = files["3D/_rels/3dmodel.model.rels"].decode("utf-8")
    lr_txt = files["Metadata/layer_config_ranges.xml"].decode("utf-8")
    ps = json.loads(files["Metadata/project_settings.config"])
    bed = C.BED

    # ---- cards to take out: every trace of the object; the layer ranges of the objects after it move
    # up one position (layer_config_ranges.xml counts objects by position, not by id)
    touched = {args.plate}
    if args.remove:
        root0, ms0 = ET.fromstring(model_txt), ET.fromstring(ms_txt)
        objs0 = ms_objects(ms0)
        res0 = [o.get("id") for o in root0.find("m:resources", NS).findall("m:object", NS)]
        drop = []
        for nm in args.remove:
            hit = [oid for oid, (n, _, parts) in objs0.items() if n == nm and tuple(p[1] for p in parts) == CARD_PARTS]
            if len(hit) != 1:
                raise SystemExit(f"--remove {nm!r}: {len(hit)} cards with that exact name")
            drop.append(hit[0])
        touched |= {pl[0] for pl in plates_of(ms0) if any(o in drop for o, _, _ in pl[2])}
        users = {}
        for o in root0.find("m:resources", NS).findall("m:object", NS):
            for c in o.find("m:components", NS).findall("m:component", NS):
                users.setdefault(c.get(P_NS + "path"), set()).add(o.get("id"))
        dead = [path for path, u in users.items() if u <= set(drop)]

        def cut(txt, pattern, n=1):
            out, k = re.subn(pattern, "", txt, flags=re.S)
            if (n is not None and k != n) or k == 0:
                raise SystemExit(f"removal: {k} matches for {pattern!r}")
            return out
        for oid in drop:
            model_txt = cut(model_txt, r'[ \t]*<object id="%s" [^>]*>.*?</object>\n' % oid)
            model_txt = cut(model_txt, r'[ \t]*<item objectid="%s" [^>]*/>\n' % oid)
            ms_txt = cut(ms_txt, r'[ \t]*<object id="%s">.*?</object>\n' % oid)
            ms_txt = cut(ms_txt, r'[ \t]*<model_instance>\s*<metadata key="object_id" value="%s"/>.*?</model_instance>\n'
                         % oid, None)
            if "</assemble>" in ms_txt:
                ms_txt = cut(ms_txt, r'[ \t]*<assemble_item object_id="%s" [^>]*/>\n' % oid, None)
        for path in dead:
            rels_txt = cut(rels_txt, r'[ \t]*<Relationship Target="%s" [^>]*/>\n' % re.escape(path))
        keep = [o for o in res0 if o not in drop]
        newpos = {res0.index(o) + 1: keep.index(o) + 1 for o in keep}

        def renum(m):
            old = int(m.group(2))
            return "" if old not in newpos else f'{m.group(1)}<object id="{newpos[old]}">{m.group(3)}</object>{m.group(4)}'
        lr_txt = re.sub(r'([ \t]*)<object id="(\d+)">(.*?)</object>(\n?)', renum, lr_txt, flags=re.S)
        entries = [(zi, d) for zi, d in entries if "/" + zi.filename not in dead]
        files = {i.filename: d for i, d in entries}
        print("removed: " + ", ".join(f"{objs0[o][0]} (object {o})" for o in drop)
              + f"; mesh files dropped: {[d.split('/')[-1] for d in dead]}")

    model_root = ET.fromstring(model_txt)
    ms_root = ET.fromstring(ms_txt)
    lr_root = ET.fromstring(lr_txt)

    objs = ms_objects(ms_root)
    plates = plates_of(ms_root)
    n_plates = len(plates)
    pidx = next((i for i, p in enumerate(plates) if p[0] == args.plate), None)
    if pidx is None:
        raise SystemExit(f"no plate {args.plate} (plates: {[p[0] for p in plates]})")
    ox, oy = M.plate_origin(pidx, n_plates, bed)
    _, plate_name, plate_inst = plates[pidx]

    # build items, in file order (their order is also the objects' order for the layer ranges)
    res_ids = [o.get("id") for o in model_root.find("m:resources", NS).findall("m:object", NS)]
    items_xml = model_root.find("m:build", NS).findall("m:item", NS)
    build = {}
    for it in items_xml:
        build.setdefault(it.get("objectid"), []).append(M.parse_tf(it.get("transform")))

    # the cards already on the plate (one instance each)
    on_plate = []
    for oid, iid, _ in plate_inst:
        name, _, parts = objs[oid]
        if tuple(p[1] for p in parts) != CARD_PARTS or len(build[oid]) != 1 or iid != "0":
            raise SystemExit(f"plate {args.plate} holds {name!r}, which is not a single card: not supported")
        on_plate.append(oid)
    if not on_plate:
        raise SystemExit("the plate has no card to copy settings from")

    # sanity: every card on the plate really sits on that plate's bed
    to_local = M.trans(-ox, -oy, 0)
    items = []
    for oid in on_plate:
        wp = dict(world_parts(model_root, files, oid, build[oid][0]))
        parts = []
        for pid, pname, _ in objs[oid][2]:                # part id = the component's mesh object id
            mesh = wp[pid]
            mesh.apply_transform(to_local)
            parts.append((pname, mesh, [], ""))
        it = M.Item(f"old:{oid}", objs[oid][0], "card", parts, [], [1, 2])
        if it.lo[0] < -1 or it.lo[1] < -1 or it.hi[0] > bed[0] + 1 or it.hi[1] > bed[1] + 1:
            raise SystemExit(f"{it.name} is not inside plate {args.plate} ({it.lo[:2]} .. {it.hi[:2]}): "
                             "plate layout not understood")
        it.oid = oid
        items.append(it)
    new_items = []
    for spec in args.card:
        name, _, fs = spec.partition("=")
        base, gold = [f.strip() for f in fs.split(",")]
        parts = [(CARD_PARTS[0], C.load_mesh(base), [], base), (CARD_PARTS[1], C.load_mesh(gold), [], gold)]
        it = M.Item(f"new:{name}", name, "card", parts, [], [1, 2])
        it.oid = None
        new_items.append(it)
    items += new_items

    # nest the whole plate again, exactly like make_3mf.py does for card plates
    nargs = types.SimpleNamespace(gap=args.gap, foot_gap=3.0, tree_gap=None, margin=args.margin,
                                  max_per_plate=0, max_feet_per_plate=0, plate_prefix="Plate")
    nested, towers = M.nest_plates(items, nargs, bed, ps)
    if len(nested) != 1 or len(nested[0][1]) != len(items):
        raise SystemExit(f"{len(items)} cards do not fit on one plate with a {args.gap} mm gap")
    placed = nested[0][1]
    tower = towers.get(0)

    # ---- the cards already there: move each object as a whole (build item transform only)
    for it, x, y, rot in placed:
        if it.oid is None:
            continue
        cx, cy = (it.lo[0] + it.hi[0]) / 2, (it.lo[1] + it.hi[1]) / 2
        D = M.trans(ox + x, oy + y, 0) @ M.rotz(rot) @ M.trans(-(ox + cx), -(oy + cy), 0)
        new_M = D @ build[it.oid][0]
        pat = attr_re("item", "objectid", it.oid)
        hits = pat.findall(model_txt)
        if len(hits) != 1:
            raise SystemExit(f"build item of object {it.oid} not found once")
        line = hits[0]
        model_txt = model_txt.replace(line, re.sub(r'transform="[^"]*"', f'transform="{M.tf(new_M)}"', line), 1)

    # ---- new cards
    all_ids = [int(v) for v in re.findall(r'\b(?:id|objectid|object_id)="(\d+)"', model_txt + ms_txt)]
    next_id = max(all_ids) + 1
    suffixes = [int(m) for m in re.findall(r'/3D/Objects/[^"]*_(\d+)\.model"', model_txt)]
    next_file = max(suffixes + [0]) + 1
    identify = max(int(v) for v in re.findall(r'identify_id" value="(\d+)"', ms_txt)) + 1
    rel_ids = [int(v) for v in re.findall(r'Id="rel-(\d+)"', rels_txt)]
    next_rel = max(rel_ids + [0]) + 1
    uuids = set(re.findall(r'UUID="([^"]+)"', model_txt))

    ref_oid = on_plate[-1]
    ref_name, ref_meta, ref_parts = objs[ref_oid]

    def settings(oid):
        _, meta, parts = objs[oid]
        return ([kv for kv in meta if kv[0] != "name"],
                [(n, [kv for kv in pm if kv[0] not in skip_part]) for _, n, pm in parts])
    skip_part = {"name", "matrix", "source_file", "source_object_id", "source_volume_id",
                 "source_offset_x", "source_offset_y", "source_offset_z"}
    cards = [oid for oid, (_, _, parts) in objs.items() if tuple(p[1] for p in parts) == CARD_PARTS]
    odd = [objs[oid][0] for oid in cards if settings(oid) != settings(ref_oid)]
    print(f"settings copied from {ref_name}; cards with other settings: {odd or 'none'} (of {len(cards)})")
    ref_pos = res_ids.index(ref_oid) + 1
    ref_ranges = next((o for o in lr_root.findall("object") if o.get("id") == str(ref_pos)), None)
    ref_ranges_txt = None
    if ref_ranges is not None:
        m = re.search(r'( *)<object id="%d">.*?</object>\n?' % ref_pos, lr_txt, re.S)
        ref_ranges_txt = m.group(0)

    def new_uuid(a, b):
        u = f"{a:08x}-{b:04x}-4c03-9d28-80fed5dfa1dc"
        while u in uuids:
            b += 1
            u = f"{a:08x}-{b:04x}-4c03-9d28-80fed5dfa1dc"
        uuids.add(u)
        return u

    add_res, add_build, add_rels, add_ms, add_inst, add_asm, add_lr = [], [], [], [], [], [], []
    new_files = {}
    pos = len(res_ids)
    for it, x, y, rot in placed:
        if it.oid is not None:
            continue
        safe = re.sub(r"[^\w-]+", "_", it.name).strip("_") or "card"
        path = f"/3D/Objects/{safe}_{next_file}.model"
        next_file += 1
        cx, cy = (it.lo[0] + it.hi[0]) / 2, (it.lo[1] + it.hi[1]) / 2
        body, comps, ms_parts = [], [], []
        place = M.trans(x, y, 0) @ M.rotz(rot)
        for k, (pname, mesh, _, src) in enumerate(it.parts):
            zc = (mesh.bounds[0, 2] + mesh.bounds[1, 2]) / 2
            pid = next_id
            next_id += 1
            body.append(M.mesh_xml(pid, mesh, np.array([cx, cy, zc]), new_uuid(0x00500000 + pid, 0)))
            Mc = place @ M.trans(0, 0, zc - it.lo[2])
            comps.append(f'    <component p:path="{path}" objectid="{pid}" p:UUID="{new_uuid(0x00600000 + pid, 0)}" '
                         f'transform="{M.tf(Mc)}"/>')
            pmeta = next(pm for _, n, pm in ref_parts if n == pname)
            lines = [f'    <part id="{pid}" subtype="normal_part">',
                     f'      <metadata key="name" value="{M.xml_esc(pname)}"/>',
                     f'      <metadata key="matrix" value="{M.matrix_meta(Mc)}"/>',
                     f'      <metadata key="source_file" value="{M.xml_esc(os.path.basename(src))}"/>',
                     f'      <metadata key="source_object_id" value="{pos}"/>',
                     '      <metadata key="source_volume_id" value="0"/>',
                     f'      <metadata key="source_offset_x" value="{M.fmt(cx)}"/>',
                     f'      <metadata key="source_offset_y" value="{M.fmt(cy)}"/>',
                     f'      <metadata key="source_offset_z" value="{M.fmt(zc)}"/>']
            lines += [f'      <metadata key="{kk}" value="{M.xml_esc(vv)}"/>' for kk, vv in pmeta if kk not in skip_part]
            lines += ['      <mesh_stat edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" '
                      'backwards_edges="0"/>', "    </part>"]
            ms_parts += lines
        oid = next_id
        next_id += 1
        pos += 1
        new_files[path.lstrip("/")] = (M.MODEL_HEAD + ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
                                       + "".join(body) + " </resources>\n <build/>\n</model>\n").encode()
        add_res.append(f'  <object id="{oid}" p:UUID="{new_uuid(0x00700000 + oid, 0)}" type="model">\n   <components>\n'
                       + "\n".join(comps) + "\n   </components>\n  </object>\n")
        add_build.append(f'  <item objectid="{oid}" p:UUID="{new_uuid(0x00800000 + oid, 0)}" '
                         f'transform="{M.tf(M.trans(ox, oy, 0))}" printable="1" auto_drop="1"/>\n')
        add_rels.append(f' <Relationship Target="{path}" Id="rel-{next_rel}" '
                        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n')
        next_rel += 1
        add_ms.append("\n".join([f'  <object id="{oid}">', f'    <metadata key="name" value="{M.xml_esc(it.name)}"/>']
                                + [f'    <metadata key="{k}" value="{M.xml_esc(v)}"/>' for k, v in ref_meta if k != "name"]
                                + ms_parts + ["  </object>"]) + "\n")
        add_inst.append("    <model_instance>\n"
                        f'      <metadata key="object_id" value="{oid}"/>\n'
                        '      <metadata key="instance_id" value="0"/>\n'
                        f'      <metadata key="identify_id" value="{identify}"/>\n'
                        "    </model_instance>\n")
        identify += 1
        add_asm.append(f'   <assemble_item object_id="{oid}" instance_id="0" '
                       f'transform="1 0 0 0 1 0 0 0 1 {M.fmt(ox)} {M.fmt(oy)} 0" offset="0 0 0" />\n')
        if ref_ranges_txt:
            add_lr.append(re.sub(r'<object id="\d+">', f'<object id="{pos}">', ref_ranges_txt, count=1))
        it.oid = str(oid)

    def insert_before(txt, marker, add, last=False):
        i = txt.rfind(marker) if last else txt.find(marker)
        if i < 0:
            raise SystemExit(f"marker {marker!r} not found")
        j = txt.rfind("\n", 0, i) + 1                    # at the start of the marker's line
        return txt[:j] + "".join(add) + txt[j:]

    model_txt = insert_before(model_txt, "</resources>", add_res, last=True)
    model_txt = insert_before(model_txt, "</build>", add_build, last=True)
    rels_txt = insert_before(rels_txt, "</Relationships>", add_rels, last=True)
    ms_txt = insert_before(ms_txt, "<plate>", add_ms)
    # the plate: new instances after its last one, and its name
    pm = re.search(r'<plate>\s*<metadata key="plater_id" value="%d"/>.*?</plate>' % args.plate, ms_txt, re.S)
    block = pm.group(0)
    nb = insert_before(block, "</plate>", add_inst, last=True) if add_inst else block
    ms_txt = ms_txt[:pm.start()] + nb + ms_txt[pm.end():]
    # plate names that count cards ("Plate 2 - 15 cards"): recount the plates that changed
    for pl in sorted(touched):
        pm = re.search(r'<plate>\s*<metadata key="plater_id" value="%d"/>.*?</plate>' % pl, ms_txt, re.S)
        block = pm.group(0)
        count = block.count("<model_instance>")
        nb = re.sub(r'(<metadata key="plater_name" value=")([^"]*?)(\d+)( cards")',
                    lambda m: f"{m.group(1)}{m.group(2)}{count}{m.group(4)}", block)
        ms_txt = ms_txt[:pm.start()] + nb + ms_txt[pm.end():]
    if "</assemble>" in ms_txt:
        ms_txt = insert_before(ms_txt, "</assemble>", add_asm, last=True)
    if add_lr:
        lr_txt = insert_before(lr_txt, "</objects>", add_lr, last=True)

    changed = {"3D/3dmodel.model": model_txt.encode("utf-8"),
               "3D/_rels/3dmodel.model.rels": rels_txt.encode("utf-8"),
               "Metadata/model_settings.config": ms_txt.encode("utf-8"),
               "Metadata/layer_config_ranges.xml": lr_txt.encode("utf-8")}
    cur = (ps.get("wipe_tower_x") or [None] * n_plates)[pidx], (ps.get("wipe_tower_y") or [None] * n_plates)[pidx]
    moved_tower = False
    if tower and cur[0] is not None and (abs(float(cur[0]) - tower[0]) > 0.5 or abs(float(cur[1]) - tower[1]) > 0.5):
        ps["wipe_tower_x"][pidx], ps["wipe_tower_y"][pidx] = f"{tower[0]:g}", f"{tower[1]:g}"
        changed["Metadata/project_settings.config"] = json.dumps(ps, indent=4).encode()
        moved_tower = True

    # ---- write: same entries, same order and compression; new object files after the last object file
    last_obj = max(i for i, (zi, _) in enumerate(entries) if zi.filename.startswith("3D/Objects/"))
    tmp = args.out + ".part"
    with zipfile.ZipFile(tmp, "w") as zout:
        for i, (zi, data) in enumerate(entries):
            zout.writestr(zi, changed.get(zi.filename, data))
            if i == last_obj:
                for path, data2 in new_files.items():
                    nzi = zipfile.ZipInfo(path, date_time=zi.date_time)
                    nzi.compress_type = zi.compress_type
                    zout.writestr(nzi, data2)
    os.replace(tmp, args.out)

    print(f"plate {args.plate} ({plate_name} -> {len(placed)} cards), tower "
          f"{'moved to %s' % (tower,) if moved_tower else 'kept at (%s, %s)' % cur}:")
    for it, x, y, rot in sorted(placed, key=lambda p: (-p[2], p[1])):
        print(f"  {'NEW ' if it.key.startswith('new:') else '    '}{it.name:16s} x {x:7.2f}  y {y:7.2f}  turned {rot:g}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
