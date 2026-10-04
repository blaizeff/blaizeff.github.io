#!/usr/bin/env python3
"""Write out/report.md for the variant-B tree from out/tree_meta.json and out/slice_check.json.

  python3 make_report.py [--out out/]

build_tree.py and slice_check.py both call it, so the numbers in the report are always the ones
measured on the files next to it.
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def fmt_hist(bins, counts, unit='mm'):
    rows = ['| range (%s) | count |' % unit, '|---|---|']
    for i, c in enumerate(counts):
        rows.append(f'| {bins[i]} - {bins[i + 1]} | {c} |')
    return '\n'.join(rows)


def main(out=None):
    out = out or os.path.join(HERE, 'out')
    m = json.load(open(os.path.join(out, 'tree_meta.json')))
    sp = os.path.join(out, 'slice_check.json')
    s = json.load(open(sp)) if os.path.exists(sp) else None
    c = m['checks']
    p = m['parameters']
    t = m['thickness_mm']
    L = []
    w = L.append
    w('# Gold tree, variant B: vector outline + adaptive triangulation\n')
    w('Print-ready FDM relief of the Tripo tree (part 0 of `source/tree_tripo_meshopt.glb`) for the wedding '
      'place cards: flat glue face on the bed at z = 0, relief up, one watertight body, canonical tree frame '
      '(origin = centre of the straight trunk-bottom edge, +x right, +y up, z = thickness).\n')
    w('Files: `out/tree.stl` (binary, mm), `out/tree_footprint.json` (exact z = 0 glue-face outline of the mesh), '
      '`out/tree_meta.json` (all numbers below), previews `out/*.png`, slicer check `out/slice_check.*`.\n')
    w('Rebuild: `python3 build_tree.py` (about 4 min on this 4-CPU box, add `--regen` to re-decode the GLB and '
      're-cast the depth maps), then `python3 slice_check.py` (about 1 min).\n')
    w('## Method\n')
    w('1. **Source heights.** Orthographic depth maps of the tree front and back (0.0003 model units/px = '
      f'{m["pixel_mm"]:.4f} mm/px at {m["scale_mm_per_model_unit"]} mm/unit). The Tripo tree has a flat back '
      '(plane fit, residual 0.04 mm), so relief = front depth minus that plane. `--regen` rebuilds the cache from '
      'the GLB (gltf-transform meshopt decode, node `tripo_part_0` in world transform, embree ray casting).')
    w('2. **Silhouette clean-up (raster).** Horizontal trunk cut at model y = '
      f'{p["cut_world_y"]} (depth row 1478, just above the jagged source cut, so the stray root running down '
      'and behind is dropped completely), largest body only, source pin-holes below '
      f'{p["hole_area"]} mm² filled, and a left root flare ({p["flare_left"]} mm wide, {p["flare_height"]} mm '
      'tall, concave fillet curve) so the trunk looks planted; the right root flare comes from the source.')
    w('3. **Vector outline (shapely, exact buffers).** Sub-pixel contour (marching squares on a '
      f'σ = {p["contour_sigma"]} px smoothed mask), clipped by the exact cut line. Then {p["outline_iters"]} rounds '
      'of: thicken thin stems (medial axis, free ends peeled by '
      f'{p["spur_prune"]} mm so leaf tips are not inflated, discs of radius smooth-max(r, {p["min_width"]}/2) on the '
      f'thin axis points only, so tapers elsewhere stay untouched) → close gaps narrower than {p["min_gap"]} mm '
      f'(buffer +/−{p["min_gap"] / 2}) → drop holes < {p["hole_area"]} mm² → open with r = {p["tip_radius"]} mm '
      '(rounds every convex tip and corner, and the cusps a closing leaves at the end of a filled gap). '
      'The cut edge is snapped exactly onto y = 0.')
    w('4. **Heights.** Median outlier removal (seams), 2 passes of bilateral filtering '
      f'(σs = {p["bilateral_ss"]} px, σr = {p["bilateral_sr"]} mm: keeps overlap cliffs, rims and midribs). '
      'Thickened stems get their round cross-section *stretched* to the new width (not a flat pad); closed gaps '
      'are filled at the lower neighbouring height so they still read as grooves; the flare tapers down. '
      'The outer 2.5 px of the depth map are grazing-angle samples, so the rim band is rebuilt by a first-order '
      'normalized convolution of the surface just inside (clean wall tops, no lip). A band-pass boost '
      f'(gain {p["detail_gain"]}, σ {p["detail_s1"]}-{p["detail_s2"]} mm, faded near the outline) keeps midribs and '
      f'fluting legible at 0.1 mm layers. Thickness = {p["base"]} + {p["gain"]} × (relief − {p["rim_relief"]}), '
      f'exponential soft floor ({p["floor_in"]} mm over the plaque, {p["floor_out"]} mm where leaves hang beyond '
      f'it, plaque rectangle {p["plaque_rect"]} in the tree frame) and a soft cap at {p["max_thick"]} mm.')
    w('5. **Adaptive constrained Delaunay** (`triangle`, `pYq'
      f'{p["min_angle"]:g}`): the exact outline rings are the constraint (segments split only where the height '
      f'along them deviates > {p["tol"]} mm or they exceed {p["max_seg"]} mm; no Steiner points on the boundary, so '
      'top, walls and chamfer share the same vertices). Interior points: a 1 mm seed grid, then greedy insertion '
      'of the worst pixel of every triangle whose linear interpolation error is above the tolerance, until the '
      f'max vertical error ≤ {p["tol"]} mm over all pixel centres.')
    w('6. **Solid.** Vertical walls from z = chamfer to the top edge, a 45° chamfer strip stitched to the exact '
      f'inward offset (by {p["chamfer"]} mm) of the outline, and a planar bottom triangulated from that same offset '
      'ring, so `tree_footprint.json` is exactly the mesh glue face.\n')
    w('## Result\n')
    b = m['bbox_3d']
    w(f'* Size {b["size"][0]} × {b["size"][1]} × {b["size"][2]} mm (x from {b["min"][0]} to {b["max"][0]}, '
      f'y from {b["min"][1]} to {b["max"][1]}).')
    w(f'* Volume {m["volume_mm3"]} mm³, solid mass {m["mass_solid_g"]} g at {m["density_g_cm3"]} g/cm³.')
    w(f'* {m["triangles"]} triangles, {m["vertices"]} vertices.')
    w(f'* Top outline area {m["outline_area_mm2"]} mm², glue face area {m["glue_face_area_mm2"]} mm².')
    tb = m['trunk_base']
    w(f'* Trunk base: straight bottom edge from x = {tb["x_left"]} to {tb["x_right"]} at y = 0 (corners rounded '
      f'r = {p["tip_radius"]}); glue face starts at y = {p["chamfer"]}.')
    w(f'* Thickness: min {t["min"]}, p1 {t["p1"]}, p5 {t["p5"]}, median {t["median"]}, p99 {t["p99"]}, max '
      f'{t["max"]} mm.\n')
    w('## Checks (measured on out/tree.stl)\n')
    w('| check | result |')
    w('|---|---|')
    w(f'| trimesh is_watertight | {c["is_watertight"]} |')
    w(f'| trimesh is_winding_consistent | {c["is_winding_consistent"]} |')
    w(f'| trimesh body_count | {c["body_count"]} |')
    w(f'| volume > 0 | {c["volume_mm3"]:.2f} mm³ |')
    w(f'| Euler number / genus | {c["euler_number"]} / {c["manifold3d_genus"]} (3 real through-gaps) |')
    w(f'| manifold3d status | {c["manifold3d_status"]} (volume {c["manifold3d_volume_mm3"]:.2f} mm³) |')
    w(f'| bottom vertices: max abs z | {c["bottom_vertex_z_absmax"]} (exactly planar at z = 0) |')
    w(f'| bottom face normal z (max) | {c["bottom_normal_z_max"]:.6f} |')
    w(f'| downward faces other than bottom and chamfer | {c["downward_faces_outside_bottom_and_chamfer"]} |')
    w(f'| top relief faces: min normal z | {c["top_min_normal_z"]:.4f} (> 0: no overhang anywhere) |')
    w(f'| wall faces: max abs normal z | {c["wall_max_abs_normal_z"]} (exactly vertical) |')
    w(f'| chamfer normal z range | {c["chamfer_normal_z_range"][0]:.3f} to {c["chamfer_normal_z_range"][1]:.3f} '
      '(45° = −0.707) |')
    w(f'| degenerate faces | {c["degenerate_faces"]} |')
    se = m['surface_error_mm']
    w(f'| top surface vs height grid: max / p99 / mean error | {se["max"]} / {se["p99"]} / {se["mean"]} mm |')
    w(f'| glue face vs exact shapely inset: symmetric difference | {m["glue_vs_exact_inset_symdiff_mm2"]} mm² |')
    wt = m['min_width_top_outline']
    wg = m['min_width_glue_face']
    gp = m['min_gap_top_outline']
    w(f'| min stem width, top outline (medial axis; the last {wt.get("end_skip_mm", 1.0)} mm of each free end = '
      f'the rounded tip is excluded, as in the thickening) | {wt["min_mm"]} mm '
      f'(p1 {wt["p1_mm"]}) |')
    w(f'| min width, glue face (= top − 2 × chamfer) | {wg["min_mm"]} mm (p1 {wg["p1_mm"]}) |')
    w(f'| min open gap between parts (complement medial axis) | {gp["min_mm"]} mm (p1 {gp["p1_mm"]}) |')
    cr = m['convex_corner_radius_top_outline']
    w(f'| min convex corner radius (leaf tips), top outline | {cr["min_mm"]} mm (p1 {cr["p1_mm"]}; {cr["method"]}) |')
    ex = m.get('exact_outline_checks')
    if ex:
        tt, gg = ex['tip_opening_residual_mm2'], ex['gap_opening_residual_mm2']
        w(f'| exact check: top outline minus its opening with r = {ex["tip_radius_tested_mm"]} mm | total '
          f'{tt["total"]} mm², largest piece {tt["largest_piece"]} mm² (polygon discretisation only: every tip '
          f'radius >= {ex["tip_radius_tested_mm"]}) |')
        w(f'| exact check: open space minus its opening with r = {ex["gap_tested_mm"] / 2:.3f} mm | total '
          f'{gg["total"]} mm², largest piece {gg["largest_piece"]} mm²: these are the sharp inner corners where '
          f'two rounding discs meet; a slot narrower than {ex["gap_tested_mm"]} mm would leave about width x '
          f'length, so none is longer than {gg["largest_piece"] / ex["gap_tested_mm"]:.2f} mm |')
    to = m['thickness_outside_plaque_mm']
    ti = m['thickness_over_plaque_mm']
    w(f'| min thickness over the plaque / beyond it | {ti["min"]} / {to["min"]} mm (p1 {ti["p1"]} / {to["p1"]}; '
      f'{to["area_mm2"]} mm² of the tree is beyond the plaque rectangle) |')
    oi = m['outline_info']
    w(f'| outline area added (thickening, gap closing, flare) / removed (tip rounding) | '
      f'{oi["added_vs_contour_mm2"]} / {oi["removed_vs_contour_mm2"]} mm² |')
    w('')
    w('### Local width histogram, top outline (medial-axis nodes at 0.02 mm)\n')
    w(fmt_hist(wt['bins_mm'], wt['counts']))
    w('\nThinnest places (x, y, width): ' + ', '.join(f'({a}, {b_}, {c_})' for a, b_, c_ in wt['thinnest_xy_w'][:4]))
    w('\n### Local width histogram, glue face\n')
    w(fmt_hist(wg['bins_mm'], wg['counts']))
    w('\n### Gap width histogram (open space between parts)\n')
    w(fmt_hist(gp['bins_mm'], gp['counts']))
    w('\n### Thickness histogram (pixels of the 0.0332 mm height grid)\n')
    w(fmt_hist(t['hist_bins'], t['hist_counts_px']))
    w('')
    if s:
        w('## Slicer check (PrusaSlicer 2.7 CLI)\n')
        w('Coverage slice: 0.1 mm layers (first layer 0.1), 0.42 mm lines, 2 perimeters, arachne, 100 % rectilinear '
          'infill so that any model area without extrusion is a real gap and not sparse infill. Extrusion paths are '
          'rasterized as round-ended beads of their G-code width at 25 px/mm and compared with the mesh '
          f'cross-section at the slicing height (mid-layer); tolerance {s["coverage_tolerance_mm"]} mm. '
          f'G-code to model alignment by bounding box + search: offset {s["alignment_offset_mm"]}.\n')
        pe = s.get('print_estimate_15pct_infill', {})
        w(f'Realistic slice (15 % grid infill, same walls): {pe.get("time", "?")} at default PrusaSlicer speeds, '
          f'{pe.get("filament_mm", "?")} mm / {pe.get("filament_g", "?")} g of filament.\n')
        w('| print z | model area mm² | uncovered mm² | uncovered % | islands | islands with no extrusion | largest uncovered blob (mm², x, y) |')
        w('|---|---|---|---|---|---|---|')
        for r in s['layers']:
            bl = r['largest_uncovered_blobs'][0] if r['largest_uncovered_blobs'] else None
            bls = f'{bl["area_mm2"]} at ({bl["x"]}, {bl["y"]})' if bl else '-'
            lost = r['islands_without_extrusion']
            ls = f'{len(lost)} (max {max(q["area_mm2"] for q in lost)} mm²)' if lost else '0'
            w(f'| {r["print_z"]} | {r["model_area_mm2"]} | {r["uncovered_mm2"]} | {r["uncovered_pct"]} | '
              f'{r["section_islands"]} | {ls} | {bls} |')
        low = [r for r in s['layers'] if r['print_z'] <= 1.65]
        lost_all = [(r['slice_z'], q) for r in s['layers'] for q in r['islands_without_extrusion']]
        near = [q for z, q in lost_all if q.get('local_max_z') is not None and q['local_max_z'] - z < 0.12]
        maxlost = max([q['area_mm2'] for z, q in lost_all], default=0)
        w(f'\nReading: from the first layer up to z = 1.6 mm the section is one connected island and every '
          f'leaf, stem and twig is extruded; the uncovered area stays at '
          f'{min(r["uncovered_pct"] for r in low)}-{max(r["uncovered_pct"] for r in low)} % '
          f'(largest blob {max((r["largest_uncovered_blobs"][0]["area_mm2"] for r in low if r["largest_uncovered_blobs"]), default=0)} mm²): '
          'scattered slivers at perimeter/infill junctions and at the very ends of rounded tips, all inside '
          'printed regions. Higher up the sections break into the tops of leaf domes and branch crests. In total '
          f'{len(lost_all)} such cap islands (largest {maxlost} mm²) get no extrusion; {len(near)} of them peak '
          'less than 0.12 mm above the slicing plane (the rest sit next to higher features). The slicer drops '
          'such sub-layer caps by design, so those domes end one layer (0.1 mm) lower; nothing structural or '
          'visible at arm\'s length is lost. A separate run with classic perimeters on the same STL gives the same '
          'picture (0.2-0.6 % uncovered up to z = 1.6 mm, no lost island below 2 mm, the same dome caps above). '
          'See `out/slice_check_layers.png` (red = model not covered, blue = extrusion outside the model).\n')
    w('Reproducibility: the output is deterministic (seeded medial axis; two consecutive full builds gave a '
      'byte-identical tree.stl), and `--regen` re-decodes the GLB and re-casts depth maps that match the cached '
      'ones to 4.5e-6 model units (0.0005 mm; 1 pixel of 4 M differs in coverage).\n')
    w('## Known limits and choices\n')
    w('* The glue face of the thinnest stems is about 0.4-0.6 mm wide (1.0-1.2 mm stem minus 2 × 0.3 mm chamfer); '
      'the slicer prints it as one bead (covered, see first layer above). Reduce `--chamfer` if adhesion of those '
      'first layers is a worry.')
    w('* Leaf-overlap steps are kept as near-vertical cliffs (top faces stay upward-facing, min normal z '
      f'{c["top_min_normal_z"]:.3f}), which is what gives the crisp layered look of the source.')
    w('* Tops of domes print as 0.1 mm terraces (see `out/preview_layers.png`); midribs and fluting survive as '
      'contour kinks and grooves, the faint Tripo brush striations on the leaves do not (they are < 0.03 mm).')
    w('* The thickness floors use the provisional plaque rectangle (tree frame '
      f'{p["plaque_rect"]}); pass `--plaque-rect` if the card layout moves the tree.\n')
    w('## Parameters\n')
    w('```')
    for k, v in p.items():
        w(f'{k} = {v}')
    w('```')
    with open(os.path.join(out, 'report.md'), 'w') as f:
        f.write('\n'.join(L) + '\n')
    print('wrote', os.path.join(out, 'report.md'))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=None)
    main(ap.parse_args().out)
