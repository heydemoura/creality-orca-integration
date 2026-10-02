#!/usr/bin/env python3
"""Command line companion for the Creality CFS Sync plugin.

Talks to a Creality K-series printer with exactly the same code the OrcaSlicer plugin uses, so the
protocol layer can be exercised (and debugged) without OrcaSlicer:

    cfs_cli.py 192.168.0.128 status            # printer state + CFS slot table
    cfs_cli.py 192.168.0.128 slots --json      # machine readable slot table
    cfs_cli.py 192.168.0.128 materials         # the printer's material library
    cfs_cli.py 192.168.0.128 watch             # stream every message (Ctrl-C to stop)
    cfs_cli.py 192.168.0.128 set 1 0 --library "Generic PLA" --color "#ff0000"
    cfs_cli.py 192.168.0.128 set 1 1 --vendor Creality --type PLA --name "Hyper PLA" --color "#00ff00" --min 190 --max 240 --pa 0.04
    cfs_cli.py 192.168.0.128 clear 1 3
    cfs_cli.py 192.168.0.128 ams-json          # the Bambu-dialect payload the plugin hands OrcaSlicer

Writes are refused while the printer is printing or feeding filament (same interlock as the
plugin and as Creality Print); pass --force to override.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin"))

import creality_cfs as cfs  # noqa: E402


def _connect(host: str, timeout: float, need_library: bool = False) -> cfs.CrealityPrinterClient:
    client = cfs.get_client(host)
    if not client.wait_ready(timeout):
        snap = client.snapshot()
        sys.exit(f"could not get CFS data from {host}: {snap.last_error or 'timeout'}")
    if need_library:
        deadline = time.time() + timeout
        while time.time() < deadline and not client.snapshot().library:
            time.sleep(0.2)
    return client


def _print_slots(snap: cfs.PrinterSnapshot) -> None:
    for box in snap.boxes:
        kind = "CFS" if box.is_cfs else "external spool" if box.type == cfs.BOX_TYPE_EXTERNAL else f"type {box.type}"
        extra = []
        if box.temp is not None:
            extra.append(f"{box.temp:.0f}°C")
        if box.humidity is not None:
            extra.append(f"{box.humidity:.0f}% RH")
        print(f"Box {box.id} ({kind}, {'active' if box.is_active else 'inactive'}){' - ' + ', '.join(extra) if extra else ''}")
        for s in box.slots:
            if not s.is_loaded:
                print(f"  {s.label}: empty")
                continue
            flags = []
            if s.selected:
                flags.append("active")
            if s.is_rfid_locked:
                flags.append("RFID")
            print(
                f"  {s.label}: {s.color or '-':8} {s.vendor} {s.name} [{s.type}] id={s.rfid or '-'} "
                f"{s.min_temp}-{s.max_temp}°C PA={s.pressure} {s.percent}%{(' (' + ', '.join(flags) + ')') if flags else ''}"
            )


def cmd_status(args: argparse.Namespace) -> None:
    client = _connect(args.host, args.timeout)
    snap = client.snapshot()
    state = {0: "idle", 1: "printing", 2: "paused/finishing", 5: "busy"}.get(snap.device_state, str(snap.device_state))
    print(f"{snap.model or 'Creality'} '{snap.hostname}' @ {snap.host}  state={state}  cfs={'yes' if snap.cfs_connected else 'no'}")
    if snap.print_file:
        print(f"  job: {os.path.basename(snap.print_file)} {snap.print_progress}% layer {snap.layer}/{snap.total_layers}, {int((snap.print_left_seconds or 0) / 60)} min left")
    if snap.nozzle_temp is not None:
        print(f"  nozzle {snap.nozzle_temp:.0f}/{snap.nozzle_target or 0:.0f}°C  bed {snap.bed_temp or 0:.0f}/{snap.bed_target or 0:.0f}°C  CFS {snap.box_temp or 0:.0f}°C")
    if snap.box_config:
        print(f"  boxConfig: {snap.box_config}")
    writable = client.check_writable(snap)
    print(f"  slot writes: {'allowed' if writable is None else 'blocked (' + writable + ')'}")
    _print_slots(snap)
    client.stop()


def cmd_slots(args: argparse.Namespace) -> None:
    client = _connect(args.host, args.timeout)
    snap = client.snapshot()
    if args.json:
        print(json.dumps([asdict(b) for b in snap.boxes], indent=1))
    else:
        _print_slots(snap)
    client.stop()


def cmd_materials(args: argparse.Namespace) -> None:
    client = _connect(args.host, args.timeout, need_library=True)
    lib = client.snapshot().library
    if args.json:
        print(json.dumps([asdict(e) for e in lib], indent=1))
    else:
        print(f"{len(lib)} materials in the printer library:")
        for e in sorted(lib, key=lambda e: (e.is_generic, e.brand, e.type, e.name)):
            print(f"  {e.id}  {e.brand:10} {e.type:8} {e.name:22} {e.min_temp}-{e.max_temp}°C  PA={e.pressure_advance}")
    client.stop()


def cmd_watch(args: argparse.Namespace) -> None:
    client = cfs.get_client(args.host)

    def listener(_client: cfs.CrealityPrinterClient, msg: dict) -> None:
        stamp = time.strftime("%H:%M:%S")
        if "__connected__" in msg:
            print(f"{stamp} connected")
        elif "__disconnected__" in msg:
            print(f"{stamp} disconnected: {msg['__disconnected__']}")
        else:
            keys = sorted(msg.keys())
            summary = json.dumps(msg)[: args.width]
            print(f"{stamp} keys={keys}\n    {summary}")

    client.add_listener(listener)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        client.stop()


def cmd_ams_json(args: argparse.Namespace) -> None:
    client = _connect(args.host, args.timeout)
    print(json.dumps(cfs.build_push_status(client.snapshot(), 1), indent=1))
    client.stop()


def _report(result: cfs.WriteResult) -> None:
    print(("OK   " if result.ok else "FAIL ") + f"slot {result.box_id}/{result.slot_id}: {result.message}")
    if result.response_code is not None:
        print(f"     printer acknowledgement: modifyMaterial={result.response_code!r}")
    if result.after:
        a = result.after
        print(f"     now: {a.get('color')} {a.get('vendor')} {a.get('name')} [{a.get('type')}] id={a.get('rfid')} {a.get('min_temp')}-{a.get('max_temp')}°C PA={a.get('pressure')}")


def cmd_set(args: argparse.Namespace) -> None:
    client = _connect(args.host, args.timeout, need_library=True)
    snap = client.snapshot()
    if args.library:
        parts = args.library.split(" ", 1)
        entry = cfs.match_library_entry(snap.library, args.vendor or "", args.type or "", args.library)
        if entry is None and len(parts) == 2:
            entry = cfs.match_library_entry(snap.library, parts[0], args.type or "", parts[1])
        if entry is None:
            sys.exit(f"no library material matches {args.library!r}; see `materials`")
        spec = cfs.MaterialSpec(
            vendor=entry.brand,
            type=entry.type,
            name=entry.name,
            color=args.color or (entry.colors[0] if entry.colors else "#FFFFFF"),
            rfid=entry.id,
            min_temp=args.min or entry.min_temp,
            max_temp=args.max or entry.max_temp,
            pressure=args.pa if args.pa is not None else entry.pressure_advance,
        )
    else:
        if not (args.type and args.name):
            sys.exit("either --library NAME or --type/--name (plus optional --vendor/--min/--max/--pa) is required")
        entry = cfs.match_library_entry(snap.library, args.vendor or "", args.type, args.name)
        spec = cfs.MaterialSpec(
            vendor=args.vendor or (entry.brand if entry else "Generic"),
            type=args.type,
            name=args.name,
            color=args.color or "#FFFFFF",
            rfid=args.rfid if args.rfid is not None else (entry.id if entry and cfs._norm(entry.name) == cfs._norm(args.name) else ""),
            min_temp=args.min or (entry.min_temp if entry else 190),
            max_temp=args.max or (entry.max_temp if entry else 240),
            pressure=args.pa if args.pa is not None else (entry.pressure_advance if entry else 0.0),
        )
    print("writing:", json.dumps(spec.to_params(args.box, args.slot)))
    if args.dry_run:
        print("dry run - nothing sent")
        client.stop()
        return
    _report(client.modify_material(args.box, args.slot, spec, force=args.force))
    client.stop()


def cmd_clear(args: argparse.Namespace) -> None:
    client = _connect(args.host, args.timeout)
    if args.dry_run:
        print("would send:", json.dumps(cfs.MaterialSpec().to_params(args.box, args.slot)))
        client.stop()
        return
    _report(client.clear_slot(args.box, args.slot, force=args.force))
    client.stop()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", help="printer IP / hostname (a URL or host:port from OrcaSlicer is accepted)")
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("slots")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_slots)
    p = sub.add_parser("materials")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_materials)
    p = sub.add_parser("watch")
    p.add_argument("--width", type=int, default=300)
    p.set_defaults(fn=cmd_watch)
    sub.add_parser("ams-json").set_defaults(fn=cmd_ams_json)

    p = sub.add_parser("set", help="write a slot record")
    p.add_argument("box", type=int, help="box id as reported by the printer (the CFS is usually 1)")
    p.add_argument("slot", type=int, help="slot index 0..3 (A..D)")
    p.add_argument("--library", help="material library entry, e.g. 'Generic PLA' or 'Creality Hyper PLA'")
    p.add_argument("--vendor")
    p.add_argument("--type")
    p.add_argument("--name")
    p.add_argument("--color", help="#RRGGBB")
    p.add_argument("--rfid", help="library id to store (default: resolved from the library when the name matches)")
    p.add_argument("--min", type=int, help="nozzle temperature range low")
    p.add_argument("--max", type=int, help="nozzle temperature range high")
    p.add_argument("--pa", type=float, help="pressure advance")
    p.add_argument("--force", action="store_true", help="write even while printing / feeding")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("clear", help="empty a slot record")
    p.add_argument("box", type=int)
    p.add_argument("slot", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_clear)

    args = ap.parse_args(argv)
    cfs.LOG.setLevel("DEBUG" if args.verbose else "WARNING")
    args.host = cfs.strip_host(args.host)
    args.fn(args)


if __name__ == "__main__":
    main()
