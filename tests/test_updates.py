import unittest

from ledbar.config import Config
from ledbar.render import Scene
from ledbar.state import Decision, Inputs, StateMachine, indicator_color
from ledbar.steam import AppStatus, DownloadState
from ledbar.updates import DEFAULT_UNITS, SystemUpdate, SystemUpdateMonitor


def fake_states(mapping):
    """Stand in for `systemctl show`."""
    return lambda units: {unit: mapping.get(unit, "inactive") for unit in units}


class SystemUpdateMonitorTests(unittest.TestCase):
    def test_disabled_never_reports(self):
        monitor = SystemUpdateMonitor(enabled=False, states=fake_states({"uupd.service": "active"}))
        self.assertFalse(monitor.poll(now=0.0).active)

    def test_detects_the_bazzite_updater(self):
        monitor = SystemUpdateMonitor(enabled=True, states=fake_states({"uupd.service": "active"}))
        state = monitor.poll(now=0.0)
        self.assertTrue(state.active)
        self.assertEqual(state.unit, "uupd.service")
        self.assertIsNone(state.fraction)      # no percentage available

    def test_detects_bootc_and_rpm_ostree(self):
        for unit in ("bootc-fetch-apply-updates.service", "rpm-ostreed-automatic.service"):
            monitor = SystemUpdateMonitor(enabled=True, states=fake_states({unit: "activating"}))
            self.assertEqual(monitor.poll(now=0.0).unit, unit, unit)

    def test_idle_when_nothing_is_running(self):
        monitor = SystemUpdateMonitor(enabled=True, states=fake_states({}))
        self.assertFalse(monitor.poll(now=0.0).active)

    def test_extra_units_are_honoured(self):
        monitor = SystemUpdateMonitor(enabled=True, units=["my-updater.service"],
                                      states=fake_states({"my-updater.service": "active"}))
        self.assertEqual(monitor.poll(now=0.0).unit, "my-updater.service")

    def test_respects_the_poll_interval(self):
        calls = []

        def counting(units):
            calls.append(1)
            return {unit: "inactive" for unit in units}

        monitor = SystemUpdateMonitor(enabled=True, poll_interval=5.0, states=counting)
        monitor.poll(now=0.0)
        monitor.poll(now=1.0)
        monitor.poll(now=2.0)
        self.assertEqual(len(calls), 1)        # cached between polls
        monitor.poll(now=6.0)
        self.assertEqual(len(calls), 2)

    def test_missing_systemctl_is_harmless(self):
        monitor = SystemUpdateMonitor(enabled=True, states=lambda units: {})
        self.assertFalse(monitor.poll(now=0.0).active)

    def test_defaults_cover_the_bazzite_mechanism(self):
        self.assertIn("uupd.service", DEFAULT_UNITS)


class SystemUpdateSceneTests(unittest.TestCase):
    """With an indicator LED available: Steam Machine behaviour."""

    def setUp(self):
        self.config = Config()
        self.config.leds.offset, self.config.leds.offset_mode = 1, "power_led"
        self.machine = StateMachine(self.config, 0.0)
        self.idle = DownloadState(steam_running=True)
        self.machine.update(Inputs(now=5.0, steam=self.idle))   # past boot

    def test_bar_matches_a_download_and_breathes_without_progress(self):
        d = self.machine.update(Inputs(now=6.0, steam=self.idle,
                                       system_update=SystemUpdate(active=True, unit="uupd.service")))
        self.assertEqual(d.scene.name, "system-update")
        self.assertEqual(d.scene.kind, "breathe")
        self.assertIn("uupd.service", d.status)

    def test_fills_when_a_fraction_is_available(self):
        d = self.machine.update(Inputs(now=7.0, steam=self.idle,
                                       system_update=SystemUpdate(active=True, unit="u", fraction=0.4)))
        self.assertEqual(d.scene.kind, "fill")
        self.assertAlmostEqual(d.scene.fraction, 0.4)
        self.assertEqual(d.scene.color, self.machine.c_bar)   # same colour as a download

    def test_outranks_a_game_download(self):
        app = AppStatus(appid=1, name="Game", flags=0, bytes_to_download=100, bytes_downloaded=50,
                        bytes_to_stage=0, bytes_staged=0, mtime=0.0, path=__import__("pathlib").Path("/x"))
        downloading = DownloadState(active=True, fraction=0.5, phase="downloading", app=app, steam_running=True)
        d = self.machine.update(Inputs(now=8.0, steam=downloading,
                                       system_update=SystemUpdate(active=True, unit="uupd.service")))
        self.assertEqual(d.scene.name, "system-update")

    def test_thermal_still_wins(self):
        d = self.machine.update(Inputs(now=9.0, steam=self.idle, cpu_temp=105.0,
                                       system_update=SystemUpdate(active=True, unit="uupd.service")))
        self.assertEqual(d.scene.name, "thermal-critical")


class SystemUpdateWithoutIndicatorTests(unittest.TestCase):
    """No indicator LED: the bar must blink, or it looks like a game download."""

    def setUp(self):
        self.config = Config()                       # offset_mode defaults to "off"
        self.machine = StateMachine(self.config, 0.0)
        self.idle = DownloadState(steam_running=True)
        self.machine.update(Inputs(now=5.0, steam=self.idle))

    def update(self, **kwargs):
        return self.machine.update(Inputs(now=6.0, steam=self.idle,
                                          system_update=SystemUpdate(active=True, unit="uupd.service", **kwargs)))

    def test_blinks_by_default(self):
        d = self.update()
        self.assertEqual(d.scene.name, "system-update")
        self.assertEqual(d.scene.kind, "blink")

    def test_blinks_even_when_a_fraction_is_known(self):
        """Without a second light the distinction matters more than the percentage."""
        self.assertEqual(self.update(fraction=0.5).scene.kind, "blink")

    def test_indicator_configured_but_offset_zero_still_blinks(self):
        self.config.leds.offset_mode, self.config.leds.offset = "power_led", 0
        machine = StateMachine(self.config, 0.0)
        machine.update(Inputs(now=5.0, steam=self.idle))
        d = machine.update(Inputs(now=6.0, steam=self.idle,
                                  system_update=SystemUpdate(active=True, unit="u")))
        self.assertEqual(d.scene.kind, "blink")

    def test_daemon_can_override_when_the_backend_cannot_address_it(self):
        """Whole-strip mode has no addressable indicator, so blink there too."""
        self.config.leds.offset, self.config.leds.offset_mode = 1, "power_led"
        machine = StateMachine(self.config, 0.0)
        self.assertTrue(machine.has_indicator)
        machine.has_indicator = False              # what Daemon.run() does in basic mode
        machine.update(Inputs(now=5.0, steam=self.idle))
        d = machine.update(Inputs(now=6.0, steam=self.idle,
                                  system_update=SystemUpdate(active=True, unit="u")))
        self.assertEqual(d.scene.kind, "blink")

    def test_a_game_download_is_still_a_plain_fill(self):
        from pathlib import Path as _P

        app = AppStatus(appid=1, name="G", flags=0, bytes_to_download=100, bytes_downloaded=50,
                        bytes_to_stage=0, bytes_staged=0, mtime=0.0, path=_P("/x"))
        d = self.machine.update(Inputs(now=7.0, steam=DownloadState(
            active=True, fraction=0.5, phase="downloading", app=app, steam_running=True)))
        self.assertEqual(d.scene.kind, "fill")     # so the two are visually distinct


class UpdateIndicatorTests(unittest.TestCase):
    """The Steam Machine keeps the bar identical and recolours the small LED."""

    def setUp(self):
        self.config = Config()

    def colour(self, name):
        return indicator_color(Decision(Scene(name=name)), self.config)

    def test_defaults_to_the_bar_colour_during_an_update(self):
        from ledbar.colors import parse_hex

        self.assertEqual(self.colour("system-update"), parse_hex(self.config.colors.bar))
        self.assertEqual(self.colour("idle"), (1.0, 1.0, 1.0))          # white normally

    def test_custom_update_colour(self):
        self.config.indicator.update_color = "#00ff00"
        self.assertEqual(self.colour("system-update"), (0.0, 1.0, 0.0))


if __name__ == "__main__":
    unittest.main()
