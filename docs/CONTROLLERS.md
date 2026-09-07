# Getting the full progress bar: a dedicated LED controller

ledbar needs **per-LED** control to draw a left-to-right progress fill. It gets
that through OpenRGB's **Direct** mode. Most modern controllers support Direct
mode well, but some motherboard ARGB headers do not:

- **ASRock Polychrome USB** boards (e.g. B650I Lightning WiFi) currently flash
  and revert to a static colour in Direct mode, even in the OpenRGB GUI. This is
  an OpenRGB driver limitation, not a ledbar bug (OpenRGB issues #4440, #3641).

You have two options:

1. **Basic (whole-strip) mode** on the motherboard header. No progress fill, but
   it works today. Run `bash install.sh --basic` (or set `whole_strip = true`
   under `[openrgb]`). See the main README.
2. **Move the strip to a dedicated controller** that OpenRGB streams to reliably,
   and keep the real progress bar. That is what this page is about. ledbar needs
   no changes: it just sees a better device.

## The reliable USB option (no Wi-Fi): a microcontroller running Adalight

OpenRGB can drive a serial LED controller over USB using the **Adalight**
protocol. Such a device shows up in OpenRGB with a real Direct mode and streams
per-LED with none of the ASRock flashing.

**What to get**

- A small microcontroller. In order of reliability with OpenRGB over USB:
  - **Arduino Nano / Pro Micro (ATmega328/32u4)** — the classic Adalight target,
    the most bulletproof.
  - **ESP32 / ESP8266** — also works over USB serial (Adalight/tpm2), and is
    cheap and common, but a few users report the ESP crashing under serial
    streaming. If that happens, use the Wi-Fi/WLED route below instead.
    - **ESP32-C3 Super Mini** is a good tiny pick: its native USB-C enumerates as
      `/dev/ttyACM0` (no separate USB-serial chip), and the same board can fall
      back to WLED over Wi-Fi. Two C3 gotchas: use a **data-safe GPIO like GPIO4
      or GPIO10** (avoid the strapping pins GPIO8/GPIO9), and its **3.3 V data**
      is slightly under the WS2812B spec — fine for a short run to 24 LEDs, but
      add a 3.3→5 V level shifter (e.g. 74AHCT125) if you see flicker.
    - For zero fuss, a classic **Arduino Nano** has 5 V logic (no level-shifter
      question) and a real UART, the most bulletproof Adalight target.
- An **internal USB 2.0 header to USB-A adapter** (9-pin motherboard header →
  USB-A socket), so the controller lives inside the case on your spare
  `USB2.0` header.
- A **5 V supply for the strip** (see the power note).

**Firmware**

- Arduino: flash the **Adalight `LEDstream`** sketch (FastLED's Adalight example),
  set `NUM_LEDS = 24` and the data pin you wired.
- ESP: flash **WLED** (it can do Adalight/tpm2 over serial), or an Adalight sketch.

**Wiring**

Recommended build: ESP32-C3 Super Mini, bar powered from the ARGB header.

```
   ┌──────────────┐   USB    ┌─────────────────────┐   data    ┌────────────────┐
   │ Motherboard  ├──────────┤  ESP32-C3 Super Mini │  GPIO4    │  WS2812B strip │
   │ USB 2.0 hdr  │  (power  │  (WLED / Adalight)   ├──[330Ω]──►│  DIN   (24 LEDs)│
   └──────────────┘  + data) └───────┬─────────────┘           └───┬────────┬───┘
                                     │ GND                          │ GND    │ 5V
   ┌──────────────┐                  │                              │        │
   │ Motherboard  │  GND ────────────┴──────────────────────────────┘        │
   │ 3-pin ARGB   │  5V ──────────────────────────────────────────────────────┘
   │ header       │  DATA  ✗ leave unconnected
   └──────────────┘
```

Connections:

| From | To | Why |
|---|---|---|
| ARGB header **5 V** | strip **5 V (+)** | powers the LEDs |
| ARGB header **GND** | strip **GND** *and* ESP **GND** | **common ground — essential** |
| ARGB header **DATA** | — | leave unconnected (the point of this build) |
| ESP **GPIO4** → **330 Ω** → | strip **DIN** | data; GPIO4 avoids the C3 strapping pins |
| ESP **USB-C** | internal USB 2.0 header (via a 9-pin→USB-A adapter) | powers the ESP + serial to the PC |
| **1000 µF cap** (optional) | across strip 5 V / GND near LED 1 | inrush protection |
| **74AHCT125** (optional) | in the data line | 3.3 → 5 V level shift, only if you see flicker |
| **1N400x diode** ×1–2 (optional) | in series on the strip's 5 V | drops ~0.6–1.2 V so 3.3 V data clears spec, an alternative to the shifter |

ESP32-C3 Super Mini pins used (**verify against your board's silkscreen —
clone layouts vary**):

| Pin | Connect to |
|---|---|
| **GPIO4** | 330 Ω → strip **DIN** (data) |
| **GND** | strip GND **and** ARGB header GND (common ground) |
| **USB-C** | internal USB 2.0 header (powers the ESP + serial to the PC) |
| **5V** | leave unused, unless you add a level shifter, then its VCC |

Avoid **GPIO8** (onboard LED) and **GPIO9** (BOOT button) for the data line —
they're strapping pins. GPIO4 is a safe choice; GPIO10 also works.

- Controller data GPIO → a **~330 Ω resistor** → strip **DIN** (the arrow on the
  strip points away from DIN; data flows one way).
- A **1000 µF capacitor** across the strip's **5 V** and **GND** near the first
  LED is good practice.
- **Power the strip from 5 V, not from the USB header.** 24 WS2812B at full white
  can pull well over 1 A; a USB 2.0 header supplies about 0.5 A. See the two
  power options below.
- The controller itself is powered and talks to the PC through its USB cable into
  the internal header adapter.

**Powering the strip — the tidy way: reuse the motherboard ARGB header**

Since the motherboard's 3-pin ARGB header (5 V, Data, GND) can't drive the strip
per-LED, use it purely as a **5 V power tap** and let the microcontroller supply
the data. This is usually the cleanest option in a case that already has the
header:

- **Strip 5 V ← ARGB header 5 V**
- **Strip GND ← ARGB header GND**
- **Strip DIN ← controller data GPIO** (through the ~330 Ω resistor)
- **Leave the ARGB header's data pin unconnected** — this is the whole point, so
  the motherboard's own controller isn't fighting the microcontroller for the
  data line.
- The controller is powered/grounded by its **own USB** into the internal header.

Why this is safe:

- **Current:** ASRock ARGB headers are typically rated **3 A (5 V / 15 W)** —
  check your board's manual. 24 WS2812B draw at most ~1.44 A (full white, full
  brightness) and far less in normal use, so one tap at one end is plenty; no
  power injection at the far end needed.
- **Common ground is automatic:** the ARGB header's GND and the controller's USB
  GND both return to the same PSU/motherboard ground, so the data signal already
  has the shared reference it needs. Just make sure the strip GND really goes to
  the header GND.
- **One 5 V source only:** the header feeds the strip; USB feeds the controller.
  Do **not** also connect the controller's 5 V pin to the strip.
- **Bonus:** the header's 5 V follows the PC's power state, so the strip goes dark
  when the machine is off — the Steam Machine behaviour you want, and no "glows
  while shut down" issue.

**Powering the strip — the alternative: straight from the PSU**

If you'd rather not use the header (or need more than ~3 A for a longer strip),
take 5 V/GND from the PSU (a SATA or Molex lead, or a small 5 V buck) and join
**all grounds** (PSU, strip, controller) together.

**Tell OpenRGB about it (once)**

1. Open the OpenRGB GUI: `~/Applications/OpenRGB*.AppImage`
2. Settings → **Serial Devices** → add one:
   - Protocol: **Adalight** (or tpm2 if your firmware uses that)
   - Port: `/dev/ttyUSB0` or `/dev/ttyACM0` (see `ls /dev/ttyUSB* /dev/ttyACM*`)
   - Baud: **115200**
   - LEDs: **24**
3. Save. The device is written to `OpenRGB.json`, so the headless
   `ledbar-openrgb` service picks it up automatically from then on.

If OpenRGB cannot open the port, your user needs access to it:

```bash
ls -l /dev/ttyUSB0            # note the group (often 'dialout' or 'uucp')
```

The port is normally `root:dialout` with no access for anyone else, so you need
one of these:

- **Group membership** (works everywhere): `sudo usermod -aG dialout $USER`, then
  **reboot**. A logout/login is usually enough, but the `systemd --user` manager
  running the ledbar services inherits its groups at session start, so a reboot is
  the reliable option.
- **udev rule** (no group change): `bash install.sh --udev` installs rules that tag
  common USB-serial LED controllers (Espressif, CH340, CP210x, FTDI, Arduino) with
  `uaccess`, granting the logged-in user access. Replug the device or run
  `sudo udevadm control --reload-rules && sudo udevadm trigger` afterwards.

**This failure is deceptive:** OpenRGB lists an Adalight device even when it cannot
open the port, because the entry comes from its config file. So `ledbar status` shows
the device and ledbar streams happily, while every write silently fails and the LEDs
never change. If WLED's Info panel shows no realtime source (`live: false`) while
ledbar says it is connected, check `test -w /dev/ttyACM0` first.

**Point ledbar at it**

- Turn basic mode back off: `whole_strip = false` under `[openrgb]` (or reinstall
  without `--basic`).
- Leave `[openrgb] device` empty to auto-pick (ledbar prefers a device with a
  Direct mode), or set it to the Adalight device's name from `ledbar status`.
- `systemctl --user restart ledbar`, then `ledbar identify` and `ledbar demo`.
  You now have the full left-to-right progress bar.

## The easy Wi-Fi option: WLED on an ESP

If USB is awkward or the ESP misbehaves over serial, flash the ESP with **WLED**
and let it join your Wi-Fi. OpenRGB talks to WLED over the network (E1.31 / DDP),
which is very stable and streams per-LED. In OpenRGB, add the WLED device (or
enable E1.31), and ledbar drives it exactly the same way. The only downside is
the dependency on Wi-Fi and your network.

## Which to choose

| | Motherboard header (basic) | Adalight over USB | WLED over Wi-Fi |
|---|---|---|---|
| Progress bar fill | no | **yes** | **yes** |
| Extra hardware | none | ~$5 board + adapter | ~$5 board |
| Network needed | no | no | yes |
| Reliability on ASRock | solid (effects only) | solid | solid |

# Status / power indicator (optional)

Like the real Steam Machine, you can have a small **status indicator** separate
from the progress bar. There are three ways to do it, and they differ in what
they can show while the PC is asleep or off.

## Option 1 — integrated: the first LED(s) of the strip (ledbar)

Reserve the first LED(s) of the chain as the indicator and let ledbar drive
them. In `~/.config/ledbar/config.toml`:

```toml
[leds]
offset = 1              # LEDs before the bar
offset_mode = "power_led"   # drive them as an indicator (default "off" = kept dark)

[indicator]
color = "#ffffff"       # white while running (like the Steam Machine indicator)
fault_color = ""        # "" = red; shown on overheat/fault
brightness = 100
sleep = "solid"         # what to leave showing at sleep: "solid" | "off"
```

ledbar drives those LEDs white normally and red on overheat/fault, while the
rest of the strip is the progress bar. Notes:

- **Per-LED only.** This needs a controller that does per-LED (the ESP/Adalight
  route). It is ignored in basic/whole-strip mode, where a single LED can't be
  addressed separately (use option 2 there).
- **Sleep:** ledbar sets the indicator to the `sleep` state on the way down; an
  Adalight strip **holds that last frame**, so if the strip stays powered in
  standby the indicator stays solid (or off) through sleep, no flashing. At
  shutdown the rail drops and it goes dark.

## Option 2 — discrete: a plain LED on the motherboard power-LED header

Wire an ordinary LED to the motherboard's **power-LED (PLED)** header (the front
panel connector). The board drives it entirely in hardware, so it shows
solid-on / sleep-blink / off with **zero software** and keeps working while the
PC is asleep or off. It is a separate light from the strip, and ledbar is not
involved at all. Simple and robust; the only "downside" is the motherboard's
sleep behaviour is usually a **blink**, which you can't change.

## Option 3 — discrete but de-flashed: an ESP between the header and the LED

If you want a discrete indicator but dislike the sleep blink, interpose the ESP:
it **reads the PLED header on a GPIO input** and **re-drives its own indicator
LED** so that sleep shows solid (or off, or your own pattern) instead of the
board's blink. This is autonomous, it works while the PC sleeps because the ESP
stays powered from USB standby.

This is firmware, not ledbar. On WLED it takes a **small custom usermod**
(WLED supports GPIO inputs via its button/usermod system and non-addressable
outputs via a PWM bus or a usermod-driven pin, but the "read PLED, de-flash the
indicator" logic itself is custom). Sketch of the usermod:

- **Input:** a GPIO reading PLED+ (share ground with the front-panel header;
  measure the voltage first — 3.3 V reads directly on an ESP32-C3, 5 V needs a
  divider or an optocoupler, which also isolates it cleanly).
- **Detect state:** sample over ~1–2 s. Steady high = awake; toggling = asleep;
  low/absent = off. (Look for edges, not one level, because a blink reads high
  half the time.)
- **Output:** drive an indicator LED (a plain LED on another GPIO, or a WLED PWM
  bus) per your rule: solid / off / your own blink while asleep, and pass-through
  while awake.
- Because it runs in WLED's `loop()`, the same ESP32-C3 can drive the ARGB bar
  from the OpenRGB stream **and** run this indicator logic at once. Its
  behaviour is configured on the ESP (WLED), not in ledbar's TOML, since ledbar
  is asleep when it matters. If you'd rather not compile a usermod, a tiny
  separate microcontroller doing only this is fully decoupled.

**Wiring (ESP32-C3 Super Mini)**

GPIO4 is already the strip data, so take two more from the free, non-strapping
pool (**GPIO3, GPIO5, GPIO6, GPIO7, GPIO10**):

| Function | Pin | Direction |
|---|---|---|
| PLED sense | **GPIO5** | input |
| Indicator LED | **GPIO6** | output |

**Ground:** everything shares one ground. The ESP is USB-powered from the PC, so
its GND *is* the motherboard's ground — the same net as `PLED−` and the
indicator LED's cathode. Ground is therefore not the design problem here; the
**PLED+ voltage** is, because the C3's pins are **not 5 V tolerant**.

Indicator LED (output):

```
GPIO6 ──[220–470 Ω]──►|── GND        (330 Ω ≈ 5 mA, well within the pin's limit)
                     LED
```

PLED sense (input) — **measure `PLED+` to `PLED−` with the PC on first**:

| PLED+ | Wiring |
|---|---|
| **~3.3 V** (most boards) | `PLED+ → 1 kΩ → GPIO5`, `PLED− → ESP GND`, plus **100 kΩ GPIO5 → GND** so "off" reads a clean low |
| **5 V** | divider: `PLED+ → 10 kΩ → GPIO5`, `GPIO5 → 15 kΩ → GND` (≈3 V), `PLED− → ESP GND` |
| **unknown / want margin** | optocoupler across the PLED, output transistor pulls GPIO5 with a pull-up to 3V3 (inverted logic in firmware) |

**Ready-made usermod:** the firmware for exactly this lives in
[`firmware/pc_power_led/`](../firmware/pc_power_led/) — the usermod source plus
build and settings instructions (WLED 0.15+/16.x).

You can tap the PLED in parallel with the real front-panel LED or replace it.
Check the board manual for `PLED+` polarity. And remember this only does anything
during sleep if the ESP stays powered — USB standby on, ErP/deep-sleep off.

## What each option shows

| | PC on | PC asleep | PC off |
|---|---|---|---|
| Option 1 (strip LED, ledbar) | white / red per state | solid or off (held frame), if strip powered in standby | off (rail cut) |
| Option 2 (dumb PLED LED) | solid | **blink** (board default) | off |
| Option 3 (ESP de-flashes PLED) | solid | solid / off / your pattern | off |

# Complete build — net list

Everything above in one place, for the recommended build: ESP32-C3 Super Mini,
strip powered from the ARGB header, discrete indicator LED with a **3.3 V**
PLED header.

| Net | From | To | Notes |
|---|---|---|---|
| **+5 V (LEDs)** | ARGB header **5 V** | strip **5 V** | header typically rated ~3 A; 24 WS2812B peak ~1.44 A |
| **Bar data** | ESP **GPIO4** | **330 Ω** → strip **DIN** | 3.3 V data; add a 74AHCT125 or a 1N400x in the strip's 5 V only if you see flicker |
| **ESP power + serial** | USB 2.0 header | ESP **USB-C** | via a 9-pin header → USB-A adapter |
| **PLED sense** | **PLED+** (3.3 V) | **1 kΩ** → ESP **GPIO5** | plus **100 kΩ** from GPIO5 to GND so "off" reads low |
| **Indicator** | ESP **GPIO6** | **330 Ω** → LED anode | LED cathode to GND |
| **Ground** | ARGB GND · PLED− · strip GND · LED cathode · ESP GND | one net | already shared through the USB cable |
| **Unused** | ARGB header **DATA** | — | leave disconnected, so the board's controller can't fight the ESP |

Optional: a **1000 µF** capacitor across the strip's 5 V/GND near the first LED.

Sanity checks before powering on: `PLED+` really is 3.3 V (a 5 V one needs a
divider — C3 pins are not 5 V tolerant), the ARGB header's data pin is not
connected to anything, and the strip's DIN is at the end of the chain the arrows
point away from.
