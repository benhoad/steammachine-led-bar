import os
import tempfile
import time
import unittest
from pathlib import Path

from ledbar import steam
from ledbar.steam import (
    STATE_COMMITTING, STATE_DOWNLOADING, STATE_FULLY_INSTALLED, STATE_STAGING, STATE_UPDATE_PAUSED,
    STATE_UPDATE_REQUIRED, STATE_UPDATE_STARTED, AppStatus, SteamMonitor, describe_flags, library_steamapps_dirs,
    parse_app_manifest, parse_vdf, running_game_appid, steam_is_running,
)

MANIFEST = '''"AppState"
{
\t"appid"\t\t"440"
\t"Universe"\t\t"1"
\t"name"\t\t"Team Fortress 2"
\t"StateFlags"\t\t"%d"
\t"installdir"\t\t"Team Fortress 2"
\t"BytesToDownload"\t\t"1000"
\t"BytesDownloaded"\t\t"250"
\t"BytesToStage"\t\t"4000"
\t"BytesStaged"\t\t"1000"
\t"UserConfig"
\t{
\t\t"language"\t\t"english"
\t}
}
'''


class VdfTests(unittest.TestCase):
    def test_nested_parse(self):
        data = parse_vdf('"a" { "b" "1" "c" { "d" "x y" } } // comment\n"e" "2"')
        self.assertEqual(data, {"a": {"b": "1", "c": {"d": "x y"}}, "e": "2"})

    def test_tolerates_truncated_file(self):
        data = parse_vdf('"AppState" { "appid" "10" "name" "Half')
        self.assertEqual(data["AppState"]["appid"], "10")

    def test_escapes(self):
        data = parse_vdf(r'"k" "quote \" here"')
        self.assertEqual(data["k"], 'quote " here')


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.steamapps = Path(self.tmp.name) / "steamapps"
        self.steamapps.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, flags, appid=440):
        path = self.steamapps / f"appmanifest_{appid}.acf"
        path.write_text(MANIFEST % flags)
        return path

    def test_parse_downloading(self):
        status = parse_app_manifest(self.write(STATE_DOWNLOADING | STATE_UPDATE_STARTED | 6))
        self.assertEqual(status.appid, 440)
        self.assertEqual(status.name, "Team Fortress 2")
        self.assertTrue(status.is_active)
        self.assertEqual(status.phase, "downloading")
        self.assertAlmostEqual(status.fraction, 0.25)

    def test_parse_staging_uses_stage_bytes(self):
        status = parse_app_manifest(self.write(STATE_STAGING | STATE_UPDATE_STARTED | 6))
        self.assertEqual(status.phase, "installing")
        self.assertAlmostEqual(status.fraction, 0.25)

    def test_committing_is_full(self):
        status = parse_app_manifest(self.write(STATE_COMMITTING | 4))
        self.assertEqual(status.fraction, 1.0)

    def test_installed_is_idle(self):
        status = parse_app_manifest(self.write(STATE_FULLY_INSTALLED))
        self.assertFalse(status.is_active)
        self.assertEqual(status.phase, "idle")

    def test_paused(self):
        status = parse_app_manifest(self.write(STATE_UPDATE_PAUSED | STATE_UPDATE_STARTED | 6))
        self.assertTrue(status.is_paused)
        self.assertFalse(status.is_active)

    def test_describe_flags(self):
        self.assertEqual(describe_flags(1026), "Update Required, Update Started")
        self.assertEqual(describe_flags(1049606), "Update Required, Fully Installed, Update Started, Downloading")

    def test_downloading_dir_detected(self):
        (self.steamapps / "downloading" / "440").mkdir(parents=True)
        status = parse_app_manifest(self.write(STATE_DOWNLOADING))
        self.assertTrue(status.downloading_dir)


class LibraryTests(unittest.TestCase):
    def test_libraryfolders_new_and_old_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Steam"
            (root / "steamapps").mkdir(parents=True)
            lib2 = Path(tmp) / "Games" / "SteamLibrary"
            (lib2 / "steamapps").mkdir(parents=True)
            lib3 = Path(tmp) / "Old"
            (lib3 / "steamapps").mkdir(parents=True)
            missing = Path(tmp) / "missing"
            (root / "steamapps" / "libraryfolders.vdf").write_text(
                '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n'
                '\t"2"\t\t"%s"\n\t"3"\t\t"%s"\n\t"contentstatsid"\t\t"1"\n}\n' % (root, lib2, lib3, missing)
            )
            dirs = library_steamapps_dirs([root])
            self.assertEqual(dirs, [root / "steamapps", lib2 / "steamapps", lib3 / "steamapps"])


class ProcTests(unittest.TestCase):
    def test_fake_proc(self):
        with tempfile.TemporaryDirectory() as proc:
            os.makedirs(os.path.join(proc, "100"))
            os.makedirs(os.path.join(proc, "200"))
            os.makedirs(os.path.join(proc, "notapid"))
            with open(os.path.join(proc, "100", "comm"), "w") as f:
                f.write("steam\n")
            with open(os.path.join(proc, "100", "cmdline"), "wb") as f:
                f.write(b"/home/x/.local/share/Steam/ubuntu12_32/steam\0")
            with open(os.path.join(proc, "200", "comm"), "w") as f:
                f.write("reaper\n")
            with open(os.path.join(proc, "200", "cmdline"), "wb") as f:
                f.write(b"reaper\0SteamLaunch\0AppId=620\0--\0/game/bin\0")
            self.assertTrue(steam_is_running(proc))
            # cmdline has NUL separators; the regex must still match
            self.assertIsNone(running_game_appid(proc))
            with open(os.path.join(proc, "200", "cmdline"), "wb") as f:
                f.write(b"reaper SteamLaunch AppId=620 -- /game/bin\0")
            self.assertEqual(running_game_appid(proc), 620)

    def test_no_proc(self):
        self.assertFalse(steam_is_running("/nonexistent/proc"))
        self.assertIsNone(running_game_appid("/nonexistent/proc"))


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Steam"
        self.steamapps = self.root / "steamapps"
        self.steamapps.mkdir(parents=True)
        self.monitor = SteamMonitor(extra_roots=[str(self.root)], stale_seconds=60, require_steam_process=False)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, flags, appid=440, mtime=None):
        path = self.steamapps / f"appmanifest_{appid}.acf"
        path.write_text(MANIFEST % flags)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_reports_active_download(self):
        self.write(STATE_DOWNLOADING | STATE_UPDATE_STARTED | 6)
        state = self.monitor.poll()
        self.assertTrue(state.active)
        self.assertEqual(state.phase, "downloading")
        self.assertAlmostEqual(state.fraction, 0.25)
        self.assertIn(self.steamapps, state.libraries)

    def test_idle_when_installed(self):
        self.write(STATE_FULLY_INSTALLED)
        self.assertFalse(self.monitor.poll().active)

    def test_stale_manifest_ignored(self):
        self.write(STATE_DOWNLOADING | STATE_UPDATE_STARTED | 6, mtime=time.time() - 3600)
        self.assertFalse(self.monitor.poll().active)

    def test_stale_but_downloading_dir_counts(self):
        self.write(STATE_DOWNLOADING | STATE_UPDATE_STARTED | 6, mtime=time.time() - 3600)
        (self.steamapps / "downloading" / "440").mkdir(parents=True)
        self.assertTrue(self.monitor.poll().active)

    def test_paused_hidden_unless_configured(self):
        self.write(STATE_UPDATE_PAUSED | STATE_UPDATE_STARTED | 6)
        self.assertFalse(self.monitor.poll().active)
        shown = SteamMonitor(extra_roots=[str(self.root)], show_paused=True, require_steam_process=False)
        state = shown.poll()
        self.assertTrue(state.active)
        self.assertTrue(state.paused)

    def test_requires_steam_process(self):
        strict = SteamMonitor(extra_roots=[str(self.root)], require_steam_process=True, proc_root="/nonexistent")
        self.write(STATE_DOWNLOADING | STATE_UPDATE_STARTED | 6)
        state = strict.poll()
        self.assertFalse(state.active)
        self.assertFalse(state.steam_running)

    def test_fresh_install_at_1026_is_detected(self):
        """A new game install: StateFlags 1026, zero bytes, downloading/ present."""
        path = self.write(STATE_UPDATE_REQUIRED | STATE_UPDATE_STARTED)
        text = path.read_text().replace('"BytesDownloaded"\t\t"250"', '"BytesDownloaded"\t\t"0"')
        text = text.replace('"BytesStaged"\t\t"1000"', '"BytesStaged"\t\t"0"')
        path.write_text(text)
        (self.steamapps / "downloading" / "440").mkdir(parents=True)
        state = self.monitor.poll()
        self.assertTrue(state.active)
        self.assertEqual(state.phase, "starting")
        # no bytes reported yet -> indeterminate, not a bar pinned at 0%
        self.assertIsNone(state.fraction)

    def test_started_without_downloading_dir_is_ignored(self):
        """1026 on its own is also what a queued download looks like."""
        self.write(STATE_UPDATE_REQUIRED | STATE_UPDATE_STARTED)
        self.assertFalse(self.monitor.poll().active)

    def test_live_download_survives_steam_not_rewriting_the_manifest(self):
        """Steam only rewrites manifests every few minutes; the bar must not drop out."""
        monitor = SteamMonitor(extra_roots=[str(self.root)], stale_seconds=600, require_steam_process=False)
        self.write(STATE_UPDATE_REQUIRED | STATE_UPDATE_STARTED, mtime=time.time() - 290)
        (self.steamapps / "downloading" / "440").mkdir(parents=True)
        state = monitor.poll()
        self.assertTrue(state.active)          # 290s old but still downloading

    def test_started_but_stale_is_ignored(self):
        self.write(STATE_UPDATE_REQUIRED | STATE_UPDATE_STARTED, mtime=time.time() - 3600)
        (self.steamapps / "downloading" / "440").mkdir(parents=True)
        self.assertFalse(self.monitor.poll().active)

    def test_started_with_bytes_gives_a_real_fraction(self):
        """Once Steam reports bytes, the same state becomes a real progress bar."""
        self.write(STATE_UPDATE_REQUIRED | STATE_UPDATE_STARTED)
        (self.steamapps / "downloading" / "440").mkdir(parents=True)
        state = self.monitor.poll()
        self.assertTrue(state.active)
        self.assertAlmostEqual(state.fraction, 0.25)   # 250/1000 from the fixture

    def test_working_phase_beats_a_merely_started_one(self):
        self.write(STATE_UPDATE_REQUIRED | STATE_UPDATE_STARTED, appid=10)
        (self.steamapps / "downloading" / "10").mkdir(parents=True)
        self.write(STATE_DOWNLOADING | STATE_UPDATE_STARTED | 6, appid=440)
        self.assertEqual(self.monitor.poll().app.appid, 440)

    def test_prefers_moving_download_over_queued(self):
        self.write(STATE_UPDATE_REQUIRED | STATE_UPDATE_STARTED | 256, appid=10)   # "update running" but no bytes
        self.write(STATE_DOWNLOADING | STATE_UPDATE_STARTED | 6, appid=440)
        state = self.monitor.poll()
        self.assertEqual(state.app.appid, 440)

    def test_cache_refreshes_on_change(self):
        path = self.write(STATE_DOWNLOADING | STATE_UPDATE_STARTED | 6)
        self.monitor.poll()
        text = path.read_text().replace('"BytesDownloaded"\t\t"250"', '"BytesDownloaded"\t\t"750"')
        path.write_text(text)
        future = time.time() + 5
        os.utime(path, (future, future))
        self.assertAlmostEqual(self.monitor.poll().fraction, 0.75)


if __name__ == "__main__":
    unittest.main()
