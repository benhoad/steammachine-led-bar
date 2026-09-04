import time
import unittest

try:
    import openrgb  # noqa: F401
    HAVE_OPENRGB = True
except ImportError:
    HAVE_OPENRGB = False

from ledbar.backends.base import BackendError
from ledbar.config import OpenRGBConfig

if HAVE_OPENRGB:
    from ledbar.backends.openrgb import OpenRGBBackend, OpenRGBModeBackend, inspect
    from ledbar.render import Scene
    from ledbar.state import Decision
    from tests.fake_openrgb import FakeServer, asrock_like, asrock_no_direct, keyboard_like, static_only


@unittest.skipUnless(HAVE_OPENRGB, "openrgb-python not installed")
class OpenRGBBackendTests(unittest.TestCase):
    def setUp(self):
        self.keyboard = keyboard_like()
        self.board = asrock_like()
        self.server = FakeServer([self.keyboard, self.board]).start()
        self.cfg = OpenRGBConfig(port=self.server.port, reconnect_seconds=0.05)

    def tearDown(self):
        self.server.stop()

    def frame(self, n, color=(10, 20, 30)):
        return [color] * n

    def test_auto_select_direct_and_resize(self):
        backend = OpenRGBBackend(self.cfg, total=24)
        backend.open()
        self.assertTrue(backend.healthy)
        self.assertEqual(backend.device_index, 1)
        self.assertEqual(backend.zone_index, 2)
        self.assertEqual(self.board.active_mode, 2)                 # Direct
        self.assertEqual(self.board.resizes, [(2, 24)])
        self.assertEqual(backend.start_index, 2)                    # after the two RGB headers
        self.assertIn("Addressable Header 1", backend.describe())
        self.assertIn("ledbar", self.server.client_names)

        colors = [(i, 255 - i, 7) for i in range(24)]
        self.assertTrue(backend.write(colors))
        time.sleep(0.05)
        self.assertEqual(self.board.device_updates, 1)
        self.assertEqual(self.board.zone_updates, [])               # never zone-level updates
        self.assertEqual(self.board.colors[2:26], colors)
        self.assertEqual(self.board.colors[0:2], [(0, 0, 0), (0, 0, 0)])
        self.assertEqual(len(self.board.colors), 27)

        backend.close([(0, 0, 0)] * 24, "off")
        time.sleep(0.05)
        self.assertEqual(self.board.colors[2:26], [(0, 0, 0)] * 24)
        self.assertFalse(backend.healthy)

    def test_no_resize_when_size_matches(self):
        self.board.resize(2, 24)
        self.board.resizes.clear()
        backend = OpenRGBBackend(self.cfg, total=24)
        backend.open()
        self.assertEqual(self.board.resizes, [])
        self.assertEqual(backend.start_index, 2)

    def test_explicit_selection_by_name_and_index(self):
        cfg = OpenRGBConfig(port=self.server.port, device="polychrome", zone="header 2")
        backend = OpenRGBBackend(cfg, total=10)
        backend.open()
        self.assertEqual((backend.device_index, backend.zone_index), (1, 3))
        self.assertEqual(self.board.resizes, [(3, 10)])
        cfg = OpenRGBConfig(port=self.server.port, device="1", zone="2")
        backend = OpenRGBBackend(cfg, total=5)
        backend.open()
        self.assertEqual((backend.device_index, backend.zone_index), (1, 2))

    def test_bad_selection_errors(self):
        with self.assertRaises(BackendError):
            OpenRGBBackend(OpenRGBConfig(port=self.server.port, device="nonexistent"), 24).open()
        with self.assertRaises(BackendError):
            OpenRGBBackend(OpenRGBConfig(port=self.server.port, device="1", zone="9"), 24).open()
        with self.assertRaises(BackendError):   # 300 LEDs do not fit a 100 LED zone
            OpenRGBBackend(OpenRGBConfig(port=self.server.port), 300).open()
        with self.assertRaises(BackendError):   # fixed size zone
            OpenRGBBackend(OpenRGBConfig(port=self.server.port, device="1", zone="PCH"), 3).open()

    def test_require_direct(self):
        server = FakeServer([keyboard_like()]).start()
        try:
            with self.assertRaises(BackendError):
                OpenRGBBackend(OpenRGBConfig(port=server.port), 24).open()
            backend = OpenRGBBackend(OpenRGBConfig(port=server.port, require_direct=False, zone="0", set_zone_size=False), 24)
            backend.open()
            self.assertTrue(backend.healthy)
        finally:
            server.stop()

    def test_reconnect_after_server_restart(self):
        backend = OpenRGBBackend(self.cfg, total=24)
        backend.open()
        port = self.server.port
        self.server.stop()
        # the first send into a dead socket can still succeed; the next one fails
        for _ in range(5):
            if not backend.write(self.frame(24)):
                break
            time.sleep(0.05)
        self.assertFalse(backend.healthy)
        self.server = FakeServer([self.keyboard, self.board], port=port).start()
        time.sleep(0.1)
        deadline = time.time() + 3
        while time.time() < deadline and not backend.write(self.frame(24)):
            time.sleep(0.05)
        self.assertTrue(backend.healthy)
        self.assertEqual(self.server.connections, 1)

    def test_server_down_at_start_then_up(self):
        self.server.stop()
        port = self.server.port
        backend = OpenRGBBackend(OpenRGBConfig(port=port, reconnect_seconds=0.05), total=24)
        backend.open()                     # must not raise: the service keeps retrying
        self.assertFalse(backend.healthy)
        self.server = FakeServer([self.board], port=port).start()
        deadline = time.time() + 3
        while time.time() < deadline and not backend.write(self.frame(24)):
            time.sleep(0.05)
        self.assertTrue(backend.healthy)

    def test_close_with_hardware_mode(self):
        backend = OpenRGBBackend(self.cfg, total=24)
        backend.open()
        backend.close([(0, 0, 255)] * 24, "mode:Breathing")
        time.sleep(0.05)
        self.assertEqual(self.board.active_mode, 1)
        self.assertEqual(self.board.colors[2], (0, 0, 255))

    def test_close_with_missing_hardware_mode_falls_back_to_off(self):
        board = asrock_like()
        board.mode_names = ["Static", "Direct"]
        server = FakeServer([board]).start()
        try:
            backend = OpenRGBBackend(OpenRGBConfig(port=server.port), total=24)
            backend.open()
            backend.write([(9, 9, 9)] * 24)
            backend.close([(0, 0, 255)] * 24, "mode:Breathing")
            time.sleep(0.05)
            self.assertEqual(board.active_mode, 1)                 # still Direct
            self.assertEqual(board.colors[2:26], [(0, 0, 0)] * 24)  # switched off
        finally:
            server.stop()

    def test_resync_reconnects(self):
        backend = OpenRGBBackend(self.cfg, total=24)
        backend.open()
        backend.resync()
        self.assertFalse(backend.healthy)
        self.assertTrue(backend.write(self.frame(24)))
        self.assertEqual(self.server.connections, 2)

    def test_inspect(self):
        text = inspect(self.cfg)
        self.assertIn("ASRock Polychrome USB", text)
        self.assertIn("Addressable Header 1", text)
        self.assertIn("resizable 0-100", text)


@unittest.skipUnless(HAVE_OPENRGB, "openrgb-python not installed")
class OpenRGBModeBackendTests(unittest.TestCase):
    def setUp(self):
        self.board = asrock_no_direct()
        self.server = FakeServer([self.board]).start()
        self.cfg = OpenRGBConfig(port=self.server.port, reconnect_seconds=0.05, whole_strip=True)

    def tearDown(self):
        self.server.stop()

    def backend(self, brightness=1.0, color_order="RGB"):
        return OpenRGBModeBackend(self.cfg, total=24, brightness=brightness, color_order=color_order)

    def decide(self, **kw):
        return Decision(Scene(**kw))

    def mode_id(self, name):
        return self.board.mode_names.index(name)

    def test_selects_and_starts_static(self):
        b = self.backend()
        b.open()
        self.assertTrue(b.healthy)
        self.assertTrue(b.mode_based)
        self.assertEqual(self.board.resizes, [(0, 24)])          # bar zone resized
        self.assertIn("whole-strip", b.describe())

    def test_solid_sets_static_and_colour(self):
        b = self.backend()
        b.open()
        import time
        self.assertTrue(b.apply_decision(self.decide(kind="solid", name="idle", color=(0.0, 0.0, 1.0)), time.monotonic()))
        time.sleep(0.05)
        self.assertEqual(self.board.active_mode, self.mode_id("Static"))
        self.assertEqual(self.board.colors[0:24], [(0, 0, 255)] * 24)
        self.assertEqual(self.board.colors[24:26], [(0, 0, 0), (0, 0, 0)])   # other zone untouched

    def test_brightness_and_colour_order(self):
        b = self.backend(brightness=0.5, color_order="GRB")
        b.open()
        import time
        b.apply_decision(self.decide(kind="solid", name="idle", color=(1.0, 0.0, 0.0)), time.monotonic())
        time.sleep(0.05)
        # red at 50% -> (128,0,0); GRB swap -> (0,128,0)
        self.assertEqual(self.board.colors[0], (0, 128, 0))

    def test_breathe_uses_breathing_mode(self):
        b = self.backend()
        b.open()
        import time
        b.apply_decision(self.decide(kind="breathe", name="boot", color=(0.0, 0.0, 1.0)), time.monotonic())
        time.sleep(0.05)
        self.assertEqual(self.board.active_mode, self.mode_id("Breathing"))

    def test_fault_segment_breathe_maps_to_breathing(self):
        b = self.backend()
        b.open()
        import time
        b.apply_decision(self.decide(kind="segment_breathe", name="fault", color=(1.0, 0.0, 0.0), segment="q2"), time.monotonic())
        time.sleep(0.05)
        self.assertEqual(self.board.active_mode, self.mode_id("Breathing"))
        self.assertEqual(self.board.colors[0], (255, 0, 0))

    def test_off_uses_off_mode_without_recolouring(self):
        b = self.backend()
        b.open()
        import time
        b.apply_decision(self.decide(kind="solid", name="idle", color=(0.0, 0.0, 1.0)), time.monotonic())
        time.sleep(0.05)
        updates = self.board.device_updates
        b.apply_decision(self.decide(kind="off", name="sleep"), time.monotonic())
        time.sleep(0.05)
        self.assertEqual(self.board.active_mode, self.mode_id("Off"))
        self.assertEqual(self.board.device_updates, updates)     # no colour write for Off

    def test_deduplicates_identical_state(self):
        b = self.backend()
        b.open()
        import time
        d = self.decide(kind="solid", name="idle", color=(0.0, 0.0, 1.0))
        b.apply_decision(d, time.monotonic())
        time.sleep(0.05)
        first = self.board.device_updates
        for _ in range(5):
            b.apply_decision(d, time.monotonic())
        time.sleep(0.05)
        self.assertEqual(self.board.device_updates, first)       # unchanged state -> no extra writes

    def test_breathe_falls_back_to_static_when_absent(self):
        server = FakeServer([static_only()]).start()
        try:
            b = OpenRGBModeBackend(OpenRGBConfig(port=server.port, whole_strip=True), total=24)
            b.open()
            import time
            b.apply_decision(self.decide(kind="breathe", name="boot", color=(0.0, 0.0, 1.0)), time.monotonic())
            time.sleep(0.05)
            self.assertTrue(b.healthy)   # no Breathing mode, but did not crash
        finally:
            server.stop()

    def test_gauge_interpolates_colour(self):
        b = self.backend()
        b.open()
        import time
        scene = self.decide(kind="gauge", name="temp", color=(0.0, 0.0, 1.0), color2=(1.0, 0.0, 0.0), fraction=1.0)
        b.apply_decision(scene, time.monotonic())
        time.sleep(0.05)
        self.assertEqual(self.board.colors[0], (255, 0, 0))      # fraction 1.0 -> hot colour


if __name__ == "__main__":
    unittest.main()
