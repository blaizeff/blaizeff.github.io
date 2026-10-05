# Gold tree relief

`build_tree.py` turns the Tripo tree (part 0 of `../source/tree_tripo_meshopt.glb`) into the gold tree that is
glued onto each place card. The result is a heightfield relief: the flat glue face sits on the bed at z = 0 and
the sculpted front points up. It is one watertight body in the canonical tree frame: origin at the centre of the
straight trunk-bottom edge, +x right, +y up, z = thickness.

The build is variant B from `variants/` (the judge's pick) plus the judge's must-fixes and grafts from variant A,
then a polish round (trunk relief continued down to the cut, must-fixes re-measured, fresh-clone build).
`variants/` is kept only for reference.

## Rebuild

```sh
python3 place-cards/tree/build_tree.py           # about 3 min on 4 CPUs; writes out/
python3 place-cards/tree/slice_check.py          # PrusaSlicer coverage check, about 1 min; rewrites out/report.md
python3 place-cards/tools/card_layout.py --check # re-fit the card to the new tree (card tool, reads out/)
```

A fresh clone needs only Python (packages listed in the `build_tree.py` docstring): the depth maps of the source
model are committed in `cache/`. Scratch files (height grid, G-code, regenerated GLB data) go to `work/`, which git
ignores. The output is deterministic: the same cache and library versions give byte-identical files (checked: a
build in a fresh clone of the repo reproduces `out/tree.stl`, `tree_footprint.json`, `tree_meta.json` and the
previews byte for byte).

Shared checks used for `out/report.md` (results go in `out/checks/`):

```sh
python3 place-cards/tools/check_mesh.py place-cards/tree/out/tree.stl --bodies 1 --relief on \
    --json place-cards/tree/out/checks/check_mesh.json --png-dir place-cards/tree/out/checks
python3 place-cards/tools/check_slice.py place-cards/tree/out/tree.stl --kind tree \
    --out-dir place-cards/tree/out/checks --tag check
python3 place-cards/tree/make_report.py
```

## Depth cache (`cache/tree_depth_q16.npz`)

Orthographic depth maps of the Tripo tree, front and back, 1665 x 2379 px at 0.0003 model units (0.0332 mm) per
pixel. Format: the hit mask as packed bits, and per map the depth of the hit pixels quantised to 16 bits over its
own range and delta-coded (mod 2^16), so the file is 2.4 MB instead of 14.7 MB of float64.

* Accuracy: depth error at most 0.000036 mm. The height grid built from it is within 0.0003 mm of a build from
  the float64 maps (identical mask and outline). The two STLs then differ only in which points the adaptive
  triangulation picks (3D distance at most 0.015 mm, the meshing tolerance `--tol`), so the committed cache, not
  the float64 maps, is the reference input.
* Provenance: written from the float64 maps every earlier build used (`tree_front.npz` / `tree_back.npz` of the
  first exploration), with `save_depth_q16()`.
* `--regen` rebuilds it from the GLB and overwrites `--depth`: meshopt decode with `npx @gltf-transform/cli`
  (node, and network on first use), node `tripo_part_0` in world transform, embree ray casting. A missing cache is
  regenerated the same way (tested from a fresh clone with an empty cache path: 2.5 min, about 15 s of it for the
  decode and ray casting). A regenerated cache differs from the committed one by 1 pixel of coverage and 0.0005 mm
  of depth (embree), and that pixel moves the outline locally by up to 0.24 mm (near (41.3, 39.3)). The tree is
  just as printable (same minimum widths, gaps, tip radii and thickness), but the card must be re-fitted with
  `tools/card_layout.py` after a regeneration. Keeping the cache committed is what keeps the card's pocket matched.
* `--float-cache DIR` reads float64 `tree_front.npz` / `tree_back.npz` instead (for comparisons).

## Outputs (`out/`)

| File | What it is |
|---|---|
| `tree.stl` | the print (binary STL, mm, about 373k triangles) |
| `tree_footprint.json` | `polygons`: exact outline of the z = 0 glue face. `outline_full`: exact outline of the vertical walls, i.e. the visible silhouette and the shape to size the pocket from. `trunk_base`: extents of the flat cut and of the glue face |
| `tree_meta.json` | every parameter and every measured number (paths relative to `place-cards/`) |
| `report.md` | method, judge fixes, polish round, checks and slicer results, written from the files above |
| `*.png` | top, oblique, side, close-ups, footprint, 0.1 mm layer view, slicer coverage, comparison with the source |

## Trunk base

The Tripo trunk flares into roots that run down and behind just above the cut, so its relief drooped over the
last 1.5 mm (36 % of the cut edge sat within 0.2 mm of the 1.4 mm floor). The build now continues the trunk
straight down: below `Y_LO` every point takes the relief of the row midway in `--base-band` along a fan of straight
lines that follow the measured flute direction (about 26° from vertical at the left edge to 43° on the right
flank), and between `Y_LO` and `Y_HI` the sample point slides back into the source along a C1 curve (a warp, so
flutes are not doubled where the source flutes curve off into the roots). The outline, the straight cut at y = 0
and the frame origin are unchanged; `tree_footprint.json` describes the same shapes as before. Only the two
root-flare tips at the ends of the cut stay low.

## Key parameters

| Option | Default | Meaning |
|---|---|---|
| `--scale` | 110.7 | mm per model unit (tree about 79 x 49 mm) |
| `--base`, `--gain`, `--rim-relief` | 1.3, 1.5, 0.9 | thickness = base + gain x (relief - rim_relief) |
| `--floor-out` / `--floor-in` | 1.4 / 1.4 | minimum thickness (soft floor). The plaque rectangle (`--plaque-rect`) is only used when the two differ; set `--floor-in 1.2 --plaque-rect ...` only if the card layout is frozen |
| `--max-thick` | 3.55 | soft cap of the total thickness |
| `--detail-gain` | 0.7 | band-pass boost of midribs and fluting (0 = off) |
| `--min-width` | 1.05 | stems thinner than this are thickened |
| `--min-gap` | 0.55 | narrower gaps are closed, filled or widened |
| `--tip-radius` | 0.42 | every convex tip is rounded to at least this radius |
| `--root-radius` | 0.55 | base parts thinner than 2x this (the root needle at the cut) are trimmed to a round cap |
| `--chamfer` / `--chamfer-min` | 0.3 / 0.15 | 45 deg bed chamfer: 0.15 mm on 1 mm stems, 0.3 mm from 1.6 mm wide parts up |
| `--cut-world-y` | -0.154 | height of the straight trunk cut (model units) |
| `--base-band` | 1.0 3.0 | trunk base: relief continued straight down below 1 mm, from the row at 2 mm, warped back by 3 mm (`0 0` = off) |
| `--base-dir-sigma` | 0.8 | smoothing of the measured flute direction across the trunk (mm) |
| `--flare-left` | 0.9 | left root flare width (mm, 0 = off) |
| `--tol` | 0.015 | max vertical error of the triangulated top surface (mm) |
| `--depth`, `--regen`, `--float-cache`, `--work` | see above | depth cache, its regeneration, float64 input, scratch folder |

`python3 build_tree.py --help` lists the rest (denoising, rim, outline and meshing details).

## Files

- `build_tree.py`: the build (depth cache, outline, heights, mesh, checks, outputs)
- `cache/tree_depth_q16.npz`: the committed depth maps (see above)
- `tree_previews.py`: numpy renders (also runs standalone on an STL)
- `slice_check.py`: slices with PrusaSlicer and rasterises the G-code against the mesh sections
- `make_report.py`: writes `out/report.md`
