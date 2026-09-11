"""The wake event must actually reach the health check, not just exist."""

import unittest

from ledbar.backends.null import NullBackend
from ledbar.config import Config
from ledbar.daemon import Daemon, InputSource
from ledbar.power import PowerMonitor
from ledbar.state import Inputs
from ledbar.steam import DownloadState


class ResumeOnce(InputSource):
    def __init__(self):
        self.fired = False

    def poll(self, now):
        return Inputs(now=now, steam=DownloadState(steam_running=True))

    def take_resume_event(self) -> bool:
        if self.fired:
            return False
        self.fired = True
        return True


class RecordingHealth:
    def __init__(self):
        self.resumes = []
        self.streaming = None

    def start(self): pass
    def stop(self): pass
    def set_streaming(self, value): self.streaming = value
    def on_resume(self, delay=12.0): self.resumes.append(delay)


class ResumeWiringTests(unittest.TestCase):
    def test_daemon_triggers_the_health_check_on_wake(self):
        config = Config()
        config.leds.fps = 60
        config.health.resume_check_seconds = 7.5
        daemon = Daemon(config, NullBackend(config.leds.total), ResumeOnce())
        health = RecordingHealth()
        daemon.health = health
        daemon.run(max_seconds=0.3)
        self.assertEqual(health.resumes, [7.5], "resume must reach the health check")

    def test_backend_is_resynced_on_wake_too(self):
        class SpyBackend(NullBackend):
            def __init__(self, total):
                super().__init__(total)
                self.resyncs = 0

            def resync(self):
                self.resyncs += 1

        config = Config()
        config.leds.fps = 60
        backend = SpyBackend(config.leds.total)
        daemon = Daemon(config, backend, ResumeOnce())
        daemon.health = RecordingHealth()
        daemon.run(max_seconds=0.3)
        self.assertEqual(backend.resyncs, 1)


class PowerMonitorSignalTests(unittest.TestCase):
    """logind emits PrepareForSleep(true) going down and (false) coming back."""

    def test_resume_is_only_reported_after_a_sleep(self):
        monitor = PowerMonitor(enabled=False)
        monitor._handle_gdbus_line(
            "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (false,)")
        self.assertFalse(monitor.take_resume_event(), "a stray false must not look like a wake")

    def test_sleep_then_wake_reports_once(self):
        monitor = PowerMonitor(enabled=False)
        monitor._handle_gdbus_line(
            "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)")
        self.assertTrue(monitor.sleeping)
        monitor._handle_gdbus_line(
            "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (false,)")
        self.assertFalse(monitor.sleeping)
        self.assertTrue(monitor.take_resume_event())
        self.assertFalse(monitor.take_resume_event(), "the event is consumed once")


if __name__ == "__main__":
    unittest.main()
