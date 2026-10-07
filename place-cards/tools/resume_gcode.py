#!/usr/bin/env python3
"""Finish a print that stopped part-way, on top of the parts still on the bed (Elegoo CC2 / Klipper).

Takes the original sliced G-code and cuts it at the travel to a given object in a given layer
(the first object that is missing that layer), and writes a new G-code that:
  1. heats up WITHOUT the normal start (no bed mesh probing, no Z homing: both would touch the parts),
  2. trusts that the bed is still where the end of the failed job parked it (SET_KINEMATIC_POSITION),
     and homes X and Y only,
  3. brings the bed up slowly and hovers over an empty corner (two pauses of 20 s to check by eye),
     then purges a line there,
  4. travels high above the parts to the cut point and goes on with the original G-code from there.

  python3 tools/resume_gcode.py --gcode plate_8.gcode --layer-z 9.8 --object-xy 164,116 \\
      --parked-z 80 --corner 245,245 --out "Feet plate - finish from layer 49 foot 12.gcode"
"""
import argparse
import math


def parse_xy(s):
    x, y = s.split(",")
    return float(x), float(y)


def skip_object(tail, xy, half):
    """Remove an object's blocks from G-code lines: from the descend to layer height before its first extrusion
    to the end of the retract + wipe after its last one. The head stays lifted and retracted, exactly as on a
    travel, and the next object's own descend + unretract follow. Speed / fan / progress lines are kept."""
    cx, cy = xy
    hx, hy = half

    def ext_in(l, pos):
        if l[:3] not in ("G1 ", "G0 "):
            return None, pos
        p = {w[0]: float(w[1:]) for w in l.split(";")[0].split()[1:] if w[0] in "XYZE"}
        x, y = p.get("X", pos[0]), p.get("Y", pos[1])
        e = p.get("E", 0)
        inside = x is not None and abs(x - cx) < hx and abs(y - cy) < hy
        return (e > 0, inside), (x, y)

    out, n, i, pos = [], 0, 0, (None, None)
    while i < len(tail):
        flag, npos = ext_in(tail[i], pos)
        if flag and flag[0] and flag[1]:
            # start of a block: back up over the descend line right before it
            k = len(out) - 1
            while k >= 0 and not (out[k].startswith("G1 Z") and len(out[k].split(";")[0].split()) == 2):
                if out[k].startswith(";LAYER_CHANGE") or (out[k][:3] == "G1 " and " E" in out[k] and "E-" not in out[k]):
                    k = -1
                    break
                k -= 1
            keep = []
            if k >= 0:
                keep = [l for l in out[k:] if l.startswith(("SET_VELOCITY_LIMIT", "M106"))]
                del out[k:]
            # skip to the last extrusion inside, then over the retract and wipe
            last = i
            j = i
            p2 = npos
            while j < len(tail):
                f2, p2n = ext_in(tail[j], p2)
                if tail[j].startswith(";LAYER_CHANGE"):
                    break
                if f2 and f2[0]:
                    if f2[1]:
                        last = j
                    else:
                        break
                p2 = p2n
                j += 1
            # after the last extrusion: drop this object's retract + wipe (Orca may put them after the layer
            # change comments when the object is the last of its layer), keep everything else in place
            between = []
            in_wipe = False
            j = last + 1
            while j < len(tail):
                t = tail[j]
                if t.startswith(";WIPE_END"):
                    j += 1
                    break
                if t.startswith(("SET_VELOCITY_LIMIT", "M106")):
                    keep.append(t)
                elif t.startswith(";WIPE_START"):
                    in_wipe = True
                elif in_wipe and t.startswith("G1 F"):
                    pass                                   # the wipe's feedrate
                elif t.startswith("G1 ") and " E-" in t:
                    pass                                   # the retract and the wipe moves
                elif t.startswith((";", "G92 E0")) or not t.strip():
                    between.append(t)                      # layer change comments etc.
                else:
                    break
                j += 1
            keep += [l for l in tail[i:last + 1] if l.startswith(("SET_VELOCITY_LIMIT", "M106"))]
            last_of = {}
            for l in keep:                       # the last speed limit and the last of each fan
                w = l.split()
                kind = w[0] if w[0] != "M106" else ("M106 " + w[1] if len(w) > 1 and w[1].startswith("P") else "M106")
                last_of[kind] = l
            out += between + list(last_of.values())
            # position after the block: where the wipe ended
            for l in tail[i:j]:
                _, pos = ext_in(l, pos)
            i = j
            n += 1
            continue
        out.append(tail[i])
        pos = npos
        i += 1
    return out, n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gcode", required=True)
    ap.add_argument("--layer-z", type=float, required=True, help="top of the first layer to (partly) print again")
    ap.add_argument("--object-xy", required=True, help="bed x,y of the first object missing that layer")
    ap.add_argument("--object-half", default="32,10", help="half size of the object's box around object-xy")
    ap.add_argument("--parked-z", type=float, default=80, help="Z the end G-code of the failed job parked the bed at")
    ap.add_argument("--corner", default="245,245", help="empty bed spot for the height check and the purge line")
    ap.add_argument("--travel-z", type=float, default=12.0, help="safe height over the parts for the first travel")
    ap.add_argument("--home", choices=["parked", "full"], default="parked",
                    help="parked: trust the parked bed (SET_KINEMATIC_POSITION) and home X/Y; full: a normal G28 (the CC2 "
                         "homes X/Y to the front-left and then touches the nozzle down right there: that spot must be bare)")
    ap.add_argument("--skip-object", action="append", default=[], help="bed x,y of an object to leave out (removed from the bed)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    lines = open(args.gcode, errors="ignore").read().split("\n")
    cx, cy = parse_xy(args.object_xy)
    hx, hy = parse_xy(args.object_half)
    zi = [i for i, l in enumerate(lines) if l.startswith(";Z:")]
    i0 = next(i for i in zi if abs(float(lines[i][3:]) - args.layer_z) < 1e-6)
    i1 = next((i for i in zi if float(lines[i][3:]) > args.layer_z + 1e-6), len(lines))
    # the cut: the descend to layer height right before the object's first extrusion
    x = y = None
    cut = None
    for i in range(i0, i1):
        l = lines[i]
        if l[:3] in ("G1 ", "G0 "):
            p = {w[0]: float(w[1:]) for w in l.split(";")[0].split()[1:] if w[0] in "XYZE"}
            x, y = p.get("X", x), p.get("Y", y)
            if p.get("E", 0) > 0 and x is not None and abs(x - cx) < hx and abs(y - cy) < hy:
                cut = i
                break
    if cut is None:
        raise SystemExit("object not found in that layer")
    # walk back over the unretract and the descend: start from the 'G1 Z<layer>' line
    j = cut
    while j > i0 and not lines[j].startswith(f"G1 Z{args.layer_z:g}"):
        j -= 1
    if j == i0:
        raise SystemExit("no descend to layer height before the object")
    sx, sy = x, y                                       # start XY of the object (end of the travel)
    # machine state just before the cut
    state = {}
    for i in range(j):
        l = lines[i]
        for key in ("M106 S", "M106 P2", "M106 P3", "SET_VELOCITY_LIMIT", "SET_PRESSURE_ADVANCE", "M104 S", "M140 S"):
            if l.startswith(key):
                state[key] = l.split(";")[0].strip()
    nozzle = int(state.get("M104 S", "M104 S220").split("S")[1])
    bed = int(state.get("M140 S", "M140 S60").split("S")[1])
    ex, ey = parse_xy(args.corner)
    layer_no = sum(1 for i in zi if float(lines[i][3:]) <= args.layer_z + 1e-6)
    total = len(zi)

    tail = lines[j:]
    for spec in args.skip_object:
        tail, n = skip_object(tail, parse_xy(spec), (hx, hy))
        print(f"left out the object at {spec}: {n} blocks removed")

    head = []
    for l in lines:
        head.append(l)
        if "EXECUTABLE_BLOCK_START" in l:
            break
    out = head + [
        f"; ===== FINISH a failed print: from layer {layer_no} (z {args.layer_z:g}) at the object at {cx:g},{cy:g} =====",
        "; No bed mesh probing: it would touch the parts on the bed.",
        "; Watch the start and switch off if the nozzle comes down anywhere but the empty corner (or the bare homing spot).",
        "M106 S0",
        "M106 P2 S0",
        "G90",
        "M83",
        f"M140 S{bed}",
        "M104 S140",
        f"M190 S{bed}",
        "G4 P180000 ; let the plate soak 3 min at temperature (it was cold)",
    ] + ([
        f"SET_KINEMATIC_POSITION Z={args.parked_z:g} ; the bed has not moved since the failed job parked it",
        "G28 X Y ; home X and Y only. If the head goes to the bed centre and the bed rises: POWER OFF",
        f"G1 X{ex:g} Y{ey:g} F12000 ; over the empty corner, bed still parked",
        f"G1 Z{min(60, args.parked_z):g} F600 ; CHECK A: gap about 60 mm",
        "G4 P15000",
        "G1 Z30 F600 ; CHECK B: gap about 30 mm",
        "G4 P15000",
        "G1 Z10 F300 ; CHECK C: gap about 10 mm",
        "G4 P15000",
    ] if args.home == "parked" else [
        "SET_KINEMATIC_POSITION Z=0 ; wherever the bed is now, call it 0 ...",
        "G1 Z15 F300 ; ... and lower it 15 mm before homing (if the printer errors here, nothing has moved)",
        "G28 ; normal homing: X/Y to the front-left, then the nozzle touches the BARE plate near the chute (those feet are removed)",
        "G1 Z20 F600 ; bed down 20 mm before any sideways move",
        f"G1 X{ex:g} Y{ey:g} F12000 ; over the empty corner, 20 mm above the plate",
    ]) + [
        "G1 Z5 F300 ; CHECK D: gap about 5 mm",
        "G4 P15000",
        "G1 Z2 F300 ; CHECK E: gap about 2 mm",
        "G4 P15000",
        "G1 Z0.2 F120 ; CHECK F: paper test, a sheet should just drag under the nozzle (nozzle at 140 C, no ooze)",
        "G4 P60000",
        "G1 Z2 F300",
        f"M109 S{nozzle} ; heat at the corner: any ooze falls on the empty plate",
        "G1 Z0.5 F300",
        "G92 E0",
        "G1 E6 F120 ; purge on the empty corner",
        "M106 S200",
        f"G1 Y{ey - 23:g} E20 F1200 ; purge line along the empty right edge",
        "G1 F6000",
        f"G1 Y{ey - 26:g} E0.8",
        "M106 S0",
        "G1 E-.8 F1800 ; retract as at the cut point",
        f"G1 Z{args.travel_z:g} F600 ; up above every part before travelling",
        f"G1 X{sx:g} Y{sy:g} F12000 ; over the start of the object",
        f"G1 Z{args.layer_z + 0.4:g} F600",
        state.get("SET_PRESSURE_ADVANCE", ""),
        state.get("M106 P3", ""),
        state.get("M106 P2", ""),
        state.get("M106 S", ""),
        state.get("SET_VELOCITY_LIMIT", ""),
        f"SET_PRINT_STATS_INFO TOTAL_LAYER={total} CURRENT_LAYER={layer_no}",
        f";LAYER_COUNT:{total}",
        "; ===== the original G-code from here =====",
    ] + tail
    open(args.out, "w").write("\n".join(l for l in out if l is not None) + "\n")
    print(f"cut at original line {j + 1} (layer {layer_no}/{total}, z {args.layer_z:g}); object starts at "
          f"{sx:g},{sy:g}; wrote {args.out}")


if __name__ == "__main__":
    main()
