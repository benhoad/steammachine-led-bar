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

For an advanced two-part layout like the Steam Machine (a progress bar plus a
small status light), you can reserve the first LED(s) of the strip as a **power
indicator**: set `offset` and `offset_mode = "power_led"` under `[leds]` (see the
`[indicator]` section in the example config). Details and the alternatives,
including a plain power-LED on the motherboard header, are in
[docs/CONTROLLERS.md](docs/CONTROLLERS.md#status--power-indicator-optional).

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
