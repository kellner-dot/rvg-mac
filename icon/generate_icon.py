#!/usr/bin/env python3
"""Generate RVG Mac icon artwork (master PNG + menu bar template icon).

Run on any machine with PIL. The .icns is built on the Mac via make_icns.sh.
"""
from PIL import Image, ImageDraw
import math, os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)))

def rounded_rect(draw, xy, radius, fill):
    draw.rounded_rectangle(xy, radius=radius, fill=fill)

def vertical_gradient(size, top, bottom):
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        px_r = int(top[0] + (bottom[0] - top[0]) * t)
        px_g = int(top[1] + (bottom[1] - top[1]) * t)
        px_b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(size):
            px[x, y] = (px_r, px_g, px_b)
    return img

def make_app_icon(size=1024):
    # macOS-style rounded square
    base = vertical_gradient(size, (24, 42, 84), (10, 18, 40))
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size, size], radius=int(size * 0.225), fill=255)
    base.putalpha(mask)

    d = ImageDraw.Draw(base, "RGBA")
    cx = size / 2

    # --- glyph: monitor with signal waves (remote desktop) ---
    # screen body
    sw, sh = size * 0.46, size * 0.32
    sx0, sy0 = cx - sw / 2, size * 0.30
    sx1, sy1 = cx + sw / 2, sy0 + sh
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=size * 0.03,
                        outline=(255, 255, 255, 255), width=int(size * 0.035))
    # stand
    d.rectangle([cx - size * 0.015, sy1, cx + size * 0.015, sy1 + size * 0.07],
                fill=(255, 255, 255, 255))
    d.rectangle([cx - size * 0.10, sy1 + size * 0.07, cx + size * 0.10,
                 sy1 + size * 0.095], fill=(255, 255, 255, 255))

    # screen content: mini "desktop" — cursor arrow + window bar
    d.rectangle([sx0 + size * 0.05, sy0 + size * 0.05,
                 sx1 - size * 0.05, sy0 + size * 0.10],
                fill=(90, 200, 250, 255))  # title bar
    # cursor arrow (simple triangle)
    ax, ay = cx - size * 0.06, sy0 + size * 0.17
    s = size * 0.045
    d.polygon([(ax, ay), (ax, ay + s), (ax + s * 0.35, ay + s * 0.72),
               (ax + s * 0.55, ay + s * 0.62), (ax + s * 0.42, ay + s * 0.38)],
              fill=(255, 255, 255, 255))

    # signal arcs above the screen (connection waves)
    for i, (r, w) in enumerate([(0.10, 0.030), (0.155, 0.028), (0.21, 0.026)]):
        rr = size * r
        y_top = sy0 - size * 0.045
        d.arc([cx - rr, y_top - rr, cx + rr, y_top + rr],
              start=200, end=340, fill=(90, 200, 250, 255), width=int(size * w))
    # signal dot
    dr = size * 0.022
    d.ellipse([cx - dr, sy0 - size * 0.045 - dr, cx + dr, sy0 - size * 0.045 + dr],
              fill=(90, 200, 250, 255))

    # status dot (green = agent running) bottom-right
    dot_r = size * 0.055
    dot_c = (size * 0.80, size * 0.80)
    d.ellipse([dot_c[0] - dot_r, dot_c[1] - dot_r,
               dot_c[0] + dot_r, dot_c[1] + dot_r],
              fill=(48, 209, 88, 255))
    d.ellipse([dot_c[0] - dot_r, dot_c[1] - dot_r,
               dot_c[0] + dot_r, dot_c[1] + dot_r],
              outline=(255, 255, 255, 255), width=int(size * 0.018))

    base.save(os.path.join(OUT, "rvg-icon-1024.png"))
    print("wrote rvg-icon-1024.png")

def make_menu_icon(px=44):
    """Monochrome template icon for the menu bar (black glyph, transparent bg)."""
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = px * 0.14
    # mini monitor
    d.rounded_rectangle([m, m + 2, px - m, px * 0.62], radius=3,
                        outline=(0, 0, 0, 255), width=3)
    d.line([px / 2, px * 0.62, px / 2, px * 0.78], fill=(0, 0, 0, 255), width=3)
    d.line([px * 0.32, px * 0.80, px * 0.68, px * 0.80],
           fill=(0, 0, 0, 255), width=3)
    # tiny signal dot above
    r = 3
    d.ellipse([px / 2 - r, 2, px / 2 + r, 2 + 2 * r], fill=(0, 0, 0, 255))
    img.save(os.path.join(OUT, "rvg-menu-icon.png"))
    print("wrote rvg-menu-icon.png (template)")

if __name__ == "__main__":
    make_app_icon()
    make_menu_icon()
