#!/usr/bin/env python3
"""Make the PLASTI-PAGE logo: receiver/p833_logo.py (used by the receiver's pages) and
plasti-page-logo.svg (the same logo as a file, for documents and signs).
By Ethan Brady & Ryan Raikes

Styled after the EPC logo: a red square behind a blue globe (here with a paging horn and
its sound), then the name in bold blue capitals. Colors measured from the EPC logo.
The letters are outlines of Liberation Sans Bold (SIL Open Font License), so the logo
looks the same on every device, whatever fonts it has.

Needs fonttools, on any PC (not on the laptop):
    python3 -m pip install fonttools
    python3 tools/make_logo.py
"""

import argparse
import os

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

BLUE, RED, LIGHT = "#004594", "#C6002A", "#E4E5F1"
NAME = "PLASTI-PAGE"
SIZE = 112                  # the mark is SIZE x SIZE; the logo is SIZE high
CAP = 84                    # height of the capitals (75% of the logo, close to EPC's tall letters)
GAP = 36                    # from the globe to the first letter
FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def num(v):
    return ("%.1f" % v).rstrip("0").rstrip(".") if abs(v) >= 0.05 else "0"


def mark():
    """The red square, the blue globe with its grid, and a paging horn sounding out."""
    c, r = SIZE / 2.0, SIZE / 2.0 - 1
    out = ['<rect x="5" y="5" width="%d" height="%d" fill="%s"/>' % (SIZE - 10, SIZE - 10, RED),
           '<circle cx="%s" cy="%s" r="%s" fill="%s"/>' % (num(c), num(c), num(r), BLUE)]
    grid = ['<line x1="%s" y1="%s" x2="%s" y2="%s"/>' % (num(c), num(c - r), num(c), num(c + r))]
    for k in (0.5, 0.866):                                   # meridians 30 and 60 degrees from the middle
        grid.append('<ellipse cx="%s" cy="%s" rx="%s" ry="%s"/>' % (num(c), num(c), num(r * k), num(r)))
    for d in (0.0, 0.423, -0.423, 0.766, -0.766):           # the equator, and 25 and 50 degrees north and south
        y, half = c + r * d, r * (1 - d * d) ** 0.5
        grid.append('<line x1="%s" y1="%s" x2="%s" y2="%s"/>' % (num(c - half), num(y), num(c + half), num(y)))
    out.append('<g fill="none" stroke="#fff" stroke-width="1.6">%s</g>' % "".join(grid))
    # the paging horn (light, like the land on EPC's globe), pointing right, and three sound waves
    out.append('<g fill="%s" stroke="%s" stroke-width="2.4" stroke-linejoin="round">'
               '<rect x="22" y="47" width="12" height="18" rx="2.5"/>'
               '<path d="M34 50 L62 34 Q67 56 62 78 L34 62 Z"/></g>' % (LIGHT, BLUE))
    waves = []
    for rad in (11, 21, 31):
        x, dy = 64 + rad * 0.788, rad * 0.616
        waves.append('<path d="M%s %s A%d %d 0 0 1 %s %s"/>' % (num(x), num(56 - dy), rad, rad, num(x), num(56 + dy)))
    out.append('<g fill="none" stroke="%s" stroke-width="5" stroke-linecap="round">%s</g>' % (LIGHT, "".join(waves)))
    return "".join(out)


def wordmark(font_path):
    """NAME as one path of letter outlines, with the font's kerning. Returns (path, width)."""
    font = TTFont(font_path)
    cmap, glyphs, hmtx = font.getBestCmap(), font.getGlyphSet(), font["hmtx"]
    kern = font["kern"].kernTables[0].kernTable if "kern" in font else {}
    scale = CAP / float(font["OS/2"].sCapHeight)
    names = [cmap[ord(ch)] for ch in NAME]
    baseline = SIZE / 2.0 + CAP / 2.0
    left = SIZE - 1 + GAP - hmtx[names[0]][1] * scale        # the first letter's ink starts GAP after the globe
    pen = SVGPathPen(glyphs, ntos=num)
    x = 0
    for i, g in enumerate(names):
        glyphs[g].draw(TransformPen(pen, (scale, 0, 0, -scale, left + x * scale, baseline)))
        x += hmtx[g][0] + (kern.get((g, names[i + 1]), 0) if i + 1 < len(names) else 0)
    last = names[-1]
    ink_right = left + (x - hmtx[last][0]) * scale + (font["glyf"][last].xMax * scale)
    return '<path d="%s"/>' % pen.getCommands(), int(ink_right + 5)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--font", default=FONT, help="a bold TrueType font (default: Liberation Sans Bold)")
    a = p.parse_args()
    the_mark = mark()
    letters, width = wordmark(a.font)
    with open(os.path.join(ROOT, "receiver", "p833_logo.py"), "w", encoding="utf-8") as f:
        f.write('''# The PLASTI-PAGE logo, for the receiver's pages. Made by tools/make_logo.py: don't edit by hand.
# By Ethan Brady & Ryan Raikes
#
# Styled after the EPC logo: a red square behind a blue globe (here with a paging horn), then
# the name in bold blue capitals. The letters are outlines of Liberation Sans Bold (SIL Open
# Font License), so the logo looks the same on every device.

from urllib.parse import quote

WIDTH, HEIGHT = %d, %d
BLUE, RED, LIGHT = "%s", "%s", "%s"
MARK = '%s'  # noqa: E501
WORDMARK = '%s'  # noqa: E501


def svg(css_class="logo"):
    """The logo as inline SVG. It's hidden from screen readers: the page's heading names it."""
    return ('<svg class="%%s" viewBox="0 0 %%d %%d" width="%%d" height="%%d" aria-hidden="true" focusable="false">'
            '%%s<g class="wm" fill="%%s">%%s</g></svg>' %% (css_class, WIDTH, HEIGHT, WIDTH, HEIGHT, MARK, BLUE, WORDMARK))


# The mark alone, as the browser tab's icon.
FAVICON = "data:image/svg+xml," + quote('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %%d %%d">%%s</svg>'
                                        %% (HEIGHT, HEIGHT, MARK), safe="")
''' % (width, SIZE, BLUE, RED, LIGHT, the_mark, letters))
    with open(os.path.join(ROOT, "plasti-page-logo.svg"), "w", encoding="utf-8") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d">\n'
                '<!-- PLASTI-PAGE logo. By Ethan Brady & Ryan Raikes. Made by tools/make_logo.py. -->\n'
                '%s<g fill="%s">%s</g></svg>\n' % (width, SIZE, width * 4, SIZE * 4, the_mark, BLUE, letters))
    print("wrote receiver/p833_logo.py and plasti-page-logo.svg (%d x %d)" % (width, SIZE))


if __name__ == "__main__":
    main()
