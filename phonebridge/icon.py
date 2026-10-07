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
    x, y = (s - w) / 2, (s - h) / 2          # centred; the badge overlaps a corner
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


_PIXMAPS = {}


def pixmaps(percent=None, charging=False, online=True, unread=0):
    """All sizes - drawn once per look (kept for the last few looks)."""
    key = (percent, charging, online, min(unread, 10))
    if key not in _PIXMAPS:
        if len(_PIXMAPS) > 64:
            _PIXMAPS.clear()
        _PIXMAPS[key] = [render(s, percent=percent, charging=charging, online=online,
                                unread=unread) for s in SIZES]
    return _PIXMAPS[key]


def write_png(path, size, **state):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    draw(cairo.Context(surface), size, **state)
    surface.write_to_png(path)


# --- a person's picture for notifications ------------------------------------
# The look of the app's lists (Adw.Avatar): the photo in a circle, else the
# initials on a colour chosen by the name, else a silhouette (a bare number).

AVATAR_COLOURS = ((0.51, 0.71, 0.93), (0.48, 0.85, 0.95), (0.55, 0.90, 0.69),
                  (0.71, 0.91, 0.54), (0.97, 0.89, 0.35), (1.00, 0.80, 0.38),
                  (1.00, 0.66, 0.35), (0.97, 0.53, 0.45), (0.91, 0.45, 0.67),
                  (0.80, 0.47, 0.83), (0.62, 0.57, 0.91), (0.89, 0.81, 0.61),
                  (0.75, 0.57, 0.43), (0.75, 0.75, 0.74))


def initials(name):
    """"Anna Maria Beispiel" -> "AB"; "" or a number -> ""."""
    words = [w for w in (name or "").replace("-", " ").split() if w[0].isalpha()]
    if not words:
        return ""
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()


def _colour(name):
    h = 0
    for ch in name or "":
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return AVATAR_COLOURS[h % len(AVATAR_COLOURS)]


def avatar_png(name, size=128, photo_png=None):
    """PNG bytes of a person's round picture."""
    import io
    import math
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    cr.arc(size / 2, size / 2, size / 2, 0, 2 * math.pi)
    cr.clip()
    photo = None
    if photo_png:
        try:
            photo = cairo.ImageSurface.create_from_png(io.BytesIO(photo_png))
        except (cairo.Error, MemoryError):
            photo = None
    if photo is not None and photo.get_width() and photo.get_height():
        # cover the circle, centred
        scale = max(size / photo.get_width(), size / photo.get_height())
        cr.translate((size - photo.get_width() * scale) / 2,
                     (size - photo.get_height() * scale) / 2)
        cr.scale(scale, scale)
        cr.set_source_surface(photo, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
    else:
        letters = initials(name)
        cr.set_source_rgb(*(_colour(name) if letters else (0.60, 0.60, 0.65)))
        cr.paint()
        cr.set_source_rgba(1, 1, 1, 0.95)
        if letters:
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(size * (0.42 if len(letters) == 1 else 0.36))
            ext = cr.text_extents(letters)
            cr.move_to(size / 2 - ext.width / 2 - ext.x_bearing,
                       size / 2 - ext.height / 2 - ext.y_bearing)
            cr.show_text(letters)
        else:
            cr.arc(size / 2, size * 0.38, size * 0.17, 0, 2 * math.pi)       # head
            cr.fill()
            cr.arc(size / 2, size * 0.98, size * 0.36, math.pi, 2 * math.pi)  # shoulders
            cr.fill()
    out = io.BytesIO()
    surface.write_to_png(out)
    return out.getvalue()
