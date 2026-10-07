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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gcode", required=True)
    ap.add_argument("--layer-z", type=float, required=True, help="top of the first layer to (partly) print again")
    ap.add_argument("--object-xy", required=True, help="bed x,y of the first object missing that layer")
    ap.add_argument("--object-half", default="32,10", help="half size of the object's box around object-xy")
    ap.add_argument("--parked-z", type=float, default=80, help="Z the end G-code of the failed job parked the bed at")
    ap.add_argument("--corner", default="245,245", help="empty bed spot for the height check and the purge line")
    ap.add_argument("--travel-z", type=float, default=12.0, help="safe height over the parts for the first travel")
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

    head = []
    for l in lines:
        head.append(l)
        if "EXECUTABLE_BLOCK_START" in l:
            break
    out = head + [
        f"; ===== FINISH a failed print: from layer {layer_no} (z {args.layer_z:g}) at the object at {cx:g},{cy:g} =====",
        "; No bed mesh probing and no Z homing: both would touch the parts on the bed.",
        f"; The bed must still be where the failed job parked it (Z{args.parked_z:g}) and the printer must not have",
        "; been switched off since. Watch the start and press Stop if the nozzle comes down anywhere but the empty corner.",
        "M106 S0",
        "M106 P2 S0",
        "G90",
        "M83",
        f"M140 S{bed}",
        "M104 S140",
        f"M190 S{bed}",
        "G4 P180000 ; let the plate soak 3 min at temperature (it was cold)",
        f"SET_KINEMATIC_POSITION Z={args.parked_z:g} ; the bed has not moved since the failed job parked it",
        "G28 X Y ; home X and Y only",
        "G1 Z40 F600 ; bed up slowly, still far below the nozzle",
        f"G1 X{ex:g} Y{ey:g} F12000 ; over the empty corner",
        f"M109 S{nozzle}",
        "G1 Z5 F300 ; CHECK 1: nozzle about 5 mm above the empty plate",
        "G4 P20000",
        "G1 Z2 F300 ; CHECK 2: nozzle about 2 mm above the empty plate (press Stop if touching or far above)",
        "G4 P20000",
        "G1 Z0.5 F300",
        "G92 E0",
        "G1 E6 F120 ; purge on the empty corner",
        "M106 S200",
        f"G1 X{ex - 30:g} E20 F1200 ; purge line",
        "G1 F6000",
        f"G1 X{ex - 35:g} E0.8",
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
    ] + lines[j:]
    open(args.out, "w").write("\n".join(l for l in out if l is not None) + "\n")
    print(f"cut at original line {j + 1} (layer {layer_no}/{total}, z {args.layer_z:g}); object starts at "
          f"{sx:g},{sy:g}; wrote {args.out}")


if __name__ == "__main__":
    main()
