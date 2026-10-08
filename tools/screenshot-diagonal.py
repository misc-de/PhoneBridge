#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The README's screenshot: the light and the dark overview in one picture,
split along a slanted line - light on the left, dark on the right.

    python3 tools/screenshot-diagonal.py data/screenshots/overview-light.png \
        data/screenshots/overview-dark.png data/screenshots/overview.png

Both screenshots come from tools/screenshot-demo.py and are the same size."""

import sys

from PIL import Image, ImageDraw, ImageFilter

TOP, BOTTOM = 0.60, 0.40     # where the line meets the top and the bottom edge
SCALE = 4                    # drawn larger and scaled down: a smooth edge
LINE = (120, 160, 230, 255)  # a thin blue seam between the two halves


def main(light_path, dark_path, out_path):
    light = Image.open(light_path).convert("RGBA")
    dark = Image.open(dark_path).convert("RGBA")
    if light.size != dark.size:
        sys.exit("the screenshots differ in size: %s / %s" % (light.size, dark.size))
    w, h = light.size
    big = (w * SCALE, h * SCALE)
    top, bottom = (w * TOP * SCALE, 0), (w * BOTTOM * SCALE, h * SCALE)

    mask = Image.new("L", big, 0)                    # white = the dark half
    ImageDraw.Draw(mask).polygon([top, (big[0], 0), big, bottom], fill=255)
    out = Image.composite(dark, light, mask.resize((w, h), Image.LANCZOS))

    seam = Image.new("L", big, 0)
    ImageDraw.Draw(seam).line([top, bottom], fill=255, width=2 * SCALE)
    seam = seam.resize((w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.4))
    # only where the window is - its rounded corners stay transparent
    alpha = out.getchannel("A")
    seam = Image.composite(seam, Image.new("L", (w, h), 0), alpha)
    out = Image.composite(Image.new("RGBA", (w, h), LINE), out, seam)
    out.putalpha(alpha)
    out.save(out_path, optimize=True)
    print("saved %s (%dx%d)" % (out_path, w, h))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
