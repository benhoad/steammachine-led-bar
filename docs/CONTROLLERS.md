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
- An **internal USB 2.0 header to USB-A adapter** (9-pin motherboard header →
  USB-A socket), so the controller lives inside the case on your spare
  `USB2.0` header.
- A **5 V supply for the strip** (see the power note).

**Firmware**

- Arduino: flash the **Adalight `LEDstream`** sketch (FastLED's Adalight example),
  set `NUM_LEDS = 24` and the data pin you wired.
- ESP: flash **WLED** (it can do Adalight/tpm2 over serial), or an Adalight sketch.

**Wiring**

- Controller data GPIO → a **~330 Ω resistor** → strip **DIN** (the arrow on the
  strip points away from DIN; data flows one way).
- A **1000 µF capacitor** across the strip's **5 V** and **GND** near the first
  LED is good practice.
- **Power the strip from 5 V, not from the USB header.** 24 WS2812B at full white
  can pull well over 1 A; a USB 2.0 header supplies about 0.5 A. Take 5 V/GND from
  the PSU (a SATA or Molex lead, or a small 5 V buck) and join **all grounds**
  (PSU, strip, controller) together.
- The controller itself is powered and talks to the PC through its USB cable into
  the internal header adapter.

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

Bazzite already tags USB serial adapters for user access via the OpenRGB udev
rules `bash install.sh --udev` installs; on other distros add yourself to that
group (`sudo usermod -aG dialout $USER`) and re-log in, or add a udev rule.

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
