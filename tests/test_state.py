import unittest
from pathlib import Path

from ledbar.config import Config
from ledbar.sensors import Fault
from ledbar.state import Inputs, StateMachine, Threshold
from ledbar.steam import AppStatus, DownloadState


def download(fraction, phase="downloading", appid=440, paused=False):
    app = AppStatus(appid=appid, name="Game", flags=1048576 | 1024 | 6, bytes_to_download=100,
                    bytes_downloaded=int(fraction * 100), bytes_to_stage=0, bytes_staged=0, mtime=0.0,
                    path=Path("/x"), downloading_dir=True)
    return DownloadState(active=True, fraction=fraction, phase=phase, app=app, paused=paused, steam_running=True)


class ThresholdTests(unittest.TestCase):
    def test_hysteresis(self):
        t = Threshold(100, 5)
        self.assertFalse(t.update(99))
        self.assertTrue(t.update(100))
        self.assertTrue(t.update(96))
        self.assertFalse(t.update(95))
        self.assertFalse(t.update(None))


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.sm = StateMachine(self.config, 0.0)
        self.idle = DownloadState(steam_running=True)

    def scene(self, **kwargs):
        return self.sm.update(Inputs(**kwargs)).scene

    def test_boot_then_idle(self):
        self.assertEqual(self.scene(now=0.5).name, "boot")
        self.assertEqual(self.scene(now=1.0, steam=self.idle).name, "boot")   # min_seconds not reached
        self.assertEqual(self.scene(now=2.5, steam=self.idle).name, "idle")

    def test_boot_times_out_without_steam(self):
        self.assertEqual(self.scene(now=50.0).name, "boot")
        self.assertEqual(self.scene(now=91.0).name, "idle")

    def test_progress_and_completion_flash(self):
        self.scene(now=5.0, steam=self.idle)
        d = self.sm.update(Inputs(now=6.0, steam=download(0.4)))
        self.assertEqual(d.scene.name, "progress")
        self.assertEqual(d.scene.fraction, 0.4)
        self.assertEqual(d.fill_key, "app-440")
        self.assertIn("40%", d.status)
        self.sm.update(Inputs(now=7.0, steam=download(0.99)))
        done = self.sm.update(Inputs(now=8.0, steam=self.idle))
        self.assertEqual(done.scene.name, "complete")
        self.assertEqual(self.scene(now=8.0 + self.config.progress.complete_seconds + 0.1, steam=self.idle).name, "idle")

    def test_cancelled_download_has_no_flash(self):
        self.scene(now=5.0, steam=self.idle)
        self.sm.update(Inputs(now=6.0, steam=download(0.3)))
        self.assertEqual(self.scene(now=7.0, steam=self.idle).name, "idle")

    def test_indeterminate_progress_breathes(self):
        self.scene(now=5.0, steam=self.idle)
        state = download(0.0)
        state.fraction = None
        self.assertEqual(self.scene(now=6.0, steam=state).name, "progress-indeterminate")

    def test_thermal_priority_over_progress(self):
        self.scene(now=5.0, steam=self.idle)
        self.assertEqual(self.scene(now=6.0, steam=download(0.5), gpu_temp=101.0).name, "thermal-critical")
        self.assertEqual(self.scene(now=7.0, steam=download(0.5), gpu_temp=97.0).name, "thermal-critical")
        self.assertEqual(self.scene(now=8.0, steam=download(0.5), gpu_temp=90.0).name, "progress")

    def test_thermal_warning_optional(self):
        self.scene(now=5.0, steam=self.idle)
        self.assertEqual(self.scene(now=6.0, steam=self.idle, cpu_temp=95.0).name, "idle")
        self.config.thermal.warning_c = 90.0
        sm = StateMachine(self.config, 0.0)
        sm.update(Inputs(now=5.0, steam=self.idle))
        self.assertEqual(sm.update(Inputs(now=6.0, steam=self.idle, cpu_temp=95.0)).scene.name, "thermal-warning")

    def test_faults_and_segments(self):
        self.scene(now=5.0, steam=self.idle)
        d = self.sm.update(Inputs(now=6.0, steam=self.idle, faults=[Fault("ssd", "q2", "no ssd")]))
        self.assertEqual(d.scene.name, "fault-ssd")
        self.assertEqual(d.scene.kind, "segment_breathe")
        self.assertEqual(d.scene.segment, "q2")

    def test_sleep_and_shutdown(self):
        self.scene(now=5.0, steam=self.idle)
        self.assertEqual(self.scene(now=6.0, steam=download(0.5), sleeping=True).name, "sleep")
        self.assertEqual(self.scene(now=7.0, steam=download(0.5), shutting_down=True).name, "shutdown")
        self.config.power.sleep = "hold"
        self.assertEqual(self.scene(now=8.0, steam=download(0.5), sleeping=True).name, "progress")

    def test_game_modes(self):
        self.scene(now=5.0, steam=self.idle)
        running = DownloadState(steam_running=True, running_appid=440)
        self.assertEqual(self.scene(now=6.0, steam=running).name, "idle")
        self.config.game.mode = "dim"
        d = self.sm.update(Inputs(now=7.0, steam=running))
        self.assertEqual(d.scene.name, "game-dim")
        self.assertAlmostEqual(d.scene.level, 0.3)
        self.config.game.mode = "off"
        self.assertEqual(self.scene(now=8.0, steam=running).name, "game-off")

    def test_idle_modes(self):
        self.scene(now=5.0, steam=self.idle)
        self.config.idle.mode = "temperature"
        d = self.sm.update(Inputs(now=6.0, steam=self.idle, cpu_temp=47.5))
        self.assertEqual(d.scene.kind, "gauge")
        self.assertAlmostEqual(d.scene.fraction, 0.5)
        self.config.idle.mode = "off"
        self.assertEqual(self.scene(now=7.0, steam=self.idle).name, "idle-off")

    def test_paused_download_dims_when_shown(self):
        self.config.progress.show_paused = True
        self.scene(now=5.0, steam=self.idle)
        d = self.sm.update(Inputs(now=6.0, steam=download(0.5, paused=True)))
        self.assertEqual(d.scene.name, "progress")
        self.assertAlmostEqual(d.scene.level, 0.35)

    def test_restart_boot(self):
        self.scene(now=5.0, steam=self.idle)
        self.sm.restart_boot(10.0)
        self.assertEqual(self.scene(now=10.5, steam=self.idle).name, "boot")


if __name__ == "__main__":
    unittest.main()
