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

### Automatic sync from the sidebar filament list

With the printer agent selected, the plugin keeps the CFS in step with OrcaSlicer's own filament
section automatically: change the preset or the colour of filament 1, 2, 3 or 4 in the sidebar and,
about two seconds after you stop changing things, the matching CFS slot (1A, 1B, 1C, 1D) is written
with that preset's material, colour, temperature range and pressure advance. Only slots that differ
are written, slots holding a Creality RFID spool are left alone, and the state found when OrcaSlicer
starts is taken as the baseline, so nothing is written until you actually change something. If the
printer is printing or moving filament at that moment, the change is queued and applied when the
printer is idle again; a notification tells you either way.

Switches in the plugin config: `auto_sync_project_to_cfs` (default on), `auto_sync_debounce_seconds`
(default 2), `auto_sync_notify` (default on). The reverse direction is OrcaSlicer's own sync button in
the sidebar, which pulls the CFS slots into the project.

Opening or creating a project takes that project's filament list as the new baseline (a project is
never pushed to the CFS just by opening it); edits you make afterwards are synced.

The watcher runs as long as the plugin is loaded and does not depend on which printer agent
OrcaSlicer has selected: it talks to the printer named in the active printer preset. Be aware that
a 3MF project embeds its own printer preset, and if that embedded preset was saved without the
*Printer Agent* setting, OrcaSlicer switches back to its default agent when the project opens. The
filament sync keeps working, but live printer status and the sidebar AMS sync button need the
plugin agent; the plugin shows a notification when this happens. To fix it permanently, set
*Printer Settings → Advanced → Printer Agent* to *Creality CFS (LAN)* with the project open and save
the project.

The plugin writes its own log to `<datadir>/orca_plugins/plugin_data/creality_cfs/creality_cfs.log`
(OrcaSlicer's Python log misses lines emitted on its UI thread).

### Writing filaments to the CFS by hand

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

### Where the built-in filament configuration comes from

The sync maps each CFS slot onto one of OrcaSlicer's **built-in filament presets** for your printer
(for example *Hyper PLA @K1 Max_CFS-C-all* or *Generic PLA @K1 Max_CFS-C-all*) and publishes that
preset's `filament_id`, so OrcaSlicer selects exactly that preset when you synchronize. OrcaSlicer's
Python API does not expose preset ids, and release builds ship the system presets as binary vendor
caches (`<datadir>/system/<Vendor>.opc`) rather than JSON, so the plugin reads those caches directly:
it understands OrcaSlicer's cache format (version 2, CRC-checked), resolves each preset's `inherits`
chain the way OrcaSlicer does, and takes the id, vendor, material type, nozzle temperature range,
pressure advance and default colour from there. JSON preset files (user presets, source checkouts)
and the ids of filaments already in the project are used as well. Only when a preset cannot be found
anywhere does the plugin publish a placeholder id, which makes OrcaSlicer fall back to the
"Generic &lt;type&gt;" preset for that slot.

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
