# Gold tree relief: build report

Print-ready FDM relief of the Tripo tree (part 0 of `source/tree_tripo_meshopt.glb`) for the wedding place cards. The flat glue face lies on the bed at z = 0 and the relief points up. It is one watertight body in the canonical tree frame: origin at the centre of the straight trunk-bottom edge, +x right, +y up, z = thickness.

The build promotes variant B (vector outline + adaptive triangulation), which the judge picked, and applies every must-fix plus the worthwhile grafts from variant A. A polish round then continued the trunk relief down to the cut, re-measured every must-fix and made the build reproducible from a fresh clone. Every number below was measured on the files in `out/` (`tree_meta.json`, `slice_check.json`, `checks/`).

## Result at a glance

| | |
|---|---|
| Size | 78.97 x 49.07 x 3.47 mm (scale 110.7 mm per model unit) |
| Mesh | 373,098 triangles, 186,545 vertices, binary STL |
| Volume / mass | 2597.9 mm³, 3.22 g solid PLA at 1.24 g/cm³ |
| Thickness | min 1.4, p1 1.429, median 2.351, max 3.473 mm (floor 1.4 mm everywhere) |
| Outline (visible silhouette) | 1110.63 mm², 3 enclosed holes [4.928, 2.374, 14.182] mm² |
| Glue face (z = 0) | 916.0 mm² |
| Bed chamfer | 45°, 0.15 to 0.3 mm (median 0.2609): 0.15 mm on 1.0 mm stems, 0.3 mm from 1.6 mm wide parts up |
| Trunk base | straight cut from x = -4.685 to 4.685 at y = 0; glue face from y = 0.205 |
| Trunk relief at the cut | thickness along the cut edge: median 2.573, p25 2.249, max 3.161 mm; 12 % of the edge within 0.2 mm of the floor (only the root-flare tips) |
| PrusaSlicer estimate | 3.04 g, 57m 47s (0.1 mm layers, 2 walls, 15 % infill) |

## Polish round: what changed

| Item | Before | Now |
|---|---|---|
| Trunk base | the relief drooped into the source's roots over the last ~1.5 mm: along the cut edge median 2.206 mm, p25 1.471 mm, 36 % of the edge within 0.2 mm of the 1.4 mm floor (the whole right half) | trunk continued straight down along its flutes: median 2.573 mm, p25 2.249 mm, 12 % near the floor (the two root-flare tips); 2 mm higher up the median is 2.618 mm. The cut edge, the outline and the frame origin did not move |
| Narrow slit (judge: near (36.5, 33.9)) | 0.562 mm on a 0.02 mm raster | exact wall-to-wall distance 0.5754 mm at (37.19, 33.90) (shapely, on `outline_full`); >= 0.55 mm |
| Fresh clone | depth maps read from the session scratch folder, else regenerated with npx (network) | depth maps committed as `cache/tree_depth_q16.npz` (2.4 MB, 16-bit, depth error < 0.00004 mm, height grid within 0.0003 mm of a float64 build); `--regen` or a missing cache rebuilds it from the GLB. Scratch goes to `work/` (git-ignored) |
| Thickness floor | 1.4 mm everywhere, but the code still built a zoned floor from a plaque rectangle | the plaque rectangle is only used when `--floor-in` differs from `--floor-out` (not by default) |

## Judge must-fixes and grafts: what changed

| Item | Variant B | Now |
|---|---|---|
| Pocket fit: footprint had only the glue face | glue face only (879.8 mm²) | `outline_full` key = exact outline of the vertical walls (1110.63 mm²), plus glue-face and flat-cut extents in `trunk_base` and sizing notes |
| Tip radius | min 0.36 mm | opening radius 0.42 mm; exact opening at 0.41 mm leaves 6e-05 mm² at most; circumradius over 0.2 mm arcs min 0.418 mm |
| Thickness beyond the plaque | min 1.358 mm, zoning tied to a provisional plaque rectangle | 1.4 mm floor everywhere (no zoning): ray-cast min 1.4 mm, 0.0 mm² below 1.4 |
| Bed contact of thin stems | glue face 0.42 mm, z = 0.1 section 0.57 mm | width-adaptive chamfer: glue face min 0.715 mm (p1 0.749), z = 0.1 section min 0.914 mm |
| Narrow slit at (36.5, 33.9) | 0.48 mm pinch | pinch widened (walls pushed back 0.045 mm each): exact wall distance 0.5754 mm at (37.19, 33.90), narrowest open gap 0.562 mm (raster), exact opening at 0.53 mm leaves 7e-05 mm² at most |
| Right root stub at the trunk cut | 0.90-0.93 mm wedge | trimmed back to a round cap (opening r = 0.55 mm near the cut, 1.1619 mm² removed) |
| Rim micro-facets and pits | along-rim roughness p99 0.086 mm at 0.06 mm from the edge | smooth extrapolation beyond the outline + 3.0 px blend: p99 0.0238 mm at 0.06 mm, interior (0.25 mm) 0.0275 mm |
| Detail boost (optional) | 1.0 | 0.7: closer to the source, midribs and fluting still 1-2 layers deep |
| Extra verifications (from A) | none | vertical-ray heightfield test, ray-cast thickness, exact manifold3d sections at 4 heights, glue-face area and holes |

## Method

1. **Source heights.** Orthographic depth maps of the tree front and back (0.0332 mm/px), read from the committed cache `cache/tree_depth_q16.npz`. The Tripo tree has a flat back, so relief = front depth minus a plane fitted to the back. `--regen` (or a missing cache) rebuilds the cache from the GLB (gltf-transform meshopt decode via npx, node `tripo_part_0` in world transform, embree ray casting).
2. **Silhouette clean-up.** Horizontal trunk cut at model y = -0.154, just above the jagged source cut, so the stray root running down and behind is dropped. Largest body only, source pin-holes under 0.3 mm² filled, and a 0.9 mm left root flare.
3. **Vector outline (shapely, exact buffers).** Sub-pixel contour of the smoothed mask, then 4 rounds of: thicken stems thinner than 1.05 mm with discs on their medial axis (free ends excluded over 1.2 mm), close gaps under 0.55 mm, drop holes under 0.3 mm², and open with r = 0.42 mm (rounds every tip). Then the base parts thinner than 1.1 mm are trimmed to a round cap (the right root needle), and a gap fixer widens pinches and fills dead-end notches narrower than 0.55 mm without leaving spikes. The cut edge is snapped exactly onto y = 0.
4. **Heights.** Median outlier removal, 2 bilateral passes (keeps overlap cliffs, rims and midribs). Trunk base: the source trunk droops into its roots just above the cut, so every point below 1.0 mm takes the relief of the row at 2.0 mm along a fan of straight lines that follow the measured flute direction (25.7° from vertical at the left edge, median 39.0°, 42.6° on the right flank), warped back into the source by 3.0 mm (a C1 warp, not a cross-fade, so no flute is doubled); the cut is not treated as a rim. Thickened stems get their round cross-section stretched; closed gaps are filled at the lower neighbouring height so they read as grooves. The outer 2.5 px of grazing-angle samples are rebuilt by a first-order extrapolation from inside, continued 4.0 px beyond the outline and blended back over 3.0 px. Band-pass detail boost x0.7 (0.08-0.45 mm). Thickness = 1.3 + 1.5 x (relief - 0.9), exponential soft floor at 1.4 mm, soft cap at 3.55 mm.
5. **Top surface.** Constrained Delaunay triangulation of the exact outline (triangle, min angle 28.0°), refined by inserting the worst pixel per triangle until the vertical error is at most 0.015 mm.
6. **Solid.** Vertical walls down to z = c, a 45° chamfer to the glue face and a planar bottom. The chamfer size c follows the local width (medial-axis radius, smoothed along the outline, slope-limited), and each glue vertex is its wall vertex moved inward by c, so the chamfer is a plain quad strip and the footprint is exactly the z = 0 face.

## Checks (build_tree.py)

| Check | Result |
|---|---|
| trimesh | watertight True, winding consistent True, bodies 1, Euler -4 (genus 3 = the 3 enclosed holes), degenerate faces 0 |
| manifold3d | Error.NoError, genus 3, volume 2597.86 mm³ |
| Overhangs | downward faces outside bottom and chamfer: 0; top faces min normal z 0.029; walls exactly vertical (max |nz| 0.0) |
| Chamfer angle | 45.00 to 45.70° from the bed (median 45.02°); 0.0 mm² outside 40-50° |
| Bed | bottom vertices at z = 0.0, bottom normals z = -1.000, min z 0.0 |
| Heightfield (vertical rays, 0.05 mm grid) | 444,257 columns hit: hit counts [15192, 0, 444257] (index = crossings), non-heightfield area 0.0 mm² |
| Thickness (rays) | min 1.4 mm, p1 1.4322 mm, 0.0 mm² under 1.4 mm |
| Widths, outline | min 1.036 mm, p1 1.089 mm, thinnest at (35.18, 37.83) (1.2 mm at each free end excluded) |
| Widths, glue face | min 0.715 mm, p1 0.749 mm |
| Exact section z = 0.0005 mm | 916.383 mm², 1 part(s), width min 0.718 mm, p1 0.749 mm; Hausdorff to the footprint 0.0005 mm |
| Exact section z = 0.1 mm | 992.994 mm², 1 part(s), width min 0.914 mm, p1 0.947 mm |
| Exact section z = 0.2 mm | 1066.489 mm², 1 part(s), width min 1.043 mm, p1 1.089 mm |
| Exact section z = 1 mm | 1110.632 mm², 1 part(s), width min 1.036 mm, p1 1.089 mm; differs from `outline_full` by 0.00032 mm² |
| Gaps | narrowest open gap 0.562 mm (p1 0.908) at (37.18, 33.91) |
| Tips | convex corner radius min 0.418 mm, p1 0.442 mm (circumradius over 0.20 mm arcs of the 0.01 mm resampled outline) |
| Exact openings | tips at r = 0.41: total 0.02777 mm², largest 6e-05 mm²; gaps at 0.53 mm: total 0.01674 mm², largest 7e-05 mm² (polygon discretisation only) |
| Rim roughness (along the edge, 0.3 mm high-pass, p99) | 0.03 mm in: 0.0223 mm, 0.06 mm in: 0.0238 mm, 0.12 mm in: 0.0244 mm, 0.25 mm in: 0.0275 mm |
| Top surface vs height grid | max 0.015 mm, p99 0.0114 mm, mean 0.00288 mm |
| Trunk base profile (rays along y = const, thickness min / median / max, share within 0.2 mm of the floor) | y = 0.02: 1.41 / 2.573 / 3.161 mm, 12 %; y = 0.5: 1.401 / 2.536 / 3.166 mm, 20 %; y = 1.0: 1.401 / 2.528 / 3.167 mm, 20 %; y = 2.0: 1.404 / 2.618 / 3.17 mm, 10 %; y = 3.0: 1.416 / 2.742 / 3.179 mm, 8 % |
| Glue inset | every glue vertex 0.1498 to 0.3 mm inside the outline; 35 % of the outline has the full 0.3 mm chamfer |

## Slicer coverage (slice_check.py)

PrusaSlicer 2.7 CLI, 0.1 mm layers, 0.42 mm lines, 2 arachne perimeters, 100 % infill (so any uncovered area is a real gap). The G-code paths are rasterised as round-capped beads and compared with the exact mesh section at each layer's mid height (tolerance 0.06 mm).

| print z | model mm² | uncovered mm² | % | islands | islands with no extrusion |
|---|---|---|---|---|---|
| 0.10 | 969.3 | 0.486 | 0.050 | 1 | 0 |
| 0.20 | 1046.4 | 0.838 | 0.080 | 1 | 0 |
| 0.30 | 1107.1 | 0.792 | 0.072 | 1 | 0 |
| 0.40 | 1125.2 | 0.859 | 0.076 | 1 | 0 |
| 0.60 | 1125.6 | 0.827 | 0.073 | 1 | 0 |
| 1.00 | 1125.7 | 0.824 | 0.073 | 1 | 0 |
| 1.30 | 1125.8 | 1.560 | 0.139 | 1 | 0 |
| 1.60 | 1081.1 | 1.205 | 0.111 | 2 | 1: 0.02 mm² at (-4.67, 0.03) top 1.818 |
| 2.00 | 899.9 | 2.405 | 0.267 | 4 | 1: 0.00 mm² at (-1.12, 46.07) top 2.117 |
| 2.50 | 491.0 | 7.083 | 1.443 | 54 | 7: 0.13 mm² at (0.92, 42.97) top 2.772, 0.20 mm² at (10.09, 36.03) top 2.493, 0.09 mm² at (36.7, 35.62) top 2.561 ... |
| 3.00 | 147.7 | 2.994 | 2.028 | 24 | 2: 0.10 mm² at (-12.61, 22.72) top 2.961, 0.04 mm² at (-17.95, 14.03) top 2.962 |

Islands with no extrusion only appear where the top surface sits within a layer of the slicing plane: the tops of leaf domes and crests, and the low root-flare tip at the left end of the trunk cut (about 1.55 mm thick, against the 1.55 mm mid-layer plane of the 1.6 mm layer). That spot simply ends one layer lower.

## Shared checks (tools/check_mesh.py, tools/check_slice.py)

`check_mesh.py --bodies 1 --relief on`:

| Check | Verdict | Value | Note |
|---|---|---|---|
| watertight | PASS | True | 0 open edges |
| winding consistent | PASS | True |  |
| outward normals | PASS | 2597.9 mm3 |  |
| manifold (manifold3d) | PASS | NoError |  |
| non-manifold edges | PASS | 0 |  |
| degenerate faces | PASS | 0 |  |
| duplicate faces | PASS | 0 |  |
| body count | PASS | 1 | expected 1 |
| triangle count | PASS | 373098 | limit 600000 |
| sits on z = 0 | PASS | 0.0 |  |
| fits the bed | PASS | 79.0x49.1x3.5 |  |
| overhangs > 45 deg | PASS | 0.00 mm2 | flat ceilings 0.00 mm2; in the bed band (z < 0.4) 0.0 mm2, ignored |
| thickness min (inner) | PASS | 1.4 mm | p1 1.51, median 2.498, below-min area 0.0 mm2 |
| total thickness | PASS | 3.47 mm | target 2.5..3.6 |
| rim height (outline wall) | INFO | 1.401 mm | median 2.294 |
| min width, outline (projection) | PASS | 1.04 mm | p1 1.088, necks < 0.90: 0 (0.0 mm2); sharp tips/corners 1 |
| min width, section z=0.05 | INFO | 0.792 mm | p1 0.862, necks < 0.90: 4 (4.0512 mm2) worst at [34.24, 37.48]; sharp tips/corners 23 |
| min width, section z=1 | PASS | 1.018 mm | p1 1.088, necks < 0.90: 0 (0.0 mm2); sharp tips/corners 2 |

`check_slice.py --kind tree` (0.1 mm layers, first layer 0.2 mm, 3 arachne walls, elephant-foot compensation 0.1): **PASS**. Never printed 0.0 mm²; wall volume not extruded 0.194 %; deep losses 0.41 mm²; 30 dome tops lost at their last layer (largest 0.2888 mm², lowest at layer 20); printed top 2+ layers lower 7.718 mm², higher 7.673 mm².

## Previews

`preview_top.png` (true proportions), `compare_hillshade.png` (next to a hillshade of the source front depth map at the same scale), `preview_oblique.png`, `preview_side.png`, `closeup_leaf_cluster.png` and `closeup_trunk_base.png` (40 px/mm), `preview_layers.png` (relief quantised to 0.1 mm layers), `slice_check_layers.png` (gold = extruded, red = model not covered, blue = extrusion outside the model).

## Known limits

- The previews use a simple numpy rasteriser, not a physically based renderer; wall striping is facet shading.
- The slicer checks use PrusaSlicer 2.7 as a stand-in for the user's OrcaSlicer profile.
- A faint trace of the Tripo brush striations remains on some leaves (well under one layer).
- The 1.4 mm floor applies everywhere. `--floor-in 1.2 --plaque-rect x0 y0 x1 y1` re-enables a thinner floor over the plaque if the card layout is ever frozen.
