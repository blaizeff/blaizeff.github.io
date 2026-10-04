#!/usr/bin/env python3
"""Slice out/tree.stl with the PrusaSlicer CLI and check that every leaf, stem and twig is extruded.

Settings follow the user's OrcaSlicer profile for the gold parts: 0.1 mm layers, 0.42 mm lines,
2 arachne perimeters, 15 % infill, 5 top / 3 bottom layers, min bead 85 %, min feature 25 %.
The G-code is parsed, the extrusion paths of selected layers are rasterised as stadium-shaped
strokes of their ;WIDTH: and compared with the model cross-section at the layer's mid height.
Reported per layer: model area, area covered, and the model area left uncovered, split into the
outline band (outer 0.9 mm, i.e. the perimeters: must be ~0) and the interior (sparse infill there
is expected). Uncovered spots larger than --min-spot are listed with their position.

Run:  python3 slice_check.py [--stl out/tree.stl] [--layer 0.1] [--first-layer 0.1]
Writes out/slice_check.json, out/slice_check_layers.png and the G-code in --work.
"""
import argparse
import json
import os
import re
import subprocess

import numpy as np
import cv2
import scipy.ndimage as ndi
import trimesh

HERE = os.path.dirname(os.path.abspath(__file__))


def slice_stl(stl, gcode, a, center):
    cmd = ['prusa-slicer', '--export-gcode', stl, '--output', gcode,
           '--nozzle-diameter', '0.4', '--filament-diameter', '1.75',
           '--layer-height', str(a.layer), '--first-layer-height', str(a.first_layer),
           '--perimeters', '2', '--perimeter-generator', 'arachne',
           '--extrusion-width', '0.42', '--perimeter-extrusion-width', '0.42',
           '--external-perimeter-extrusion-width', '0.42', '--first-layer-extrusion-width', '0.42',
           '--infill-extrusion-width', '0.45', '--solid-infill-extrusion-width', '0.45',
           '--top-infill-extrusion-width', '0.42',
           '--fill-density', '15%', '--fill-pattern', 'rectilinear',
           '--top-solid-layers', '5', '--bottom-solid-layers', '3',
           '--min-bead-width', '85%', '--min-feature-size', '25%',
           '--elefant-foot-compensation', str(a.efc),
           '--bed-shape', '0x0,256x0,256x256,0x256', '--center', f'{center[0]},{center[1]}',
           '--skirts', '0', '--brim-width', '0', '--gcode-comments']
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)
    return ' '.join(cmd)


def parse_gcode(path):
    """-> dict z -> list of (x0, y0, x1, y1, width, type)"""
    layers = {}
    x = y = z = e = 0.0
    width = 0.42
    typ = ''
    rel_e = False
    z_cur = None
    num = re.compile(r'([XYZEF])(-?\d*\.?\d+)')
    with open(path) as f:
        for line in f:
            if line.startswith(';WIDTH:'):
                width = float(line[7:])
                continue
            if line.startswith(';TYPE:'):
                typ = line[6:].strip()
                continue
            if line.startswith(';Z:'):
                z_cur = round(float(line[3:]), 3)
                continue
            if line.startswith('M83'):
                rel_e = True
            elif line.startswith('M82'):
                rel_e = False
            if line.startswith('G92'):
                for k, v in num.findall(line):
                    if k == 'E':
                        e = float(v)
                continue
            if not (line.startswith('G1 ') or line.startswith('G0 ')):
                continue
            body = line.split(';')[0]
            vals = dict((k, float(v)) for k, v in num.findall(body))
            nx, ny = vals.get('X', x), vals.get('Y', y)
            if 'Z' in vals:
                z = vals['Z']
            de = 0.0
            if 'E' in vals:
                de = vals['E'] if rel_e else vals['E'] - e
                e = e + vals['E'] if rel_e else vals['E']
            if de > 0 and (nx != x or ny != y) and z_cur is not None:
                layers.setdefault(z_cur, []).append((x, y, nx, ny, width, typ))
            x, y = nx, ny
    return layers


def rasterise_paths(segs, origin, res, shape, offset):
    img = np.zeros(shape, np.uint8)
    for (x0, y0, x1, y1, w, _) in segs:
        p0 = ((x0 - offset[0] - origin[0]) / res, (origin[1] - (y0 - offset[1])) / res)
        p1 = ((x1 - offset[0] - origin[0]) / res, (origin[1] - (y1 - offset[1])) / res)
        t = max(1, int(round(w / res)))
        cv2.line(img, (int(round(p0[0] * 4)), int(round(p0[1] * 4))), (int(round(p1[0] * 4)), int(round(p1[1] * 4))),
                 1, thickness=t, lineType=cv2.LINE_8, shift=2)
    return img.astype(bool)


def rasterise_section(mesh, z, origin, res, shape):
    sec = mesh.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
    img = np.zeros(shape, np.uint8)
    if sec is None:
        return img.astype(bool)
    p2 = sec.to_2D(to_2D=np.eye(4), check=False)[0]
    for poly in p2.polygons_full:
        ext = np.array(poly.exterior.coords)
        pts = np.c_[(ext[:, 0] - origin[0]) / res, (origin[1] - ext[:, 1]) / res]
        cv2.fillPoly(img, [np.round(pts * 4).astype(np.int32)], 1, lineType=cv2.LINE_8, shift=2)
        for hole in poly.interiors:
            hh = np.array(hole.coords)
            pts = np.c_[(hh[:, 0] - origin[0]) / res, (origin[1] - hh[:, 1]) / res]
            cv2.fillPoly(img, [np.round(pts * 4).astype(np.int32)], 0, lineType=cv2.LINE_8, shift=2)
    return img.astype(bool)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--stl', default=os.path.join(HERE, 'out', 'tree.stl'))
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    ap.add_argument('--work', default='/tmp/claude-0/-home-user-blaizeff-github-io/24db3fd0-37aa-5abe-8f21-'
                                      '849f3f5e2464/scratchpad/work/agents/A')
    ap.add_argument('--layer', type=float, default=0.1)
    ap.add_argument('--first-layer', type=float, default=0.1)
    ap.add_argument('--efc', type=float, default=0.1, help='elephant foot compensation (user profile: 0.1)')
    ap.add_argument('--res', type=float, default=0.01, help='raster resolution (mm/px)')
    ap.add_argument('--band', type=float, default=0.9, help='outline band that the perimeters must cover (mm)')
    ap.add_argument('--min-spot', type=float, default=0.01, help='report uncovered spots above this (mm2)')
    ap.add_argument('--tag', default='')
    a = ap.parse_args()
    os.makedirs(a.work, exist_ok=True)
    mesh = trimesh.load(a.stl)
    b = mesh.bounds
    center = (128.0, 128.0)
    offset = (center[0] - (b[0, 0] + b[1, 0]) / 2, center[1] - (b[0, 1] + b[1, 1]) / 2)
    gcode = os.path.join(a.work, f'tree{a.tag}.gcode')
    cmd = slice_stl(a.stl, gcode, a, center)
    layers = parse_gcode(gcode)
    zs = sorted(layers)
    # The object may be re-centred by the slicer: align on the first layer's path bbox vs the model.
    allp = np.array([s[:4] for z in zs[:3] for s in layers[z]])
    gx0, gy0 = allp[:, [0, 2]].min(), allp[:, [1, 3]].min()
    gx1, gy1 = allp[:, [0, 2]].max(), allp[:, [1, 3]].max()
    offset = ((gx0 + gx1) / 2 - (b[0, 0] + b[1, 0]) / 2, (gy0 + gy1) / 2 - (b[0, 1] + b[1, 1]) / 2)

    origin = (b[0, 0] - 1, b[1, 1] + 1)
    shape = (int((b[1, 1] - b[0, 1] + 2) / a.res), int((b[1, 0] - b[0, 0] + 2) / a.res))
    pick = [zs[0], zs[1], zs[2]] + [z for z in zs if any(abs(z - t) < 1e-6 for t in (0.4, 0.8, 1.2, 1.6, 2.0, 2.5, 3.0, 3.3))]
    pick = sorted(set(pick))
    report = {'command': cmd, 'gcode': gcode, 'n_layers': len(zs), 'layer_z': [zs[0], zs[-1]],
              'xy_offset_gcode_minus_model': offset, 'layers': []}
    tiles = []
    band_px = int(round(a.band / a.res))
    for z in pick:
        zmid = z - (a.first_layer if z == zs[0] else a.layer) / 2
        S_model = rasterise_section(mesh, zmid, origin, a.res, shape)
        S = S_model
        if z == zs[0] and a.efc > 0:
            # the slicer deliberately shrinks the first layer by the elephant-foot compensation
            S = ndi.binary_erosion(S_model, iterations=int(round(a.efc / a.res)))
        C = rasterise_paths(layers[z], origin, a.res, shape, offset)
        Cd = ndi.binary_dilation(C, iterations=2)  # 0.02 mm slack for rasterisation
        unc = S & ~Cd
        # local feature half width: largest inscribed radius within 0.3 mm
        dt = cv2.distanceTransform(S.astype(np.uint8), cv2.DIST_L2, 5) * a.res
        k = int(round(0.3 / a.res))
        lt = cv2.dilate(dt.astype(np.float32), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1)))
        interior = ndi.binary_erosion(S, iterations=band_px)
        unc_band = unc & ~interior
        # spots: open by 0.03 mm so single-pixel slivers between strokes do not count
        spots = ndi.binary_opening(unc_band, iterations=3)
        lab, n = ndi.label(spots)
        sp = []
        sliver_area = feature_area = 0.0
        if n:
            areas = ndi.sum(spots, lab, range(1, n + 1)) * a.res ** 2
            coms = ndi.center_of_mass(spots, lab, range(1, n + 1))
            wloc = ndi.maximum(lt, lab, range(1, n + 1))
            for ar, (cy, cx), wl in zip(areas, coms, wloc):
                in_feature = 2 * wl >= 0.5
                if in_feature:
                    feature_area += ar
                else:
                    sliver_area += ar
                if ar >= a.min_spot:
                    sp.append({'area_mm2': round(float(ar), 4), 'x': round(origin[0] + cx * a.res, 2),
                               'y': round(origin[1] - cy * a.res, 2),
                               'local_feature_width_mm': round(float(2 * wl), 3)})
        types = {}
        for s in layers[z]:
            types[s[5]] = types.get(s[5], 0) + np.hypot(s[2] - s[0], s[3] - s[1])
        widths = np.array([s[4] for s in layers[z]])
        # features: connected parts of the section and whether each one received extrusion
        flab, fn = ndi.label(S)
        fed = ndi.sum(C & S, flab, range(1, fn + 1)) if fn else []
        feat_area = ndi.sum(S, flab, range(1, fn + 1)) if fn else []
        unfed = [float(fa * a.res ** 2) for fa, fe in zip(feat_area, fed) if fe < 0.5 * fa]
        L = {'z': z, 'section_z': round(zmid, 3), 'model_area_mm2': round(float(S.sum() * a.res ** 2), 2),
             'extruded_area_mm2': round(float((C & S).sum() * a.res ** 2), 2),
             'outside_model_mm2': round(float((C & ~ndi.binary_dilation(S, iterations=5)).sum() * a.res ** 2), 3),
             'uncovered_total_mm2': round(float(unc.sum() * a.res ** 2), 3),
             'uncovered_band_mm2': round(float(unc_band.sum() * a.res ** 2), 3),
             'uncovered_band_spots_mm2_in_features_ge_0.5mm': round(feature_area, 4),
             'uncovered_band_spots_mm2_in_slivers_lt_0.5mm': round(sliver_area, 4),
             'uncovered_band_spots_ge_min': sp,
             'separate_section_parts': int(fn), 'parts_without_extrusion_mm2': unfed,
             'path_length_by_type_mm': {k: round(v, 1) for k, v in types.items()},
             'width_min_max': [float(widths.min()), float(widths.max())]}
        report['layers'].append(L)
        print(json.dumps({k: v for k, v in L.items() if k not in ('path_length_by_type_mm', 'uncovered_band_spots_ge_min')}))
        # preview tile: model grey, extruded gold, uncovered band red, uncovered interior pink
        img = np.full(shape + (3,), 255, np.uint8)
        img[S_model] = (150, 150, 150)
        img[S] = (190, 190, 190)
        img[C] = (212, 175, 55)
        img[unc & interior] = (255, 200, 200)
        img[unc_band] = (255, 0, 0)
        img[ndi.binary_dilation(spots, iterations=6) & ~spots] = (255, 0, 0)
        small = cv2.resize(img, (shape[1] // 4, shape[0] // 4), interpolation=cv2.INTER_AREA)
        cv2.putText(small, f'z={z:.2f}', (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
        tiles.append(small)
    # mosaic
    cols = 3
    th, tw = tiles[0].shape[:2]
    rows_n = (len(tiles) + cols - 1) // cols
    mos = np.full((rows_n * th, cols * tw, 3), 255, np.uint8)
    for i, t in enumerate(tiles):
        mos[(i // cols) * th:(i // cols + 1) * th, (i % cols) * tw:(i % cols + 1) * tw] = t
    cv2.imwrite(os.path.join(a.out, f'slice_check_layers{a.tag}.png'), cv2.cvtColor(mos, cv2.COLOR_RGB2BGR))
    # full-resolution crop of the first layer around the trunk and a thin-stem area for inspection
    with open(os.path.join(a.out, f'slice_check{a.tag}.json'), 'w') as f:
        json.dump(report, f, indent=1)
    tot = sum(L['uncovered_band_mm2'] for L in report['layers'])
    print('layers', len(zs), 'checked', len(pick), 'uncovered band total mm2', round(tot, 3),
          'spots', sum(len(L['uncovered_band_spots_ge_min']) for L in report['layers']))


if __name__ == '__main__':
    main()
