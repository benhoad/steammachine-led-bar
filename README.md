# ledbar

Makes the LED strip in your PC behave like the light bar on a Steam Machine:

| What the PC is doing | What the bar shows |
|---|---|
| Booting, Steam not open yet | blue, breathing |
| Steam is open | blue, solid |
| Steam is downloading or installing a game | blue bar filling from left to right |
| CPU or GPU over 100 °C | red, solid |
| Hardware/system fault (optional checks) | red, breathing on part of the bar |
| Asleep or shut down | off |

Works on Bazzite with any LED strip that OpenRGB can control in "Direct" mode.
Built for 24 WS2812B LEDs on an ASRock motherboard's 3-pin ARGB header. If your
board can't do per-LED through OpenRGB, there is a whole-strip "basic" mode and a
guide for adding a small dedicated controller ([docs/CONTROLLERS.md](docs/CONTROLLERS.md)).

## Install on Bazzite

Each step is one command. Run them in a terminal (Konsole / Ptyxis) in Desktop Mode.

**1. Put this folder on the PC.**

```bash
git clone https://github.com/benhoad/steammachine-led-bar.git && cd steammachine-led-bar
```

**2. Run the installer.**

```bash
bash install.sh --openrgb --udev
```

`--openrgb` downloads the current OpenRGB release candidate AppImage into `~/Applications`.
This is needed: Bazzite's own `ujust install-openrgb` ships 1.0rc2, which cannot drive ASRock's
ARGB headers per LED (fixed in 1.0rc3). `--udev` asks for your password once, to let OpenRGB open
the LED controller without root. Everything else goes into your home folder. The LED bar starts
at the end of this step.

**3. Optional.** If you also want OpenRGB in the app menu, run `ujust install-openrgb` too;
ledbar always uses the newest AppImage it finds.

> **If the strip flashes and won't hold a colour** (some ASRock boards can't do
> per-LED through OpenRGB, even in the OpenRGB app), re-run the installer with
> `bash install.sh --basic`. That switches to **whole-strip mode**: solid and
> breathing hardware effects per state, no progress fill. To keep the real
> left-to-right progress bar, add a small dedicated LED controller instead, see
> [docs/CONTROLLERS.md](docs/CONTROLLERS.md).

**4. Check that it found your board.**

```bash
ledbar status
```

Look for a line starting with `selected:` near the bottom. It should name your
motherboard and an "Addressable" zone. If it says the device has no Direct mode,
see *Problems* below.

**5. Find out which way round your strip is.**

```bash
ledbar identify
```

The first LED turns red, the second green, the third blue, then a white dot
runs along the strip. Note what you see.

**6. Edit the config.**

```bash
nano ~/.config/ledbar/config.toml
```

Change these lines at the top and save (Ctrl+O, Enter, Ctrl+X):

```toml
count = 24            # how many LEDs you have
reverse = true        # true if the RED led from step 5 was at the RIGHT end
brightness = 60       # maximum brightness, 0-100
color_order = "RGB"   # "GRB" if red and green were swapped in step 5
```

**7. Restart the bar.**

```bash
systemctl --user restart ledbar
```

**8. Watch every pattern once.**

```bash
ledbar demo
```

Done. The bar now starts automatically every time you log in, including the
automatic login into Game Mode. To have it start at boot even when nobody logs in
(for example a machine that boots to the SDDM login screen), run once:

```bash
sudo loginctl enable-linger $USER
```

(or use `bash install.sh --boot`).

When ledbar stops (reboot, shutdown, logout) it hands the strip to the controller's
own *Breathing* mode in the bar colour, so the bar keeps breathing through a reboot and
from power-on until ledbar takes over, like a booting Steam Machine. If your board keeps
its lighting on while shut down, there is a UEFI option for RGB in the soft-off state;
or set `on_exit = "off"` under `[power]` in the config.

## Other Linux distros

Only step 1 is Bazzite-specific. ledbar itself is plain Python (3.10+) with systemd
user services, and Steam is found whether it is native, Flatpak or Snap.

1. Install OpenRGB. For ASRock's USB controller you need **1.0rc3 or newer** (`bash install.sh --openrgb`
   fetches it as an AppImage on any distro); other controllers work from 0.9:
   Arch / CachyOS / ChimeraOS `sudo pacman -S openrgb`, Fedora / Nobara `sudo dnf install openrgb`,
   openSUSE `sudo zypper install OpenRGB`, Ubuntu 24.04+ / Debian 13+ `sudo apt install openrgb`,
   anything else (including SteamOS) `flatpak install flathub org.openrgb.OpenRGB` or the
   AppImage from [openrgb.org](https://openrgb.org/) placed in `~/Applications`.
2. Continue from step 2 above. `bash install.sh --udev` skips the udev step when your
   OpenRGB package already ships its rules, and finds a packaged, Flatpak or AppImage OpenRGB
   automatically (or set `command` under `[openrgb]` in the config).

Without systemd (Void, Alpine, Devuan, Gentoo/OpenRC) the installer copies the files but
cannot enable services: run `openrgb --server --noautoconnect` and `ledbar run` from your
session's autostart instead.

## What OpenRGB needs

Nothing, in the default setup: the `ledbar-openrgb` service runs OpenRGB headless as an
SDK server, and ledbar selects the device, switches it to Direct mode, sets the zone's
LED count and streams the colours itself. Do not set zone sizes, modes or profiles in
the OpenRGB app for the strip.

If you run the OpenRGB app yourself instead, tick **Start server** (and optionally
**Start at login** / **Start minimized**) in Settings → General Settings, keep the
default host/port `127.0.0.1:6742`, and install ledbar with
`bash install.sh --no-openrgb-service` so only one OpenRGB instance touches the controller.

## Everyday commands

| Command | What it does |
|---|---|
| `ledbar status` | shows what it sees: Steam, temperatures, OpenRGB device |
| `ledbar demo` | cycles through all patterns (`ledbar demo --list` for single states) |
| `ledbar wled-setup` | applies the WLED settings ledbar needs (`--dry-run` to preview) |
| `ledbar ha wake` | fires the Home Assistant wake hook by hand, to test it (`sleep` too) |
| `systemctl --user restart ledbar` | apply config changes |
| `journalctl --user -u ledbar -f` | live log |
| `bash uninstall.sh` | remove it (keeps your config unless `--purge`) |

To update: pull or copy the new folder and run `bash install.sh` again. It stops the
running service, replaces the files, keeps your config, and starts the service again,
so there is never more than one copy running.

## Useful settings

All settings, with explanations, are in `~/.config/ledbar/config.toml`
(a fresh copy is in [ledbar/config.example.toml](ledbar/config.example.toml)). Popular ones:

```toml
[colors]
bar = "#1a9fff"       # any colour instead of Steam blue

[idle]
mode = "solid"        # "off", "breathe", or "temperature" (bar = CPU temperature)

[game]
mode = "same"         # "off" or "dim" while a game is running

[thermal]
warning_c = 90        # amber breathing above this temperature (0 = off)
```

### Smooth progress (optional)

By default ledbar reads progress from Steam's `appmanifest` files. That needs no
setup, but Steam only rewrites them **every few minutes**, so the bar advances in
jumps rather than smoothly.

For live, continuously updating progress, let ledbar read it from the Steam client
itself. Enable Steam's debug interface once:

```bash
touch ~/.steam/steam/.cef-enable-remote-debugging
```

Restart Steam. ledbar picks it up automatically (`[steam] source = "auto"`, the
default) and falls straight back to manifests if it's ever unavailable, so nothing
breaks if you skip this or if a Steam update changes things. `ledbar status` shows
which source is in use.

For an advanced two-part layout like the Steam Machine (a progress bar plus a
small status light), you can reserve the first LED(s) of the strip as a **power
indicator**: set `offset` and `offset_mode = "power_led"` under `[leds]` (see the
`[indicator]` section in the example config). Details and the alternatives,
including a plain power-LED on the motherboard header, are in
[docs/CONTROLLERS.md](docs/CONTROLLERS.md#status--power-indicator-optional).

### OS updates

Bazzite updates itself in the background (`uupd.timer`) and applies on reboot.
ledbar can show that too:

```toml
[updates]
system = true
```

What you see depends on whether you have a status indicator:

- **No indicator (the usual setup):** the bar **blinks** in the bar colour. Without
  a second light to carry the meaning, blinking is what keeps an OS update from
  looking identical to a game download.
- **With an indicator** (`offset` plus `offset_mode = "power_led"`): the bar behaves
  exactly as it does for a Steam download and the **indicator turns blue** instead of
  white. That's how a real Steam Machine distinguishes a firmware update from a
  content download — the bar is identical, only the small light changes.

An OS update takes priority over a game download, since it's the one you don't want
to reboot through. It still loses to overheating and hardware faults. Note there's no
percentage: the Bazzite updater doesn't publish one, so ledbar shows a fill only if
something hands it a fraction.

### Home Assistant: TV and soundbar with the PC

A games console tells the TV to wake over the HDMI cable. A PC cannot — consumer
motherboards have no HDMI-CEC — but ledbar is already watching logind for sleep
and wake, so it can tell Home Assistant instead:

```toml
[homeassistant]
enabled = true
url = "http://homeassistant.local:8123"
token_file = "~/.config/ledbar/ha-token"    # or token = "...", or $LEDBAR_HA_TOKEN
on_wake  = ["media_player.turn_on media_player.tv,media_player.soundbar"]
on_sleep = ["media_player.turn_off media_player.tv,media_player.soundbar"]
```

An action is either a **service call** — `"<domain>.<service> <entity>[,<entity>]"`,
with optional service data as trailing JSON — or a **webhook**, `"webhook:<id>"`,
which needs no token at all and is the quickest way to start:

```toml
on_wake  = ["webhook:steam_machine"]
on_sleep = ["webhook:steam_machine"]
```

Create it in Home Assistant with an automation on a **Webhook** trigger; the body
is `{"event": "wake"}` or `{"event": "sleep"}`, so one webhook and one automation
can handle both directions (`{{ trigger.json.event }}`). The token, for service
calls, comes from your Home Assistant profile → Security → Long-lived access tokens.

Test the hooks without suspending anything:

```bash
ledbar ha wake
```

(`ledbar ha sleep`, and `--dry-run` to print the requests instead of sending them.)

Two details that are handled for you: waking is retried for ~20 s, because the
machine is back before the network is; and going to sleep, ledbar holds a logind
delay lock so the request actually leaves before the machine suspends. There is
also `on_shutdown`, which is best effort — the machine may be gone before it lands.

## Optional: the power-LED indicator (WLED usermod)

If you drive the strip with an ESP running WLED, you can also have it read the
motherboard's power-LED header and drive a separate indicator LED — so it shows
a **steady** light while the PC sleeps instead of the motherboard's blink. That
needs a custom WLED build, because usermods are compiled in.

Written for WLED 0.15+ / 16.x, which enables usermods per build environment
(there is no `usermods_list.cpp` any more).

**1. Install PlatformIO** in a venv, so nothing touches Bazzite's immutable root.

```bash
python3 -m venv ~/.platformio-venv && ~/.platformio-venv/bin/pip install platformio
```

**2. Clone WLED at the version you're running** (check Info in the WLED UI, and
match it so nothing else changes).

```bash
git clone --branch v16.0.1 --depth 1 https://github.com/wled/WLED.git && cd WLED
```

**3. Copy the usermod in.** The folder name is what you enable in step 4.

```bash
cp -r ~/steammachine-led-bar/firmware/pc_power_led usermods/
```

**4. Enable it.** In `platformio.ini`, find `[env:esp32c3dev]` and add it to the
line that's already there:

```
custom_usermods = audioreactive pc_power_led
```

**5. Build and flash** over the USB cable.

```bash
~/.platformio-venv/bin/pio run -e esp32c3dev -t upload --upload-port /dev/ttyACM0
```

Your WLED settings live in the filesystem partition, so they normally survive the
flash. Afterwards the usermod appears under Config → Usermods as `PCPowerLED`,
and WLED's Info panel gains a "PC power" row showing `awake` / `asleep` / `off`,
which is the quickest way to check the sense wiring.

Wiring, settings and troubleshooting:
[firmware/pc_power_led/readme.md](firmware/pc_power_led/readme.md).

## Problems

- **`ledbar` command not found.** Log out and back in, or use `~/.local/share/ledbar/bin/ledbar`.
- **`ledbar-openrgb` service failed (status=1).** Run `~/.local/share/ledbar/bin/ledbar-openrgb-server --check`:
  it says whether it found OpenRGB, whether the AppImage runs (FUSE), and whether port 6742 is already
  taken by another OpenRGB. `journalctl --user -u ledbar-openrgb -n 30` shows the actual error.
- **`OpenRGB reports no devices`.** Run `bash install.sh --udev` if you skipped it (installs `/etc/udev/rules.d/60-ledbar-openrgb.rules`), then reboot once.
- **Static colours work in the OpenRGB app but Direct / `ledbar identify` show nothing.** First make sure
  OpenRGB is 1.0rc3 or newer (`bash install.sh --openrgb`; the January 2026 fix). If it still flashes and
  reverts to a static colour, your board can't do per-LED through OpenRGB (some ASRock Polychrome USB
  boards, e.g. the B650I Lightning WiFi). Two choices: `bash install.sh --basic` for whole-strip mode
  (effects only, no fill), or add a dedicated controller for the full progress bar,
  [docs/CONTROLLERS.md](docs/CONTROLLERS.md).
- **`device ... has no 'Direct' mode`.** Your board's LED controller can't do per-LED animation safely (older ASRock boards with the SMBus controller). Any other OpenRGB-supported ARGB controller can drive the strip instead.
- **`ledbar identify` / `ledbar demo` show nothing.** They pause the running service automatically
  (it would otherwise overwrite the pattern). If the strip stays dark, try the other header:
  `zone = "Addressable Header 2"` under `[openrgb]`, and check the strip's DIN is on the first LED
  and the 5 V/GND pins are right.
- **The strip keeps flipping back to a colour you set in the OpenRGB app.** The controller falls back
  to its stored effect whenever ledbar stops sending frames. Make sure `keepalive_seconds` under
  `[leds]` is `0.25` (older configs had `2.0`), and that no second OpenRGB with hardware access is
  running (`pgrep -af -i openrgb` should list only the `ledbar-openrgb` service).
- **ledbar says it's connected but the LEDs never change (USB/serial controller).** Check
  `test -w /dev/ttyACM0`. OpenRGB lists a serial device even when it can't open the port, so
  everything looks fine while writes fail. Fix with `sudo usermod -aG dialout $USER` then reboot,
  or `bash install.sh --udev` for the udev-rule route.
- **The bar freezes and ledbar still says "connected" (USB/serial controller).** Adalight is one-way,
  so ledbar cannot see that its frames are being dropped. The usual cause is an ESP whose USB endpoint
  wedged because the PC rebooted while the board stayed powered on USB standby. Check with
  `curl -s http://<controller>/json/info | tr ',' '\n' | grep live` — `live:false` while ledbar is
  running means the frames are going nowhere. Reboot the controller (`curl -s http://<controller>/reset`)
  then `systemctl --user restart ledbar-openrgb ledbar`. Set `[health] host` to have ledbar watch for
  this, and `action = "reset"` to have it recover on its own.
- **Wrong colours or backwards bar.** Re-run `ledbar identify` and fix `color_order` / `reverse` / `offset` in the config.
- **Bar stays breathing.** It waits up to 90 s for Steam to start. Set `wait_for_steam = false` under `[boot]` if you don't start Steam at login.
- **Nothing during a download.** `ledbar status` lists the manifests it sees. Libraries on other drives are found automatically; unusual locations go in `[steam] paths`.
- **Using the OpenRGB app while ledbar runs.** Start it with `~/Applications/OpenRGB*.AppImage --nodetect`, or stop the services first: `systemctl --user stop ledbar ledbar-openrgb`.

## Try it without the hardware

```bash
python3 -m ledbar --backend terminal demo
```

draws the strip in the terminal. More background, including how Steam progress
and temperatures are read, is in [docs/DETAILS.md](docs/DETAILS.md).

## License

MIT.
