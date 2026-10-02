# Upstream patch for OrcaSlicer (C++)

`0001-creality-k1-max-cfs-and-slot-write.patch` is a `git diff` against OrcaSlicer `main` at the
commit recorded in `BASE_COMMIT` (2026-10-02). It complements the Python plugin by teaching
OrcaSlicer's **built-in** Creality support the same two things:

1. **K1 Max is CFS-capable.** OrcaSlicer already reads CFS slots over port 9999 for the K2 family and
   a few K1 models, but its model table (`cfs_capable_models()` in `CrealityPrint.cpp`) did not list
   `"K1 Max"`, which is what a K1 Max with the CFS upgrade reports at `http://<ip>/info`. Without the
   entry the "Send to printer" dialog shows no CFS colour mapping and the built-in `CrealityPrintAgent`
   falls back to plain Moonraker behaviour. The patch adds `"K1 Max"` (verified live) and
   `"K1 Max_CFS-C"` (by analogy with the existing `"K1_CFS-C"`) to both the capability table and the
   K1-family table that selects the `/usr/data` G-code path.

2. **Writing slots.** `CrealityPrint::modify_material()` sends `set modifyMaterial` over the
   WebSocket, refuses to run while the printer is not idle, and verifies the result by re-reading
   `boxsInfo`. `CrealityPrintAgent::send_message_to_printer()` intercepts OrcaSlicer's
   `ams_filament_setting` command (what the AMS slot edit dialog publishes), resolves the filament
   preset behind `tray_info_idx` to vendor / product name / temperature range / pressure advance, maps
   the AMS index back to the printer's box id and performs the write. Other commands still go to the
   Moonraker base agent.

## Applying

```sh
git clone https://github.com/OrcaSlicer/OrcaSlicer.git
cd OrcaSlicer
git checkout $(cat /path/to/orca-patch/BASE_COMMIT)   # or any newer main; resolve conflicts if needed
git apply --3way /path/to/orca-patch/0001-creality-k1-max-cfs-and-slot-write.patch
```

Then build OrcaSlicer the normal way (`./build_linux.sh -u && ./build_linux.sh -dsi` on Linux, or the
platform scripts in the repository root).

## Status

* The Python plugin in `../plugin` is the tested deliverable; it was exercised against a live K1 Max
  and against a fake printer in the test-suite.
* **This patch has not been compiled.** The machine it was written on has no OrcaSlicer build
  environment (immutable OS, no CMake, no prebuilt deps) and building the full dependency tree takes
  hours. The code only uses APIs already present in the touched files (`ws_connect`,
  `ws_send_and_read`, `build_ams_payload`, `PresetBundle` / `ConfigOption*` accessors), and
  `git diff --check` is clean, but expect to fix small compile errors before submitting it upstream.
* `tests/slic3rutils/test_creality_cfs_match.cpp` upstream keeps compiling: `parse_cfs_response()` and
  `match_filament_preset()` signatures are unchanged (one field, `raw_box_id`, was added to `CFSSlot`).
* The wire format and the idle interlock follow Creality Print's device manager; see
  `../docs/protocol.md`.
