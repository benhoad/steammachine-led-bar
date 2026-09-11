import struct
import time
import unittest

from ledbar.steamcef import (
    CefDownloadSource, CefProgress, MiniWebSocket, WebSocketError, _fraction_from_percent, encode_frame,
)
from tests.fake_cdp import FakeCdpServer

OVERVIEW = {"paused": False, "percent": 54.2, "appid": 2552450, "state": "Running", "rate": 12345678.0}


class FrameCodecTests(unittest.TestCase):
    def test_small_frame_is_masked_and_final(self):
        frame = encode_frame(b"hello")
        self.assertEqual(frame[0], 0x81)             # FIN + text
        self.assertTrue(frame[1] & 0x80)             # masked, as clients must be
        self.assertEqual(frame[1] & 0x7F, 5)
        mask = frame[2:6]
        self.assertEqual(bytes(b ^ mask[i % 4] for i, b in enumerate(frame[6:])), b"hello")

    def test_medium_and_large_length_encodings(self):
        medium = encode_frame(b"x" * 200)
        self.assertEqual(medium[1] & 0x7F, 126)
        self.assertEqual(struct.unpack("!H", medium[2:4])[0], 200)
        large = encode_frame(b"x" * 70000)
        self.assertEqual(large[1] & 0x7F, 127)
        self.assertEqual(struct.unpack("!Q", large[2:10])[0], 70000)

    def test_boundary_at_125_126(self):
        self.assertEqual(encode_frame(b"x" * 125)[1] & 0x7F, 125)
        self.assertEqual(encode_frame(b"x" * 126)[1] & 0x7F, 126)

    def test_percent_normalisation(self):
        self.assertEqual(_fraction_from_percent(0), 0.0)
        self.assertAlmostEqual(_fraction_from_percent(54.2), 0.542)
        self.assertEqual(_fraction_from_percent(100), 1.0)
        self.assertEqual(_fraction_from_percent(0.5), 0.5)    # tolerate a 0..1 API
        self.assertEqual(_fraction_from_percent(250), 1.0)    # clamped


class MiniWebSocketTests(unittest.TestCase):
    def test_rejects_non_ws_url(self):
        with self.assertRaises(WebSocketError):
            MiniWebSocket("http://127.0.0.1:1/x").connect()


class CefDownloadSourceTests(unittest.TestCase):
    def test_reads_live_progress(self):
        server = FakeCdpServer(overview=OVERVIEW).start()
        try:
            source = CefDownloadSource(port=server.port, timeout=3.0)
            progress = source.poll()
            self.assertIsInstance(progress, CefProgress)
            self.assertTrue(progress.active)
            self.assertAlmostEqual(progress.fraction, 0.542)
            self.assertEqual(progress.appid, 2552450)
            self.assertFalse(progress.paused)
            self.assertTrue(source.available)
            # it must pick SharedJSContext, not the first target offered
            self.assertTrue(any("RegisterForDownloadOverview" in e for e in server.evaluations))
            source.close()
        finally:
            server.stop()

    def test_idle_when_steam_reports_nothing(self):
        idle = {"paused": False, "percent": 0, "appid": 0, "state": "None", "rate": 0}
        server = FakeCdpServer(overview=idle, downloading=False).start()
        try:
            source = CefDownloadSource(port=server.port, timeout=3.0)
            progress = source.poll()
            self.assertIsNotNone(progress)
            self.assertFalse(progress.active)
            self.assertIsNone(progress.fraction)
            source.close()
        finally:
            server.stop()

    def test_vague_activity_falls_back_to_manifests(self):
        """Steam signalling 'downloading' with no app and no progress is not usable.

        Trusting it paints a 0% bar, which looks identical to the strip being off.
        """
        vague = {"paused": False, "percent": 0, "appid": 0, "state": "Running", "rate": 0}
        server = FakeCdpServer(overview=vague, downloading=True).start()
        try:
            source = CefDownloadSource(port=server.port, timeout=3.0)
            self.assertIsNone(source.poll())        # None = let the manifests decide
            self.assertTrue(source.available)       # but the connection is still fine
        finally:
            server.stop()

    def test_named_app_at_zero_percent_is_indeterminate(self):
        """A real download that has not reported bytes yet breathes, never a 0% bar."""
        starting = {"paused": False, "percent": 0, "appid": 2552450, "state": "Running", "rate": 0}
        server = FakeCdpServer(overview=starting, downloading=True).start()
        try:
            progress = CefDownloadSource(port=server.port, timeout=3.0).poll()
            self.assertTrue(progress.active)
            self.assertIsNone(progress.fraction)    # indeterminate, not zero
            self.assertEqual(progress.appid, 2552450)
        finally:
            server.stop()

    def test_transfer_rate_alone_counts_as_concrete(self):
        moving = {"paused": False, "percent": 0, "appid": 0, "state": "Running", "rate": 5_000_000.0}
        server = FakeCdpServer(overview=moving, downloading=True).start()
        try:
            progress = CefDownloadSource(port=server.port, timeout=3.0).poll()
            self.assertIsNotNone(progress)
            self.assertTrue(progress.active)
        finally:
            server.stop()

    def test_unavailable_when_nothing_is_listening(self):
        source = CefDownloadSource(port=1, timeout=0.5, retry_seconds=0.05)
        self.assertIsNone(source.poll())
        self.assertFalse(source.available)
        self.assertTrue(source.last_error)

    def test_unavailable_when_steam_api_is_missing(self):
        server = FakeCdpServer(overview=OVERVIEW, register_ok=False).start()
        try:
            source = CefDownloadSource(port=server.port, timeout=3.0, retry_seconds=0.05)
            self.assertIsNone(source.poll())
            self.assertIn("unavailable", source.last_error)
        finally:
            server.stop()

    def test_missing_shared_js_context(self):
        server = FakeCdpServer(overview=OVERVIEW, title="Some Other Context").start()
        try:
            source = CefDownloadSource(port=server.port, timeout=3.0, retry_seconds=0.05)
            self.assertIsNone(source.poll())
            self.assertIn("SharedJSContext", source.last_error)
        finally:
            server.stop()

    def test_backoff_avoids_hammering_a_dead_port(self):
        source = CefDownloadSource(port=1, timeout=0.3, retry_seconds=30.0)
        self.assertIsNone(source.poll())
        started = time.monotonic()
        self.assertIsNone(source.poll())       # returns immediately, does not retry
        self.assertLess(time.monotonic() - started, 0.2)


if __name__ == "__main__":
    unittest.main()
