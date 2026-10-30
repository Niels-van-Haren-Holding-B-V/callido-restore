#!/usr/bin/env python3
"""Regenerate the app logo files from assets/logo-source.jpg (dev tool, needs Pillow and macOS iconutil).

Writes assets/logo.png (tile with transparent rounded corners, 1024px), assets/logo-64.png (embedded as favicon and
header logo in static/index.html) and assets/AppIcon.icns (tile at 824/1024 on a transparent canvas, all macOS sizes).
Usage: python3 scripts/make_logo.py [--embed]   (--embed also rewrites the data: URIs in static/index.html)
"""
import base64, os, re, shutil, subprocess, sys
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A = os.path.join(ROOT, "assets")
TH = 140          # distance from white that counts as "tile" (the drop shadow stays below ~110)
INSET = 8         # pixels shaved off the tile edge where the JPEG blends it into white

src = Image.open(os.path.join(A, "logo-source.jpg")).convert("RGB"); W, H = src.size; px = src.load()
def dist(p): return max(255 - p[0], 255 - p[1], 255 - p[2])
cols = [x for x in range(W) if any(dist(px[x, y]) > TH for y in range(0, H, 2))]
rows = [y for y in range(H) if any(dist(px[x, y]) > TH for x in range(0, W, 2))]
L, R, T, B = min(cols), max(cols), min(rows), max(rows)
top_first = next(x for x in range(L, R) if dist(px[x, T + 1]) > TH)
left_first = next(y for y in range(T, B) if dist(px[L + 1, y]) > TH)
r = round((top_first - L + left_first - T) / 2)                       # corner radius: the top row starts r px in
tile = src.crop((L, T, R + 1, B + 1)); tw, th = tile.size
S = 4
m = Image.new("L", (tw * S, th * S), 0)
ImageDraw.Draw(m).rounded_rectangle((INSET * S, INSET * S, tw * S - INSET * S - 1, th * S - INSET * S - 1), radius=(r - INSET) * S, fill=255)
logo = tile.convert("RGBA"); logo.putalpha(m.resize((tw, th), Image.LANCZOS))
logo = logo.crop((INSET, INSET, tw - INSET, th - INSET)).resize((1024, 1024), Image.LANCZOS)
logo.save(os.path.join(A, "logo.png"), optimize=True)
logo.resize((64, 64), Image.LANCZOS).save(os.path.join(A, "logo-64.png"), optimize=True)

icon = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0)); t = logo.resize((824, 824), Image.LANCZOS); icon.paste(t, (100, 100), t)
iconset = os.path.join(A, "AppIcon.iconset"); shutil.rmtree(iconset, ignore_errors=True); os.makedirs(iconset)
for n in (16, 32, 128, 256, 512):
    icon.resize((n, n), Image.LANCZOS).save(f"{iconset}/icon_{n}x{n}.png", optimize=True)
    icon.resize((n * 2, n * 2), Image.LANCZOS).save(f"{iconset}/icon_{n}x{n}@2x.png", optimize=True)
subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(A, "AppIcon.icns")], check=True)
shutil.rmtree(iconset)
print(f"tile {tw}x{th} at ({L},{T}), corner radius {r}; wrote logo.png, logo-64.png, AppIcon.icns")

if "--embed" in sys.argv:
    b64 = base64.b64encode(open(os.path.join(A, "logo-64.png"), "rb").read()).decode()
    p = os.path.join(ROOT, "static", "index.html"); s = open(p).read()
    s, n = re.subn(r"data:image/png;base64,[A-Za-z0-9+/=]+", "data:image/png;base64," + b64, s)
    open(p, "w").write(s); print(f"embedded into static/index.html ({n} places)")
