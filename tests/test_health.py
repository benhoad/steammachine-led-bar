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


class ResumeTests(unittest.TestCase):
    """Waking the host can leave the controller's USB endpoint wedged."""

    def test_short_grace_declares_a_dead_link_quickly(self):
        fetch = FakeFetch(live=False)
        m = monitor(fetch, grace_seconds=600.0)          # routine grace is long
        m.check(now=100.0)                                # seeds the clock
        self.assertTrue(m.check(now=115.0).healthy)       # 15s: fine by the long grace
        state = m.check(now=115.0, grace=8.0)             # the resume check is impatient
        self.assertFalse(state.healthy)

    def test_resume_recovers_when_configured_to_reset(self):
        fetch = FakeFetch(live=False)
        commands = []
        m = monitor(fetch, runner=commands.append, action="reset", grace_seconds=600.0)
        m.check(now=100.0)
        m.check(now=115.0, grace=8.0)
        self.assertIn("http://10.0.0.9/reset", fetch.urls)
        self.assertEqual(commands, [["systemctl", "--user", "restart", "ledbar-openrgb"]])

    def test_resume_is_quiet_when_the_link_came_back_by_itself(self):
        fetch = FakeFetch(live=True)
        commands = []
        m = monitor(fetch, runner=commands.append, action="reset")
        m.check(now=100.0)
        state = m.check(now=115.0, grace=8.0)
        self.assertTrue(state.healthy)
        self.assertNotIn("http://10.0.0.9/reset", fetch.urls)
        self.assertEqual(commands, [])

    def test_resume_returns_as_soon_as_the_link_is_live(self):
        """A healthy wake must cost nothing, whatever re-enumeration takes."""
        import time as _t
        fetch = FakeFetch(live=False)
        commands = []
        m = monitor(fetch, runner=commands.append, action="reset")
        m.on_resume(delay=10.0, poll=0.05)
        _t.sleep(0.12)
        fetch.live = True                       # link comes back mid-window
        _t.sleep(0.25)
        self.assertNotIn("http://10.0.0.9/reset", fetch.urls)   # never intervened
        self.assertEqual(commands, [])

    def test_resume_intervenes_only_after_the_window(self):
        import time as _t
        fetch = FakeFetch(live=False)           # never comes back
        commands = []
        m = monitor(fetch, runner=commands.append, action="reset")
        m.on_resume(delay=0.2, poll=0.05)
        _t.sleep(0.1)
        self.assertNotIn("http://10.0.0.9/reset", fetch.urls)   # still waiting
        _t.sleep(0.5)
        self.assertIn("http://10.0.0.9/reset", fetch.urls)      # window expired, acted
        self.assertEqual(commands, [["systemctl", "--user", "restart", "ledbar-openrgb"]])

    def test_check_reports_liveness_separately_from_health(self):
        m = monitor(FakeFetch(live=False), grace_seconds=600.0)
        state = m.check(now=100.0)
        self.assertFalse(state.live)            # not receiving
        self.assertTrue(state.healthy)          # but still inside the grace period

    def test_on_resume_restarts_the_clock(self):
        fetch = FakeFetch(live=False)
        m = monitor(fetch)
        m.check(now=100.0)
        m.check(now=300.0)                                # unhealthy by now
        self.assertFalse(m.snapshot().healthy)
        m.on_resume(delay=999.0)                          # thread will not fire during the test
        self.assertNotEqual(m.snapshot().last_live, 0.0)  # clock restarted at the resume

    def test_on_resume_is_a_noop_when_disabled(self):
        m = LinkHealthMonitor(enabled=False, host="1.2.3.4", fetch=FakeFetch(live=False),
                              discover=lambda: "")
        m.on_resume(delay=999.0)
        self.assertEqual(m.snapshot().last_live, 0.0)


class IdleColourTests(unittest.TestCase):
    """WLED forgets its idle colour on reboot, so ledbar re-pushes it."""

    def setUp(self):
        self.posts = []

        def fake_set(host, color, timeout=5.0):
            self.posts.append((host, color))

        import ledbar.wledsetup as wledsetup
        self._real = wledsetup.set_idle_color
        wledsetup.set_idle_color = fake_set

    def tearDown(self):
        import ledbar.wledsetup as wledsetup
        wledsetup.set_idle_color = self._real

    def make(self, uptimes):
        """A fetch whose reported uptime walks through the given values."""
        seq = list(uptimes)

        class Fetch:
            urls = []

            def __call__(self, url, timeout=3.0):
                return {"live": True, "uptime": seq.pop(0) if seq else 0}

        return monitor(Fetch(), idle_color=(0, 0, 0))

    def test_pushed_on_first_contact(self):
        m = self.make([100])
        m.check(now=10.0)
        self.assertEqual(self.posts, [("10.0.0.9", (0, 0, 0))])

    def test_not_pushed_again_while_it_keeps_running(self):
        m = self.make([100, 130, 160])
        for t in (10.0, 40.0, 70.0):
            m.check(now=t)
        self.assertEqual(len(self.posts), 1)

    def test_pushed_again_after_the_controller_reboots(self):
        m = self.make([100, 130, 5])          # uptime goes backwards = it restarted
        for t in (10.0, 40.0, 70.0):
            m.check(now=t)
        self.assertEqual(len(self.posts), 2)

    def test_not_pushed_when_unset(self):
        m = self.make([100])
        m.idle_color = None
        m.check(now=10.0)
        self.assertEqual(self.posts, [])


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
