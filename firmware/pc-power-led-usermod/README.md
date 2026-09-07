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

1. Clone WLED and check out a release tag (0.14 or newer):
   ```bash
   git clone https://github.com/wled/WLED.git && cd WLED
   ```
2. Copy this folder in:
   ```bash
   cp -r <this-repo>/firmware/pc-power-led-usermod usermods/
   ```
3. Register it in `wled00/usermods_list.cpp` — add the include near the other
   usermod includes:
   ```cpp
   #include "../usermods/pc-power-led-usermod/usermod_pc_power_led.h"
   ```
   and inside `registerUsermods()`:
   ```cpp
   usermods.add(new PcPowerLedUsermod());
   ```
   (On WLED versions that use the `REGISTER_USERMOD` macro you can instead add
   `static PcPowerLedUsermod pc_power_led; REGISTER_USERMOD(pc_power_led);` at
   the bottom of the header and skip this step.)
4. Build and flash for your board — check `platformio.ini` for the ESP32-C3
   environment name:
   ```bash
   pio run -e esp32c3dev -t upload
   ```

Optionally override the default pins at build time with
`-D PCPLED_SENSE_PIN=5 -D PCPLED_LED_PIN=6`.

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

The code is written against the WLED usermod v2 API and syntax-checks clean
(`-Wall -Wextra`), but it has **not been compiled against real WLED headers or
run on hardware** — treat the first flash as the real test. If your board's PLED
does not blink in sleep (some don't), this usermod has nothing to de-flash;
check the Info panel to see what it detects.

Note that the indicator only does anything during sleep if the ESP stays powered
— USB standby on, ErP/deep-sleep off in the BIOS.
