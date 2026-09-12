"""The wake/sleep hooks: what gets sent, and that the right edges fire it."""

import threading
import time
import unittest

from ledbar.config import ConfigError, config_from_dict
from ledbar.homeassistant import HomeAssistant, MissingToken, parse_action
from ledbar.power import PowerMonitor


class Recorder:
    """Stands in for the HTTP POST; can be told to fail the first N calls."""

    def __init__(self, fail_times: int = 0, error: Exception | None = None):
        self.calls: list[tuple[str, dict, dict]] = []
        self.fail_times = fail_times
        self.error = error or OSError("network is unreachable")
        self.lock = threading.Lock()

    def __call__(self, url, payload, headers, timeout):
        with self.lock:
            self.calls.append((url, payload, headers))
            if self.fail_times > 0:
                self.fail_times -= 1
                raise self.error
        return "{}"


def client(**kwargs) -> HomeAssistant:
    kwargs.setdefault("enabled", True)
    kwargs.setdefault("url", "http://ha.local:8123")
    return HomeAssistant(**kwargs)


class ParseTests(unittest.TestCase):
    def test_webhook(self):
        action = parse_action("webhook:steam_machine")
        self.assertEqual(action.webhook, "steam_machine")
        self.assertFalse(action.needs_token)
        self.assertEqual(action.url("http://ha.local:8123/"), "http://ha.local:8123/api/webhook/steam_machine")
        # the event goes in the body so one webhook can serve both directions
        self.assertEqual(action.payload("wake"), {"event": "wake"})

    def test_service_with_entities(self):
        action = parse_action("media_player.turn_on media_player.tv,media_player.soundbar")
        self.assertEqual((action.domain, action.service), ("media_player", "turn_on"))
        self.assertTrue(action.needs_token)
        self.assertEqual(action.url("http://ha.local:8123"), "http://ha.local:8123/api/services/media_player/turn_on")
        self.assertEqual(action.payload("sleep"),
                         {"entity_id": ["media_player.tv", "media_player.soundbar"]})

    def test_single_entity_is_not_a_list(self):
        self.assertEqual(parse_action("script.turn_on script.movie_time").payload("wake"),
                         {"entity_id": "script.movie_time"})

    def test_service_without_entity(self):
        self.assertEqual(parse_action("script.movie_time").payload("wake"), {})

    def test_trailing_json_is_service_data(self):
        action = parse_action('media_player.select_source media_player.soundbar {"source": "HDMI 2"}')
        self.assertEqual(action.payload("wake"),
                         {"source": "HDMI 2", "entity_id": "media_player.soundbar"})

    def test_bad_specs_are_rejected(self):
        for spec in ("", "   ", "turn_on", "webhook:", "media_player.turn.on", ".turn_on",
                     'light.turn_on light.x {not json}', 'light.turn_on light.x [1]',
                     "media_player.turn_on tv"):
            with self.assertRaises(ValueError, msg=spec):
                parse_action(spec)


class SendTests(unittest.TestCase):
    def test_sleep_sends_inline_with_the_token(self):
        post = Recorder()
        ha = client(token="abc123", on_sleep=["media_player.turn_off media_player.tv"], post=post)
        ha.fire("sleep")
        self.assertEqual(len(post.calls), 1)
        url, payload, headers = post.calls[0]
        self.assertEqual(url, "http://ha.local:8123/api/services/media_player/turn_off")
        self.assertEqual(payload, {"entity_id": "media_player.tv"})
        self.assertEqual(headers, {"Authorization": "Bearer abc123"})

    def test_webhooks_need_no_token(self):
        post = Recorder()
        ha = client(on_sleep=["webhook:pc_sleep"], post=post)
        ha.fire("sleep")
        self.assertEqual(post.calls[0][2], {}, "a webhook must not be sent an Authorization header")

    def test_token_file_and_env(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".token", delete=False) as handle:
            handle.write("  from-file\n")
        self.assertEqual(client(token_file=handle.name).token(), "from-file")
        self.assertEqual(client(token="inline", token_file=handle.name).token(), "inline",
                         "an explicit token wins over the file")
        os.environ["LEDBAR_HA_TOKEN"] = "from-env"
        try:
            self.assertEqual(client().token(), "from-env")
        finally:
            del os.environ["LEDBAR_HA_TOKEN"]

    def test_a_service_call_without_a_token_is_reported_not_sent(self):
        post = Recorder()
        ha = client(on_wake=["light.turn_on light.x"], post=post)
        results = ha.send_now("wake")
        self.assertEqual(post.calls, [])
        self.assertIsInstance(results[0][1], MissingToken)

    def test_nothing_is_sent_when_disabled_or_unconfigured(self):
        post = Recorder()
        HomeAssistant(enabled=False, url="http://ha.local", on_sleep=["webhook:x"], post=post).fire("sleep")
        HomeAssistant(enabled=True, url="", on_sleep=["webhook:x"], post=post).fire("sleep")
        client(on_wake=["webhook:x"], post=post).fire("sleep")     # no sleep actions
        self.assertEqual(post.calls, [])

    def test_a_failing_hook_never_raises(self):
        post = Recorder(fail_times=1)
        client(on_sleep=["webhook:x"], post=post, wake_retry_seconds=0.0).fire("sleep")
        self.assertEqual(len(post.calls), 1, "sleep gets one shot: the machine is suspending")

    def test_wake_retries_until_the_network_is_back(self):
        # the machine resumes before the network does, so the first POST fails
        post = Recorder(fail_times=2)
        ha = client(on_wake=["webhook:pc_wake"], post=post, wake_retry_seconds=5.0)
        ha._run("wake", ha.actions["wake"], retry_seconds=5.0)
        self.assertEqual(len(post.calls), 3)

    def test_wake_gives_up_at_the_deadline(self):
        post = Recorder(fail_times=99)
        ha = client(on_wake=["webhook:pc_wake"], post=post)
        started = time.monotonic()
        ha._run("wake", ha.actions["wake"], retry_seconds=0.0)
        self.assertEqual(len(post.calls), 1)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_fire_wake_does_not_block_the_caller(self):
        started = time.monotonic()
        post = Recorder(fail_times=99)
        ha = client(on_wake=["webhook:x"], post=post, wake_retry_seconds=30.0)
        ha.fire("wake")
        self.assertLess(time.monotonic() - started, 0.5, "wake must not run on the caller's thread")
        ha.stop()


class WiringTests(unittest.TestCase):
    """logind signals must reach the hooks, on the right edges."""

    def test_events_fire_on_the_right_transitions(self):
        events: list[str] = []
        monitor = PowerMonitor(enabled=False, on_event=events.append)
        line = "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (%s,)"
        monitor._handle_gdbus_line(line % "false")          # a stray false is not a wake
        self.assertEqual(events, [])
        monitor._handle_gdbus_line(line % "true")
        monitor._handle_gdbus_line(line % "false")
        monitor._handle_gdbus_line(
            "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForShutdown (true,)")
        self.assertEqual(events, ["sleep", "wake", "shutdown"])

    def test_a_broken_hook_does_not_stop_the_monitor(self):
        def boom(event):
            raise RuntimeError("home assistant is on fire")

        monitor = PowerMonitor(enabled=False, on_event=boom)
        monitor._handle_gdbus_line(
            "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)")
        self.assertTrue(monitor.sleeping, "the bar still has to know it is sleeping")

    def test_the_delay_lock_is_released_for_the_suspend_and_retaken_on_wake(self):
        monitor = PowerMonitor(enabled=False, inhibit=True)
        taken = []
        monitor._take_inhibitor = lambda: taken.append("take")
        monitor._release_inhibitor = lambda: taken.append("release")
        line = "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (%s,)"
        monitor._handle_gdbus_line(line % "true")
        monitor._handle_gdbus_line(line % "false")
        self.assertEqual(taken, ["release", "take"])

    def test_only_a_sleep_hook_needs_the_delay_lock(self):
        self.assertFalse(client(on_wake=["webhook:x"]).needs_delay_lock)
        self.assertTrue(client(on_sleep=["webhook:x"]).needs_delay_lock)
        self.assertFalse(client(on_sleep=["webhook:x"], inhibit_sleep=False).needs_delay_lock)
        self.assertFalse(HomeAssistant(enabled=False, url="http://x", on_sleep=["webhook:x"]).needs_delay_lock)

    def test_the_daemon_builds_a_client_from_the_config(self):
        from ledbar.config import Config
        from ledbar.daemon import LiveInputs

        config = Config()
        config.homeassistant.enabled = True
        config.homeassistant.url = "http://ha.local:8123"
        config.homeassistant.on_wake = ["webhook:pc_wake"]
        inputs = LiveInputs(config)
        self.assertTrue(inputs.homeassistant.active)
        self.assertEqual(inputs.power.on_event, inputs.homeassistant.fire)


class ConfigTests(unittest.TestCase):
    def test_actions_are_validated_at_load(self):
        for data in ({"on_wake": ["nonsense"]},
                     {"on_sleep": ["webhook:"]},
                     {"url": "ha.local:8123"},
                     {"timeout": 0}):
            with self.assertRaises(ConfigError, msg=str(data)):
                config_from_dict({"homeassistant": data})

    def test_enabled_without_actions_warns_rather_than_failing(self):
        config = config_from_dict({"homeassistant": {"enabled": True, "url": "http://ha.local"}})
        self.assertTrue(config.warnings)
        self.assertTrue(config.homeassistant.enabled)


if __name__ == "__main__":
    unittest.main()
