# Shelly → pi-decoder integration

Scripts that run **on a Gen3 Shelly device** (not on the Pi) and drive the TV
through pi-decoder's existing HTTP REST API over the LAN.

## `blu-wall-switch-4.js` — BLU Wall Switch 4 as a TV remote

A battery BLU Wall Switch 4 (Bluetooth) talks to a mains-powered Gen3 Shelly
that has BLE — here the **Plug M Gen3** — which acts as the BLE gateway and runs
the script. Each button press is turned into a direct HTTP call to the Pi:

| Button | Action            | Pi endpoint            |
|--------|-------------------|------------------------|
| 1      | Toggle TV on/off  | `POST /api/cec/toggle` |
| 2      | Volume up         | `POST /api/cec/volume-up` |
| 3      | Switch to PC HDMI | `POST /api/cec/input` `{port}` |
| 4      | Volume down       | `POST /api/cec/volume-down` |

No MQTT broker and no Companion in the path — the plug calls the Pi directly.

### Why no encryption handling in the script

The BLU Wall Switch 4 advertises **encrypted** BTHome data. You do **not**
decrypt it in the script. Instead you add the switch to the Plug M as a BTHome
**component** and paste its key (from the Shelly **Debug** app) into the
component's encryption-key box. The firmware then decrypts every advertisement
and re-emits each button press as a normal component **event**
(`bthomesensor:<id>` → `single_push` / `double_push` / `long_push`). The script
just listens for those events with `Shelly.addEventHandler` — it never sees the
key or the raw BLE payload.

### Setup

1. **On the Plug M Gen3**: pair the BLU Wall Switch 4 as a component
   (*Add device → Bluetooth*), enable its button sensors, and paste the
   encryption key from the Debug app. Make sure **Bluetooth gateway** is on.
2. Create a new script (*Scripts → Add script*), paste in
   `blu-wall-switch-4.js`, and edit the `CONFIG` block:
   - `PI_HOST` — the Pi's LAN address, e.g. `http://192.168.1.50` (port 80 is
     the pi-decoder default, so no port needed).
   - `PC_HDMI_PORT` — which TV HDMI input the PC is on (1-4).
   - `BUTTON_COMPONENTS` — the `bthomesensor` id for each physical button.
3. **Discover the button ids**: start the script, open its log/console, and
   press each button once. Each press prints e.g. `BLU event: bthomesensor:201
   single_push`. Note which id maps to buttons 1–4 and fill in
   `BUTTON_COMPONENTS` (the battery is a separate id — ignore it). Save.
4. Enable **Run on startup** so it survives a reboot.

### Notes

- Button 1 calls `/api/cec/toggle`, which reads TV power state on the Pi and
  flips it in one call (added to pi-decoder for this). If power state can't be
  read it defaults to powering on.
- The switch also emits `double_push` / `triple_push` / `long_push`. The script
  only acts on `TRIGGER_EVENT` (default `single_push`); extend `doAction` if you
  want extra functions on the same buttons.
