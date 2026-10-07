# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The panel icon: sizes and the byte order StatusNotifierItem wants."""

import unittest

from phonebridge import icon


def pixel(data, size, x, y):
    i = (y * size + x) * 4
    return tuple(data[i:i + 4])  # A, R, G, B


class Icon(unittest.TestCase):
    def test_sizes(self):
        maps = icon.pixmaps(percent=50, online=True)
        self.assertEqual([(w, h) for w, h, _d in maps], [(s, s) for s in icon.SIZES])
        for w, h, data in maps:
            self.assertEqual(len(data), w * h * 4)

    def test_argb_network_order(self):
        size, _h, data = icon.render(48, percent=100, online=True)
        a, r, g, b = pixel(data, size, 24, 35)
        self.assertEqual(a, 255)
        self.assertGreater(g, 150)
        self.assertLess(r, 120)

    def test_low_battery_is_red(self):
        size, _h, data = icon.render(48, percent=10, online=True)
        a, r, g, b = pixel(data, size, 24, 38)
        self.assertEqual(a, 255)
        self.assertGreater(r, 180)
        self.assertLess(g, 80)

    def test_badge_only_with_unread(self):
        corner = lambda d: pixel(d, 48, 45, 12)  # noqa: E731 - only the badge reaches here
        _w, _h, plain = icon.render(48, percent=50, online=True)
        _w, _h, badged = icon.render(48, percent=50, online=True, unread=3)
        self.assertEqual(corner(plain)[0], 0)
        self.assertEqual(corner(badged)[0], 255)

    def test_offline_draws(self):
        _w, _h, data = icon.render(22, online=False)
        self.assertTrue(any(data[0::4]))

    def test_centred(self):
        for size in icon.SIZES:
            _w, _h, data = icon.render(size, percent=50, online=True)
            cols = [x for x in range(size)
                    if any(data[(y * size + x) * 4] for y in range(size))]
            left, right = cols[0], size - 1 - cols[-1]
            self.assertLessEqual(abs(left - right), 1, "size %d: %d|%d" % (size, left, right))

    def test_initials(self):
        self.assertEqual(icon.initials("Anna Maria Example"), "AE")
        self.assertEqual(icon.initials("doctolib"), "D")
        self.assertEqual(icon.initials("+49 155 123"), "")
        self.assertEqual(icon.initials(""), "")

    def test_person_pictures(self):
        import cairo
        import io
        for name in ("Anna Example", "+4915550000001", ""):
            png = icon.avatar_png(name, 64)
            img = cairo.ImageSurface.create_from_png(io.BytesIO(png))
            self.assertEqual((img.get_width(), img.get_height()), (64, 64))
            data = img.get_data()
            self.assertEqual(data[3], 0)                       # a corner: outside the circle
            self.assertEqual(data[(32 * 64 + 32) * 4 + 3], 255)  # the middle: inside
        # the same name, the same colour; a photo fills the circle
        self.assertEqual(icon.avatar_png("Anna B", 32), icon.avatar_png("Anna B", 32))
        red = cairo.ImageSurface(cairo.FORMAT_ARGB32, 40, 20)
        ctx = cairo.Context(red)
        ctx.set_source_rgb(1, 0, 0)
        ctx.paint()
        buf = io.BytesIO()
        red.write_to_png(buf)
        img = cairo.ImageSurface.create_from_png(io.BytesIO(
            icon.avatar_png("Anna", 64, buf.getvalue())))
        b, g, r, a = img.get_data()[(32 * 64 + 32) * 4:(32 * 64 + 32) * 4 + 4]
        self.assertEqual((r, g, b, a), (255, 0, 0, 255))

    def test_level_colors(self):
        self.assertEqual(icon.level_color(14), icon.RED)
        self.assertEqual(icon.level_color(29), icon.ORANGE)
        self.assertEqual(icon.level_color(30), icon.GREEN)


if __name__ == "__main__":
    unittest.main()
