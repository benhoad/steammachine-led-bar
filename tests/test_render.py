import unittest

from ledbar.colors import parse_hex, reorder, to_rgb8
from ledbar.render import (
    Compositor, FrameShaper, Scene, breathe_level, fill_frame, pulse_level, render_scene, segment_mask,
)

BLUE = (0.0, 0.0, 1.0)


class ColorTests(unittest.TestCase):
    def test_parse_hex(self):
        self.assertEqual(parse_hex("#ff0000"), (1.0, 0.0, 0.0))
        self.assertEqual(parse_hex("00ff00"), (0.0, 1.0, 0.0))
        self.assertEqual(parse_hex("#00f"), (0.0, 0.0, 1.0))
        with self.assertRaises(ValueError):
            parse_hex("blue")

    def test_gamma_and_brightness(self):
        self.assertEqual(to_rgb8((1.0, 1.0, 1.0), 2.2, 1.0), (255, 255, 255))
        # brightness is linear regardless of gamma: 20 % really is 20 % of the drive level
        self.assertEqual(to_rgb8((1.0, 0.0, 0.0), 1.0, 0.5), (128, 0, 0))
        self.assertEqual(to_rgb8((1.0, 0.0, 0.0), 2.2, 0.5), (128, 0, 0))
        self.assertEqual(to_rgb8((1.0, 1.0, 1.0), 2.2, 0.2), (51, 51, 51))
        # gamma only bends mid-level values (fades, breathing, colour mixing)
        self.assertLess(to_rgb8((0.5, 0.0, 0.0), 2.2, 1.0)[0], 128)
        self.assertGreater(to_rgb8((0.1, 0.62, 1.0), 2.2, 0.2)[2], 40)   # Steam blue at 20 % stays visible

    def test_reorder(self):
        self.assertEqual(reorder((1, 2, 3), "GRB"), (2, 1, 3))
        self.assertEqual(reorder((1, 2, 3), "BGR"), (3, 2, 1))


class PatternTests(unittest.TestCase):
    def test_fill_hard_edges(self):
        frame = fill_frame(8, 0.5, BLUE)
        self.assertEqual([c[2] for c in frame], [1, 1, 1, 1, 0, 0, 0, 0])

    def test_fill_partial_led(self):
        frame = fill_frame(8, 0.56, BLUE)
        self.assertAlmostEqual(frame[4][2], 0.48)
        self.assertEqual(frame[5][2], 0.0)

    def test_fill_hint_of_progress(self):
        frame = fill_frame(24, 0.001, BLUE)
        self.assertGreater(frame[0][2], 0.0)
        self.assertEqual(fill_frame(24, 0.0, BLUE)[0][2], 0.0)

    def test_fill_complete(self):
        self.assertTrue(all(c == BLUE for c in fill_frame(24, 1.0, BLUE)))

    def test_segments(self):
        self.assertEqual(segment_mask(8, 0.25, 0.5), [False, False, True, True, False, False, False, False])
        self.assertEqual(segment_mask(8, 0.5, 1.0), [False] * 4 + [True] * 4)

    def test_breathe_range(self):
        levels = [breathe_level(t / 10, 3.0, 0.06) for t in range(31)]
        self.assertAlmostEqual(min(levels), 0.06, places=3)
        self.assertAlmostEqual(max(levels), 1.0, places=3)

    def test_pulse_returns_to_one(self):
        self.assertEqual(pulse_level(2.0, 1.5), 1.0)
        self.assertGreater(pulse_level(0.2, 1.5), 1.0)

    def test_render_kinds(self):
        n = 6
        self.assertEqual(render_scene(Scene(kind="off"), n, 0.0), [(0, 0, 0)] * n)
        self.assertEqual(render_scene(Scene(kind="solid", color=BLUE, level=0.5), n, 0.0), [(0, 0, 0.5)] * n)
        seg = render_scene(Scene(kind="segment_breathe", color=BLUE, segment="q4", period=2.0), 8, 1.0)
        self.assertEqual([c != (0, 0, 0) for c in seg], [False] * 6 + [True] * 2)
        gauge = render_scene(Scene(kind="gauge", color=(0, 0, 1.0), color2=(1.0, 0, 0), fraction=1.0), n, 0.0)
        self.assertEqual(gauge[0], (1.0, 0.0, 0.0))


class ShaperTests(unittest.TestCase):
    def test_reverse_and_offset(self):
        shaper = FrameShaper(4, reverse=True, offset=2, brightness=1.0, gamma=1.0)
        physical = shaper.shape(fill_frame(4, 0.5, BLUE))
        self.assertEqual(physical, [(0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 255), (0, 0, 255)])

    def test_color_order(self):
        shaper = FrameShaper(1, gamma=1.0, color_order="GRB")
        self.assertEqual(shaper.shape([(1.0, 0.0, 0.0)]), [(0, 255, 0)])

    def test_wrong_length(self):
        with self.assertRaises(ValueError):
            FrameShaper(4).shape([BLUE] * 3)

    def test_fade_out(self):
        frames = FrameShaper(2, gamma=1.0).fade_out([(200, 100, 0), (0, 0, 50)], 4)
        self.assertEqual(len(frames), 4)
        self.assertEqual(frames[-1], [(0, 0, 0), (0, 0, 0)])


class CompositorTests(unittest.TestCase):
    def test_crossfade(self):
        comp = Compositor(4, fade_seconds=1.0, fill_tau=0.0)
        comp.set_scene(Scene(kind="solid", name="a", color=(1.0, 0.0, 0.0)), 0.0)
        self.assertEqual(comp.render(0.0)[0], (0.0, 0.0, 0.0))   # the first scene fades in from black
        self.assertEqual(comp.render(1.0)[0], (1.0, 0.0, 0.0))
        comp.set_scene(Scene(kind="solid", name="b", color=(0.0, 0.0, 1.0)), 1.0)
        mid = comp.render(1.5)
        self.assertAlmostEqual(mid[0][0], 0.5)
        self.assertAlmostEqual(mid[0][2], 0.5)
        done = comp.render(2.5)
        self.assertEqual(done[0], (0.0, 0.0, 1.0))

    def test_instant_switch(self):
        comp = Compositor(2, fade_seconds=1.0)
        comp.set_scene(Scene(kind="solid", name="a", color=(1.0, 0.0, 0.0)), 0.0)
        comp.render(0.0)
        comp.set_scene(Scene(kind="off", name="sleep"), 1.0, instant=True)
        self.assertEqual(comp.render(1.0)[0], (0.0, 0.0, 0.0))

    def test_fill_smoothing_and_reset(self):
        comp = Compositor(10, fade_seconds=0.0, fill_tau=1.0)
        fill = Scene(kind="fill", name="progress", color=BLUE, fraction=0.0)
        comp.set_scene(fill, 0.0, fill_key="a")
        comp.render(0.0)
        comp.set_scene(fill.with_fraction(1.0), 0.1, fill_key="a")
        partial = comp.render(0.1)
        self.assertLess(sum(c[2] for c in partial), 10.0)   # eased, not jumped
        comp.set_scene(fill.with_fraction(0.0), 0.2, fill_key="b")   # a new download: jump
        self.assertEqual(sum(c[2] for c in comp.render(0.2)), 0.0)


if __name__ == "__main__":
    unittest.main()
