"""Applies the WLED settings ledbar needs, over WLED's JSON config API.

Each of these was arrived at the hard way, and every one has a failure mode
attached to getting it wrong:

* master on at boot - realtime data cannot render while WLED's master switch is
  off, so the strip stays dark no matter what ledbar sends
* force max brightness - otherwise WLED scales ledbar's colours by its own
  master brightness on top of ledbar's, dimming everything twice
* realtime gamma off - ledbar already gamma-corrects; doing it twice crushes
  the low end, which matters a lot at low brightness
* off refresh - keeps clocking frames out, so a strip that loses power (an ARGB
  header is unpowered during sleep) repaints instead of latching the random
  state WS2812Bs wake up in
* LED count and current limit - a mismatched length leaves LEDs unaddressed, and
  the default 850 mA limiter silently dims a 24-LED bar

Deliberately left alone: colour order, reverse and skip (ledbar handles
direction itself), Wi-Fi, and brightness - setting WLED's brightness to 0 reads
as "off" and blocks realtime entirely.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Callable, Optional

log = logging.getLogger("ledbar.wledsetup")


def http_get(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - user-supplied host
        return json.loads(response.read().decode("utf-8", errors="replace"))


def http_post(url: str, payload: dict, timeout: float = 5.0) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except ValueError:
        return {"raw": body}


def _current_max_power(total: int) -> int:
    """Headroom over the worst case, so the limiter never dims the bar."""
    return max(1000, int(total * 55 * 1.15 / 100 + 1) * 100)


def plan(cfg: dict, total: int, pin: Optional[int] = None) -> tuple[dict, list[str]]:
    """Work out the payload and a human-readable list of what would change."""
    hw = cfg.get("hw", {}).get("led", {})
    instances = hw.get("ins") or [{}]
    ins0 = dict(instances[0])
    live = cfg.get("if", {}).get("live", {})
    default = cfg.get("def", {})

    changes: list[str] = []

    def note(label: str, old: Any, new: Any) -> None:
        if old != new:
            changes.append(f"{label}: {old!r} -> {new!r}")

    max_power = _current_max_power(total)
    note("LED count", hw.get("total"), total)
    note("output length", ins0.get("len"), total)
    note("max PSU current (mA)", hw.get("maxpwr"), max_power)
    note("off refresh", ins0.get("ref"), True)
    note("master on at boot", default.get("on"), True)
    note("force max brightness", live.get("maxbri"), True)
    note("realtime gamma disabled", live.get("no-gc"), True)
    if pin is not None:
        note("data GPIO", (ins0.get("pin") or [None])[0], pin)

    ins0["len"] = total
    ins0["ref"] = True
    ins0["maxpwr"] = max_power
    if pin is not None:
        ins0["pin"] = [pin]

    payload = {
        "hw": {"led": {"total": total, "maxpwr": max_power, "ins": [ins0]}},
        "def": {"on": True},
        "if": {"live": {"maxbri": True, "no-gc": True}},
    }
    return payload, changes


def configure(host: str, total: int, pin: Optional[int] = None, dry_run: bool = False,
              get: Optional[Callable[[str, float], dict]] = None,
              post: Optional[Callable[[str, dict, float], dict]] = None,
              timeout: float = 5.0) -> list[str]:
    """Apply ledbar's required settings to the WLED at ``host``.

    Returns the list of changes made (empty when it was already correct).
    """
    get = get or http_get
    post = post or http_post
    cfg = get(f"http://{host}/json/cfg", timeout)
    payload, changes = plan(cfg, total, pin)
    if not changes or dry_run:
        return changes
    result = post(f"http://{host}/json/cfg", payload, timeout)
    if isinstance(result, dict) and result.get("success") is False:
        raise RuntimeError(f"WLED rejected the configuration: {result}")
    return changes


def set_idle_color(host: str, color: tuple[int, int, int],
                   post: Optional[Callable[[str, dict, float], dict]] = None,
                   timeout: float = 5.0) -> None:
    """Set what WLED shows when ledbar is not streaming.

    WLED falls back to its own colour once realtime data stops - going into
    sleep, while ledbar is restarting, or during the gap after a wake.  Its
    factory default is amber (#FFAA00), which is startling on a bar that should
    be dark.  ledbar pushes this rather than relying on WLED to persist it,
    because WLED only stores light state in presets and those cannot be saved
    while realtime is active.

    ``on`` is forced true: realtime cannot render while the master switch is off.
    """
    post = post or http_post
    post(f"http://{host}/json/state",
         {"on": True, "seg": [{"id": 0, "fx": 0, "col": [list(color), [0, 0, 0], [0, 0, 0]]}]},
         timeout)
