#!/usr/bin/env python3
"""Print-time lab: change settings in an OrcaSlicer project (3MF), slice one plate with the real
OrcaSlicer CLI, and report Orca's own time estimate with a split per feature and per layer band.

The 3MF is not modified; a variant is written next to the results. Settings are changed where Orca
reads them for that part: per object, per part (volume), per height range, or project-wide.

  python3 tools/orca_lab.py --in "print/Place cards - TEST tree.3mf" --plate 2 --label base
  python3 tools/orca_lab.py --in X.3mf --plate 2 --label fast \\
      --part "tree:Gold tree:inner_wall_speed=120" --part "tree::internal_solid_infill_speed=200" \\
      --range "tree:0:1.4:layer_height=0.2" --project "z_hop=0.2"

Selectors: KIND is tree / card / foot (by object name: "Tree...", "Foot...", anything else is a
card). --part KIND:PARTNAME:key=value (PARTNAME is a substring of the part name, empty = every
part), --obj KIND:key=value, --range KIND:z0:z1:key=value[,key=value] (a height range modifier;
layer_height defaults to the object's), --clear-ranges KIND, --project key=value (a list value is
set for every filament / extruder slot; key=a|b|c sets one value per slot), --pause-z Z (moves the feet pause; "keep" by default).

Needs the OrcaSlicer 2.4.2 AppImage extracted at /opt/orca/squashfs-root (or --orca) and xvfb-run.
Prints one line per run and writes <out-dir>/<label>/result.json.
"""
import argparse
import collections
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

ORCA = "/opt/orca/squashfs-root/AppRun"


def kind_of(name):
    return "tree" if name.startswith("Tree") else "foot" if name.startswith("Foot") else "card"


def parse_kv(s):
    k, v = s.split("=", 1)
    return k.strip(), v.strip()


def patch_3mf(src, dst, args):
    zin = zipfile.ZipFile(src)
    files = {n: zin.read(n) for n in zin.namelist()}
    ms = ET.fromstring(files["Metadata/model_settings.config"])
    # keyed by the object's 1-based position: that is how layer_config_ranges.xml refers to objects
    objs = {}
    for i, o in enumerate(ms.findall("object")):
        md = {m.get("key"): m for m in o.findall("metadata")}
        objs[str(i + 1)] = (kind_of(md["name"].get("value")), o)

    def set_meta(el, key, val):
        for m in el.findall("metadata"):
            if m.get("key") == key:
                m.set("value", val)
                return
        new = ET.Element("metadata", {"key": key, "value": val})
        # metadata must come before the parts
        kids = list(el)
        pos = next((i for i, k in enumerate(kids) if k.tag != "metadata"), len(kids))
        el.insert(pos, new)

    for spec in args.obj or []:
        kind, kv = spec.split(":", 1)
        k, v = parse_kv(kv)
        for _, (kd, o) in objs.items():
            if kd == kind:
                set_meta(o, k, v)
    for spec in args.part or []:
        kind, pname, kv = spec.split(":", 2)
        k, v = parse_kv(kv)
        hit = 0
        for _, (kd, o) in objs.items():
            if kd != kind:
                continue
            for p in o.findall("part"):
                nm = next((m.get("value") for m in p.findall("metadata") if m.get("key") == "name"), "")
                if pname in nm:
                    set_meta(p, k, v)
                    hit += 1
        if not hit:
            raise SystemExit(f"--part {spec}: no part matched")
    files["Metadata/model_settings.config"] = ET.tostring(ms, encoding="utf-8", xml_declaration=True)

    # height range modifiers
    if args.range or args.clear_ranges:
        lr_raw = files.get("Metadata/layer_config_ranges.xml")
        lr = ET.fromstring(lr_raw) if lr_raw else ET.Element("objects")
        for kind in args.clear_ranges or []:
            for oel in list(lr.findall("object")):
                if objs.get(oel.get("id"), (None,))[0] == kind:
                    lr.remove(oel)
        for spec in args.range or []:
            kind, z0, z1, kvs = spec.split(":", 3)
            opts = dict(parse_kv(x) for x in kvs.split(",") if x)
            for oid, (kd, o) in objs.items():
                if kd != kind:
                    continue
                oel = next((x for x in lr.findall("object") if x.get("id") == oid), None)
                if oel is None:
                    oel = ET.SubElement(lr, "object", {"id": oid})
                lh = opts.get("layer_height") or next(
                    (m.get("value") for m in o.findall("metadata") if m.get("key") == "layer_height"), "0.2")
                r = ET.SubElement(oel, "range", {"min_z": z0, "max_z": z1})
                o1 = ET.SubElement(r, "option", {"opt_key": "layer_height"})
                o1.text = lh
                for k, v in opts.items():
                    if k == "layer_height":
                        continue
                    e = ET.SubElement(r, "option", {"opt_key": k})
                    e.text = v
        # keep each object's ranges sorted
        for oel in lr.findall("object"):
            rs = sorted(oel.findall("range"), key=lambda r: float(r.get("min_z")))
            for r in list(oel):
                oel.remove(r)
            for r in rs:
                oel.append(r)
        files["Metadata/layer_config_ranges.xml"] = ET.tostring(lr, encoding="utf-8", xml_declaration=True)

    # project settings
    if args.project:
        ps = json.loads(files["Metadata/project_settings.config"])
        for spec in args.project:
            k, v = parse_kv(spec)
            if k not in ps:
                print(f"  note: {k} is not in the project settings; adding it", file=sys.stderr)
            if "|" in v:                                   # one value per filament / extruder slot
                ps[k] = v.split("|")
            elif isinstance(ps.get(k), list):
                ps[k] = [v] * len(ps[k])
            else:
                ps[k] = v
        files["Metadata/project_settings.config"] = json.dumps(ps, indent=4).encode()

    if args.pause_z and args.pause_z != "keep" and "Metadata/custom_gcode_per_layer.xml" in files:
        s = files["Metadata/custom_gcode_per_layer.xml"].decode()
        s = re.sub(r'top_z="[0-9.]+"', f'top_z="{float(args.pause_z):g}"', s)
        files["Metadata/custom_gcode_per_layer.xml"] = s.encode()

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, b in files.items():
            zout.writestr(n, b)


def orca_time(line):
    s = line.split("=", 1)[1]
    t = 0
    for n, u in re.findall(r"(\d+)([dhms])", s):
        t += int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
    return t


def gcode_stats(path):
    """Orca's total, plus a rough trapezoid estimate per feature / layer band / retraction, scaled
    to Orca's total (so the split adds up to Orca's number)."""
    t = collections.Counter()
    band = collections.Counter()
    ext = collections.Counter()
    n_retr = 0
    x = y = z = 0.0
    F = 1500.0
    acc = 10000.0
    typ = "start"
    prev = None
    prev_v = 0.0
    total = None
    layers = []
    pauses = []
    cur_z = 0.0
    for line in open(path, errors="ignore"):
        if line.startswith(";TYPE:"):
            typ = line[6:].strip()
            continue
        if line.startswith(";Z:"):
            cur_z = float(line[3:])
            layers.append(cur_z)
            continue
        if line.startswith("; estimated printing time (normal mode)"):
            total = orca_time(line)
            continue
        if line.startswith(";PAUSE_PRINT") or line.startswith("; PAUSE_PRINT"):
            pauses.append(cur_z)
        if not line or line[0] not in "GM":
            continue
        c = line.split(";")[0].split()
        if not c:
            continue
        if c[0] == "M204":
            for w in c[1:]:
                if w[0] == "S":
                    acc = float(w[1:])
            continue
        if c[0] not in ("G0", "G1", "G2", "G3"):
            continue
        p = {}
        for w in c[1:]:
            if w[0] in "XYZEF":
                try:
                    p[w[0]] = float(w[1:])
                except ValueError:
                    pass
        if "F" in p:
            F = p["F"]
        nx, ny, nz = p.get("X", x), p.get("Y", y), p.get("Z", z)
        dx, dy, dz = nx - x, ny - y, nz - z
        d = math.hypot(dx, dy)
        e = p.get("E", 0.0)
        v = max(F / 60.0, 1e-3)
        bkey = f"z {math.floor(cur_z * 2) / 2:.1f}-{math.floor(cur_z * 2) / 2 + 0.5:.1f}"
        if d < 1e-9 and abs(dz) < 1e-9:
            if e:
                tt = abs(e) / v
                t["retract / unretract"] += tt
                band[bkey] += tt
                if e < 0:
                    n_retr += 1
            continue
        if d < 1e-9:
            tt = abs(dz) / min(v, 20) + 2 * math.sqrt(abs(dz) / 500)
            t["z moves (lift, layer change)"] += tt
            band[bkey] += tt
            z = nz
            continue
        kind = typ if e > 0 else "travel"
        ux, uy = dx / d, dy / d
        if prev is not None:
            cosang = ux * prev[0] + uy * prev[1]
            vj = min(v, prev_v) * max(0.0, (1 + cosang) / 2) ** 2 + 4.5
        else:
            vj = 0.0
        vj = min(vj, v)
        da = (v * v - vj * vj) / (2 * acc)
        if 2 * da > d:
            vp = math.sqrt(vj * vj + acc * d)
            tt = 2 * (vp - vj) / acc
        else:
            tt = 2 * (v - vj) / acc + (d - 2 * da) / v
        t[kind] += tt
        band[bkey] += tt
        if e > 0:
            ext[kind] += d
        prev = (ux, uy)
        prev_v = v
        x, y, z = nx, ny, nz
    est = sum(t.values()) or 1
    k = (total / est) if total else 1.0
    feat = {kd: round(tt * k / 60, 1) for kd, tt in t.most_common()}
    bands = {b: round(tt * k / 60, 1) for b, tt in sorted(band.items(), key=lambda kv: float(kv[0].split()[1].split("-")[0]))}
    lz = sorted(set(round(v, 4) for v in layers))
    return {"orca_time_s": total, "minutes_by_feature": feat, "minutes_by_z_band": bands,
            "extruded_m_by_feature": {kd: round(m / 1000, 1) for kd, m in ext.most_common()},
            "retractions": n_retr, "layers": len(lz), "layer_z": lz, "pauses_z": pauses}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--plate", type=int, required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--out-dir", default=os.path.join(tempfile.gettempdir(), "orca_lab"))
    ap.add_argument("--part", action="append")
    ap.add_argument("--obj", action="append")
    ap.add_argument("--range", action="append")
    ap.add_argument("--clear-ranges", action="append")
    ap.add_argument("--project", action="append")
    ap.add_argument("--pause-z", default="keep")
    ap.add_argument("--orca", default=ORCA)
    ap.add_argument("--keep-gcode", action="store_true", help="keep the G-code (default: deleted, it is big)")
    args = ap.parse_args()

    out = os.path.abspath(os.path.join(args.out_dir, args.label))
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    variant = os.path.join(out, "variant.3mf")
    patch_3mf(args.src, variant, args)
    datadir = tempfile.mkdtemp(prefix="orca_data_")
    t0 = time.time()
    cmd = ["xvfb-run", "-a", args.orca, "--datadir", datadir, "--slice", str(args.plate), "--outputdir", out, variant]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=out)     # Orca drops debug logs in its cwd
    shutil.rmtree(datadir, ignore_errors=True)
    g = os.path.join(out, f"plate_{args.plate}.gcode")
    res = {"label": args.label, "src": os.path.abspath(args.src), "plate": args.plate,
           "overrides": {"part": args.part, "obj": args.obj, "range": args.range, "clear_ranges": args.clear_ranges,
                         "project": args.project, "pause_z": args.pause_z},
           "slice_seconds": round(time.time() - t0, 1)}
    if r.returncode != 0 or not os.path.exists(g):
        res["error"] = (r.stdout[-1500:] + "\n" + r.stderr[-1500:]).strip()
        json.dump(res, open(os.path.join(out, "result.json"), "w"), indent=1)
        print(f"{args.label}: SLICE FAILED (exit {r.returncode}); see {out}/result.json")
        sys.exit(1)
    try:
        sr = json.load(open(os.path.join(out, "result.json")))
        res["orca_result"] = {k: sr.get(k) for k in ("error_string", "return_code")}
        res["warnings"] = [p.get("warning_message") for p in sr.get("sliced_plates", []) if p.get("warning_message")]
    except (OSError, ValueError):
        pass
    res.update(gcode_stats(g))
    for line in open(g, errors="ignore"):
        if line.startswith("; filament used [g]"):
            res["filament_g"] = line.split("=", 1)[1].strip()
            break
    if args.keep_gcode:
        res["gcode"] = g
    else:
        os.remove(g)
    json.dump(res, open(os.path.join(out, "result.json"), "w"), indent=1)
    h = res["orca_time_s"] / 3600
    top = ", ".join(f"{k} {v:.0f}" for k, v in list(res["minutes_by_feature"].items())[:5])
    print(f"{args.label}: {h:.2f} h ({res['orca_time_s']} s), {res['layers']} layers, {res['retractions']} retractions, "
          f"filament {res.get('filament_g')} g | {top}")


if __name__ == "__main__":
    main()
