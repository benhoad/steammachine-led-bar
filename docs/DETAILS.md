# ledbar details

Background information that is not needed to install or use ledbar.

## What it shows

The defaults follow Valve's [Steam Machine LED reference](https://help.steampowered.com/en/faqs/view/6945-03B8-1EC9-0237).

| Situation | Steam Machine | ledbar default | Configurable |
|---|---|---|---|
| Booting, Steam not up yet | blue, breathing | blue breathing until the Steam client process appears (max 90 s) | `[boot]` |
| Steam running, nothing happening | blue, solid | blue, solid | `[idle]` mode: solid / off / breathe / CPU temperature gauge |
| Downloading, installing or updating a game | blue, filling left → right | blue fill showing Steam's download or install progress, eased between updates | `[progress]` |
| Download finished | – | short bright pulse, then back to idle | `[progress] complete_flash` |
| Game running | (unchanged) | unchanged | `[game]` mode: same / off / dim / colour |
| Overheating (CPU or GPU > 100 °C) | red, solid | red, solid | `[thermal] critical_c`, optional amber warning level |
| Memory training failed / SSD missing / GPU failure / no RAM | red, breathing left half / 2nd quarter / right half / 4th quarter | same patterns, driven by optional software checks (`[faults]`), also shown by `ledbar demo` | `[faults]` |
| Sleep | bar off | bar off (or dim / hold) | `[power] sleep` |
| Shutdown / service stopped | off | fade out, then off (or hold, or hand the strip to a hardware mode such as *Breathing*) | `[power] on_exit` |

The firmware-level faults (no RAM, no SSD, memory training) cannot be detected
by software running on the OS that would not be running. `ledbar` renders the
same patterns and lets you drive them from software checks: a nearly full Steam
library disk shows the "SSD" pattern, failed systemd units the "memory
training" pattern. Both are off by default.

## How it works

* **Steam progress** comes from the `appmanifest_<appid>.acf` files Steam
  rewrites while it works: the `StateFlags` bit mask says whether an app is
  downloading, staging (installing) or committing, and `BytesDownloaded /
  BytesToDownload` (or the `BytesStaged` pair) gives the fraction. Manifests
  that Steam has not touched for two minutes are ignored unless a
  `downloading/<appid>` folder still exists, and nothing is reported unless a
  Steam client process is running. No Steam client internals are involved, so
  this survives client updates. Steam rewrites the file periodically while it
  works; the fill eases towards each new value so it moves smoothly.
* **Booting** is emulated at login: the bar breathes until the Steam process
  appears. If you want the bar to breathe from power-on, save a hardware
  *Breathing* mode as the controller's default in the OpenRGB GUI (or set
  `on_exit = "mode:Breathing"` so the strip keeps breathing through a reboot).
* **Temperatures** are read from `/sys/class/hwmon` (`k10temp`, `coretemp`,
  `amdgpu` junction/edge, Intel `i915`/`xe`), falling back to `nvidia-smi` for
  NVIDIA cards. Thresholds have hysteresis.
* **Sleep and shutdown** are detected through logind's `PrepareForSleep` /
  `PrepareForShutdown` signals using `gdbus monitor` (or `busctl`); after
  resume the OpenRGB connection is re-established and Direct mode re-applied.
* **Output**: OpenRGB drivers such as ASRock Polychrome USB only honour
  per-LED colours on a whole-device update, so `ledbar` always sends the whole
  device. The zone is resized once to `count + offset` LEDs. Frames are only
  sent when something changed (plus a keep-alive every couple of seconds), and
  a game running detection uses Steam's `reaper SteamLaunch AppId=` command line.

## Development

```bash
python3 -m unittest discover -s tests -t .     # unit tests (no hardware needed)
pip install openrgb-python                      # enables the OpenRGB backend tests (fake SDK server)
```

Layout: `ledbar/steam.py` (manifests, libraries, processes), `sensors.py`
(hwmon, disk, systemd), `power.py` (logind), `state.py` (priorities),
`render.py` (patterns, crossfades, physical mapping), `backends/` (OpenRGB,
terminal, null), `daemon.py` (loop), `cli.py`.

## Basic (whole-strip) mode

Some controllers (notably ASRock Polychrome USB motherboard headers) cannot
stream per-LED through OpenRGB's Direct mode; the strip flashes and reverts to a
saved effect. For those, `[openrgb] whole_strip = true` (or `install.sh --basic`)
switches ledbar to drive the whole bar with the controller's own hardware effects
instead of per-LED frames:

| ledbar state | hardware effect |
|---|---|
| idle, on-and-ready | Static, bar colour |
| booting, downloading/installing | Breathing, bar colour |
| overheating | Static, red |
| fault | Breathing, red (no per-quadrant segment) |
| sleep / off | Off |

There is no progress fill (the bar can't show a percentage), and the fault
patterns lose their quadrant, but it is completely stable because it uses only
the modes the controller supports natively, and it writes only when the state
changes. To keep the true progress bar, use a dedicated controller
(docs/CONTROLLERS.md).

## Where download progress comes from

Two sources, selected by `[steam] source` (default `"auto"`).

**`appmanifest` files (always available).** Steam keeps an
`appmanifest_<appid>.acf` per app and rewrites it as work proceeds. It needs no
setup and no Steam configuration, but it is coarse: measured on a 65 GB install,
Steam left `StateFlags` at **1026** (Update Required + Update Started) for the
entire download without ever setting the `Downloading` bit, kept `BytesDownloaded`
at **0** for minutes while preallocating, then jumped straight to 37.9 GB, and
rewrote the file only every few minutes. ledbar therefore treats a corroborated
1026 (a `downloading/<appid>` folder plus a manifest Steam is still touching) as a
live download, shows a breathing animation rather than a 0% bar while the counters
are zero, and uses a generous 10-minute staleness window so the bar does not drop
out between writes.

**The Steam client (opt-in).** Steam's UI is Chromium and its JS context exposes a
real push API — `SteamClient.Downloads.RegisterForDownloadOverview` and
`RegisterForDownloadItems` — which is what Steam's own download page renders from,
and what a Steam Machine's LED bar uses natively. ledbar reaches it over CEF's
remote debugging port, evaluating JS in `SharedJSContext` to register those
callbacks and read the accumulated state. This gives smooth, continuously updating
progress. It requires the user to enable debugging once
(`touch ~/.steam/steam/.cef-enable-remote-debugging`, then restart Steam) and is
tied to Steam internals Valve can rename, so it is strictly an enhancement: when
it is unreachable, ledbar logs once and falls back to manifests.

The CDP transport is a small built-in WebSocket client (`ledbar/steamcef.py`), so
this adds no third-party dependency.

## OS updates

`[updates] system = true` watches for the OS updating itself. The signal is
simply whether the updater's systemd unit is running — `uupd.service` on Bazzite,
plus `bootc-fetch-apply-updates.service` and `rpm-ostreed-automatic.service`, and
anything extra listed in `[updates] units`.

The presentation depends on whether a status indicator exists.

**With an indicator** (`[leds] offset_mode = "power_led"` and a non-zero `offset`,
on a backend that can actually address it) ledbar copies the Steam Machine: a
firmware update there renders with the *same* filling blue bar as a content
download, and the only difference is the small indicator LED — blue for the update,
white for a download. ledbar keeps the bar identical and switches the indicator to
`[indicator] update_color`, which defaults to the bar colour.

**Without an indicator** that trick is unavailable, and an OS update would be
indistinguishable from a game download. So the bar **blinks** instead. The daemon
decides this at startup and tells the state machine (`StateMachine.has_indicator`),
which also covers whole-strip/basic mode, where the indicator LEDs cannot be
addressed separately even if they are configured.

An OS update outranks a game download in the priority order, but still loses to
overheating and hardware faults.

A real percentage is not currently shown: rpm-ostree publishes transaction
progress over D-Bus, but that surface is shifting as Universal Blue migrates to
bootc, so ledbar reports a fraction only if something hands it one and otherwise
shows the indeterminate breathing animation. Detection is deliberately the
robust part; progress is the optional part.

## Link health

Adalight is one-way: the controller never acknowledges anything, so
`backend.write()` returning True only means OpenRGB accepted the bytes. If the
controller stops consuming them the whole stack still looks healthy while the
strip sits frozen — which is exactly what happens when an ESP32's native USB
endpoint wedges after the host reboots while the board stays powered by USB
standby.

`[health]` closes that blind spot. While ledbar believes it is streaming, it
polls the controller's `/json/info` on a background thread (never the render
loop) and checks that `live` is true. Being unable to reach the controller is
deliberately *not* treated as a fault, so a Wi-Fi blip does not raise a false
alarm; only a reachable controller reporting no realtime source does, and only
after `grace_seconds`.

`action = "warn"` (the default) logs it and reports it in `ledbar status`. It
cannot be shown on the bar, because the bar is the broken thing.

`action = "reset"` also recovers: it reboots the controller over HTTP and then
runs `restart_command` so OpenRGB reopens the re-enumerated serial port, which it
will not do on its own. Recovery goes over the network rather than the serial
link on purpose — in the failure being detected, the controller is not reading
serial at all, so a serial command would vanish into the same void as the pixel
data. Resets are capped at `max_resets_per_hour` so a persistent fault cannot
become a reboot loop.

The controller address comes from `[health] host`, or from mDNS discovery
(`_wled._tcp` via `avahi-browse`) when that is left empty.
