#include "wled.h"

/*
 * PCPowerLED — a WLED usermod that de-flashes the PC's power indicator.
 *
 * The motherboard blinks its power-LED (PLED) header while the PC is asleep.
 * This usermod reads that header on a GPIO input, works out whether the PC is
 * awake / asleep / off, and drives its own indicator LED on another GPIO
 * according to rules you choose — so "asleep" can be a steady light (or off, or
 * your own blink) instead of the motherboard's flashing.
 *
 * It is completely independent of the ARGB strip: WLED keeps streaming the LED
 * bar as usual, and this runs alongside it in loop(). That matters because the
 * PC (and ledbar) are switched off during sleep — only the ESP is awake to act.
 *
 * Wiring (ESP32-C3 Super Mini, 3.3 V PLED header):
 *
 *   PLED+ ──[1 kΩ]──┬── GPIO5 (sense)      100 kΩ from GPIO5 to GND, so that
 *                   └──[100 kΩ]── GND      "off" reads a clean low
 *   PLED− ───────────── GND
 *   GPIO6 ──[330 Ω]──►|── GND              indicator LED
 *
 * If PLED+ is 5 V use a divider (10 kΩ / 15 kΩ) — ESP32-C3 pins are NOT 5 V
 * tolerant. If you sense through an optocoupler instead, set "senseInverted".
 *
 * Install: see README.md next to this file.
 */

#ifndef PCPLED_SENSE_PIN
  #define PCPLED_SENSE_PIN 5
#endif
#ifndef PCPLED_LED_PIN
  #define PCPLED_LED_PIN 6
#endif

class PcPowerLedUsermod : public Usermod {
  public:
    /* what the indicator does in a given PC state */
    static const uint8_t MODE_OFF   = 0;
    static const uint8_t MODE_SOLID = 1;
    static const uint8_t MODE_BLINK = 2;

    /* detected PC state */
    static const uint8_t STATE_UNKNOWN = 0;
    static const uint8_t STATE_OFF     = 1;
    static const uint8_t STATE_ASLEEP  = 2;
    static const uint8_t STATE_AWAKE   = 3;

  private:
    /*---------------------------------------------------------------------*\
    | Settings (shown on WLED's Usermods settings page, saved to cfg.json)  |
    \*---------------------------------------------------------------------*/
    bool     enabled       = true;
    int8_t   sensePin      = PCPLED_SENSE_PIN;   // reads PLED+
    int8_t   ledPin        = PCPLED_LED_PIN;     // drives the indicator LED
    bool     senseInverted = false;              // true when sensing via an optocoupler
    uint8_t  awakeMode     = MODE_SOLID;
    uint8_t  sleepMode     = MODE_SOLID;         // the point of this usermod: no flashing
    uint8_t  offMode       = MODE_OFF;
    uint16_t blinkPeriodMs = 1000;               // used by MODE_BLINK

    /*---------------------------------------------------------------------*\
    | Detection tuning                                                      |
    |                                                                       |
    | A blinking PLED keeps producing edges; a steady one produces none.    |
    | So: two or more edges within BLINK_TIMEOUT_MS means "asleep", and     |
    | otherwise the current level decides between "awake" and "off".        |
    | Requiring two edges stops the single transition at wake-up from being |
    | mistaken for a blink.                                                 |
    \*---------------------------------------------------------------------*/
    static const uint16_t SAMPLE_INTERVAL_MS = 10;
    static const uint16_t BLINK_TIMEOUT_MS   = 3000;
    static const uint8_t  EDGES_FOR_BLINK    = 2;

    /*---------------------------------------------------------------------*\
    | Runtime                                                               |
    \*---------------------------------------------------------------------*/
    bool     initDone     = false;
    bool     pinsOk       = false;
    bool     lastLevel    = false;
    bool     ledOn        = false;
    uint8_t  recentEdges  = 0;
    uint8_t  pcState      = STATE_UNKNOWN;
    uint32_t lastSampleAt = 0;
    uint32_t lastEdgeAt   = 0;

    static const char _name[];
    static const char _enabled[];

    /*---------------------------------------------------------------------*/

    bool allocatePins() {
      pinsOk = false;
      if (sensePin < 0 || ledPin < 0) return false;
      if (!PinManager::allocatePin(sensePin, false, PinOwner::UM_Unspecified)) {
        DEBUG_PRINTLN(F("PCPowerLED: sense pin unavailable"));
        return false;
      }
      if (!PinManager::allocatePin(ledPin, true, PinOwner::UM_Unspecified)) {
        PinManager::deallocatePin(sensePin, PinOwner::UM_Unspecified);
        DEBUG_PRINTLN(F("PCPowerLED: LED pin unavailable"));
        return false;
      }
      /* an external 100 kΩ pull-down defines "off"; an optocoupler needs a pull-up */
      pinMode(sensePin, senseInverted ? INPUT_PULLUP : INPUT);
      pinMode(ledPin, OUTPUT);
      digitalWrite(ledPin, LOW);
      ledOn        = false;
      lastLevel    = readSense();
      lastEdgeAt   = millis();
      recentEdges  = 0;
      pcState      = STATE_UNKNOWN;
      pinsOk       = true;
      return true;
    }

    void releasePins(int8_t sense, int8_t led) {
      if (sense >= 0) PinManager::deallocatePin(sense, PinOwner::UM_Unspecified);
      if (led   >= 0) PinManager::deallocatePin(led,   PinOwner::UM_Unspecified);
      pinsOk = false;
    }

    bool readSense() {
      bool level = digitalRead(sensePin) == HIGH;
      return senseInverted ? !level : level;
    }

    /* Decide awake / asleep / off from the recent edge history. */
    void classify(uint32_t now, bool level) {
      if (now - lastEdgeAt > BLINK_TIMEOUT_MS) recentEdges = 0;
      if (recentEdges >= EDGES_FOR_BLINK)      pcState = STATE_ASLEEP;
      else if (level)                          pcState = STATE_AWAKE;
      else                                     pcState = STATE_OFF;
    }

    /* Drive the indicator LED according to the rule for the current state. */
    void applyOutput(uint32_t now) {
      uint8_t mode;
      switch (pcState) {
        case STATE_AWAKE:  mode = awakeMode; break;
        case STATE_ASLEEP: mode = sleepMode; break;
        default:           mode = offMode;   break;
      }

      bool wanted;
      switch (mode) {
        case MODE_SOLID: wanted = true; break;
        case MODE_BLINK: {
          uint32_t half = blinkPeriodMs / 2;
          if (half < 50) half = 50;
          wanted = ((now / half) % 2) == 0;
          break;
        }
        default: wanted = false; break;
      }

      if (wanted != ledOn) {
        ledOn = wanted;
        digitalWrite(ledPin, ledOn ? HIGH : LOW);
      }
    }

  public:
    void setup() override {
      if (enabled) allocatePins();
      initDone = true;
    }

    void loop() override {
      if (!enabled || !pinsOk) return;

      uint32_t now = millis();
      if (now - lastSampleAt < SAMPLE_INTERVAL_MS) return;
      lastSampleAt = now;

      bool level = readSense();
      if (level != lastLevel) {
        lastLevel   = level;
        lastEdgeAt  = now;
        if (recentEdges < 255) recentEdges++;
      }

      classify(now, level);
      applyOutput(now);
    }

    const char* stateName() const {
      switch (pcState) {
        case STATE_AWAKE:  return "awake";
        case STATE_ASLEEP: return "asleep";
        case STATE_OFF:    return "off";
        default:           return "unknown";
      }
    }

    void addToJsonInfo(JsonObject& root) override {
      JsonObject user = root["u"];
      if (user.isNull()) user = root.createNestedObject("u");
      JsonArray arr = user.createNestedArray(F("PC power"));
      if (!enabled)      arr.add(F("disabled"));
      else if (!pinsOk)  arr.add(F("pin error"));
      else               arr.add(stateName());
    }

    void addToConfig(JsonObject& root) override {
      JsonObject top      = root.createNestedObject(FPSTR(_name));
      top[FPSTR(_enabled)] = enabled;
      top["sensePin"]      = sensePin;
      top["ledPin"]        = ledPin;
      top["senseInverted"] = senseInverted;
      top["awakeMode"]     = awakeMode;
      top["sleepMode"]     = sleepMode;
      top["offMode"]       = offMode;
      top["blinkPeriodMs"] = blinkPeriodMs;
    }

    bool readFromConfig(JsonObject& root) override {
      JsonObject top = root[FPSTR(_name)];
      if (top.isNull()) return false;

      int8_t previousSense = sensePin;
      int8_t previousLed   = ledPin;
      bool   complete      = true;

      complete &= getJsonValue(top[FPSTR(_enabled)], enabled,       true);
      complete &= getJsonValue(top["sensePin"],      sensePin,      (int8_t)PCPLED_SENSE_PIN);
      complete &= getJsonValue(top["ledPin"],        ledPin,        (int8_t)PCPLED_LED_PIN);
      complete &= getJsonValue(top["senseInverted"], senseInverted, false);
      complete &= getJsonValue(top["awakeMode"],     awakeMode,     (uint8_t)MODE_SOLID);
      complete &= getJsonValue(top["sleepMode"],     sleepMode,     (uint8_t)MODE_SOLID);
      complete &= getJsonValue(top["offMode"],       offMode,       (uint8_t)MODE_OFF);
      complete &= getJsonValue(top["blinkPeriodMs"], blinkPeriodMs, (uint16_t)1000);

      /* settings can be edited at runtime, so re-take the pins if they moved */
      if (initDone && (sensePin != previousSense || ledPin != previousLed)) {
        releasePins(previousSense, previousLed);
        if (enabled) allocatePins();
      }
      return complete;
    }

    uint16_t getId() override {
      return USERMOD_ID_UNSPECIFIED;
    }
};

const char PcPowerLedUsermod::_name[]    PROGMEM = "PCPowerLED";
const char PcPowerLedUsermod::_enabled[] PROGMEM = "enabled";

/*---------------------------------------------------------*\
| WLED 0.15+/16.x registers usermods with this macro; there  |
| is no usermods_list.cpp any more.                          |
\*---------------------------------------------------------*/
static PcPowerLedUsermod pc_power_led_usermod;
REGISTER_USERMOD(pc_power_led_usermod);
