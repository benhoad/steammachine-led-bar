# PCPowerLED — WLED usermod

De-flashes the PC's power indicator. The motherboard blinks its power-LED (PLED)
header while the PC is asleep; this reads that header on a GPIO and drives its
own indicator LED on another GPIO, so "asleep" can be a **steady** light (or off,
or your own blink) instead of the motherboard's flashing.

It runs alongside WLED's normal LED output, so one ESP32-C3 can drive the ledbar
strip from OpenRGB **and** run this indicator. That separation matters: the PC
(and ledbar) are off during sleep, so only the ESP is awake to act.

## Wiring (ESP32-C3 Super Mini, 3.3 V PLED)

```
  PLED+ ──[1 kΩ]──┬── GPIO5 (sense)     100 kΩ from GPIO5 to GND so that
                  └──[100 kΩ]── GND     "off" reads a clean low
  PLED− ──────────── GND
  GPIO6 ──[330 Ω]──►|── GND             indicator LED
```

Everything shares one ground (the ESP is USB-powered from the PC, so its GND is
already the motherboard's). **ESP32-C3 pins are not 5 V tolerant** — measure
`PLED+` to `PLED−` first. If it is 5 V, use a divider (10 kΩ / 15 kΩ) or an
optocoupler; with an optocoupler set `senseInverted`.

Avoid GPIO2, GPIO8 and GPIO9 (strapping pins). GPIO4 is normally the strip data.

## Build

Written for **WLED 0.15+ / 16.x**, which registers usermods with
`REGISTER_USERMOD` and enables them per build environment — there is no
`usermods_list.cpp` any more.

1. **PlatformIO**, in a venv so it works on an immutable OS like Bazzite:
   ```bash
   python3 -m venv ~/.platformio-venv && ~/.platformio-venv/bin/pip install platformio
   ```
   Then use `~/.platformio-venv/bin/pio` below (or add it to your PATH).

2. **Clone WLED at the version you are running** (check Info in the WLED UI, and
   match it so nothing else changes):
   ```bash
   git clone --branch v16.0.1 --depth 1 https://github.com/wled/WLED.git && cd WLED
   ```

3. **Drop this folder in** — the directory name is what you enable in step 4:
   ```bash
   cp -r <this-repo>/firmware/pc_power_led usermods/
   ```

4. **Enable it** in `platformio.ini`, in the environment you build. For the
   ESP32-C3 that is `[env:esp32c3dev]`; add it alongside what's already there:
   ```ini
   custom_usermods = audioreactive pc_power_led
   ```
   (Keep `audioreactive` if you want to retain the stock build's feature set.)

5. **Build and flash** over the USB cable:
   ```bash
   ~/.platformio-venv/bin/pio run -e esp32c3dev -t upload --upload-port /dev/ttyACM0
   ```

Your settings live in the filesystem partition, so an upgrade flash normally
keeps the LED configuration you already set. Optionally override the default
pins at build time with `-D PCPLED_SENSE_PIN=5 -D PCPLED_LED_PIN=6`.

Note that the stock `esp32c3dev` environment already sets
`-DARDUINO_USB_CDC_ON_BOOT=1`, which is what makes WLED's serial (and therefore
OpenRGB's Adalight input) work over the board's native USB.

## Settings

WLED → Config → **Usermods** → `PCPowerLED`:

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | on | master switch |
| `sensePin` | 5 | GPIO reading `PLED+` |
| `ledPin` | 6 | GPIO driving the indicator LED |
| `senseInverted` | off | turn on when sensing through an optocoupler |
| `awakeMode` | 1 (solid) | indicator while the PC is on |
| `sleepMode` | 1 (solid) | **indicator while asleep — the reason this exists** |
| `offMode` | 0 (off) | indicator while the PC is off |
| `blinkPeriodMs` | 1000 | period used by mode 2 |

Modes: `0` = off, `1` = solid, `2` = blink.

The detected state (`awake` / `asleep` / `off`) is shown in WLED's **Info**
panel as "PC power", which is the quickest way to check your wiring.

## How the detection works

A blinking PLED keeps producing edges; a steady one produces none. The usermod
samples the pin every 10 ms and treats **two or more edges within 3 s** as
"asleep"; otherwise the current level decides between "awake" (high) and "off"
(low). Requiring two edges stops the single transition at wake-up being mistaken
for a blink. Expect state changes to settle within about 3 seconds.

## Status

Written against the WLED v16 usermod API and syntax-checked clean
(`-Wall -Wextra`) against a stub, but **not yet compiled against real WLED
headers or run on hardware** — treat the first build as the real test. If your
board's PLED does not blink in sleep (some don't), this has nothing to de-flash;
the Info panel will show what it detects.

The indicator only does anything during sleep if the ESP stays powered — USB
standby on, ErP/deep-sleep off in the BIOS.
