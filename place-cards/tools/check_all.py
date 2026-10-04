#!/usr/bin/env python3
"""Run every check and the packaging in one go, for a given tree and the test cards.

  1. check_mesh.py   on the tree (one body, relief analysis), the foot and every plaque part
  2. check_slice.py  on the tree (PrusaSlicer, 0.1 mm layers, the "will every leaf print" proof)
  3. check_assembly.py for each test name (collisions, trunk gap, masses, tipping)
  4. make_3mf.py     the OrcaSlicer TEST project: plate 1 cards, plate 2 trees, plate 3 feet
  5. render_scene.py hero / front / close-up renders and the side-by-side with the reference

Outputs: check reports and images in --out-dir (default renders/checks), the 3MF in --print-dir
(default print/), renders in --render-dir (default renders/), and a one-page summary
<out-dir>/summary_<tag>.json printed as a table at the end.

Run (from the repo root):
  python3 place-cards/tools/check_all.py --tree place-cards/tree/out/tree.stl
  python3 place-cards/tools/check_all.py --tree place-cards/tree/variants/B/out/tree.stl --tag B \
      --out-dir place-cards/renders/variants/B --render-dir place-cards/renders/variants/B --no-3mf
  options: --names Sophie Max-Antoine, --stl-dir place-cards/stl, --scad ..., --skip slice,render,
           --reference photo.webp, --slice-args "--layer 0.08"
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable


def run(cmd, log):
    t = time.time()
    with open(log, "w") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True)
    lines = open(log).read().strip().splitlines()
    key = [ln for ln in lines if ln.startswith(("OVERALL", "VERDICT", "VALIDATION", "wrote"))]
    tail = (key or lines or [""])[-1]
    return {"cmd": " ".join(shlex.quote(c) for c in cmd), "exit": r.returncode, "log": log,
            "seconds": round(time.time() - t, 1), "last_line": tail}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tree", default=os.path.join(ROOT, "tree", "out", "tree.stl"))
    ap.add_argument("--names", nargs="+", default=["Sophie", "Max-Antoine"])
    ap.add_argument("--stl-dir", default=os.path.join(ROOT, "stl"))
    ap.add_argument("--scad", default=os.path.join(ROOT, "wedding_place_cards.scad"))
    ap.add_argument("--tag", default="final")
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "renders", "checks"))
    ap.add_argument("--render-dir", default=os.path.join(ROOT, "renders"))
    ap.add_argument("--print-dir", default=os.path.join(ROOT, "print"))
    ap.add_argument("--reference", help="reference photo for the side-by-side")
    ap.add_argument("--skip", default="", help="comma list of: mesh, slice, assembly, 3mf, render")
    ap.add_argument("--no-3mf", action="store_true")
    ap.add_argument("--slice-args", default="", help="extra arguments for check_slice.py")
    args = ap.parse_args()
    skip = set(s.strip() for s in args.skip.split(",") if s.strip())
    if args.no_3mf:
        skip.add("3mf")
    os.makedirs(args.out_dir, exist_ok=True)
    logs = os.path.join(args.out_dir, "logs")
    os.makedirs(logs, exist_ok=True)
    foot = os.path.join(args.stl_dir, "foot.stl")
    cards = [(n, os.path.join(args.stl_dir, f"plaque_{n}_base.stl"), os.path.join(args.stl_dir, f"plaque_{n}_gold.stl"))
             for n in args.names]
    res = {"tag": args.tag, "tree": os.path.abspath(args.tree), "steps": {}}
    T = args.tag

    if "mesh" not in skip:
        res["steps"]["mesh_tree"] = run([PY, os.path.join(HERE, "check_mesh.py"), args.tree, "--bodies", "1", "--relief", "on",
                                         "--json", os.path.join(args.out_dir, f"check_mesh_tree_{T}.json"),
                                         "--png-dir", args.out_dir], os.path.join(logs, f"mesh_tree_{T}.log"))
        parts = [foot] + [p for _, b, g in cards for p in (b, g)]
        res["steps"]["mesh_parts"] = run([PY, os.path.join(HERE, "check_mesh.py"), *parts, "--relief", "off", "--allow-float",
                                          "--json", os.path.join(args.out_dir, f"check_mesh_parts_{T}.json")],
                                         os.path.join(logs, f"mesh_parts_{T}.log"))
    if "slice" not in skip:
        res["steps"]["slice_tree"] = run([PY, os.path.join(HERE, "check_slice.py"), args.tree, "--kind", "tree",
                                          "--out-dir", args.out_dir, "--tag", f"tree_{T}", *shlex.split(args.slice_args)],
                                         os.path.join(logs, f"slice_tree_{T}.log"))
    if "assembly" not in skip:
        for n, b, g in cards:
            res["steps"][f"assembly_{n}"] = run([PY, os.path.join(HERE, "check_assembly.py"), "--scad", args.scad,
                                                 "--name", f"{n}_{T}", "--base", b, "--gold", g, "--foot", foot,
                                                 "--tree", args.tree, "--out-dir", args.out_dir],
                                                os.path.join(logs, f"assembly_{n}_{T}.log"))
    if "3mf" not in skip:
        os.makedirs(args.print_dir, exist_ok=True)
        cmd = [PY, os.path.join(HERE, "make_3mf.py"), "--out", os.path.join(args.print_dir, "place_cards_TEST.3mf"),
               "--tree", args.tree, "--trees", "2", "--foot", foot, "--feet", "2", "--plate-prefix", "TEST",
               "--card-suffix", " (test card)", "--title", "Place cards - TEST 2 cards, 2 trees, 2 feet"]
        for n, b, g in cards:
            cmd += ["--card", f"{n}={b},{g}"]
        res["steps"]["make_3mf"] = run(cmd, os.path.join(logs, f"make_3mf_{T}.log"))
    if "render" not in skip and cards:
        n, b, g = cards[0]
        cmd = [PY, os.path.join(HERE, "render_scene.py"), "--scad", args.scad, "--name", n if T == "final" else f"{n}_{T}",
               "--base", b, "--gold", g, "--foot", foot, "--tree", args.tree, "--views", "hero,front,closeup",
               "--out-dir", args.render_dir]
        if args.reference:
            cmd += ["--reference", args.reference]
        res["steps"]["render"] = run(cmd, os.path.join(logs, f"render_{T}.log"))

    # summary from the JSON reports
    summ = {}
    try:
        m = json.load(open(os.path.join(args.out_dir, f"check_mesh_tree_{T}.json")))
        summ["mesh_tree"] = m["overall"]
        r = list(m["meshes"].values())[0]
        summ["tree_triangles"] = r["topology"]["triangles"]
        summ["tree_bbox"] = r["bed"]["size"]
        rl = r.get("relief", {})
        summ["tree_thickness_inner_min"] = rl.get("thickness_inner_mm", {}).get("min")
        summ["tree_width_min_outline"] = rl.get("widths", {}).get("outline (projection)", {}).get("width_min_mm")
    except (OSError, ValueError, KeyError, IndexError):
        pass
    try:
        s = json.load(open(os.path.join(args.out_dir, f"tree_{T}_slice.json")))["summary"]
        summ["slice"] = s["status"]
        summ["slice_never_printed_mm2"] = s["never_printed_mm2"]
        summ["slice_lost_volume_pct"] = s["lost_volume_pct"]
        summ["slice_lost_islands"] = s["lost_islands_count"]
        summ["slice_deep_loss_mm2"] = s.get("deep_loss_area_mm2")
        summ["slice_reasons"] = s.get("reasons")
    except (OSError, ValueError, KeyError):
        pass
    for n, _, _ in cards:
        try:
            a = json.load(open(os.path.join(args.out_dir, f"assembly_{n}_{T}.json")))
            summ[f"assembly_{n}"] = a["overall"]
            summ[f"trunk_gap_{n}"] = min(a["gaps"]["trunk_to_foot_mm"]) if a["gaps"]["trunk_to_foot_mm"] else None
            summ[f"overlaps_{n}"] = {k: v for k, v in a["overlaps_mm3"].items() if v > 0.05}
            rec = a["scenes"][0]["tipping"]
            summ[f"tipping_{n}"] = {k: v["tip_angle_deg"] for k, v in rec.items() if isinstance(v, dict)}
            summ[f"mass_{n}_g"] = a["scenes"][0]["mass_g"]
            summ[f"foot_x_range_{n}"] = (a.get("foot_x_range_side_tilt") or {}).get("range")
            summ[f"foot_x_recommended_{n}"] = a["scenes"][0]["foot_x"]
        except (OSError, ValueError, KeyError, IndexError):
            pass
    res["summary"] = summ
    js = os.path.join(args.out_dir, f"summary_{T}.json")
    with open(js, "w") as f:
        json.dump(res, f, indent=1)
    print(f"== check_all ({T}) tree {args.tree}")
    for k, st in res["steps"].items():
        print(f"  {k:<22} exit {st['exit']}  {st['seconds']:6.1f} s  {st['last_line'][:110]}")
    for k, v in summ.items():
        print(f"  {k:<28} {v}")
    print("summary:", js)


if __name__ == "__main__":
    main()
