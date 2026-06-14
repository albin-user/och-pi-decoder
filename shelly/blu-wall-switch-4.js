// ─────────────────────────────────────────────────────────────────────────
// Shelly BLU Wall Switch 4  ->  pi-decoder TV control
//
// Runs as a script on a Gen3 Shelly device that acts as the BLE gateway
// (e.g. the Plug M Gen3). The BLU Wall Switch 4 must already be added to this
// device as a BTHome *component* with its encryption key pasted in from the
// Shelly Debug app. The firmware then decrypts the advertisements for us, so
// this script never touches the key or raw BLE data — it just reacts to the
// already-decoded button events and POSTs to the Pi's existing REST API.
//
// Button mapping (as requested):
//   Button 1 -> toggle TV power      POST /api/cec/toggle
//   Button 2 -> volume up            POST /api/cec/volume-up
//   Button 3 -> switch to PC HDMI    POST /api/cec/input  {port: PC_HDMI_PORT}
//   Button 4 -> volume down          POST /api/cec/volume-down
//
// SETUP — three things to fill in below:
//   1. PI_HOST       the Pi's address on the LAN.
//   2. PC_HDMI_PORT  which TV HDMI input the PC is on (1-4).
//   3. ACTION_BY_IDX which button index does what (verify with the log).
//
// How this device reports buttons: the BLU Wall Switch 4 shows up as ONE
// component (e.g. bthomedevice:200), and each press carries an `idx` field
// (0-3) identifying the physical button — there is NOT a separate component
// per button. So we key actions on `idx`, not on the component id.
//
// Finding which idx is which physical button (step 3): install + start this
// script, open the log (Debug -> Console, or the embedded web UI log), and
// press each button once. Every press prints:
//     BLU event: idx 2  single_push
// Note which idx maps to the physical button you want for each action and set
// ACTION_BY_IDX accordingly.
// ─────────────────────────────────────────────────────────────────────────

let CONFIG = {
  // 1. Pi-decoder base URL — no trailing slash. Port 80 is the pi-decoder
  //    default, so usually just the IP. Use the IP (not hostname) to avoid
  //    DNS issues from the plug.
  PI_HOST: "http://192.168.1.50",

  // 2. TV HDMI input the PC is plugged into (1-4). Switches to it.
  PC_HDMI_PORT: 2,

  // 3. Button index (idx 0-3 from the event) -> action. Defaults assume
  //    idx 0=button1 .. idx 3=button4; confirm with the log and reorder if
  //    your physical layout differs. Valid actions: "toggle", "volume_up",
  //    "volume_down", "source_pc".
  ACTION_BY_IDX: {
    0: "toggle",       // toggle TV power
    1: "volume_up",    // volume up
    2: "source_pc",    // switch to PC HDMI
    3: "volume_down",  // volume down
  },

  // Component id of the BLU switch on this plug. If you only have one BLU
  // device it's almost always bthomedevice:200. Set null to accept any
  // bthomedevice (fine unless you pair more than one BLU device here).
  BLU_COMPONENT: "bthomedevice:200",

  // Which press type triggers an action. The BLU also emits double_push,
  // triple_push and long_push — keep to single_push for plain taps.
  TRIGGER_EVENT: "single_push",

  // How many CEC volume steps per press (the API clamps to 1..20).
  VOLUME_STEPS: 2,

  // HTTP timeout (seconds) for calls to the Pi.
  HTTP_TIMEOUT: 5,
};

function postPi(path, bodyObj) {
  Shelly.call(
    "HTTP.POST",
    {
      url: CONFIG.PI_HOST + path,
      body: bodyObj ? JSON.stringify(bodyObj) : "{}",
      content_type: "application/json",
      timeout: CONFIG.HTTP_TIMEOUT,
    },
    function (res, err_code, err_msg) {
      if (err_code !== 0) {
        print("POST", path, "FAILED:", err_code, err_msg);
      } else {
        print("POST", path, "->", res.code);
      }
    }
  );
}

function doAction(action) {
  if (action === "toggle") {
    postPi("/api/cec/toggle", null);
  } else if (action === "volume_up") {
    postPi("/api/cec/volume-up", { steps: CONFIG.VOLUME_STEPS });
  } else if (action === "volume_down") {
    postPi("/api/cec/volume-down", { steps: CONFIG.VOLUME_STEPS });
  } else if (action === "source_pc") {
    postPi("/api/cec/input", { port: CONFIG.PC_HDMI_PORT });
  } else {
    print("Unknown action:", action);
  }
}

// The firmware delivers decoded BLU button presses as component events.
// Event shape (from the device log):
//   { component: "bthomedevice:200", id: 200,
//     info: { component: "bthomedevice:200", id: 200,
//             event: "single_push", idx: 2, channel: -1 } }
// All four buttons share one component; `idx` (0-3) picks the button.
Shelly.addEventHandler(function (e) {
  if (!e || !e.info) return;

  let comp = e.component || e.info.component;
  if (!comp || comp.indexOf("bthomedevice:") !== 0) return;
  if (CONFIG.BLU_COMPONENT !== null && comp !== CONFIG.BLU_COMPONENT) return;

  let evt = e.info.event;
  if (!evt) return; // status updates with no `event` — ignore

  let idx = e.info.idx;
  if (idx === undefined || idx === null) return;

  print("BLU event: idx", idx, evt); // keep for discovery / debugging

  if (evt !== CONFIG.TRIGGER_EVENT) return;

  let action = CONFIG.ACTION_BY_IDX[idx];
  if (action) {
    doAction(action);
  } else {
    print("No action mapped for idx", idx, "- add it to ACTION_BY_IDX");
  }
});

print("BLU Wall Switch 4 -> pi-decoder ready. Target:", CONFIG.PI_HOST);
