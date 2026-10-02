"""Encoder for OrcaSlicer's vendor preset cache (.opc) - the mirror image of the plugin's reader.

Used by the tests to build small synthetic caches that exercise every option encoding the reader
has to skip over. Format: see src/libslic3r/PresetCacheFormat.cpp in OrcaSlicer (version 2).
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

OPC_MAGIC = 0x4F52435A
OPC_VERSION = 2
VEC = 0x4000
CO = {
    "float": 1, "floats": 1 + VEC, "int": 2, "ints": 2 + VEC, "string": 3, "strings": 3 + VEC,
    "percent": 4, "percents": 4 + VEC, "float_or_percent": 5, "floats_or_percents": 5 + VEC,
    "point": 6, "points": 6 + VEC, "point3": 7, "bool": 8, "bools": 8 + VEC, "enum": 9, "enums": 9 + VEC,
    "points_groups": 10 + VEC, "ints_groups": 11 + VEC,
}


class _W:
    def __init__(self) -> None:
        self.buf = bytearray()

    def u8(self, v): self.buf += struct.pack("<B", v)
    def u16(self, v): self.buf += struct.pack("<H", v)
    def u32(self, v): self.buf += struct.pack("<I", v)
    def i32(self, v): self.buf += struct.pack("<i", v)
    def u64(self, v): self.buf += struct.pack("<Q", v)
    def f64(self, v): self.buf += struct.pack("<d", v)

    def string(self, s: str):
        b = s.encode("utf-8")
        self.u64(len(b))
        self.buf += b

    def strings(self, items):
        self.u64(len(items))
        for s in items:
            self.string(s)


def _write_option(w: _W, kind: str, value: Any, enum_index: dict[str, int]) -> None:
    t = CO[kind]
    if kind in ("enum", "enums"):
        values = value if isinstance(value, list) else [value]
        w.u32(len(values))
        for v in values:
            if isinstance(v, str) and v in enum_index:
                w.u16(enum_index[v])
            else:
                w.u16(0)
                w.i32(int(v) if not isinstance(v, str) else 0)
        return
    if kind == "points_groups":
        w.u64(len(value))
        for group in value:
            w.u64(len(group))
            for x, y in group:
                w.f64(x); w.f64(y)
        return
    if kind == "ints_groups":
        w.u64(len(value))
        for group in value:
            w.u64(len(group))
            for v in group:
                w.i32(v)
        return
    is_vec = bool(t & VEC)
    base = t & ~VEC
    values = value if is_vec else [value]
    if is_vec:
        w.u64(len(values))
    for v in values:
        if base in (1, 4):
            w.f64(float(v))
        elif base == 2:
            w.i32(int(v))
        elif base == 3:
            w.string(str(v))
        elif base == 5:
            w.f64(float(v[0])); w.u8(1 if v[1] else 0)
        elif base == 6:
            w.f64(v[0]); w.f64(v[1])
        elif base == 7:
            w.f64(v[0]); w.f64(v[1]); w.f64(v[2])
        elif base == 8:
            w.u8(1 if v else 0)
        else:
            raise ValueError(kind)


def build_opc(
    vendor: str,
    filaments: list[dict],
    *,
    processes: list[dict] | None = None,
    machines: list[dict] | None = None,
    dictionary: dict[str, str] | None = None,
    enum_values: list[str] | None = None,
    models: int = 1,
    version: int = OPC_VERSION,
    corrupt_crc: bool = False,
) -> bytes:
    """Build a cache file.

    Each preset dict: {"name", "inherits", "filament_id", "setting_id", "instantiation",
    "config": {key: (kind, value)}, "includes": [...], "renamed_from": [...]}.
    `dictionary` maps option key -> kind for every key used by any preset (order = index order).
    """
    processes = processes or []
    machines = machines or []
    enum_values = [""] + list(enum_values or [])
    enum_index = {name: i for i, name in enumerate(enum_values) if name}
    if dictionary is None:
        dictionary = {}
        for group in (processes, filaments, machines):
            for p in group:
                for k, (kind, _v) in p.get("config", {}).items():
                    dictionary.setdefault(k, kind)
    keys = list(dictionary.keys())
    key_index = {k: i for i, k in enumerate(keys)}

    w = _W()
    w.u32(version)
    w.string(vendor)
    w.string("02.03.00.01")
    w.strings(keys)
    w.u64(len(keys))
    for k in keys:
        w.u16(CO[dictionary[k]])
    w.strings(enum_values)
    # vendor map with one vendor
    w.u64(1)
    w.string(vendor)
    w.string(vendor); w.string(vendor.lower()); w.string("2.3.0.1"); w.string("https://example/updates"); w.string("https://example/changelog")
    w.u64(models)
    for m in range(models):
        w.string(f"M{m}"); w.string(f"Model {m}"); w.string(f"model_{m}"); w.u8(0); w.string("Family")
        w.u64(2); w.string("0.4"); w.string("0.6")
        w.strings(["Generic PLA @Test"]); w.strings([])
        for _ in range(9):
            w.string("")
    w.strings(["Generic PLA @Test"]); w.strings([])

    def entries(group):
        w.u32(len(group))
        for p in group:
            w.string(p["name"]); w.string(p.get("sub_path", "filament/x.json"))
            cfg = p.get("config", {})
            w.u32(len(cfg))
            for k, (kind, value) in cfg.items():
                w.u16(key_index[k])
                _write_option(w, kind, value, enum_index)
            w.string(p.get("inherits", "")); w.strings(p.get("includes", [])); w.string(p.get("description", ""))
            w.string(p.get("instantiation", "true")); w.string(p.get("setting_id", "")); w.string(p.get("filament_id", ""))
            w.strings(p.get("renamed_from", []))

    entries(processes)
    entries(filaments)
    entries(machines)
    w.u64(0)  # parse_errors
    body = bytes(w.buf)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    if corrupt_crc:
        crc ^= 0xFFFF
    return struct.pack("<IIQI", OPC_MAGIC, version, len(body), crc) + body
