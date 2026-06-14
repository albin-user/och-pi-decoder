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
//   1. PI_HOST            the Pi's address on the LAN.
//   2. PC_HDMI_PORT       which TV HDMI input the PC is on (1-4).
//   3. BUTTON_COMPONENTS  the bthomesensor:<id> for each physical button.
//
// Finding the component ids (step 3): install + start this script, open the
// script log (Debug -> Console, or the embedded web UI log), then press each
// button once. Every BLU event is printed like:
//     BLU event: bthomesensor:201  single_push
// Note which id fires for buttons 1..4 and put them in BUTTON_COMPONENTS.
// (The battery is a separate bthomesensor id — ignore that one.)
// ─────────────────────────────────────────────────────────────────────────

let CONFIG = {
  // 1. Pi-decoder base URL — no trailing slash. Port 80 is the pi-decoder
  //    default, so usually just the IP. Use the IP (not hostname) to avoid
  //    DNS issues from the plug.
  PI_HOST: "http://192.168.1.50",

  // 2. TV HDMI input the PC is plugged into (1-4). Button 3 switches to it.
  PC_HDMI_PORT: 2,

  // 3. Physical button -> bthomesensor component id. Fill these in after
  //    discovering them from the log (see header). Values are the numeric id.
  BUTTON_COMPONENTS: {
    button1: 200, // toggle power
    button2: 201, // volume up
    button3: 202, // source -> PC
    button4: 203, // volume down
  },

  // Which press type triggers an action. The BLU also emits double_push,
  // triple_push and long_push — keep to single_push for plain taps.
  TRIGGER_EVENT: "single_push",

  // How many CEC volume steps per press (the API clamps to 1..20).
  VOLUME_STEPS: 2,

  // HTTP timeout (seconds) for calls to the Pi.
  HTTP_TIMEOUT: 5,
};

// Build id -> action lookup once, so the event handler is a simple map read.
let ACTION_BY_ID = {};
ACTION_BY_ID[CONFIG.BUTTON_COMPONENTS.button1] = "toggle";
ACTION_BY_ID[CONFIG.BUTTON_COMPONENTS.button2] = "volume_up";
ACTION_BY_ID[CONFIG.BUTTON_COMPONENTS.button3] = "source_pc";
ACTION_BY_ID[CONFIG.BUTTON_COMPONENTS.button4] = "volume_down";

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
//   { component: "bthomesensor:201", id: 201,
//     info: { component: "bthomesensor:201", id: 201, event: "single_push" } }
Shelly.addEventHandler(function (e) {
  if (!e || !e.info) return;

  let comp = e.component || e.info.component;
  if (!comp || comp.indexOf("bthomesensor:") !== 0) return;

  let evt = e.info.event;
  if (!evt) return; // bthomesensor also emits value updates with no `event`

  let id = e.id !== undefined ? e.id : e.info.id;
  print("BLU event:", comp, evt); // keep for discovery / debugging

  if (evt !== CONFIG.TRIGGER_EVENT) return;

  let action = ACTION_BY_ID[id];
  if (action) {
    doAction(action);
  } else {
    print("No mapping for", comp, "- add id", id, "to BUTTON_COMPONENTS");
  }
});

print("BLU Wall Switch 4 -> pi-decoder ready. Target:", CONFIG.PI_HOST);
