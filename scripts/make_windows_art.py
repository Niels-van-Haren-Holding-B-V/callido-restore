#!/usr/bin/env python3
"""Regenerate the Windows installer artwork from assets/logo.png (dev tool, needs Pillow; runs on any OS).

Writes
  assets/AppIcon.ico                 the app/exe/shortcut icon (16 to 256 px)
  packaging/windows/banner.bmp       493 x 58, top strip of the installer's inner dialogs (WixUIBannerBmp)
  packaging/windows/dialog.bmp       493 x 312, Welcome / Finish dialog background (WixUIDialogBmp)

The look follows the app (static/index.html): the logo tile, and the brand gradient #6a4bc9 -> #b55aa6 -> #ef6b5b used as an
accent only, and the Callido raccoon at the foot of the Welcome panel (as in the app footer). No text is drawn: the installer renders its own titles in the white areas, so the art needs no font.
Usage: python3 scripts/make_windows_art.py
"""
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO = os.path.join(ROOT, "assets", "logo.png")
RACCOON = os.path.join(ROOT, "assets", "callido-raccoon.png")    # the Callido mark, as in the app footer
OUT_ICO = os.path.join(ROOT, "assets", "AppIcon.ico")
OUT_DIR = os.path.join(ROOT, "packaging", "windows")
STOPS = [(0.0, (0x6a, 0x4b, 0xc9)), (0.55, (0xb5, 0x5a, 0xa6)), (1.0, (0xef, 0x6b, 0x5b))]   # --grad in static/index.html
PANEL = (255, 255, 255)
LINE = (0xe5, 0xe5, 0xea)                                                                      # --line


def grad_color(t):
    for (t0, c0), (t1, c1) in zip(STOPS, STOPS[1:]):
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0
            return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
    return STOPS[-1][1]


def gradient(w, h):
    """135 degree gradient (top left to bottom right), as in the app's --grad."""
    img = Image.new("RGB", (w, h)); px = img.load(); span = (w + h - 2) or 1
    for y in range(h):
        for x in range(w): px[x, y] = grad_color((x + y) / span)
    return img


def logo(size, path=LOGO):
    return Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)


def paste(dst, src, xy):
    dst.paste(src, xy, src)


def make_ico():
    logo(256).save(OUT_ICO, format="ICO", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])


def make_banner():
    # WiX draws the dialog title and description in black on the left ~400 px; the logo sits on the right.
    w, h = 493, 58
    img = Image.new("RGB", (w, h), PANEL)
    paste(img, logo(44), (w - 44 - 10, (h - 44) // 2))
    img.paste(gradient(w, 3), (0, h - 3))                 # thin brand accent along the bottom edge
    img.save(os.path.join(OUT_DIR, "banner.bmp"))


def make_dialog():
    # The left 164 px are the art panel; WiX puts the Welcome/Finish text on the white area to the right of it.
    w, h, panel = 493, 312, 164
    img = Image.new("RGB", (w, h), PANEL)
    img.paste(gradient(panel, h), (0, 0))
    tile = logo(112)
    shade = Image.new("RGBA", (tile.width + 16, tile.height + 16), (0, 0, 0, 0))
    ImageDraw.Draw(shade).rounded_rectangle((8, 12, tile.width + 8, tile.height + 12), radius=24, fill=(40, 20, 80, 70))
    x, y = (panel - tile.width) // 2, 64
    paste(img, shade, (x - 8, y - 8)); paste(img, tile, (x, y))
    mark = logo(40, RACCOON)
    paste(img, mark, ((panel - mark.width) // 2, h - mark.height - 22))
    ImageDraw.Draw(img).line((panel, 0, panel, h), fill=LINE)
    img.save(os.path.join(OUT_DIR, "dialog.bmp"))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    make_ico(); make_banner(); make_dialog()
    for p in (OUT_ICO, os.path.join(OUT_DIR, "banner.bmp"), os.path.join(OUT_DIR, "dialog.bmp")):
        print("wrote", os.path.relpath(p, ROOT))


if __name__ == "__main__":
    main()
