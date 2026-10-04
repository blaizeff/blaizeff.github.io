// Wedding place cards: ivory plaque, raised gold name + border, low wood foot.
// Plaque prints flat, lettering up. Everything gold starts at the same layer,
// so each plaque plate needs exactly ONE switch from white to gold.
//   make = "plaques" -> the cards (part = "both", "base" = white, "gold" = gold)
//   make = "feet"    -> the slotted feet (wood)
//   make = "test"    -> 2 cards + 2 feet, to check the finish before the full run
// Font: Lora SemiBold (free on Google Fonts). Save this file as UTF-8 so accents render.

make = "plaques";
part = "both";
gold_piece = -1;   // -1 = whole gold; 0 = straight part, 1.. = bevel steps (for fast export)

names = [
  "Patrick", "Clément", "Lise", "Benoit", "Nancy", "Josée", "David", "Sophie", "Mélanie", // 1 Houle I
  "Alexandre", "Johnny", "Suzanne", "Alexandre", "Sandra", "Lucienne", "Yvon", "Manon", "Hassan", // 2 Lanouette
  "Melody", "Noémie", "Charlotte", "Emilie", "Amélie", // 3 Bridesmaids
  "Samuel", "Mylène", "Audrey", "Noémie", "Michael", "Anushka", "Philomène", "Logan", // 4 Jeunes
  "Max-Antoine", "Brandon", "Victor", "Michael", "Prabh", "Ashwin", // 4 Groomsmen
  "Patric", "Holly", "Zachary", "Audrey-Anne", "France", "Richard", // 5 Flowers-F
  "Alex", "Coralie", "Julie", "Anthony", "Anosha", "Laura", "Samuel", // 6 Amis
  "Melissa", "Cédric" // DJ Booth
];

// ---------- plaque ----------
font        = "Lora:style=SemiBold";  // Lora SemiBold: elegant, strokes thick enough to print cleanly
cap_h       = 10;      // height of a capital letter, identical on every card
cap_ratio   = 0.972;   // capital height / font size for Lora (re-measure if you change font)
min_w       = 95;      // plaque width; long names make the plaque wider, never the text smaller
plaque_h    = 35;
plaque_t    = 2.4;     // ivory body (12 layers at 0.2)
gold_h      = 1.0;     // raised gold name and border: tall enough to really stand out
gold_chamfer = 0.2;    // 45 deg bevel on the top edge of every letter (printed as 2 x 0.1 mm steps)
gold_step    = 0.1;    // layer height used for the gold
corner      = 4;       // plaque corner radius
border_in   = 3;       // border distance from the plaque edge
border_w    = 1.0;     // border line width (2 extrusion lines)
border_r    = 3;       // border corner radius
name_gap    = 5;       // minimum space between the name and the border
edge_chamfer = 0.4;    // 45 deg chamfer on the bed-side edge: no elephant foot, crisp outline
edge_fillet  = 0.8;    // rounded top edge: softer to the touch (and a lead-in for the slot)
pitch       = [130, plaque_h + 8];

// ---------- foot ----------
// ---------- engraving on the front of the foot ----------
engrave_font  = "Lora:style=SemiBold";
engrave_lines = [["B&K", 4.3, 1.04], ["11.10.2026", 2.9, 1.14]];  // text, font size, letter spacing
engrave_gap   = 4.6;   // baseline to baseline
engrave_bold  = 0.03;  // a hair of extra weight so the finest serifs still carve cleanly
engrave_depth = 0.6;   // depth measured square to the face
engrave_angle = 45;    // carved downward at 45 deg: no overhanging ceilings inside the letters

feet        = 55;      // 52 guests + 3 spares, fits one plate (5 x 11)
foot_len    = 44;
foot_d      = 18;      // depth at the bottom
foot_top_d  = 11;      // depth at the top (trapezoid profile)
foot_h      = 13;      // a little taller: room for B&K + date on the front
lean        = 22;      // plaque leans back: readable from a seat and when walking past
slot_fit    = 0.2;     // total clearance (the 0.2 test foot fit best)
slot_depth  = 5;       // how far the plaque sits in the foot, measured along the plaque
slot_y      = -1.0;    // slot slightly forward so the leaning plaque balances over the foot
lip_h       = 2.2;     // the front wall grips only this much; above it a channel
channel     = gold_h + 0.4;  // clears the raised gold border hidden behind the foot
foot_gap    = 4;
foot_chamfer = 0.5;    // bed-side chamfer on the foot
foot_fillet  = 1.0;    // rounded top edges on the foot

// ---------- test ----------
test_names  = ["Sophie", "Max-Antoine"];
test_fits   = [0.1, 0.2, 0.3, 0.4, 0.5];

$fn = 48;
size = cap_h / cap_ratio;

module label(n)
  translate([0, -cap_h / 2]) text(n, size = size, font = font, halign = "center", valign = "baseline");

// Raw rectangle: 95 x 35, or wider if the name needs it
module raw(n)
  hull() {
    square([min_w, plaque_h], center = true);
    minkowski() {
      hull() scale([1, 0.001]) label(n);
      square([2 * (border_in + border_w + name_gap), plaque_h], center = true);
    }
  }

// Rounded rectangle inset from the raw one
module rr(n, inset, r) offset(r = r) offset(delta = -inset - r) raw(n);

// Edge profile: chamfer at the bottom, quarter-round at the top. Returns [z, inset] pairs.
function edge_profile(t, c, r) =
  concat([[0, c], [c, 0], [t - r, 0]], [for (a = [15 : 15 : 90]) [t - r + r * sin(a), r - r * cos(a)]]);

module profiled(t, c, r) {
  pts = edge_profile(t, c, r);
  hull() for (p = pts)
    translate([0, 0, min(p[0], t - 0.001)]) linear_extrude(0.001) offset(r = -p[1]) children();
}

module plaque_base(n) profiled(plaque_t, edge_chamfer, edge_fillet) rr(n, 0, corner);

module gold_2d(n) {
  difference() { rr(n, border_in, border_r); rr(n, border_in + border_w, border_r - border_w); }
  label(n);
}

// Straight sides, then a stepped 45 deg bevel on top (one step per 0.1 mm gold layer)
module plaque_gold(n) {
  steps = round(gold_chamfer / gold_step);
  translate([0, 0, plaque_t]) {
    if (gold_piece <= 0) linear_extrude(gold_h - gold_chamfer) gold_2d(n);
    for (i = [1 : steps]) if (gold_piece == -1 || gold_piece == i)
      translate([0, 0, gold_h - gold_chamfer + (i - 1) * gold_step])
        linear_extrude(gold_step) offset(r = -i * gold_step) gold_2d(n);
  }
}

module plaque(n) {
  if (part != "gold") plaque_base(n);
  if (part != "base") plaque_gold(n);
}

module engrave_2d()
  translate([0, (engrave_gap - engrave_lines[0][1] * cap_ratio) / 2])
    for (i = [0 : len(engrave_lines) - 1])
      translate([0, -i * engrave_gap])
        offset(r = engrave_bold)
          text(engrave_lines[i][0], size = engrave_lines[i][1], font = engrave_font,
               halign = "center", valign = "baseline", spacing = engrave_lines[i][2]);

// Carves the text into the sloped front face, angled downward so it prints without overhangs
module engraving() {
  a  = atan(((foot_d - foot_top_d) / 2) / foot_h);           // face tilt from vertical
  z0 = (foot_chamfer + foot_h - foot_fillet) / 2;              // middle of the flat part of the face
  y0 = -foot_d / 2 + (foot_d - foot_top_d) / 2 * z0 / foot_h;
  t  = engrave_depth / cos(engrave_angle - a);
  multmatrix([[1, 0, 0, 0],
              [0, sin(a),  cos(engrave_angle), y0],
              [0, cos(a), -sin(engrave_angle), z0],
              [0, 0, 0, 1]])
    translate([0, 0, -1]) linear_extrude(t + 1) engrave_2d();
}

module foot(fit = slot_fit, tag = "") {
  w = plaque_t + fit;
  difference() {
    // Low trapezoid with softened ends, chamfered at the bed and rounded on top
    hull() for (p = edge_profile(foot_h, foot_chamfer, foot_fillet)) {
      z = min(p[0], foot_h - 0.001);
      d = foot_d - (foot_d - foot_top_d) * z / foot_h;
      translate([0, 0, z]) linear_extrude(0.001)
        offset(r = -p[1]) offset(r = 1.5) square([foot_len - 3, d - 3], center = true);
    }
    translate([0, slot_y, foot_h]) rotate([-lean, 0, 0]) {
      // slot, open at both ends so any plaque width fits
      translate([-foot_len, -w / 2, -slot_depth]) cube([2 * foot_len, w, slot_depth + 10]);
      // channel in the front wall above the lip, for the raised border
      translate([-foot_len, -w / 2 - channel, -slot_depth + lip_h]) cube([2 * foot_len, channel + 0.01, slot_depth + 10]);
    }
    engraving();
    if (tag != "")
      translate([-foot_len / 2 + 0.5, 0, foot_h * 0.42]) rotate([90, 0, -90])
        linear_extrude(1) text(tag, size = 3.6, font = font, halign = "center", valign = "center");
  }
}

if (make == "plaques") {
  cols = ceil(sqrt(len(names)));
  for (i = [0 : len(names) - 1])
    translate([(i % cols) * pitch[0], -floor(i / cols) * pitch[1], 0]) plaque(names[i]);
} else if (make == "foot") {
  foot();
} else if (make == "feet") {
  for (i = [0 : feet - 1])
    translate([(i % 5) * (foot_len + foot_gap), -floor(i / 5) * (foot_d + foot_gap), 0]) foot();
} else {
  for (i = [0 : 1]) translate([(i - 0.5) * (foot_len + 8), 0, 0]) foot();
  for (i = [0 : len(test_names) - 1])
    translate([0, foot_d / 2 + 8 + plaque_h / 2 + i * (plaque_h + 6), 0]) plaque(test_names[i]);
}
