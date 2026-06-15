# Shelly → pi-decoder integration

Scripts that run **on a Gen3 Shelly device** (not on the Pi) and drive the TV
through pi-decoder's existing HTTP REST API over the LAN.

## `blu-wall-switch-4.js` — BLU Wall Switch 4 as a TV remote

A battery BLU Wall Switch 4 (Bluetooth) talks to a mains-powered Gen3 Shelly
that has BLE — here the **Plug M Gen3** — which acts as the BLE gateway and runs
the script. Each button press is turned into a direct HTTP call to the Pi.

Current mapping (button `idx` → action, as set in `ACTION_BY_IDX`):

| `idx` | Action            | Pi endpoint                    |
|-------|-------------------|--------------------------------|
| 0     | Toggle TV on/off  | `POST /api/cec/toggle`         |
| 1     | Switch to PC HDMI | `POST /api/cec/input` `{port}` |
| 2     | Volume up         | `POST /api/cec/volume-up`      |
| 3     | Volume down       | `POST /api/cec/volume-down`    |

No MQTT broker and no Companion in the path — the plug calls the Pi directly.
pi-decoder has no MQTT support, but it already exposes a full CEC REST API, so
direct HTTP is the fewest moving parts. (`/api/cec/toggle` was the one endpoint
added for this — everything else already existed.)

### Why no encryption handling in the script

The BLU Wall Switch 4 advertises **encrypted** BTHome data. You do **not**
decrypt it in the script. Instead you add the switch to the Plug M as a BTHome
**component** and paste its key (from the Shelly **Debug** app) into the
component's encryption-key box. The firmware then decrypts every advertisement
and re-emits each button press as a normal component **event**. The script just
listens for those events with `Shelly.addEventHandler` — it never sees the key
or the raw BLE payload.

All four buttons arrive on a **single** component (e.g. `bthomedevice:200`); the
event's **`idx`** field (0–3) identifies which button was pressed (there is *not*
a separate `bthomesensor` component per button). The script keys actions on
`idx`, e.g.:

```
Event from bthomedevice:200: {"event":"single_push","idx":2, ...}
```

Confirmed against this device's log: each event `idx` lines up with the
`button:N` field that fires in the raw `BTHomeData` line — `idx:0`↔`button:0`,
`idx:1`↔`button:1`, `idx:2`↔`button:2`, `idx:3`↔`button:3`. So `idx` is simply
the button index. (In the raw advert, `button:N=128` is the press-down phase and
`button:N=1` is the single-press the firmware turns into `single_push`.)

### Setup

1. **On the Plug M Gen3**: pair the BLU Wall Switch 4 as a component
   (*Add device → Bluetooth*), enable its button sensors, and paste the
   encryption key from the Debug app. Make sure **Bluetooth gateway** is on.
2. Create a new script (*Scripts → Add script*), paste in
   `blu-wall-switch-4.js`, and edit the `CONFIG` block:
   - `PI_HOST` — the Pi's LAN address, e.g. `http://192.168.1.50` (port 80 is
     the pi-decoder default, so no port needed).
   - `PC_HDMI_PORT` — which TV HDMI input the PC is on (1-4).
   - `ACTION_BY_IDX` — which button index (0–3) does what.
   - `BLU_COMPONENT` — the switch's component id (usually `bthomedevice:200`).
3. **Confirm which idx is which physical button**: start the script, open its
   log/console, and press each button once. Each press prints e.g.
   `BLU event: idx 2 single_push`. Note which idx maps to the physical button
   you want for each action and set `ACTION_BY_IDX` accordingly. Save.
4. Enable **Run on startup** so it survives a reboot.

### Notes

- **Pi dependency**: the toggle button calls `/api/cec/toggle`, which is new
  (`cec.toggle()` in `src/pi_decoder/cec.py` + the route in `web/app.py`). The
  updated pi-decoder must be deployed to the Pi or that button returns 404; the
  other three work against any existing build. `toggle` reads TV power state and
  flips it in one call, treating an unreadable state as "off" (powers on).
- The switch also emits `double_push` / `triple_push` / `long_push`. The script
  only acts on `TRIGGER_EVENT` (default `single_push`); extend `doAction` if you
  want extra functions on the same buttons.
