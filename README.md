# Creality CFS Sync for OrcaSlicer

Brings Creality Print's printer-side filament features to OrcaSlicer for Creality K-series printers
with a CFS (Creality Filament System):

* **Read** the CFS slots (material, colour, remaining %, temperature range, pressure advance) over the
  LAN and expose them to OrcaSlicer as AMS trays, so *Synchronize filament list from AMS* and the
  per-filament sync badges work.
* **Write** filament records to the slots: push a project filament (or an entry of the printer's own
  material library) into any slot from a dock panel, and answer OrcaSlicer's native AMS slot edit
  command the same way.

It uses the same JSON-over-WebSocket protocol (TCP 9999) that Creality Print's device manager uses,
documented in [`docs/protocol.md`](docs/protocol.md). Developed and verified against a **K1 Max with
the CFS upgrade** (firmware DWIN 2.3.5.34); the protocol is shared with the K1 / K1C / K1 SE and K2
series.

## What is in the repository

| Path | Purpose |
| --- | --- |
| `plugin/creality_cfs.py` | The OrcaSlicer plugin (single file, standard library only) |
| `tools/cfs_cli.py` | Command line tool using the same protocol code, for testing without OrcaSlicer |
| `tests/` | Unit tests plus a fake printer that speaks the protocol; `tests/fixtures/k1max_capture.json` is a live capture |
| `docs/protocol.md` | The reverse-engineered protocol |
| `orca-patch/` | Optional C++ patch for OrcaSlicer itself (K1 Max in the built-in CFS model table + native slot writes) |

## Requirements

* **OrcaSlicer with the Python plugin system**: a nightly build of 2.5.0-dev (the plugin framework is
  not in 2.4.x releases). On Linux, download `OrcaSlicer_Linux_AppImage_Ubuntu2404_nightly.AppImage`
  from the `nightly-builds` release on GitHub; it bundles Python 3.12, no extra installation needed.
* A Creality K-series printer with CFS on the same network, reachable on TCP port 9999 (stock
  firmware; rooted printers keep this service too).

## Installing the plugin

1. OrcaSlicer menu *Plugins* (or the Plugins button) &rarr; *Install* &rarr; choose
   `plugin/creality_cfs.py`. The plugin shows two capabilities: the printer agent **Creality CFS
   (LAN)** and the script **Creality CFS Manager**. Enable both.
2. In *Printer settings* make sure the connection fields point at the printer: *Host type* =
   **Creality Print**, *Hostname, IP or URL* = the printer's IP (a port or a URL from an older setup
   such as `192.168.0.128:4409` is fine, only the host part is used).
3. In *Printer settings &rarr; Advanced &rarr; Printer Agent* pick **Creality CFS (LAN)** and save the
   preset. OrcaSlicer connects through the plugin from now on; the Device tab shows the printer, and
   the sidebar's AMS sync button pulls the CFS slots.
4. The first time the plugin opens a connection OrcaSlicer may ask you to grant the plugin network
   (socket) access; answer *Yes* so the permission is remembered.

### Reading the CFS into the project

Click the *Synchronize filament list from AMS* button in the sidebar (the one that normally talks to
an AMS). Each loaded slot appears with its colour and the best matching filament preset for your
printer; empty slots are left out. Matching follows OrcaSlicer's own rules for Creality printers:
exact product name wins (for example *Hyper PLA*), then vendor, then the plainest preset of the same
material type.

### Writing filaments to the CFS

Run **Creality CFS Manager** from the Plugins dialog. A panel docks next to the 3D view showing every
slot as the printer reports it. For each slot pick a *project filament* (the colours of your project
are used) or a *library material* (the printer's own list, e.g. *Creality Hyper PLA*), adjust the
colour if you like and press **Write**. **Project &rarr; CFS** writes the project's filaments into
slots 1A, 1B, ... in order. **Clear** empties a slot record.

What gets written: the printer's library identity for the material when one matches (so the printer
UI shows a known material), the colour, and by default the slicer preset's nozzle temperature range
and pressure advance (set `temps_from` to `library` in the plugin's config to mirror Creality Print,
which writes the library values instead).

Writes are refused while the printer is printing or moving filament, exactly like Creality Print's
UI; the panel tells you why. The plugin config has `allow_write_while_printing` if you really need
to override that.

## Command line tool

```sh
python3 tools/cfs_cli.py 192.168.0.128 status
python3 tools/cfs_cli.py 192.168.0.128 materials
python3 tools/cfs_cli.py 192.168.0.128 set 1 0 --library "Generic PLA" --color "#ff1e1e"
python3 tools/cfs_cli.py 192.168.0.128 set 1 1 --vendor Creality --type PLA --name "Hyper PLA" --color "#00ff00"
python3 tools/cfs_cli.py 192.168.0.128 clear 1 3
python3 tools/cfs_cli.py 192.168.0.128 watch
```

`--dry-run` prints the exact message without sending it.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The suite covers colour conversions, parsing of the live capture, library and preset matching, the
Bambu-dialect payload OrcaSlicer consumes, and an end-to-end run of the WebSocket client against the
fake printer (reads, verified writes, the printing interlock, reconnects, heartbeats).

## Verification status

* Reading (status, slot table, material library) was verified against the real K1 Max, both with
  the CLI and inside OrcaSlicer 2.5.0-dev (nightly AppImage, 2026-10-02): the plugin registers its
  agent, the printer preset selects it, the agent connects, and OrcaSlicer's own log shows the status
  pushes being parsed and the sidebar AMS list loading the four CFS trays with the project's real
  filament id. The dock panel opened and its page script passes a syntax check.
* Writing was verified against the fake printer, which implements the `modifyMaterial` behaviour
  observed in Creality Print's code. A live write was not possible during development because the
  printer was in the middle of a 4 hour job (the interlock refused, as designed); the CLI makes it a
  one-line check once the printer is idle:
  `python3 tools/cfs_cli.py <ip> set 1 3 --library "Generic PLA" --color "#9ea7ae"` (writes slot 1D
  back to what it already holds, so nothing changes on the printer).
* The OrcaSlicer integration (agent + panel) follows the host API in OrcaSlicer `main`; see
  `orca-patch/README.md` for the state of the optional C++ patch.

### Known limitation: filament ids of system presets

OrcaSlicer's Python API does not expose a preset's `filament_id`, and release builds ship the system
presets as binary `.opc` bundles, so the plugin can only learn ids from presets that are (or have
been) in a project, and from user presets saved as JSON. For a slot whose best-matching preset has
no known id, the plugin publishes a non-empty placeholder id; OrcaSlicer then falls back to the
"Generic &lt;type&gt;" preset for that slot during sync, and the panel still shows the preset the
plugin would have picked. Putting a preset into the project once teaches the plugin its id.

### Developer switches

* `CREALITY_CFS_AUTO_OPEN=1` in OrcaSlicer's environment opens the manager panel automatically at
  startup (same as the `auto_open_panel` config option).
* Plugin log lines go to `<datadir>/log/python_*.log`; OrcaSlicer's own log (`debug_*.log.0`) shows
  the parsed `push_status` payloads.

## Notes on the design

* OrcaSlicer internally speaks Bambu's MQTT dialect for every printer. The agent translates the
  Creality status stream into `push_status` messages with an `ams` block, which is the same path the
  built-in Moonraker / Qidi / Snapmaker agents use. CFS boxes become AMS units 0, 1, ...; the external
  spool holder is not an AMS unit.
* Uploads and print start keep using OrcaSlicer's built-in *Creality Print* host; the agent is only
  responsible for status, filament sync and slot writes.
* The plugin reads OrcaSlicer's own filament preset JSON files to learn each preset's `filament_id`
  (the Python API exposes a preset's settings but not its id); nothing else on disk is touched.

## License

AGPL-3.0, the license of both OrcaSlicer and Creality Print.
