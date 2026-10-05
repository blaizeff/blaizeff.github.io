#!/usr/bin/env python3
"""Slicer check for the gold tree: does every leaf, stem and twig actually get extruded?

Slices out/tree.stl with the PrusaSlicer CLI (0.1 mm layers, 0.42 mm lines, 2 perimeters, arachne),
parses the G-code, rasterizes the extrusion paths of selected layers (bead = line width wide, round
ends) and compares them with the model cross-section at the slicing height (mid-layer) taken from
the mesh itself. Reports the model area not covered by any extrusion and where it is.

  python3 slice_check.py [--stl out/tree.stl] [--out out/] [--layers 0.1,0.2,0.3,0.6,1.0,...]

Writes out/slice_check.json and out/slice_check_layers.png (grey = model section, gold = extruded,
red = model not covered, blue = extrusion outside the model).
"""
import argparse
import json
import os
import re
import subprocess

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, 'work')        # G-code and debug images (git-ignored)


def slice_stl(stl, gcode, a, fill='100%'):
    cmd = ['prusa-slicer', '--export-gcode', stl, '--output', gcode,
           '--layer-height', str(a.layer), '--first-layer-height', str(a.layer),
           '--nozzle-diameter', '0.4', '--filament-diameter', '1.75',
           '--extrusion-width', str(a.width), '--first-layer-extrusion-width', str(a.width),
           '--perimeters', '2', '--perimeter-generator', a.generator,
           '--top-solid-layers', '5', '--bottom-solid-layers', '3', '--fill-density', fill, '--fill-pattern', 'rectilinear' if fill == '100%' else 'grid',
           '--skirts', '0', '--brim-width', '0', '--center', '128,128',
           '--use-relative-e-distances', '--gcode-comments', '--filament-density', '1.24']
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit('prusa-slicer failed:\n' + r.stdout[-2000:] + r.stderr[-2000:])
    # record paths relative to place-cards/ so the report does not depend on where the repo is cloned
    pc = os.path.dirname(HERE)
    shown = [os.path.relpath(c, pc) if os.path.isabs(c) and c.startswith(pc + os.sep) else c for c in cmd]
    return ' '.join(shown), r.stdout[-600:]


def parse_gcode(path):
    """Extrusion segments per print_z: {z: [(x0, y0, x1, y1, width, type), ...]}."""
    layers = {}
    x = y = 0.0
    z = 0.0
    width = 0.42
    typ = ''
    cur = None
    info = {}
    num = re.compile(r'([XYZEF])(-?\d*\.?\d+)')
    with open(path) as f:
        for line in f:
            if line.startswith(';'):
                if line.startswith(';Z:'):
                    z = round(float(line[3:]), 4)
                    cur = layers.setdefault(z, [])
                elif line.startswith(';WIDTH:'):
                    width = float(line[7:])
                elif line.startswith(';TYPE:'):
                    typ = line[6:].strip()
                elif 'estimated printing time (normal mode)' in line:
                    info['time'] = line.split('=')[-1].strip()
                elif line.startswith('; filament used [mm]'):
                    info['filament_mm'] = float(line.split('=')[-1])
                elif line.startswith('; filament used [g]'):
                    info['filament_g'] = float(line.split('=')[-1])
                continue
            if not (line.startswith('G1') or line.startswith('G0')):
                continue
            body = line.split(';')[0]
            vals = dict((k, float(v)) for k, v in num.findall(body))
            nx = vals.get('X', x)
            ny = vals.get('Y', y)
            e = vals.get('E', 0.0)
            if e > 0 and ('X' in vals or 'Y' in vals) and cur is not None:
                cur.append((x, y, nx, ny, width, typ))
            x, y = nx, ny
    return layers, info


def section_polygon(mesh, zc):
    """Model cross-section at height zc as shapely (Multi)Polygon in model xy."""
    from shapely.ops import unary_union
    sec = mesh.section(plane_origin=[0, 0, zc], plane_normal=[0, 0, 1.0])
    if sec is None:
        return None
    T = np.eye(4)
    T[2, 3] = -zc
    planar, _ = sec.to_2D(to_2D=T, check=False)
    polys = list(planar.polygons_full)
    return unary_union(polys) if polys else None


def raster_poly(poly, origin, shape, ppmm):
    import cv2
    img = np.zeros(shape, np.uint8)
    if poly is None:
        return img.astype(bool)
    SH = 4
    for g in getattr(poly, 'geoms', [poly]):
        if g.geom_type != 'Polygon':
            continue
        rings = [g.exterior] + list(g.interiors)
        pts = []
        for r in rings:
            xy = np.asarray(r.coords)
            c = (xy[:, 0] - origin[0]) * ppmm
            rr = (origin[1] - xy[:, 1]) * ppmm
            pts.append(np.round(np.c_[c, rr] * (1 << SH)).astype(np.int32))
        cv2.fillPoly(img, pts, 1, lineType=cv2.LINE_8, shift=SH)
    return img.astype(bool)


def raster_paths(segs, off, origin, shape, ppmm):
    """Beads as thick lines with round ends (cv2 thick lines are round-capped)."""
    import cv2
    img = np.zeros(shape, np.uint8)
    SH = 4
    for x0, y0, x1, y1, w, t in segs:
        p0 = ((x0 - off[0] - origin[0]) * ppmm, (origin[1] - (y0 - off[1])) * ppmm)
        p1 = ((x1 - off[0] - origin[0]) * ppmm, (origin[1] - (y1 - off[1])) * ppmm)
        th = max(1, int(round(w * ppmm)))
        cv2.line(img, (int(p0[0] * 16), int(p0[1] * 16)), (int(p1[0] * 16), int(p1[1] * 16)), 1, th,
                 lineType=cv2.LINE_8, shift=SH)
    return img.astype(bool)


def main():
    import trimesh
    from scipy import ndimage as ndi
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--stl', default=os.path.join(HERE, 'out', 'tree.stl'))
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    ap.add_argument('--work', default=WORK)
    ap.add_argument('--layer', type=float, default=0.1)
    ap.add_argument('--width', type=float, default=0.42)
    ap.add_argument('--generator', default='arachne')
    ap.add_argument('--layers', default='0.1,0.2,0.3,0.4,0.6,1.0,1.3,1.6,2.0,2.5,3.0')
    ap.add_argument('--ppmm', type=float, default=25.0)
    ap.add_argument('--tol', type=float, default=0.06, help='coverage tolerance (mm)')
    ap.add_argument('--no-report', action='store_true', help='do not (re)write report.md')
    a = ap.parse_args()
    os.makedirs(a.work, exist_ok=True)
    gcode = os.path.join(a.work, 'tree_check.gcode')
    # coverage slice: 100 % infill so any uncovered model area is a real gap, not sparse infill
    cmd, sout = slice_stl(a.stl, gcode, a, '100%')
    layers, _ = parse_gcode(gcode)
    # realistic slice (15 % infill) for time / filament
    gcode2 = os.path.join(a.work, 'tree_print15.gcode')
    cmd2, _ = slice_stl(a.stl, gcode2, a, '15%')
    _, ginfo = parse_gcode(gcode2)
    mesh = trimesh.load(a.stl)
    b = mesh.bounds
    # PrusaSlicer --center puts the bbox centre at (128, 128)
    off0 = np.array([128 - (b[0, 0] + b[1, 0]) / 2, 128 - (b[0, 1] + b[1, 1]) / 2])
    ppmm = a.ppmm
    origin = (b[0, 0] - 1.0, b[1, 1] + 1.0)
    shape = (int((b[1, 1] - b[0, 1] + 2) * ppmm), int((b[1, 0] - b[0, 0] + 2) * ppmm))
    zs = sorted(layers.keys())
    want = [float(v) for v in a.layers.split(',')]
    tolpx = int(round(a.tol * ppmm))
    kern = np.ones((2 * tolpx + 1, 2 * tolpx + 1), bool)
    yy, xx = np.mgrid[-tolpx:tolpx + 1, -tolpx:tolpx + 1]
    kern = xx * xx + yy * yy <= tolpx * tolpx
    # fine alignment on the first layer (sub-mm offset search)
    z1 = zs[0]
    sec1 = raster_poly(section_polygon(mesh, z1 - a.layer / 2), origin, shape, ppmm)
    best = None
    for dx in np.arange(-0.3, 0.31, 0.04):
        for dy in np.arange(-0.3, 0.31, 0.04):
            ext = raster_paths(layers[z1], off0 + [dx, dy], origin, shape, ppmm)
            score = (ext & sec1).sum() - (ext & ~sec1).sum()
            if best is None or score > best[0]:
                best = (score, dx, dy)
    off = off0 + [best[1], best[2]]
    results = []
    tiles = []
    for zw in want:
        zz = min(zs, key=lambda q: abs(q - zw))
        zc = zz - a.layer / 2
        poly = section_polygon(mesh, zc)
        sec = raster_poly(poly, origin, shape, ppmm)
        ext = raster_paths(layers[zz], off, origin, shape, ppmm)
        unc = sec & ~ndi.binary_dilation(ext, kern)
        over = ext & ~ndi.binary_dilation(sec, kern)
        lab, n = ndi.label(unc)
        blobs = []
        if n:
            areas = ndi.sum(unc, lab, range(1, n + 1)) / ppmm ** 2
            cms = ndi.center_of_mass(unc, lab, range(1, n + 1))
            for ar, (cr, cc) in sorted(zip(areas, cms), key=lambda t: -t[0])[:12]:
                if ar < 0.01:
                    continue
                blobs.append({'area_mm2': round(float(ar), 4),
                              'x': round(float(origin[0] + cc / ppmm), 2), 'y': round(float(origin[1] - cr / ppmm), 2)})
        # islands of the section that get no extrusion at all (a whole feature lost)
        slab, sn = ndi.label(sec)
        lost = []
        if sn:
            hit = ndi.maximum(ext, slab, range(1, sn + 1))
            sizes = ndi.sum(sec, slab, range(1, sn + 1)) / ppmm ** 2
            cms = ndi.center_of_mass(sec, slab, range(1, sn + 1))
            for k in range(sn):
                if not hit[k]:
                    lx = float(origin[0] + cms[k][1] / ppmm)
                    ly = float(origin[1] - cms[k][0] / ppmm)
                    rad = float(np.sqrt(sizes[k] / np.pi)) + 0.3
                    near = np.hypot(mesh.vertices[:, 0] - lx, mesh.vertices[:, 1] - ly) < rad
                    lost.append({'area_mm2': round(float(sizes[k]), 4), 'x': round(lx, 2), 'y': round(ly, 2),
                                 'local_max_z': round(float(mesh.vertices[near, 2].max()), 3) if near.any() else None})
        ra = sec.sum() / ppmm ** 2
        results.append({'print_z': zz, 'slice_z': round(zc, 3), 'model_area_mm2': round(float(ra), 2),
                        'extruded_area_mm2': round(float(ext.sum() / ppmm ** 2), 2),
                        'uncovered_mm2': round(float(unc.sum() / ppmm ** 2), 4),
                        'uncovered_pct': round(float(100 * unc.sum() / max(sec.sum(), 1)), 3),
                        'outside_mm2': round(float(over.sum() / ppmm ** 2), 4),
                        'section_islands': int(sn), 'islands_without_extrusion': lost,
                        'largest_uncovered_blobs': blobs, 'segments': len(layers[zz])})
        img = np.full(shape + (3,), 255, np.uint8)
        img[sec] = (200, 200, 200)
        img[ext & sec] = (212, 175, 55)
        img[over] = (60, 90, 230)
        img[ndi.binary_dilation(unc, iterations=1)] = (230, 30, 30)
        tiles.append((zz, img))
    # montage (downscaled)
    from PIL import Image, ImageDraw
    sc = 0.5
    ims = []
    for zz, img in tiles:
        im = Image.fromarray(img)
        im = im.resize((int(im.size[0] * sc), int(im.size[1] * sc)), Image.NEAREST)
        ImageDraw.Draw(im).text((10, 10), f'print z {zz:.2f} mm', fill=(0, 0, 0))
        ims.append(im)
    cols = 3
    w, h = ims[0].size
    rows = (len(ims) + cols - 1) // cols
    mont = Image.new('RGB', (cols * w, rows * h), (255, 255, 255))
    for i, im in enumerate(ims):
        mont.paste(im, ((i % cols) * w, (i // cols) * h))
    mont.save(os.path.join(a.out, 'slice_check_layers.png'))
    # first layer full resolution
    Image.fromarray(tiles[0][1]).save(os.path.join(a.work, 'slice_first_layer_full.png'))
    out = {'command_coverage_slice': cmd, 'command_print_estimate': cmd2, 'print_estimate_15pct_infill': ginfo, 'layers_in_gcode': len(zs), 'max_print_z': zs[-1],
           'alignment_offset_mm': [round(float(v), 3) for v in off], 'alignment_refine_mm': [best[1], best[2]],
           'coverage_tolerance_mm': a.tol, 'raster_ppmm': ppmm, 'layers': results}
    with open(os.path.join(a.out, 'slice_check.json'), 'w') as f:
        json.dump(out, f, indent=1)
    for r in results:
        print(f"z {r['print_z']:.2f}: model {r['model_area_mm2']:8.2f} mm2, uncovered {r['uncovered_mm2']:.4f} mm2 "
              f"({r['uncovered_pct']:.3f}%), outside {r['outside_mm2']:.3f}, islands {r['section_islands']}, "
              f"lost {len(r['islands_without_extrusion'])}")
    print(ginfo)
    if not a.no_report:
        import sys
        sys.path.insert(0, HERE)
        import make_report
        make_report.main(a.out)


if __name__ == '__main__':
    main()
