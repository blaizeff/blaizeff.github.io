# Wedding place cards with gold tree: design brief (shared by all agents)

## Goal
Add a gold, sculpted tree to the user's existing wedding place card, matching the reference image
(`/tmp/claude-0/-home-user-blaizeff-github-io/24db3fd0-37aa-5abe-8f21-849f3f5e2464/images/1.webp`):
an ivory plaque with a raised gold name ("Sophie") and a thin gold border, standing in a low wood foot
engraved "B&K / 11.10.2026", and a gold tree whose trunk starts at the foot on the left and whose
canopy arches over the top-left of the plaque, with leaves reaching past the plaque's top and left edges.

The tree is a **separate gold print glued onto the plaque**. It must be fully adapted for FDM printing
and still keep the beauty and detail of the source model: leaf midribs, domed leaves, fluted
branches, trunk texture.

## Where everything is
* Repo (commit target): `/home/user/blaizeff.github.io`, branch `claude/tree-design-3d-printing-rmge5a`.
  Project folder: `place-cards/`.
* `place-cards/wedding_place_cards.scad`: the user's current, tested SCAD (plaques + feet). It is the
  master file to evolve. An untouched copy is in `place-cards/source/previous/`.
* `place-cards/source/previous/Place cards - TEST 2 cards, 2 feet.3mf`: the user's last OrcaSlicer
  project (Bambu-format 3MF): printer profile, filaments, per-part settings, plates. Unzipped at
  `$WORK/prev3mf_unzipped/`.
* `place-cards/source/tree_tripo_meshopt.glb`: the tree source model. It is AI-generated (Tripo):
  meshopt-compressed, 7 parts, not watertight, about 1.9M triangles. It is the whole scene re-made
  from the photo. **Part 0 is the tree.** Parts 1-6 are the card, the foot and letters (ignore them,
  except as a reference for layout).
* `$WORK = /tmp/claude-0/-home-user-blaizeff-github-io/24db3fd0-37aa-5abe-8f21-849f3f5e2464/scratchpad/work`
  (scratch, shared, not committed):
  * `tree_part0_raw.ply`: the tree part, decompressed, in world transform (model units).
    `tripo_part_{0..6}.ply`: all parts.
  * `tree_front.npz` / `tree_back.npz`: orthographic depth maps of the tree, ray-cast along -x / +x.
    Keys: `depth` [rows, cols] (NaN = miss), `zs` (column -> world z), `ys` (row -> world y, top row =
    max y), `res` = 0.0003 model units/px. **Flip columns (`depth[:, ::-1]`) to get the front view
    the right way round** (tree on the left, canopy sweeping right). In the source model, +x points
    toward the viewer, y is up, and the plaque front face is at x ≈ 0.0242.
  * `tree_hill.png`: hillshade of the front depth map. `front_color.png`: whole Tripo scene, front view.
  * `tree_footprint_provisional.json`: provisional glue-face outline of the tree (raw mask, no cleanup)
    in the canonical tree frame (see below). Scale 110.7 mm/unit, trunk cut at row 1470.
  * Tools: `zrender.py` (numpy triangle rasterizer), `splat.py` (fast vertex splat for dense meshes),
    `depthmap.py` (embree raycast depth maps).
* Installed: OpenSCAD 2021.01 (`openscad`; PNG previews need `xvfb-run -a openscad ...`), Lora font
  (`Lora:style=SemiBold`, etc.), PrusaSlicer 2.7 CLI (`prusa-slicer --export-gcode ...` works headless),
  Python 3.11 with numpy, scipy, trimesh (+embree ray casting), manifold3d, shapely, scikit-image,
  opencv, fast_simplification, triangle, matplotlib, pillow, fontTools. Node 22
  (`npx @gltf-transform/cli` decodes meshopt). Chromium + Playwright (`/opt/pw-browsers`) for
  three.js renders if wanted. The machine has 4 CPUs and 15 GB RAM: keep heavy jobs reasonable.

## Printer and materials (from the user's 3MF)
Elegoo Centauri Carbon 2, 0.4 mm nozzle, OrcaSlicer 2.4.x, 256 × 256 bed, 0.2 mm layers (gold at
0.1 mm in the card's gold Z range), arachne walls, 0.42 mm line width.
Filament 1 = Elegoo Rapid PLA+ ivory `#F4F0E6`. 2 = Elegoo PLA **Silk** gold `#D4AF37`.
3 = Elegoo PLA Wood `#7A4F2E`. Card plates print ivory and then gold with **exactly one switch**:
everything above the ivory body is gold, and everything gold starts at the same Z. Keep it that way.

## The existing design (keep it)
Read the SCAD. Key facts: plaque 95 × 35 (wider for long names, never smaller text), 2.4 ivory body
with a bed chamfer and a rounded top edge, raised gold Lora SemiBold name (cap height 10) and border
(inset 3, width 1, 1.0 high, stepped 45° top bevel in 0.1 steps). Foot: 44 long, trapezoid
18 → 11 deep, 13 high, slot 5 deep along the plaque, plaque leans back 22°, slot_y −1, lip 2.2, channel
for the raised border, engraved front face (45° carve). **Keep the foot design.** Change foot geometry
only if a check proves a real problem, and keep any change minimal and called out. The names list is
the user's real guest list: keep it.

## Decisions already made
1. **Tree = heightfield relief.** The flat back is the glue face and sits on the bed. The front relief
   points up. A heightfield cannot have overhangs, and the top surface comes out smooth and shiny in
   silk. Derive the heights from the Tripo front surface so the sculpted detail survives. Denoise AI
   artifacts without blurring the designed detail. The relief should read clearly at arm's length.
2. **Default scale: about 110.7 mm per model unit.** That gives a tree about 79 mm wide and 49 mm tall,
   matching the photo against a 35 mm plaque (30 mm visible above the foot). Make scale a parameter.
3. **Canonical tree frame:** origin = bottom-centre of the trunk base, +x right, +y up (front view),
   z = 0 on the glue face, z up = relief. All tree outputs (STL, footprint JSON) use this frame.
   The card places the tree with a 2D translation `tree_pos` in the plaque frame.
4. **Printability targets for the tree (0.4 nozzle):**
   * Every stem, petiole and twig at least about 1.0 mm wide in XY. Leaf tips rounded (radius at
     least about 0.4).
   * Thickness at least about 1.0 to 1.2 mm everywhere, at least about 1.4 where leaves hang beyond the plaque.
   * Total thickness in the 2.5 to 3.6 mm range (parameterised base thickness plus relief gain).
   * About 0.3 mm 45° chamfer on the bed edge (stops elephant foot, so the tree drops into its pocket).
   * One watertight, manifold, single-body mesh with a sensible triangle count (no 2M-triangle STLs).
   * Clean trunk bottom: a straight horizontal cut (parallel to the plaque bottom) with a tasteful root
     flare. Drop the stray root fragments that run down and behind in the source.
   * Expected print: gold silk, at 0.08 to 0.12 mm layers.
5. **Card adaptations** (in the SCAD; derived data may come from a Python helper):
   * Asymmetric layout: tree on the left. The name sits right of the tree, clear of the tree's
     footprint by `name_gap`. The plaque grows to the right for long names. Check every guest name.
   * **Plaque = the user's original card (user decision, 2026-10-05):** plain rounded rectangle, the
     original `profiled()` hull edge profile and `offset(r)` gold bevel. No backing, no leaf notches, no
     corner cuts. The plaque's left end hides behind the trunk (`tree_pos` is chosen for that by
     tools/card_layout.py). Only the pocket (from the top) and the foot tick (in the bed face) are cut
     into it.
   * **Foot and names (user decision, 2026-10-05):** `foot_len = 62` (profile, slot and engraving
     unchanged). The left end goes under the trunk and the foot spans about 65 % of a 95 mm plaque.
     Border margins stay at 3.0 mm on every side. The top looks tighter only from low camera angles;
     from a seated guest's eye it reads larger.
     Every name is left-aligned right after the tree with `tree_name_gap = 3`.
   * **Steel-shot ballast (user decision, 2026-10-05):** no 100 % infill. Each foot has a closed pocket
     (`ballast_*`, z 1.0 to 6.6, 1.6 walls, 3.4 cm³) that is filled with 1 mm steel shot and glue
     during an automatic pause (M600 before z 6.8 on the feet plates; the bridge roof prints over it).
     The weight is in the steel, so the foot is back to the slim 18 → 11 profile with `slot_y = -1.0`
     at 15 % lightning infill. Filled to 90 %, every card needs at least 25° to tip backwards (58° to
     the side); empty, 14.5°.
   * **Name padding (user decision, 2026-10-05):** the gap from the name's right end to the gold border
     equals the gap above and below the cap height (`name_pad`, 8.5 mm). The plaque follows the name
     (`min_w = 0`) and only stays longer when it must reach past the foot (short names such as Alex).
   * **Silk top coat (user decision, 2026-10-05):** the card gold parts (letters and border) print their
     top surfaces at 20 mm/s and 1000 mm/s² so the silk comes out shiny and smooth. The trees keep their
     settings ("leaves are perfect").
   * Pocket: a recess in the ivory top surface shaped like the tree's glue face (footprint ∩ plaque,
     offset by about 0.15 clearance, small slivers closed). About 0.6 deep, parameterised, 0 disables.
     It locates the tree exactly for 52 assemblies and hides the glue line. It is below the gold start,
     so there is still one filament switch.
   * Gold border: becomes an open shape that ends cleanly where it passes "behind" the tree (end
     covered by a solid branch or leaf, rounded or square, never chopped into fragments between
     leaves). It never enters the pocket. The bottom border run is hidden inside the foot, as today.
6. **Assembly:** the trunk bottom must clear the foot. The plaque leans 22°, so tree relief pushes
   forward and up, and the trunk bottom face must sit above the foot's top surface plus clearance.
   About 0.6 mm visible gap reads as "planted". The foot slot is open at both ends: the recommended
   position puts the foot's left end under the trunk, as in the photo. Tipping must stay stable with
   realistic masses (PLA 1.24 g/cm³, the user's infill and walls).

## File ownership (parallel agents must not edit each other's files)
* Tree builder: `place-cards/tree/**`. Script `place-cards/tree/build_tree.py`. Outputs in
  `place-cards/tree/out/`: `tree.stl`, `tree_footprint.json` (same schema as the provisional one,
  without `provisional`), `tree_meta.json`, preview PNGs.
* Card: `place-cards/wedding_place_cards.scad`, `place-cards/tools/card_layout.py`,
  `place-cards/tree_data.scad` (generated include: polygons, placement, tables).
* Checks and packaging: `place-cards/tools/check_*.py`, `place-cards/tools/make_3mf.py`,
  `place-cards/tools/render_*.py`, `place-cards/print/`, `place-cards/renders/`.
* Nobody commits or pushes except the orchestrator. Do not edit `BRIEF.md`.

## Code style
Match the user's SCAD: short comments that say why, readable parameter names, the same idioms.
It must run on OpenSCAD 2021.01 (installed here) and on current releases. Python: plain scripts with
argparse and no notebooks, deterministic output, and a docstring saying how to run it.
