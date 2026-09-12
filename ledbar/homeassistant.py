"""Tells Home Assistant when the machine sleeps and wakes.

A PC has no HDMI-CEC, so unlike a console it cannot tell the TV and the soundbar
to follow it in and out of standby.  Home Assistant already knows how to reach
them; all it is missing is the moment the machine's power state changes - and
ledbar is listening to logind for exactly that anyway, because it is what makes
the bar go dark during sleep.  So the hook costs one HTTP request.

Two kinds of action, both plain HTTP:

* ``webhook:<id>``            -> POST /api/webhook/<id>              (no token)
* ``<domain>.<service> ...``  -> POST /api/services/<domain>/<service>

Each has its own awkwardness, and both are handled here rather than by the
caller:

* **Waking**: the machine is back long before the network is, so the first
  attempt usually fails with "network unreachable".  Wake actions are therefore
  retried for a while, on their own thread, instead of being dropped.
* **Sleeping**: the request has to get out *before* the suspend.  These run
  inline so that ``power.py`` can hold its logind delay lock until they finish.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("ledbar.homeassistant")

EVENTS = ("wake", "sleep", "shutdown")
TOKEN_ENV = "LEDBAR_HA_TOKEN"


class MissingToken(RuntimeError):
    """A service call was configured but there is no access token to make it with."""


@dataclass(frozen=True)
class Action:
    """One configured thing to do: a webhook, or a service call."""

    spec: str                                    # what the user wrote, for logs
    webhook: str = ""                            # webhook id, when this is a webhook
    domain: str = ""                             # e.g. "media_player"
    service: str = ""                            # e.g. "turn_on"
    data: dict = field(default_factory=dict)     # entity_id and any extra service data

    @property
    def needs_token(self) -> bool:
        return not self.webhook

    def url(self, base: str) -> str:
        base = base.rstrip("/")
        if self.webhook:
            return f"{base}/api/webhook/{self.webhook}"
        return f"{base}/api/services/{self.domain}/{self.service}"

    def payload(self, event: str) -> dict:
        # A webhook trigger carries whatever it is POSTed (trigger.json in the
        # automation), so tell it which event this was; one webhook can then
        # handle both directions.  A service call takes only service data.
        if self.webhook:
            return {"event": event, **self.data}
        return dict(self.data)


def parse_action(spec: str) -> Action:
    """Parse one action string, or raise ``ValueError`` describing what is wrong.

    ``"webhook:steam_machine_wake"``
    ``"media_player.turn_on media_player.tv"``
    ``"media_player.turn_on media_player.tv,media_player.soundbar"``
    ``'media_player.select_source media_player.soundbar {"source": "HDMI 2"}'``
    """
    text = spec.strip()
    if not text:
        raise ValueError("empty action")

    data: dict = {}
    brace = text.find("{")
    if brace != -1:
        try:
            parsed = json.loads(text[brace:])
        except ValueError as exc:
            raise ValueError(f"{spec!r}: the trailing {{...}} is not valid JSON ({exc})") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{spec!r}: the trailing {{...}} must be a JSON object")
        data = parsed
        text = text[:brace].strip()

    if text.lower().startswith("webhook:"):
        webhook = text.split(":", 1)[1].strip()
        if not webhook or " " in webhook:
            raise ValueError(f"{spec!r}: expected 'webhook:<id>' with the id from the automation's trigger")
        return Action(spec=spec.strip(), webhook=webhook, data=data)

    parts = text.split()
    if not parts:
        raise ValueError(f"{spec!r}: missing the service to call")
    domain, _, service = parts[0].partition(".")
    if not domain or not service or "." in service:
        raise ValueError(
            f"{spec!r}: expected 'domain.service' (e.g. 'media_player.turn_on') or 'webhook:<id>'")
    entities = [e for part in parts[1:] for e in part.split(",") if e]
    for entity in entities:
        # every Home Assistant entity id is "<domain>.<name>"; "tv" is a typo, not an entity
        head, _, tail = entity.partition(".")
        if not head or not tail or "." in tail:
            raise ValueError(f"{spec!r}: {entity!r} is not an entity id (expected 'domain.name', "
                             "e.g. 'media_player.tv')")
    if entities:
        data = {**data, "entity_id": entities if len(entities) > 1 else entities[0]}
    return Action(spec=spec.strip(), domain=domain, service=service, data=data)


def parse_actions(specs: list[str]) -> list[Action]:
    return [parse_action(spec) for spec in specs]


def http_post(url: str, payload: dict, headers: dict, timeout: float = 5.0) -> str:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - user-configured host
        return response.read().decode("utf-8", errors="replace")


class HomeAssistant:
    """Fires the configured actions, never on the render thread."""

    def __init__(self, enabled: bool = False, url: str = "", token: str = "", token_file: str = "",
                 on_wake: Optional[list[str]] = None, on_sleep: Optional[list[str]] = None,
                 on_shutdown: Optional[list[str]] = None, timeout: float = 5.0,
                 wake_retry_seconds: float = 20.0, inhibit_sleep: bool = True,
                 post: Optional[Callable[[str, dict, dict, float], str]] = None) -> None:
        self.enabled = enabled
        self.url = url.rstrip("/")
        self._token = token
        self._token_file = token_file
        self.timeout = timeout
        self.wake_retry_seconds = wake_retry_seconds
        self.inhibit_sleep = inhibit_sleep
        self._post = post or http_post
        self.actions = {
            "wake": parse_actions(on_wake or []),
            "sleep": parse_actions(on_sleep or []),
            "shutdown": parse_actions(on_shutdown or []),
        }
        self._stop = threading.Event()
        self._token_cache: Optional[str] = None
        self._token_warned = False

    # -- what we are set up to do -----------------------------------------

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.url) and any(self.actions.values())

    @property
    def needs_delay_lock(self) -> bool:
        """Sleep hooks only get sent if the suspend waits for them."""
        return (self.active and self.inhibit_sleep
                and bool(self.actions["sleep"] or self.actions["shutdown"]))

    def describe(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.url:
            return "no [homeassistant] url set"
        counts = ", ".join(f"{len(self.actions[e])} on {e}" for e in EVENTS if self.actions[e])
        return f"{self.url} ({counts})" if counts else f"{self.url} (no actions configured)"

    def token(self) -> str:
        """The first token that is set: config, then file, then the environment."""
        if self._token_cache is None:
            token = self._token.strip()
            if not token and self._token_file:
                try:
                    token = Path(self._token_file).expanduser().read_text().strip()
                except OSError as exc:
                    log.warning("home assistant: cannot read token_file %s: %s", self._token_file, exc)
            if not token:
                token = os.environ.get(TOKEN_ENV, "").strip()
            self._token_cache = token
        return self._token_cache

    # -- firing ------------------------------------------------------------

    def fire(self, event: str) -> None:
        """Run the actions for ``wake`` / ``sleep`` / ``shutdown``.

        Sleep and shutdown run inline: the caller is holding the logind delay
        lock that keeps the machine up long enough for them, and releases it
        when this returns.  Wake runs on its own thread because it retries.
        """
        actions = self.actions.get(event) or []
        if not self.active or not actions:
            return
        if event == "wake":
            threading.Thread(target=self._run, args=(event, actions, self.wake_retry_seconds),
                             name="ledbar-ha-wake", daemon=True).start()
        else:
            self._run(event, actions, 0.0)

    def _run(self, event: str, actions: list[Action], retry_seconds: float) -> None:
        deadline = time.monotonic() + retry_seconds
        delay = 1.0
        pending = list(actions)
        while True:
            failures: list[tuple[Action, Exception]] = []
            for action in pending:
                try:
                    self._send(event, action)
                except MissingToken as exc:                   # retrying will not conjure one up
                    self._warn_token(exc)
                except Exception as exc:                      # a hook must never take the daemon down
                    failures.append((action, exc))
            if not failures:
                return
            if time.monotonic() >= deadline:
                for action, exc in failures:
                    log.warning("home assistant: %s action %s failed: %s", event, action.spec, exc)
                return
            log.debug("home assistant: %d %s action(s) failed, retrying in %.0fs", len(failures), event, delay)
            pending = [action for action, _ in failures]
            if self._stop.wait(delay):
                return
            delay = min(delay * 2.0, 5.0)

    def send_now(self, event: str) -> list[tuple[Action, Optional[Exception]]]:
        """Run one event's actions inline and report each result (``ledbar ha`` uses this)."""
        results: list[tuple[Action, Optional[Exception]]] = []
        for action in self.actions.get(event) or []:
            try:
                self._send(event, action)
                results.append((action, None))
            except Exception as exc:
                results.append((action, exc))
        return results

    def _send(self, event: str, action: Action) -> None:
        headers: dict[str, str] = {}
        if action.needs_token:
            token = self.token()
            if not token:
                raise MissingToken(
                    f"{action.spec} needs an access token: set [homeassistant] token or token_file, "
                    f"or ${TOKEN_ENV} (webhook: actions need no token)")
            headers["Authorization"] = f"Bearer {token}"
        self._post(action.url(self.url), action.payload(event), headers, self.timeout)
        log.info("home assistant: %s -> %s", event, action.spec)

    def _warn_token(self, exc: Exception) -> None:
        if not self._token_warned:
            log.error("home assistant: %s", exc)
            self._token_warned = True

    def stop(self) -> None:
        self._stop.set()
