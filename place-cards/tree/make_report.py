#!/usr/bin/env python3
"""Write out/report.md for the gold tree from the measurements next to it.

Reads out/tree_meta.json (build_tree.py), out/slice_check.json (slice_check.py) and, when present,
out/checks/check_mesh.json and out/checks/check_slice.json (the shared tools/check_mesh.py and
tools/check_slice.py run on out/tree.stl, see README.md). build_tree.py and slice_check.py both call it,
so the numbers in the report are always the ones measured on the files next to it.

  python3 make_report.py [--out out/]
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load(path):
    return json.load(open(path)) if os.path.exists(path) else None


def f(v, n=3):
    return 'n/a' if v is None else (f'{v:.{n}f}' if isinstance(v, float) else str(v))


def xy(p):
    return f'({p[0]:.2f}, {p[1]:.2f})'


def main(out=None):
    out = out or os.path.join(HERE, 'out')
    m = load(os.path.join(out, 'tree_meta.json'))
    s = load(os.path.join(out, 'slice_check.json'))
    tm = load(os.path.join(out, 'checks', 'check_mesh.json'))
    ts = load(os.path.join(out, 'checks', 'check_slice.json'))
    p = m['parameters']
    c = m['checks']
    t = m['thickness_mm']
    sol = m['solid_checks']
    ray = sol['vertical_ray_test']
    sec = sol['exact_sections']
    ch = m['chamfer']
    L = []
    w = L.append

    w('# Gold tree relief: build report\n')
    w('Print-ready FDM relief of the Tripo tree (part 0 of `source/tree_tripo_meshopt.glb`) for the wedding place '
      'cards. The flat glue face lies on the bed at z = 0 and the relief points up. It is one watertight body in the '
      'canonical tree frame: origin at the centre of the straight trunk-bottom edge, +x right, +y up, z = thickness.\n')
    w('The build promotes variant B (vector outline + adaptive triangulation), which the judge picked, and applies '
      'every must-fix plus the worthwhile grafts from variant A. Every number below was measured on the files in '
      '`out/` (`tree_meta.json`, `slice_check.json`, `checks/`).\n')

    w('## Result at a glance\n')
    b = m['bbox_3d']['size']
    w('| | |\n|---|---|')
    w(f'| Size | {b[0]:.2f} x {b[1]:.2f} x {b[2]:.2f} mm (scale {m["scale_mm_per_model_unit"]} mm per model unit) |')
    w(f'| Mesh | {m["triangles"]:,} triangles, {m["vertices"]:,} vertices, binary STL |')
    w(f'| Volume / mass | {m["volume_mm3"]:.1f} mm³, {m["mass_solid_g"]} g solid PLA at {m["density_g_cm3"]} g/cm³ |')
    w(f'| Thickness | min {t["min"]}, p1 {t["p1"]}, median {t["median"]}, max {t["max"]} mm (floor {p["floor_out"]} mm everywhere) |')
    w(f'| Outline (visible silhouette) | {m["outline_area_mm2"]} mm², 3 enclosed holes {m["outline_holes_mm2"]} mm² |')
    w(f'| Glue face (z = 0) | {m["glue_face_area_mm2"]} mm² |')
    w(f'| Bed chamfer | 45°, {ch["chamfer_per_wall_vertex_mm"]["min"]} to {ch["chamfer_per_wall_vertex_mm"]["max"]} mm '
      f'(median {ch["chamfer_per_wall_vertex_mm"]["median"]}): {p["chamfer_min"]} mm on {p["chamfer_w0"]} mm stems, '
      f'{p["chamfer"]} mm from {p["chamfer_w1"]} mm wide parts up |')
    tb = m['trunk_base']
    w(f'| Trunk base | straight cut from x = {tb["x_left"]} to {tb["x_right"]} at y = 0; glue face from y = {tb["glue_face_y_min"]} |')
    if s:
        pe = s['print_estimate_15pct_infill']
        w(f'| PrusaSlicer estimate | {pe["filament_g"]} g, {pe["time"]} (0.1 mm layers, 2 walls, 15 % infill) |')
    w('')

    w('## Judge must-fixes and grafts: what changed\n')
    wt = m['min_width_top_outline']
    wg = m['min_width_glue_face']
    s01 = sec.get('0.1', {})
    tips = m['convex_corner_radius_top_outline']
    ex = m['exact_outline_checks']
    gap = m['min_gap_top_outline']
    gf = [g for g in m['outline_info'].get('gap_fix', []) if g['action'].startswith('pinch')]
    w('| Item | Variant B | Now |\n|---|---|---|')
    w(f'| Pocket fit: footprint had only the glue face | glue face only (879.8 mm²) | `outline_full` key = exact outline of the '
      f'vertical walls ({m["outline_area_mm2"]} mm²), plus glue-face and flat-cut extents in `trunk_base` and sizing notes |')
    w(f'| Tip radius | min 0.36 mm | opening radius {p["tip_radius"]} mm; exact opening at {ex["tip_radius_tested_mm"]} mm leaves '
      f'{ex["tip_opening_residual_mm2"]["largest_piece"]} mm² at most; circumradius over 0.2 mm arcs min {tips["min_mm"]} mm |')
    w(f'| Thickness beyond the plaque | min 1.358 mm, zoning tied to a provisional plaque rectangle | {p["floor_out"]} mm floor '
      f'everywhere (no zoning): ray-cast min {ray["thickness_top_z_mm"]["min"]} mm, '
      f'{ray["thickness_top_z_mm"]["area_below_1.4_mm2"]} mm² below 1.4 |')
    w(f'| Bed contact of thin stems | glue face 0.42 mm, z = 0.1 section 0.57 mm | width-adaptive chamfer: glue face min '
      f'{wg["min_mm"]} mm (p1 {wg["p1_mm"]}), z = 0.1 section min {f(s01.get("width_min_mm"))} mm |')
    w(f'| Narrow slit at (36.5, 33.9) | 0.48 mm pinch | pinch widened (walls pushed back '
      f'{gf[0]["action"].split("back ")[1].split(" mm")[0] if gf else "?"} mm each): narrowest open gap now {gap["min_mm"]} mm '
      f'(raster), exact opening at {ex["gap_tested_mm"]} mm leaves {ex["gap_opening_residual_mm2"]["largest_piece"]} mm² at most |')
    w(f'| Right root stub at the trunk cut | 0.90-0.93 mm wedge | trimmed back to a round cap (opening r = {p["root_radius"]} mm '
      f'near the cut, {m["outline_info"].get("root_trim_mm2")} mm² removed) |')
    rr = sol['rim_roughness']['mm_inside_edge']
    w(f'| Rim micro-facets and pits | along-rim roughness p99 0.086 mm at 0.06 mm from the edge | smooth extrapolation beyond '
      f'the outline + {p["rim_blend"]} px blend: p99 {rr["0.06"]["p99"]} mm at 0.06 mm, interior (0.25 mm) {rr["0.25"]["p99"]} mm |')
    w(f'| Detail boost (optional) | 1.0 | {p["detail_gain"]}: closer to the source, midribs and fluting still 1-2 layers deep |')
    w('| Extra verifications (from A) | none | vertical-ray heightfield test, ray-cast thickness, exact manifold3d sections '
      'at 4 heights, glue-face area and holes |')
    w('')

    w('## Method\n')
    w(f'1. **Source heights.** Orthographic depth maps of the tree front and back ({m["pixel_mm"]:.4f} mm/px). The Tripo '
      'tree has a flat back, so relief = front depth minus a plane fitted to the back. `--regen` rebuilds the cached depth '
      'maps from the GLB (gltf-transform meshopt decode, node `tripo_part_0` in world transform, embree ray casting).')
    w(f'2. **Silhouette clean-up.** Horizontal trunk cut at model y = {p["cut_world_y"]}, just above the jagged source cut, so '
      f'the stray root running down and behind is dropped. Largest body only, source pin-holes under {p["hole_area"]} mm² '
      f'filled, and a {p["flare_left"]} mm left root flare.')
    w(f'3. **Vector outline (shapely, exact buffers).** Sub-pixel contour of the smoothed mask, then {p["outline_iters"]} rounds '
      f'of: thicken stems thinner than {p["min_width"]} mm with discs on their medial axis (free ends excluded over '
      f'{p["spur_prune"]} mm), close gaps under {p["min_gap"]} mm, drop holes under {p["hole_area"]} mm², and open with '
      f'r = {p["tip_radius"]} mm (rounds every tip). Then the base parts thinner than {2 * p["root_radius"]:.1f} mm are trimmed '
      'to a round cap (the right root needle), and a gap fixer widens pinches and fills dead-end notches narrower than '
      f'{p["min_gap"]} mm without leaving spikes. The cut edge is snapped exactly onto y = 0.')
    w(f'4. **Heights.** Median outlier removal, {p["bilateral_iters"]} bilateral passes (keeps overlap cliffs, rims and '
      'midribs). Thickened stems get their round cross-section stretched; closed gaps are filled at the lower neighbouring '
      f'height so they read as grooves. The outer {p["rim_band"]} px of grazing-angle samples are rebuilt by a first-order '
      f'extrapolation from inside, continued {p["rim_ext"]} px beyond the outline and blended back over {p["rim_blend"]} px. '
      f'Band-pass detail boost x{p["detail_gain"]} ({p["detail_s1"]}-{p["detail_s2"]} mm). Thickness = {p["base"]} + '
      f'{p["gain"]} x (relief - {p["rim_relief"]}), exponential soft floor at {p["floor_out"]} mm, soft cap at {p["max_thick"]} mm.')
    w(f'5. **Top surface.** Constrained Delaunay triangulation of the exact outline (triangle, min angle {p["min_angle"]}°), '
      f'refined by inserting the worst pixel per triangle until the vertical error is at most {p["tol"]} mm.')
    w('6. **Solid.** Vertical walls down to z = c, a 45° chamfer to the glue face and a planar bottom. The chamfer size c '
      'follows the local width (medial-axis radius, smoothed along the outline, slope-limited), and each glue vertex is its '
      'wall vertex moved inward by c, so the chamfer is a plain quad strip and the footprint is exactly the z = 0 face.')
    w('')

    w('## Checks (build_tree.py)\n')
    w('| Check | Result |\n|---|---|')
    w(f'| trimesh | watertight {c["is_watertight"]}, winding consistent {c["is_winding_consistent"]}, bodies {c["body_count"]}, '
      f'Euler {c["euler_number"]} (genus 3 = the 3 enclosed holes), degenerate faces {c["degenerate_faces"]} |')
    w(f'| manifold3d | {c["manifold3d_status"]}, genus {c["manifold3d_genus"]}, volume {c["manifold3d_volume_mm3"]:.2f} mm³ |')
    w(f'| Overhangs | downward faces outside bottom and chamfer: {c["downward_faces_outside_bottom_and_chamfer"]}; top faces min '
      f'normal z {c["top_min_normal_z"]:.3f}; walls exactly vertical (max |nz| {c["wall_max_abs_normal_z"]}) |')
    ca = c['chamfer_angle_from_horizontal_deg']
    w(f'| Chamfer angle | {ca["min"]:.2f} to {ca["max"]:.2f}° from the bed (median {ca["median"]:.2f}°); '
      f'{ca["area_outside_40_50_mm2"]} mm² outside 40-50° |')
    w(f'| Bed | bottom vertices at z = {c["bottom_vertex_z_absmax"]}, bottom normals z = {c["bottom_normal_z_max"]:.3f}, min z {c["min_vertex_z"]} |')
    w(f'| Heightfield (vertical rays, {ray["grid_mm"]} mm grid) | {ray["rays_hitting"]:,} columns hit: hit counts '
      f'{ray["hit_count_histogram"]} (index = crossings), non-heightfield area {ray["non_heightfield_columns_area_mm2"]} mm² |')
    w(f'| Thickness (rays) | min {ray["thickness_top_z_mm"]["min"]} mm, p1 {ray["thickness_top_z_mm"]["p1"]} mm, '
      f'{ray["thickness_top_z_mm"]["area_below_1.4_mm2"]} mm² under 1.4 mm |')
    w(f'| Widths, outline | min {wt["min_mm"]} mm, p1 {wt["p1_mm"]} mm, thinnest at {xy(wt["thinnest_xy_w"][0])} '
      f'({wt["end_skip_mm"]} mm at each free end excluded) |')
    w(f'| Widths, glue face | min {wg["min_mm"]} mm, p1 {wg["p1_mm"]} mm |')
    for k, v in sec.items():
        extra = ''
        if 'hausdorff_vs_footprint_mm' in v:
            extra = f'; Hausdorff to the footprint {v["hausdorff_vs_footprint_mm"]} mm'
        if 'symdiff_vs_outline_full_mm2' in v:
            extra = f'; differs from `outline_full` by {v["symdiff_vs_outline_full_mm2"]} mm²'
        w(f'| Exact section z = {k} mm | {v["area_mm2"]} mm², {v["parts"]} part(s), width min {v["width_min_mm"]} mm, '
          f'p1 {v["width_p1_mm"]} mm{extra} |')
    w(f'| Gaps | narrowest open gap {gap["min_mm"]} mm (p1 {gap["p1_mm"]}) at {xy(gap["thinnest_xy_w"][0])} |')
    w(f'| Tips | convex corner radius min {tips["min_mm"]} mm, p1 {tips["p1_mm"]} mm ({tips["method"]}) |')
    w(f'| Exact openings | tips at r = {ex["tip_radius_tested_mm"]}: total {ex["tip_opening_residual_mm2"]["total"]} mm², '
      f'largest {ex["tip_opening_residual_mm2"]["largest_piece"]} mm²; gaps at {ex["gap_tested_mm"]} mm: total '
      f'{ex["gap_opening_residual_mm2"]["total"]} mm², largest {ex["gap_opening_residual_mm2"]["largest_piece"]} mm² '
      '(polygon discretisation only) |')
    w('| Rim roughness (along the edge, 0.3 mm high-pass, p99) | ' + ', '.join(
        f'{k} mm in: {v["p99"]} mm' for k, v in sol['rim_roughness']['mm_inside_edge'].items()) + ' |')
    se = m['surface_error_mm']
    w(f'| Top surface vs height grid | max {se["max"]} mm, p99 {se["p99"]} mm, mean {se["mean"]} mm |')
    w(f'| Glue inset | every glue vertex {ch["glue_vertex_inset_mm"]["min"]} to {ch["glue_vertex_inset_mm"]["max"]} mm inside '
      f'the outline; {int(round(100 * ch["chamfer_per_wall_vertex_mm"]["fraction_full"]))} % of the outline has the full '
      f'{p["chamfer"]} mm chamfer |')
    w('')

    if s:
        w('## Slicer coverage (slice_check.py)\n')
        w('PrusaSlicer 2.7 CLI, 0.1 mm layers, 0.42 mm lines, 2 arachne perimeters, 100 % infill (so any uncovered area is '
          'a real gap). The G-code paths are rasterised as round-capped beads and compared with the exact mesh section at '
          'each layer\'s mid height (tolerance 0.06 mm).\n')
        w('| print z | model mm² | uncovered mm² | % | islands | islands with no extrusion |\n|---|---|---|---|---|---|')
        for r in s['layers']:
            lost = r['islands_without_extrusion']
            ls = ', '.join(f'{q["area_mm2"]:.2f} mm² at ({q["x"]}, {q["y"]}) top {q["local_max_z"]}' for q in lost[:3])
            w(f'| {r["print_z"]:.2f} | {r["model_area_mm2"]:.1f} | {r["uncovered_mm2"]:.3f} | {r["uncovered_pct"]:.3f} | '
              f'{r["section_islands"]} | {len(lost)}{": " + ls if ls else ""}{" ..." if len(lost) > 3 else ""} |')
        w('\nIslands with no extrusion only appear at the tops of leaf domes and crests, whose peak sits within a layer '
          'of the slicing plane: that dome simply ends one layer lower.\n')

    if tm or ts:
        w('## Shared checks (tools/check_mesh.py, tools/check_slice.py)\n')
        if tm:
            tr = tm['meshes'][list(tm['meshes'])[0]]
            w('`check_mesh.py --bodies 1 --relief on`:\n')
            w('| Check | Verdict | Value | Note |\n|---|---|---|---|')
            for v in tr['verdicts']:
                if isinstance(v, dict):
                    w(f'| {v.get("check", v.get("name", ""))} | {v.get("status", "")} | {v.get("value", "")} | {v.get("note", "")} |')
                else:
                    w('| ' + ' | '.join(str(x) for x in v) + ' |')
            w('')
        if ts:
            su = ts['summary']
            st = ts['settings']
            w(f'`check_slice.py --kind tree` ({st["layer_height"]} mm layers, first layer {st["first_layer_height"]} mm, '
              f'{st["perimeters"]} {st["perimeter_generator"]} walls, elephant-foot compensation {st["elefant_foot_compensation"]}): '
              f'**{su["status"]}**. Never printed {su["never_printed_mm2"]} mm²; wall volume not extruded {su["lost_volume_pct"]} %; '
              f'deep losses {su["deep_loss_area_mm2"]} mm²; {su["lost_islands_count"]} dome tops lost at their last layer '
              f'(largest {su["lost_islands_largest_mm2"]} mm², lowest at layer {su["lost_islands_lowest_layer"]}); printed top '
              f'2+ layers lower {su["printed_top_lower_by_2plus_layers_mm2"]} mm², higher {su["printed_top_higher_by_2plus_layers_mm2"]} mm².\n')

    w('## Previews\n')
    w('`preview_top.png` (true proportions), `compare_hillshade.png` (next to the source hillshade at the same scale), '
      '`preview_oblique.png`, `preview_side.png`, `closeup_leaf_cluster.png` and `closeup_trunk_base.png` (40 px/mm), '
      '`preview_layers.png` (relief quantised to 0.1 mm layers), `slice_check_layers.png` (gold = extruded, red = model not '
      'covered, blue = extrusion outside the model).\n')

    w('## Known limits\n')
    w('- The previews use a simple numpy rasteriser, not a physically based renderer; wall striping is facet shading.')
    w('- The slicer checks use PrusaSlicer 2.7 as a stand-in for the user\'s OrcaSlicer profile.')
    w('- A faint trace of the Tripo brush striations remains on some leaves (well under one layer).')
    w(f'- The 1.4 mm floor applies everywhere. `--floor-in 1.2 --plaque-rect x0 y0 x1 y1` re-enables a thinner floor over '
      'the plaque if the card layout is ever frozen.')

    with open(os.path.join(out, 'report.md'), 'w') as fh:
        fh.write('\n'.join(L) + '\n')
    return os.path.join(out, 'report.md')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    print(main(ap.parse_args().out))
