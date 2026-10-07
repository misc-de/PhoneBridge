# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The panel icon, drawn: a phone whose body fills up with the battery
level - green, orange below 30 %, red below 15 % - with a bolt while it
charges, grey and crossed out while it is not connected, and a blue badge
with the number of unread messages.

StatusNotifierItem wants ARGB32 in network byte order (big endian); cairo
draws in the machine's byte order, so the bytes are swapped where needed."""

import sys

import cairo

SIZES = (16, 22, 24, 32, 48)
LIGHT = (0.93, 0.93, 0.93)
HALO = (0.0, 0.0, 0.0, 0.55)
GREEN = (0.20, 0.82, 0.48)
ORANGE = (1.0, 0.47, 0.0)
RED = (0.88, 0.11, 0.14)
BLUE = (0.21, 0.52, 0.89)
GREY = (0.6, 0.6, 0.6)


def level_color(percent):
    if percent < 15:
        return RED
    if percent < 30:
        return ORANGE
    return GREEN


def _rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5708, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
    cr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    cr.arc(x + r, y + r, r, 3.1416, 4.7124)
    cr.close_path()


def draw(cr, s, percent=None, charging=False, online=True, unread=0):
    lw = max(1.0, s / 14)
    w, h = s * 0.52, s * 0.88
    x, y = s * 0.12, (s - h) / 2
    r = s * 0.1
    outline = LIGHT if online else GREY

    # body: dark halo first, so the light outline reads on light panels too
    _rounded(cr, x, y, w, h, r)
    cr.set_source_rgba(*HALO)
    cr.set_line_width(lw * 2.6)
    cr.stroke_preserve()
    cr.set_source_rgb(*outline)
    cr.set_line_width(lw)
    cr.stroke()

    if online and percent is not None:
        inset = lw * 1.6
        ih = h - 2 * inset
        fill = ih * max(0, min(100, percent)) / 100
        cr.save()
        _rounded(cr, x + inset, y + inset, w - 2 * inset, ih, max(0.5, r - inset))
        cr.clip()
        cr.rectangle(x, y + inset + ih - fill, w, fill)
        cr.set_source_rgb(*level_color(percent))
        cr.fill()
        cr.restore()

    if online and charging:
        cx, cy = x + w / 2, y + h / 2
        u = h / 9
        cr.move_to(cx + 0.6 * u, cy - 2.6 * u)
        cr.line_to(cx - 1.3 * u, cy + 0.4 * u)
        cr.line_to(cx - 0.1 * u, cy + 0.4 * u)
        cr.line_to(cx - 0.6 * u, cy + 2.6 * u)
        cr.line_to(cx + 1.3 * u, cy - 0.4 * u)
        cr.line_to(cx + 0.1 * u, cy - 0.4 * u)
        cr.close_path()
        cr.set_source_rgba(*HALO)
        cr.set_line_width(lw)
        cr.stroke_preserve()
        cr.set_source_rgb(1, 1, 1)
        cr.fill()

    if not online:
        cr.move_to(x - lw, y + h + lw)
        cr.line_to(x + w + lw, y - lw)
        cr.set_source_rgba(*HALO)
        cr.set_line_width(lw * 3)
        cr.stroke_preserve()
        cr.set_source_rgb(*RED)
        cr.set_line_width(lw * 1.4)
        cr.stroke()

    if unread:
        br = s * 0.24
        bx, by = s - br - 0.5, br + 0.5
        cr.arc(bx, by, br, 0, 6.2832)
        cr.set_source_rgba(*HALO)
        cr.set_line_width(lw)
        cr.stroke_preserve()
        cr.set_source_rgb(*BLUE)
        cr.fill()
        if s >= 22:
            text = str(unread) if unread < 10 else "+"
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(br * 1.5)
            ext = cr.text_extents(text)
            cr.move_to(bx - ext.width / 2 - ext.x_bearing, by - ext.height / 2 - ext.y_bearing)
            cr.set_source_rgb(1, 1, 1)
            cr.show_text(text)


def render(size, **state):
    """(width, height, ARGB32 bytes in network byte order)."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    draw(cr, size, **state)
    surface.flush()
    stride = surface.get_stride()
    raw = bytes(surface.get_data())
    rows = [raw[i * stride:i * stride + size * 4] for i in range(size)]
    data = bytearray(b"".join(rows))
    if sys.byteorder == "little":
        data[0::4], data[1::4], data[2::4], data[3::4] = (
            data[3::4], data[2::4], data[1::4], data[0::4])
    return size, size, bytes(data)


def pixmaps(**state):
    return [render(s, **state) for s in SIZES]


def write_png(path, size, **state):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    draw(cairo.Context(surface), size, **state)
    surface.write_to_png(path)
