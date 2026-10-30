#!/usr/bin/env python3
"""Regenerate the app logo files from assets/logo-source.jpg (dev tool, needs Pillow and macOS iconutil).

Writes assets/logo.png (tile with transparent rounded corners, 512 px), assets/logo-64.png (embedded as favicon and
header logo in static/index.html) and assets/AppIcon.icns (tile at 824/1024 on a transparent canvas, all macOS sizes).
Usage: python3 scripts/make_logo.py [--embed]   (--embed also rewrites the data: URI in static/index.html)
"""
import base64, os, re, shutil, subprocess, sys
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
THRESHOLD = 140   # distance from white that counts as "tile" (the drop shadow stays below ~110)
INSET = 8         # pixels shaved off the tile edge where the JPEG blends it into white
LOGO_PX = 512     # size of the shipped logo.png (the README shows it at 96 px)


def _dist(p):
    return max(255 - p[0], 255 - p[1], 255 - p[2])


def find_tile(src):
    """Bounding box (left, top, right, bottom) and corner radius of the rounded tile on the white background."""
    w, h = src.size; px = src.load()
    cols = [x for x in range(w) if any(_dist(px[x, y]) > THRESHOLD for y in range(0, h, 2))]
    rows = [y for y in range(h) if any(_dist(px[x, y]) > THRESHOLD for x in range(0, w, 2))]
    left, right, top, bottom = min(cols), max(cols), min(rows), max(rows)
    top_first = next(x for x in range(left, right) if _dist(px[x, top + 1]) > THRESHOLD)
    left_first = next(y for y in range(top, bottom) if _dist(px[left + 1, y]) > THRESHOLD)
    radius = round((top_first - left + left_first - top) / 2)      # the top row starts r px in from the corner
    return (left, top, right, bottom), radius


def cut_tile(src, box, radius, size):
    """Crop the tile, give it transparent rounded corners (geometric mask, 4x supersampled) and resize to size x size."""
    left, top, right, bottom = box
    tile = src.crop((left, top, right + 1, bottom + 1)); tw, th = tile.size
    s = 4
    mask = Image.new("L", (tw * s, th * s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((INSET * s, INSET * s, tw * s - INSET * s - 1, th * s - INSET * s - 1), radius=(radius - INSET) * s, fill=255)
    logo = tile.convert("RGBA"); logo.putalpha(mask.resize((tw, th), Image.LANCZOS))
    return logo.crop((INSET, INSET, tw - INSET, th - INSET)).resize((size, size), Image.LANCZOS)


def write_icns(logo1024, out_path):
    """macOS app icon: the tile at 824/1024 on a transparent canvas (HIG icon grid), all sizes via iconutil."""
    icon = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    tile = logo1024.resize((824, 824), Image.LANCZOS); icon.paste(tile, (100, 100), tile)
    iconset = os.path.join(ASSETS, "AppIcon.iconset"); shutil.rmtree(iconset, ignore_errors=True); os.makedirs(iconset)
    for n in (16, 32, 128, 256, 512):
        icon.resize((n, n), Image.LANCZOS).save(f"{iconset}/icon_{n}x{n}.png", optimize=True)
        icon.resize((n * 2, n * 2), Image.LANCZOS).save(f"{iconset}/icon_{n}x{n}@2x.png", optimize=True)
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out_path], check=True)
    shutil.rmtree(iconset)


def embed(png_path, html_path):
    """Replace the data: URI of the app logo in index.html: the header <img id="logo"> and, if it carries its own
    data URI, the favicon <link rel="icon">. Other embedded images (e.g. the footer logo) are left alone."""
    b64 = base64.b64encode(open(png_path, "rb").read()).decode()
    html = open(html_path).read()
    uri = "data:image/png;base64," + b64
    html, n1 = re.subn(r'(<img id="logo" src=")data:image/png;base64,[A-Za-z0-9+/=]+(")', lambda m: m.group(1) + uri + m.group(2), html, count=1)
    html, n2 = re.subn(r'(<link rel="icon"[^>]*href=")data:image/png;base64,[A-Za-z0-9+/=]+(")', lambda m: m.group(1) + uri + m.group(2), html, count=1)
    open(html_path, "w").write(html)
    return n1 + n2


def main(argv):
    src = Image.open(os.path.join(ASSETS, "logo-source.jpg")).convert("RGB")
    box, radius = find_tile(src)
    logo1024 = cut_tile(src, box, radius, 1024)
    logo1024.resize((LOGO_PX, LOGO_PX), Image.LANCZOS).save(os.path.join(ASSETS, "logo.png"), optimize=True)
    logo1024.resize((64, 64), Image.LANCZOS).save(os.path.join(ASSETS, "logo-64.png"), optimize=True)
    write_icns(logo1024, os.path.join(ASSETS, "AppIcon.icns"))
    print(f"tile {box[2]-box[0]+1}x{box[3]-box[1]+1} at ({box[0]},{box[1]}), corner radius {radius}; wrote logo.png ({LOGO_PX} px), logo-64.png, AppIcon.icns")
    if "--embed" in argv:
        n = embed(os.path.join(ASSETS, "logo-64.png"), os.path.join(ROOT, "static", "index.html"))
        print(f"embedded into static/index.html ({n} place(s))")


if __name__ == "__main__":
    main(sys.argv[1:])
