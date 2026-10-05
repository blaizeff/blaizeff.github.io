#!/usr/bin/env python3
"""Photo-style render of the assembled place card: ivory plaque, metallic silk gold name, border
and tree, wood foot, on a linen table cloth. three.js in headless Chromium (Playwright).

The scene is assembled exactly like check_assembly.py / the SCAD's assembly() (lean, slot,
tree_pos, foot position read from the SCAD and tree_data.scad). Meshes go to the page as raw
float32 buffers (crease-angle normals: smooth relief, crisp plaque edges); the wood gets faint
0.2 mm layer lines. Lighting: warm low key light with soft shadows, fill, and an image-based
room environment for the gold reflections; ACES tone mapping.

Views (--views, comma separated): hero (front-left, above, like the reference photo), front,
side, top, closeup (tree and trunk/foot join). PNGs go to --out-dir as
scene_<name>_<view>.png; with --reference a side-by-side compare_<name>.png is made too.

Needs: Node + the global playwright package with Chromium in /opt/pw-browsers (or
PLAYWRIGHT_BROWSERS_PATH), and three.js: found in --three-dir, else installed once with npm
into ~/.cache/place-cards-render.

Run (from the repo root):
  python3 place-cards/tools/render_scene.py --name Sophie \
      --base place-cards/stl/plaque_Sophie_base.stl --gold place-cards/stl/plaque_Sophie_gold.stl \
      --foot place-cards/stl/foot.stl --tree place-cards/tree/out/tree.stl \
      --reference /path/to/reference.webp --out-dir place-cards/renders
  options: --views hero,front --size 1536x1024 --scad other.scad --tree-pos X,Y --foot-x X
           --exposure 1.0 --samples 2 (supersampling factor)
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_common as C  # noqa: E402

THREE_VERSION = "0.169.0"
CACHE = os.path.expanduser("~/.cache/place-cards-render")

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<style>html,body{margin:0;background:#000;overflow:hidden}</style>
<script type="importmap">{"imports":{"three":"http://render.local/three/build/three.module.js",
"three/addons/":"http://render.local/three/examples/jsm/"}}</script>
</head><body>
<script type="module">
import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
const cfg = await (await fetch('http://render.local/scene.json')).json();
const W = cfg.width, H = cfg.height;
const renderer = new THREE.WebGLRenderer({antialias: true, preserveDrawingBuffer: true});
renderer.setPixelRatio(1);
renderer.setSize(W, H);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = cfg.exposure;
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(cfg.background);
const pmrem = new THREE.PMREMGenerator(renderer);
// studio environment: warm gradient dome plus two soft boxes, so the metallic gold reads as
// gold (a plain room box mirrors white panels into the flat letter tops)
function studio() {
  const s = new THREE.Scene();
  const geo = new THREE.SphereGeometry(100, 64, 32);
  const col = [];
  const p = geo.attributes.position;
  for (let i = 0; i < p.count; i++) {
    const t = p.getY(i) / 100;                       // -1 bottom .. 1 top
    const top = [0.95, 0.86, 0.74], mid = [0.7, 0.6, 0.48], bot = [0.2, 0.15, 0.1];
    const a = t > 0 ? t : -t, c = t > 0 ? top : bot;
    const f = 0.72 - 0.28 * p.getZ(i) / 100;         // darker on the camera side, like a real room
    col.push(f * (mid[0] + (c[0] - mid[0]) * Math.pow(a, 0.7)), f * (mid[1] + (c[1] - mid[1]) * Math.pow(a, 0.7)),
             f * (mid[2] + (c[2] - mid[2]) * Math.pow(a, 0.7)));
  }
  geo.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  s.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({vertexColors: true, side: THREE.BackSide})));
  for (const [pos, size, k] of cfg.softboxes) {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(size[0], size[1]),
      new THREE.MeshBasicMaterial({color: new THREE.Color(k, k * 0.93, k * 0.82), side: THREE.DoubleSide}));
    m.position.set(...pos); m.lookAt(0, 0, 0); s.add(m);
  }
  return s;
}
scene.environment = pmrem.fromScene(cfg.room ? new RoomEnvironment() : studio(), 0.02).texture;
scene.environmentIntensity = cfg.env;

function canvasTex(w, h, draw, rep) {
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  draw(c.getContext('2d'), w, h);
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  if (rep) t.repeat.set(rep[0], rep[1]);
  t.anisotropy = 8;
  return t;
}
// 0.2 mm layer lines for the wood foot (v = z / 1.6 mm, 8 lines per tile)
const lines = canvasTex(8, 256, (g, w, h) => {
  for (let y = 0; y < h; y++) { const s = 0.5 + 0.5 * Math.cos(2 * Math.PI * y / 32); const v = Math.round(255 * (0.55 + 0.45 * s));
    g.fillStyle = `rgb(${v},${v},${v})`; g.fillRect(0, y, w, 1); }
});
// linen weave for the cloth
const weave = canvasTex(256, 256, (g, w, h) => {
  g.fillStyle = '#808080'; g.fillRect(0, 0, w, h);
  for (let i = 0; i < w; i += 4) { g.fillStyle = (i / 4) % 2 ? '#8c8c8c' : '#747474'; g.fillRect(i, 0, 2, h); }
  for (let j = 0; j < h; j += 4) { g.fillStyle = (j / 4) % 2 ? 'rgba(150,150,150,0.55)' : 'rgba(100,100,100,0.55)'; g.fillRect(0, j, w, 2); }
}, [40, 40]);

const mats = {
  ivory: new THREE.MeshPhysicalMaterial({color: cfg.colours.ivory, roughness: 0.38, metalness: 0.0,
           clearcoat: 0.25, clearcoatRoughness: 0.35, sheen: 0.25, sheenColor: new THREE.Color('#fffaf0')}),
  gold: new THREE.MeshPhysicalMaterial({color: cfg.colours.gold, roughness: 0.34, metalness: 1.0}),
  wood: new THREE.MeshStandardMaterial({color: cfg.colours.wood, roughness: 0.8, metalness: 0.0, envMapIntensity: 0.3,
           bumpMap: lines, bumpScale: 1.2}),
};
async function bin(url, T) { return new T(await (await fetch(url)).arrayBuffer()); }
const group = new THREE.Group();
for (const p of cfg.parts) {
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(await bin(p.pos, Float32Array), 3));
  g.setAttribute('normal', new THREE.BufferAttribute(await bin(p.nrm, Float32Array), 3));
  if (p.uv) g.setAttribute('uv', new THREE.BufferAttribute(await bin(p.uv, Float32Array), 2));
  g.setIndex(new THREE.BufferAttribute(await bin(p.idx, Uint32Array), 1));
  const m = new THREE.Mesh(g, mats[p.material]);
  m.castShadow = true; m.receiveShadow = true;
  group.add(m);
}
scene.add(group);
const cloth = new THREE.Mesh(new THREE.PlaneGeometry(2000, 2000),
  new THREE.MeshStandardMaterial({color: cfg.cloth, roughness: 0.95, bumpMap: weave, bumpScale: 0.6}));
cloth.rotation.x = -Math.PI / 2; cloth.receiveShadow = true;
scene.add(cloth);
scene.add(new THREE.HemisphereLight(0xfff6ea, 0xcdb89c, cfg.hemi));
const key = new THREE.DirectionalLight(0xffe7c4, cfg.key);
key.position.set(...cfg.keyPos); key.target.position.set(...cfg.target);
key.castShadow = true; key.shadow.mapSize.set(4096, 4096);
const sc = key.shadow.camera; sc.left = -140; sc.right = 140; sc.top = 140; sc.bottom = -140; sc.near = 1; sc.far = 1200;
key.shadow.bias = -0.0004; key.shadow.normalBias = 0.02; key.shadow.radius = 6;
scene.add(key); scene.add(key.target);
const fill = new THREE.DirectionalLight(0xf3f6ff, cfg.fill);
fill.position.set(...cfg.fillPos); scene.add(fill);
let cam;
if (cfg.ortho) {
  const hh = cfg.orthoH / 2, ww = hh * W / H;
  cam = new THREE.OrthographicCamera(-ww, ww, hh, -hh, 1, 5000);
} else {
  cam = new THREE.PerspectiveCamera(cfg.fov, W / H, 1, 5000);
}
cam.position.set(...cfg.camPos); cam.up.set(0, 1, 0); cam.lookAt(...cfg.target);
renderer.render(scene, cam);
window.__done = true;
</script></body></html>
"""

NODE = r"""
const { chromium } = require('playwright');
const fs = require('fs'), path = require('path');
(async () => {
  const [dir, out, w, h] = process.argv.slice(2);
  const three = process.env.THREE_DIR;
  const browser = await chromium.launch({args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader',
                                                '--ignore-gpu-blocklist']});
  const page = await browser.newPage({viewport: {width: +w, height: +h}});
  page.on('console', m => console.log('page:', m.text()));
  page.on('pageerror', e => console.log('pageerror:', e.message));
  await page.route('http://render.local/**', route => {
    const u = new URL(route.request().url());
    let f = u.pathname.startsWith('/three/') ? path.join(three, u.pathname.slice(7)) : path.join(dir, u.pathname.slice(1));
    const type = f.endsWith('.js') ? 'text/javascript' : f.endsWith('.json') ? 'application/json'
               : f.endsWith('.html') ? 'text/html' : 'application/octet-stream';
    if (!fs.existsSync(f)) return route.fulfill({status: 404, body: 'missing ' + f});
    route.fulfill({status: 200, contentType: type, body: fs.readFileSync(f)});
  });
  await page.goto('http://render.local/index.html');
  await page.waitForFunction('window.__done === true', null, {timeout: 600000});
  const el = await page.$('canvas');
  await el.screenshot({path: out});
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


def find_three(arg):
    for d in ([arg] if arg else []) + [os.path.join(CACHE, "node_modules", "three")]:
        if d and os.path.exists(os.path.join(d, "build", "three.module.js")):
            return d
    os.makedirs(CACHE, exist_ok=True)
    subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--silent", "--prefix", CACHE,
                    f"three@{THREE_VERSION}"], check=True)
    return os.path.join(CACHE, "node_modules", "three")


def node_env(three):
    env = dict(os.environ)
    env["THREE_DIR"] = three
    npm_root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    env["NODE_PATH"] = os.pathsep.join(p for p in [npm_root, env.get("NODE_PATH", "")] if p)
    if "PLAYWRIGHT_BROWSERS_PATH" not in env and os.path.isdir("/opt/pw-browsers"):
        env["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"
    return env


def assemble(args):
    P = C.card_params(args.scad, args.tree_data, name=args.name)   # name: this card's foot position
    if args.tree_pos:
        P["tree_pos"] = [float(v) for v in args.tree_pos.split(",")]
    meshes = {"base": C.load_mesh(args.base), "foot": C.load_mesh(args.foot)}
    if args.gold:
        meshes["gold"] = C.load_mesh(args.gold)
    if args.tree:
        meshes["tree"] = C.load_mesh(args.tree)
    lo, hi = meshes["base"].bounds
    rect_x0 = 0.0 if lo[0] < -0.5 else lo[0]
    foot_x = args.foot_x if args.foot_x is not None else P.get("assembly_foot_x")
    if foot_x is None:
        foot_x = (rect_x0 + hi[0]) / 2
    Mp = C.plaque_to_world(P)
    M = {"base": Mp, "gold": Mp, "foot": C.foot_to_world(foot_x)}
    if "tree" in meshes:
        M["tree"] = Mp @ C.tree_to_plaque(P)
    world = {k: m.copy().apply_transform(M[k]) for k, m in meshes.items()}
    return world, P


def export_parts(world, d):
    """World (z up, viewer at -y) -> three.js (y up, viewer at +z) buffers."""
    import trimesh
    parts = []
    material = {"base": "ivory", "gold": "gold", "tree": "gold", "foot": "wood"}
    R = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], float)      # (x, y, z) -> (x, z, -y)
    for k, m in world.items():
        angle = math.radians(35 if k == "tree" else 28)
        sm = trimesh.graph.smooth_shade(m, angle=angle, facet_minarea=None) if k != "tree" else m
        v = np.asarray(sm.vertices, float)
        n = np.asarray(sm.vertex_normals, float)
        f = np.asarray(sm.faces, np.uint32)
        rec = {"name": k, "material": material[k]}
        for key, arr, dt in (("pos", v @ R.T, np.float32), ("nrm", n @ R.T, np.float32), ("idx", f, np.uint32)):
            fn = f"{k}_{key}.bin"
            np.ascontiguousarray(arr, dt).tofile(os.path.join(d, fn))
            rec[key] = f"http://render.local/{fn}"
        if k == "foot":
            uv = np.c_[v[:, 0] / 10.0, v[:, 2] / 1.6]        # 8 layer lines (0.2 mm) per 1.6 mm tile
            np.ascontiguousarray(uv, np.float32).tofile(os.path.join(d, f"{k}_uv.bin"))
            rec["uv"] = f"http://render.local/{k}_uv.bin"
        parts.append(rec)
    return parts


def view_config(view, world, args):
    allv = np.vstack([m.vertices for m in world.values()])
    lo, hi = allv.min(0), allv.max(0)
    c = (lo + hi) / 2
    size = hi - lo
    tgt = [float(c[0]), float(c[2]) - 1.0, float(-c[1])]          # three.js coordinates
    cfg = {"ortho": False, "fov": 24}
    if view == "hero":
        az, el, dist = -24, 19, 1.72 * size[0]
        tgt[1] -= 2
    elif view == "front":
        az, el, dist = 0, 8, 1.9 * size[0]
    elif view == "side":
        az, el, dist = -88, 8, 1.9 * size[0]
    elif view == "top":
        az, el, dist = 0, 89, 2.0 * size[0]
    elif view == "closeup":
        t = world["tree"].bounds if "tree" in world else world["base"].bounds
        tc = (t[0] + t[1]) / 2
        tgt = [float(tc[0]), float(tc[2]) - 6.0, float(-tc[1])]
        az, el, dist = -18, 18, 1.75 * (t[1][0] - t[0][0])
    else:
        raise SystemExit(f"unknown view {view}")
    a, e = math.radians(az), math.radians(el)
    cam = [tgt[0] + dist * math.cos(e) * math.sin(a), tgt[1] + dist * math.sin(e), tgt[2] + dist * math.cos(e) * math.cos(a)]
    cfg.update({"camPos": cam, "target": tgt})
    return cfg


def render(args):
    world, P = assemble(args)
    three = find_three(args.three_dir)
    env = node_env(three)
    W, H = [int(v) for v in args.size.lower().split("x")]
    ss = max(1, int(args.samples))
    os.makedirs(args.out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="render_")
    parts = export_parts(world, tmp)
    with open(os.path.join(tmp, "index.html"), "w") as f:
        f.write(PAGE)
    with open(os.path.join(tmp, "render.js"), "w") as f:
        f.write(NODE)
    outs = []
    for view in [v.strip() for v in args.views.split(",") if v.strip()]:
        cfg = {"width": W * ss, "height": H * ss, "exposure": args.exposure, "env": args.env,
               "background": args.background, "cloth": args.cloth, "hemi": 0.55, "key": args.key, "fill": 0.35,
               "keyPos": [-170.0, 230.0, 110.0], "fillPos": [260.0, 30.0, 40.0], "room": args.room,
               "softboxes": [[[-75, 35, 30], [40, 30], 3.2], [[75, 30, 40], [30, 25], 1.8], [[0, 25, -85], [80, 30], 1.4]],
               "colours": {"ivory": C.FILAMENTS["ivory"]["colour"], "gold": C.FILAMENTS["gold"]["colour"],
                           "wood": C.FILAMENTS["wood"]["colour"]},
               "parts": parts}
        cfg.update(view_config(view, world, args))
        with open(os.path.join(tmp, "scene.json"), "w") as f:
            json.dump(cfg, f)
        raw = os.path.join(tmp, f"{view}.png")
        r = subprocess.run(["node", os.path.join(tmp, "render.js"), tmp, raw, str(W * ss), str(H * ss)],
                           env=env, capture_output=True, text=True, timeout=1200)
        if r.returncode != 0 or not os.path.exists(raw):
            raise SystemExit("render failed:\n" + r.stdout[-2000:] + r.stderr[-2000:])
        from PIL import Image
        im = Image.open(raw).convert("RGB")
        if ss > 1:
            im = im.resize((W, H), Image.LANCZOS)
        out = os.path.join(args.out_dir, f"scene_{args.name}_{view}.png")
        im.save(out, optimize=True)
        outs.append(out)
        print("wrote", out)
    if args.reference and outs:
        compare(args, outs[0])
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return outs


def compare(args, render_png):
    from PIL import Image, ImageDraw, ImageFont
    ref = Image.open(args.reference).convert("RGB")
    ren = Image.open(render_png).convert("RGB")
    h = 720
    ref = ref.resize((int(ref.width * h / ref.height), h), Image.LANCZOS)
    ren = ren.resize((int(ren.width * h / ren.height), h), Image.LANCZOS)
    pad = 16
    out = Image.new("RGB", (ref.width + ren.width + 3 * pad, h + 2 * pad + 36), "white")
    out.paste(ref, (pad, pad + 36))
    out.paste(ren, (2 * pad + ref.width, pad + 36))
    d = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    d.text((pad, pad), "reference (concept image)", fill="black", font=font)
    d.text((2 * pad + ref.width, pad), f"render of the printable model ({args.name})", fill="black", font=font)
    p = os.path.join(args.out_dir, f"compare_{args.name}.png")
    out.save(p, optimize=True)
    print("wrote", p)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scad", default=C.DEFAULT_SCAD)
    ap.add_argument("--tree-data")
    ap.add_argument("--name", default="card")
    ap.add_argument("--base", required=True)
    ap.add_argument("--gold")
    ap.add_argument("--foot", required=True)
    ap.add_argument("--tree")
    ap.add_argument("--tree-pos")
    ap.add_argument("--foot-x", type=float)
    ap.add_argument("--views", default="hero,front")
    ap.add_argument("--size", default="1536x1024")
    ap.add_argument("--samples", type=int, default=2, help="supersampling factor")
    ap.add_argument("--exposure", type=float, default=1.25)
    ap.add_argument("--env", type=float, default=1.0, help="environment (reflection) intensity")
    ap.add_argument("--key", type=float, default=1.6, help="key light intensity")
    ap.add_argument("--room", action="store_true", help="three.js RoomEnvironment instead of the studio dome")
    # warm linen a few shades darker than the ivory filament, so the plaque reads as ivory
    ap.add_argument("--background", default="#cbb698", help="backdrop colour")
    ap.add_argument("--cloth", default="#d6c3a6", help="table cloth colour")
    ap.add_argument("--reference", help="reference photo for a side-by-side compare_<name>.png")
    ap.add_argument("--three-dir", help="folder of the three npm package")
    ap.add_argument("--out-dir", default=os.path.join(C.ROOT, "renders"))
    ap.add_argument("--keep", action="store_true", help="keep the temporary page folder")
    args = ap.parse_args()
    render(args)


if __name__ == "__main__":
    main()
