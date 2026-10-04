# Gold tree relief

`build_tree.py` turns the Tripo tree (part 0 of `../source/tree_tripo_meshopt.glb`) into the gold tree that is
glued onto each place card. The result is a heightfield relief: the flat glue face sits on the bed at z = 0 and
the sculpted front points up. It is one watertight body in the canonical tree frame: origin at the centre of the
straight trunk-bottom edge, +x right, +y up, z = thickness.

The build is variant B from `variants/` (the judge's pick) plus the judge's must-fixes and grafts from variant A.
`variants/` is kept only for reference.

## Rebuild

```sh
python3 place-cards/tree/build_tree.py           # about 4 min on 4 CPUs; writes out/
python3 place-cards/tree/slice_check.py          # PrusaSlicer coverage check, about 1 min; rewrites out/report.md
python3 place-cards/tools/card_layout.py --check # re-fit the card to the new tree (card tool, reads out/)
```

`--regen` re-decodes the GLB with `npx @gltf-transform/cli` and re-casts the depth maps. Without it the build uses
the cached depth maps in the shared work folder. The output is deterministic: two builds give byte-identical files.

Shared checks used for `out/report.md` (results go in `out/checks/`):

```sh
python3 place-cards/tools/check_mesh.py place-cards/tree/out/tree.stl --bodies 1 --relief on \
    --json place-cards/tree/out/checks/check_mesh.json --png-dir place-cards/tree/out/checks
python3 place-cards/tools/check_slice.py place-cards/tree/out/tree.stl --kind tree \
    --out-dir place-cards/tree/out/checks --tag check
python3 place-cards/tree/make_report.py
```

## Outputs (`out/`)

| File | What it is |
|---|---|
| `tree.stl` | the print (binary STL, mm, about 373k triangles) |
| `tree_footprint.json` | `polygons`: exact outline of the z = 0 glue face. `outline_full`: exact outline of the vertical walls, i.e. the visible silhouette and the shape to size the pocket from. `trunk_base`: extents of the flat cut and of the glue face |
| `tree_meta.json` | every parameter and every measured number |
| `report.md` | method, judge fixes, checks and slicer results, written from the files above |
| `*.png` | top, oblique, side, close-ups, footprint, 0.1 mm layer view, slicer coverage |

## Key parameters

| Option | Default | Meaning |
|---|---|---|
| `--scale` | 110.7 | mm per model unit (tree about 79 x 49 mm) |
| `--base`, `--gain`, `--rim-relief` | 1.3, 1.5, 0.9 | thickness = base + gain x (relief - rim_relief) |
| `--floor-out` / `--floor-in` | 1.4 / 1.4 | minimum thickness (soft floor). Set `--floor-in 1.2 --plaque-rect ...` only if the card layout is frozen |
| `--max-thick` | 3.55 | soft cap of the total thickness |
| `--detail-gain` | 0.7 | band-pass boost of midribs and fluting (0 = off) |
| `--min-width` | 1.05 | stems thinner than this are thickened |
| `--min-gap` | 0.55 | narrower gaps are closed, filled or widened |
| `--tip-radius` | 0.42 | every convex tip is rounded to at least this radius |
| `--root-radius` | 0.55 | base parts thinner than 2x this (the root needle at the cut) are trimmed to a round cap |
| `--chamfer` / `--chamfer-min` | 0.3 / 0.15 | 45 deg bed chamfer: 0.15 mm on 1 mm stems, 0.3 mm from 1.6 mm wide parts up |
| `--cut-world-y` | -0.154 | height of the straight trunk cut (model units) |
| `--flare-left` | 0.9 | left root flare width (mm, 0 = off) |
| `--tol` | 0.015 | max vertical error of the triangulated top surface (mm) |

`python3 build_tree.py --help` lists the rest (denoising, rim, outline and meshing details).

## Files

- `build_tree.py`: the build (outline, heights, mesh, checks, outputs)
- `tree_previews.py`: numpy renders (also runs standalone on an STL)
- `slice_check.py`: slices with PrusaSlicer and rasterises the G-code against the mesh sections
- `make_report.py`: writes `out/report.md`
