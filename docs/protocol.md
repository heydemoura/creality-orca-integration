# Creality K-series LAN protocol (CFS subset)

This is the protocol Creality Print's device manager uses to talk to K1 / K2 series printers on
the local network. It was reconstructed from the Creality Print sources (`resources/web/deviceMgr`
and `src/slic3r/GUI/print_manage`) and verified live against a **K1 Max with CFS** on 2026-10-02
(DWIN firmware 2.3.5.34). Everything below was observed on that machine unless marked otherwise.

## Transport

| Endpoint | Purpose |
| --- | --- |
| `http://<ip>/info` | `{"mac": "FCEE28021E54", "model": "K1 Max", "version": "1.0.0"}` - model detection |
| `ws://<ip>:9999/` | JSON command / status channel (the one used here) |
| `http://<ip>/upload/<file>` | G-code upload (already implemented by OrcaSlicer's "Creality Print" host) |

* WebSocket frames must be **TEXT** frames on K1-family firmware (K2 accepts both).
* Right after connecting the printer sends a burst of status objects (temperatures, `model`,
  `hostname`, `deviceState`, `cfsConnect`, ...) and then keeps pushing changed fields.
* The printer periodically sends the text `heart_beat`. Creality Print answers with `ok`; the
  firmware closes idle links that never answer.
* Requests are `{"method": "get" | "set", "params": {...}}`. Answers are plain objects keyed by
  the same names as the request parameters, so several requests can be batched in one message.

## Reading the CFS

Request:

```json
{"method": "get", "params": {"boxsInfo": 1, "boxConfig": 1, "reqMaterials": 1}}
```

Response (abridged, real K1 Max data):

```json
{
 "boxsInfo": {
  "same_material": [["000001", "0ff1e1e", [{"boxId": 1, "materialId": 0}], "PLA"], ...],
  "materialBoxs": [
   {"id": 0, "state": 0, "type": 1,
    "materials": [{"id": 0, "vendor": "", "type": "", "color": "", "name": "", "minTemp": 0, "maxTemp": 0,
                   "selected": 0, "percent": 100, "editStatus": 0, "rfid": "", "state": 0}]},
   {"id": 1, "state": 1, "type": 0, "temp": 29.0, "humidity": 50.0,
    "materials": [
     {"id": 0, "vendor": "Generic", "type": "PLA", "name": "Generic PLA", "rfid": "00001",
      "color": "#0ff1e1e", "minTemp": 190, "maxTemp": 240, "pressure": 0.02, "percent": 100,
      "state": 1, "selected": 0, "editStatus": 1},
     ...
    ]}
  ]
 }
}
```

Field meanings:

| Field | Meaning |
| --- | --- |
| `materialBoxs[].id` | box id. The CFS is `1` on a K1 Max; `0` is the external spool holder |
| `materialBoxs[].type` | `0` CFS, `1` external spool holder, `2` CFS Mini/Lite variants |
| `materialBoxs[].state` | `1` box present/active, `0` inactive |
| `temp`, `humidity` | CFS environment (°C, % RH) |
| `materials[].id` | slot index 0..3 = A..D |
| `materials[].state` | `0` empty, `1` loaded (user editable), `2` loaded with a Creality RFID spool (read only) |
| `materials[].color` | `#0RRGGBB` - 7 hex digits, a literal `0` after `#` |
| `materials[].rfid` | material library id (`base.id` of a `retMaterials` entry, e.g. `00001` = Generic PLA) |
| `materials[].minTemp/maxTemp` | nozzle temperature range stored with the slot |
| `materials[].pressure` | pressure advance stored with the slot |
| `materials[].percent` | remaining filament estimate |
| `materials[].selected` | `1` for the slot currently feeding the extruder |
| `materials[].editStatus` | `1` when the record was set by the user / slicer |

`boxConfig` on the K1 Max: `{"autoRefill": 1, "cAutoFeed": 1, "cSelfTest": 0, "cAutoUpdateFilament": 0}`.

### Material library

`{"method":"get","params":{"reqMaterials":1}}` answers with `retMaterials`, an array of slicer-style
material profiles. The K1 Max ships 40 entries. Each looks like

```json
{"engineVersion": "1.6.0", "printerIntName": "F008", "nozzleDiameter": ["0.4"],
 "kvParam": {"pressure_advance": "0.04", "nozzle_temperature_range_low": "190", "...": "..."},
 "base": {"id": "01001", "brand": "Creality", "name": "Hyper PLA", "meterialType": "PLA",
          "colors": ["#ffffff"], "minTemp": 190, "maxTemp": 240, "density": 1.24, "diameter": "1.75"}}
```

(`meterialType` is spelled that way by the firmware.) The slot's `rfid` is `base.id`.

## Writing a slot

```json
{"method": "set", "params": {"modifyMaterial": {
  "boxId": 1, "id": 0,
  "rfid": "01001", "vendor": "Creality", "type": "PLA", "name": "Hyper PLA",
  "color": "#0ff0000",
  "minTemp": 190.00000001, "maxTemp": 240.00000001, "pressure": 0.04}}}
```

* Creality Print only writes materials picked from the library, so it sends the library's
  `minTemp`/`maxTemp` and `kvParam.pressure_advance`. It adds `1e-8` to the temperatures so they are
  serialised as floats; integers are accepted as well.
* Clearing a slot is the same command with every string empty and every number `0`.
* The firmware answers with `{"modifyMaterial": <code>}` and then pushes a fresh `boxsInfo`.
  Creality Print treats code `0` as success for normal CFS units. The plugin verifies every write by
  re-reading `boxsInfo` and comparing the slot, so it does not depend on the code.
* Creality Print refuses edits unless `deviceState == 0` (idle) and the slot `state != 2` (no RFID
  spool). `feedState` values other than `0`, `3`, `14` mean filament is moving; edits are blocked then too.

## Other commands used by Creality Print (for reference)

| Params | Effect |
| --- | --- |
| `{"feedInOrOut": {"boxId": 1, "materialId": 0, "isFeed": 1}}` | load (1) / unload (0) a slot |
| `{"refreshBox": {"boxId": 1, "materialId": 0}}` | re-read the slot's RFID tag |
| `{"boxConfig": {"autoRefill": 1, "cAutoFeed": 1, "cSelfTest": 0}}` | CFS behaviour switches |
| `{"materialBinStatus": {"addr": -1}}` (get) | drying status per box |
| `{"dryBox": {...}}`, `{"autoDry": {...}}` | drying control |
| `{"colorMatch": {"path": "<gcode>", "list": [{"id": "T1A", "type": "PLA", "color": "#0ff0000", "boxId": 1, "materialId": 0}]}}` then `{"multiColorPrint": {"gcode": "<path>", "enableSelfTest": 0}}` | start a multi-colour print with a slot mapping |
| `{"opGcodeFile": "printprt:<path>", "enableSelfTest": 0}` | start a single-colour print |
| `{"gcodeCmd": "M104 S200"}` | run one G-code line |
| `{"nozzleTempControl": 200}`, `{"bedTempControl": {"num": 0, "val": 60}}` | temperatures |
| `{"pause": 1}`, `{"pause": 0}`, `{"stop": 1}` | job control |

Printer state fields pushed over the same channel: `deviceState` (`0` idle, `1` printing, `2`
paused/finishing, `5` busy), `state`, `printProgress`, `printFileName`, `printJobTime`,
`printLeftTime`, `layer`, `TotalLayer`, `nozzleTemp`, `targetNozzleTemp`, `bedTemp0`,
`targetBedTemp0`, `boxTemp`, `cfsConnect`, `feedState`, `materialStatus`, `model`, `hostname`,
`modelVersion`, `err`.

## How the plugin maps this onto OrcaSlicer

OrcaSlicer's device layer speaks Bambu's MQTT dialect internally. The printer agent in
`plugin/creality_cfs.py` therefore publishes

```json
{"print": {"command": "push_status", "msg": 0, "gcode_state": "RUNNING", "nozzle_temper": 220.8, ...,
           "ams": {"ams": [{"id": "0", "info": "0002", "humidity_raw": "50", "temp": "29.0",
                            "tray": [{"id": "0", "tray_info_idx": "<orca filament_id>", "tray_type": "PLA",
                                      "tray_sub_brands": "Generic PLA", "tray_color": "FF1E1EFF",
                                      "nozzle_temp_min": "190", "nozzle_temp_max": "240", "remain": 100}, ...]}],
                   "ams_exist_bits": "1", "tray_exist_bits": "F"}}}
```

through the agent's local-message callback, which is exactly what the built-in Moonraker / Qidi /
Snapmaker agents feed into `DevFilaSystemParser::ParseV1_0`. CFS boxes are renumbered 0, 1, 2 ... in id
order and the external spool holder is skipped, matching OrcaSlicer's own `CrealityPrintAgent`.

In the other direction, OrcaSlicer's `ams_filament_setting` command (`tray_info_idx` = Orca
filament id, `tray_color` = `RRGGBBAA`, `nozzle_temp_min/max`, `tray_type`) is translated into a
`modifyMaterial` write; `gcode_line`, `set_nozzle_temp`, `set_bed_temp`, `pause`, `resume` and
`stop` are mapped onto their Creality equivalents.
