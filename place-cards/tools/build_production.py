#!/usr/bin/env python3
"""Build the production OrcaSlicer project: every guest's card, the trees and the feet, on as few
plates as possible.

  1. Exports each guest's plaque (ivory base + gold) from wedding_place_cards.scad with OpenSCAD,
     4 at a time, into stl/all/. A card is re-exported only when the SCAD, tree_data.scad or the
     name changed (stl/all/manifest.json keeps a hash per card).
  2. Hands every card (52, duplicates included), the trees and the feet to make_3mf.py, which
     nests them by their real footprints (cards with their prime tower, trees and feet alone) and
     validates the result.

Spacing: 4 mm between cards (as in the test print), 3 mm between trees and between feet (that
is what puts 18 trees on a plate). Spares: up to --spares extra trees and feet, but only where
they fit on the plates the guests need anyway (spares never add a plate).

Run (from anywhere; OpenSCAD and PrusaSlicer on PATH):
  python3 place-cards/tools/build_production.py
  options: --out "print/Place cards - ALL GUESTS.3mf", --jobs 4, --spares 3, --no-slice-check,
           --fast (the optional faster settings: "... - ALL GUESTS - FAST.3mf")
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # place-cards/
sys.path.insert(0, HERE)
import check_common as C  # noqa: E402

SCAD = os.path.join(ROOT, "wedding_place_cards.scad")
DATA = os.path.join(ROOT, "tree_data.scad")
OUT_DIR = os.path.join(ROOT, "stl", "all")


def safe(name):
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)


def export_cards(names, jobs):
    """plaque_<name>_{base,gold}.stl for every distinct name, re-exported only when stale."""
    os.makedirs(OUT_DIR, exist_ok=True)
    src = hashlib.sha1(open(SCAD, "rb").read() + open(DATA, "rb").read()).hexdigest()
    man_path = os.path.join(OUT_DIR, "manifest.json")
    try:
        manifest = json.load(open(man_path))
    except (OSError, ValueError):
        manifest = {}
    todo = []
    for n in names:
        for part in ("base", "gold"):
            f = os.path.join(OUT_DIR, "plaque_%s_%s.stl" % (safe(n), part))
            key = "%s|%s" % (n, part)
            if manifest.get(key) != src or not os.path.exists(f):
                todo.append((n, part, f, key))

    def run(job):
        n, part, f, key = job
        cmd = ["openscad", "-o", f, "-D", 'make="plaques"', "-D", "names=%s" % json.dumps([n], ensure_ascii=False),
               "-D", 'part="%s"' % part, SCAD]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(f):
            raise SystemExit("OpenSCAD failed for %s %s:\n%s" % (n, part, r.stderr[-800:]))
        print("  exported %-14s %s" % (n, part), flush=True)
        return key

    if todo:
        print("exporting %d plaque parts with %d jobs ..." % (len(todo), jobs), flush=True)
        # the gold parts are the slow ones: start them first
        todo.sort(key=lambda j: j[1] != "gold")
        with ThreadPoolExecutor(jobs) as ex:
            for key in ex.map(run, todo):
                manifest[key] = src
                json.dump(manifest, open(man_path, "w"), indent=1, ensure_ascii=False)
    else:
        print("all %d cards up to date in %s" % (len(names), OUT_DIR))
    return {n: (os.path.join(OUT_DIR, "plaque_%s_base.stl" % safe(n)),
                os.path.join(OUT_DIR, "plaque_%s_gold.stl" % safe(n))) for n in names}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(ROOT, "print", "Place cards - ALL GUESTS.3mf"))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--spares", type=int, default=3, help="extra trees and feet, only where they fit")
    ap.add_argument("--tree", default=os.path.join(ROOT, "tree", "out", "tree.stl"))
    ap.add_argument("--foot", default=os.path.join(ROOT, "stl", "foot.stl"))
    ap.add_argument("--export-only", action="store_true")
    ap.add_argument("--no-slice-check", action="store_true")
    ap.add_argument("--fast", action="store_true", help='make_3mf --fast; default output "... - ALL GUESTS - FAST.3mf"')
    args = ap.parse_args()
    if args.fast and args.out == ap.get_default("out"):
        args.out = args.out.replace(".3mf", " - FAST.3mf")

    names = C.parse_scad(SCAD)["names"]
    files = export_cards(list(dict.fromkeys(names)), args.jobs)
    if args.export_only:
        return
    seen = {}
    cards = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        label = n if seen[n] == 1 else "%s (%d)" % (n, seen[n])
        cards.append("%s=%s,%s" % (label, *files[n]))
    import make_3mf
    margs = ["--out", args.out, "--title", "Place cards - all guests", "--pack", "nest",
             "--tree", args.tree, "--trees", str(len(names)), "--foot", args.foot, "--feet", str(len(names)),
             "--spares", str(args.spares), "--gap", "4", "--tree-gap", "3", "--foot-gap", "3"]
    for c in cards:
        margs += ["--card", c]
    if not args.no_slice_check:
        margs.append("--slice-check")
    if args.fast:
        margs += ["--fast", "--title", "Place cards - all guests (fast)"]
    make_3mf.main(margs)


if __name__ == "__main__":
    main()
