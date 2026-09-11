import unittest

from ledbar.health import LinkHealthMonitor


class FakeFetch:
    """Stands in for HTTP; records URLs and returns canned info."""

    def __init__(self, live=True, fail=False):
        self.live = live
        self.fail = fail
        self.urls = []

    def __call__(self, url, timeout=3.0):
        self.urls.append(url)
        if self.fail:
            raise OSError("connection refused")
        if url.endswith("/reset"):
            return {}
        return {"live": self.live, "leds": {"count": 24}}


def monitor(fetch, runner=None, **kwargs):
    kwargs.setdefault("host", "10.0.0.9")
    kwargs.setdefault("grace_seconds", 60.0)
    m = LinkHealthMonitor(fetch=fetch, runner=runner or (lambda cmd: None),
                          discover=lambda: "", **kwargs)
    m.set_streaming(True)
    return m


class LivenessTests(unittest.TestCase):
    def test_healthy_when_the_controller_reports_live(self):
        m = monitor(FakeFetch(live=True))
        state = m.check(now=100.0)
        self.assertTrue(state.checked)
        self.assertTrue(state.healthy)

    def test_grace_period_before_declaring_a_fault(self):
        m = monitor(FakeFetch(live=False))
        m.check(now=100.0)                       # starts the clock
        self.assertTrue(m.check(now=140.0).healthy)   # 40s < 60s grace
        state = m.check(now=200.0)                    # 100s > grace
        self.assertFalse(state.healthy)
        self.assertIn("no realtime source", state.reason)

    def test_not_streaming_is_never_a_fault(self):
        """Nothing is expected when ledbar is not sending, e.g. while asleep."""
        m = monitor(FakeFetch(live=False))
        m.set_streaming(False)
        m.check(now=100.0)
        self.assertTrue(m.check(now=500.0).healthy)

    def test_unreachable_controller_is_not_a_fault(self):
        """A Wi-Fi blip must not be reported as a dead link."""
        m = monitor(FakeFetch(fail=True))
        state = m.check(now=100.0)
        self.assertFalse(state.checked)
        self.assertTrue(state.healthy)
        self.assertIn("unreachable", state.reason)

    def test_recovers_when_frames_land_again(self):
        fetch = FakeFetch(live=False)
        m = monitor(fetch)
        m.check(now=100.0)
        self.assertFalse(m.check(now=200.0).healthy)
        fetch.live = True
        state = m.check(now=230.0)
        self.assertTrue(state.healthy)
        self.assertEqual(state.reason, "")

    def test_warn_action_does_not_touch_the_controller(self):
        fetch = FakeFetch(live=False)
        commands = []
        m = monitor(fetch, runner=commands.append, action="warn")
        m.check(now=100.0)
        m.check(now=200.0)
        self.assertNotIn("http://10.0.0.9/reset", fetch.urls)
        self.assertEqual(commands, [])


class ResetTests(unittest.TestCase):
    def test_reset_reboots_and_reopens_the_port(self):
        fetch = FakeFetch(live=False)
        commands = []
        m = monitor(fetch, runner=commands.append, action="reset")
        m.check(now=100.0)
        m.check(now=200.0)
        self.assertIn("http://10.0.0.9/reset", fetch.urls)
        # OpenRGB holds a stale fd after the controller re-enumerates
        self.assertEqual(commands, [["systemctl", "--user", "restart", "ledbar-openrgb"]])

    def test_gives_the_controller_time_before_resetting_again(self):
        """After a reset, wait out the grace window before trying anything else.

        The link stays reported as unhealthy - it genuinely is, until liveness
        comes back - but we must not hammer the controller with reboots.
        """
        fetch = FakeFetch(live=False)
        m = monitor(fetch, action="reset")
        m.check(now=100.0)
        m.check(now=200.0)                       # first reset
        before = fetch.urls.count("http://10.0.0.9/reset")
        m.check(now=210.0)                       # inside the post-reset grace window
        m.check(now=240.0)
        self.assertEqual(fetch.urls.count("http://10.0.0.9/reset"), before)

    def test_rate_limited(self):
        fetch = FakeFetch(live=False)
        m = monitor(fetch, action="reset", max_resets_per_hour=2)
        now = 100.0
        for _ in range(6):
            m.check(now=now)
            now += 200.0
        self.assertEqual(fetch.urls.count("http://10.0.0.9/reset"), 2)

    def test_restart_command_can_be_disabled(self):
        fetch = FakeFetch(live=False)
        commands = []
        m = monitor(fetch, runner=commands.append, action="reset", restart_command="")
        m.check(now=100.0)
        m.check(now=200.0)
        self.assertIn("http://10.0.0.9/reset", fetch.urls)
        self.assertEqual(commands, [])


class HostTests(unittest.TestCase):
    def test_configured_host_wins(self):
        m = LinkHealthMonitor(host="1.2.3.4", fetch=FakeFetch(), discover=lambda: "9.9.9.9")
        self.assertEqual(m.resolve_host(), "1.2.3.4")

    def test_falls_back_to_discovery_once(self):
        calls = []

        def discover():
            calls.append(1)
            return "9.9.9.9"

        m = LinkHealthMonitor(host="", fetch=FakeFetch(), discover=discover)
        self.assertEqual(m.resolve_host(), "9.9.9.9")
        self.assertEqual(m.resolve_host(), "9.9.9.9")
        self.assertEqual(len(calls), 1)          # cached

    def test_no_host_means_no_checks(self):
        fetch = FakeFetch()
        m = LinkHealthMonitor(host="", fetch=fetch, discover=lambda: "")
        m.set_streaming(True)
        state = m.check(now=100.0)
        self.assertEqual(fetch.urls, [])
        self.assertTrue(state.healthy)

    def test_disabled_does_nothing(self):
        fetch = FakeFetch(live=False)
        m = LinkHealthMonitor(enabled=False, host="1.2.3.4", fetch=fetch, discover=lambda: "")
        m.set_streaming(True)
        m.check(now=100.0)
        self.assertEqual(fetch.urls, [])


if __name__ == "__main__":
    unittest.main()
