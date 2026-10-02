# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "Creality CFS Sync"
# description = "Read and write Creality CFS filament slots (K1 / K2 series) over the LAN protocol used by Creality Print."
# author = "Heyde Moura"
# version = "0.1.0"
# ///
"""Creality CFS Sync - OrcaSlicer plugin.

Ports the printer-side filament features of Creality Print to OrcaSlicer:

* READ  - the CFS (Creality Filament System) slots are read from the printer and published to
          OrcaSlicer as AMS trays, so the sidebar "Synchronize filament list from AMS" button and the
          per-filament sync badges work exactly like they do for other multi-material systems.
* WRITE - a dock panel ("Creality CFS Manager") shows the live slot table and lets you push a
          project filament (or an entry of the printer's own material library, or a custom
          material) into any slot. The same write path also answers OrcaSlicer's native
          `ams_filament_setting` command, should the AMS edit dialog be opened for this printer.

The protocol is the JSON-over-WebSocket link on TCP port 9999 that Creality Print's device manager
uses (see docs/protocol.md in the repository). Everything here is standard library only, so the
plugin runs inside OrcaSlicer's bundled Python without any package installation, and the same file
is importable from plain CPython for the CLI tool and the test-suite (the `orca` module is optional).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
import logging
import os
import re
import socket
import struct
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Optional

try:  # The host module only exists inside OrcaSlicer.
    import orca  # type: ignore

    # An unrelated package called "orca" may be installed in a plain CPython (e.g. the screen
    # reader); only treat the module as the host when it carries the plugin API.
    if not (hasattr(orca, "printer_agent") and hasattr(orca, "register_capability")):
        orca = None  # type: ignore
except ImportError:  # pragma: no cover - exercised by the CLI / tests
    orca = None  # type: ignore

__version__ = "0.1.0"

AGENT_ID = "creality_cfs"
AGENT_NAME = "Creality CFS (LAN)"
MANAGER_NAME = "Creality CFS Manager"
WS_PORT = 9999
HTTP_PORT = 80

# Return codes understood by OrcaSlicer's MachineObject::publish_json funnel.
ORCA_NETWORK_SUCCESS = 0
ORCA_NETWORK_ERR_CMD_NOT_SUPPORTED = -7010
ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE = -7020

# Creality `deviceState` values (from Creality Print's device UI): 0 idle, 1 printing, 2 paused /
# finishing, 5 busy. Anything other than 0 blocks slot writes unless the user overrides it.
DEVICE_STATE_IDLE = 0
DEVICE_STATE_PRINTING = 1

# Creality `feedState` values that mean "no filament motion in progress" (also from the device UI).
FEED_STATES_IDLE = {0, 3, 14}

# Slot `state`: 0 empty, 1 loaded (editable), 2 loaded with a Creality RFID spool (read only on
# the printer side too).
SLOT_STATE_EMPTY = 0
SLOT_STATE_LOADED = 1
SLOT_STATE_RFID = 2

BOX_TYPE_CFS = 0
BOX_TYPE_EXTERNAL = 1
BOX_TYPE_CFS_MINI = 2  # Creality Print groups type 0 and 2 together as "cfs"

LOG = logging.getLogger("creality_cfs")
if not LOG.handlers:
    _handler = logging.StreamHandler(sys.stderr)  # OrcaSlicer tees stderr into <datadir>/log/python_*.log
    _handler.setFormatter(logging.Formatter("%(asctime)s creality_cfs %(levelname)s %(message)s"))
    LOG.addHandler(_handler)
LOG.setLevel(logging.INFO)


def _attach_plugin_log_file() -> Optional[str]:
    """Also log into the plugin's own storage folder (<datadir>/orca_plugins/plugin_data/creality_cfs/).

    OrcaSlicer's stderr tee can lose lines emitted on its UI thread, which is where most of the
    sync decisions are made; the plugin's storage folder is the place OrcaSlicer reserves for a
    plugin's own files, so a small rotating log there keeps the whole story in one place.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        plugins_dir = os.path.dirname(here)
        if os.path.basename(plugins_dir) != "orca_plugins":
            return None
        key = os.path.basename(here)
        folder = os.path.join(plugins_dir, "plugin_data", key)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "creality_cfs.log")
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(threadName)s %(levelname)s %(message)s"))
        LOG.addHandler(handler)
        return path
    except Exception:
        return None


# =====================================================================================================
# Colour helpers
# =====================================================================================================

_HEX6 = re.compile(r"^[0-9a-fA-F]{6}$")


def normalize_hex(color: str | None, default: str = "#FFFFFF") -> str:
    """Return "#RRGGBB" (upper case) for any of the colour spellings seen in the wild.

    Accepts "#RRGGBB", "RRGGBB", "#0RRGGBB" (Creality), "#RRGGBBAA" / "RRGGBBAA" (Bambu / Orca),
    "0xRRGGBB" and "#AARRGGBB"-style 9 char strings used by Creality's cloud API.
    """
    if not color:
        return default
    value = color.strip()
    if value.lower().startswith("0x"):
        value = value[2:]
    if value.startswith("#"):
        value = value[1:]
    value = "".join(ch for ch in value if ch in "0123456789abcdefABCDEF")
    if len(value) == 7 and value[0] == "0":  # Creality "#0RRGGBB"
        value = value[1:]
    elif len(value) == 8:
        # Orca/Bambu "RRGGBBAA" or Creality cloud "AARRGGBB" (always FF alpha first). Prefer the
        # Orca reading unless the string starts with FF and the Orca reading would be odd.
        if value[:2].upper() == "FF" and value[6:].upper() != "FF":
            value = value[2:]
        else:
            value = value[:6]
    if not _HEX6.match(value):
        return default
    return "#" + value.upper()


def hex_to_creality(color: str) -> str:
    """"#RRGGBB" -> "#0RRGGBB" - the spelling the printer stores and expects in modifyMaterial."""
    value = normalize_hex(color)
    return "#0" + value[1:].lower()


def hex_to_bbl(color: str) -> str:
    """"#RRGGBB" -> "RRGGBBFF" - the spelling OrcaSlicer's AMS parser expects."""
    value = normalize_hex(color)
    return value[1:].upper() + "FF"


# =====================================================================================================
# Data model
# =====================================================================================================


@dataclass
class MaterialLibraryEntry:
    """One entry of the printer's material library (`reqMaterials` -> `retMaterials`)."""

    id: str
    brand: str
    type: str
    name: str
    min_temp: int
    max_temp: int
    pressure_advance: float
    colors: list[str] = field(default_factory=list)

    @property
    def is_generic(self) -> bool:
        return self.brand.lower() == "generic"


@dataclass
class CfsSlot:
    box_id: int
    slot_id: int
    state: int = SLOT_STATE_EMPTY
    vendor: str = ""
    type: str = ""
    name: str = ""
    rfid: str = ""
    color: str = ""  # "#RRGGBB" or "" when empty
    min_temp: int = 0
    max_temp: int = 0
    pressure: float = 0.0
    percent: int = 0
    selected: bool = False
    edit_status: int = 0

    @property
    def is_loaded(self) -> bool:
        if self.state == SLOT_STATE_EMPTY:
            return False
        return bool(self.vendor or self.type or self.name or self.color)

    @property
    def is_rfid_locked(self) -> bool:
        return self.state == SLOT_STATE_RFID

    @property
    def label(self) -> str:
        return f"{self.box_id}{chr(ord('A') + self.slot_id)}"


@dataclass
class CfsBox:
    id: int
    type: int
    state: int
    temp: Optional[float] = None
    humidity: Optional[float] = None
    slots: list[CfsSlot] = field(default_factory=list)

    @property
    def is_cfs(self) -> bool:
        return self.type in (BOX_TYPE_CFS, BOX_TYPE_CFS_MINI)

    @property
    def is_active(self) -> bool:
        return self.state == 1


@dataclass
class MaterialSpec:
    """What to write into a slot. Empty strings / zeros clear the slot."""

    vendor: str = ""
    type: str = ""
    name: str = ""
    color: str = ""  # "#RRGGBB" or ""
    rfid: str = ""  # material library id when known ("00001" = Generic PLA)
    min_temp: int = 0
    max_temp: int = 0
    pressure: float = 0.0

    @property
    def is_clear(self) -> bool:
        return not (self.vendor or self.type or self.name or self.color)

    def to_params(self, box_id: int, slot_id: int) -> dict[str, Any]:
        if self.is_clear:
            return {
                "modifyMaterial": {
                    "boxId": box_id,
                    "id": slot_id,
                    "rfid": "",
                    "type": "",
                    "vendor": "",
                    "name": "",
                    "color": "",
                    "minTemp": 0,
                    "maxTemp": 0,
                    "pressure": 0,
                }
            }
        # Creality Print adds 1e-8 so the numbers serialise as floats; the firmware accepts ints too,
        # but we mirror the reference client exactly.
        return {
            "modifyMaterial": {
                "boxId": box_id,
                "id": slot_id,
                "rfid": self.rfid or "",
                "type": self.type,
                "vendor": self.vendor,
                "name": self.name,
                "color": hex_to_creality(self.color) if self.color else "",
                "minTemp": float(self.min_temp) + 1e-8,
                "maxTemp": float(self.max_temp) + 1e-8,
                "pressure": float(self.pressure or 0.0),
            }
        }


@dataclass
class WriteResult:
    ok: bool
    box_id: int
    slot_id: int
    message: str
    response_code: Optional[Any] = None
    verified: bool = False
    before: Optional[dict] = None
    after: Optional[dict] = None


@dataclass
class PrinterSnapshot:
    host: str = ""
    connected: bool = False
    model: str = ""
    hostname: str = ""
    mac: str = ""
    firmware: str = ""
    device_state: Optional[int] = None
    feed_state: Optional[int] = None
    cfs_connected: Optional[bool] = None
    print_file: str = ""
    print_progress: Optional[int] = None
    print_left_seconds: Optional[int] = None
    layer: Optional[int] = None
    total_layers: Optional[int] = None
    nozzle_temp: Optional[float] = None
    nozzle_target: Optional[float] = None
    bed_temp: Optional[float] = None
    bed_target: Optional[float] = None
    box_temp: Optional[float] = None
    boxes: list[CfsBox] = field(default_factory=list)
    box_config: dict[str, Any] = field(default_factory=dict)
    library: list[MaterialLibraryEntry] = field(default_factory=list)
    last_error: str = ""
    updated_at: float = 0.0

    @property
    def is_idle(self) -> bool:
        return self.device_state == DEVICE_STATE_IDLE and (self.feed_state is None or self.feed_state in FEED_STATES_IDLE)

    @property
    def cfs_boxes(self) -> list[CfsBox]:
        return [b for b in self.boxes if b.is_cfs and b.is_active]

    @property
    def external_box(self) -> Optional[CfsBox]:
        for b in self.boxes:
            if b.type == BOX_TYPE_EXTERNAL:
                return b
        return None

    def find_slot(self, box_id: int, slot_id: int) -> Optional[CfsSlot]:
        for b in self.boxes:
            if b.id == box_id:
                for s in b.slots:
                    if s.slot_id == slot_id:
                        return s
        return None


# =====================================================================================================
# Parsing
# =====================================================================================================


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_boxes(boxs_info: dict | None) -> list[CfsBox]:
    """Parse the `boxsInfo` object (`{"materialBoxs": [...], ...}`) into CfsBox records."""
    boxes: list[CfsBox] = []
    if not isinstance(boxs_info, dict):
        return boxes
    for raw in boxs_info.get("materialBoxs") or []:
        if not isinstance(raw, dict):
            continue
        box = CfsBox(
            id=_int(raw.get("id")),
            type=_int(raw.get("type")),
            state=_int(raw.get("state")),
            temp=_num(raw["temp"]) if "temp" in raw else None,
            humidity=_num(raw["humidity"]) if "humidity" in raw else None,
        )
        for mat in raw.get("materials") or []:
            if not isinstance(mat, dict):
                continue
            color_raw = str(mat.get("color") or "")
            slot = CfsSlot(
                box_id=box.id,
                slot_id=_int(mat.get("id")),
                state=_int(mat.get("state")),
                vendor=str(mat.get("vendor") or ""),
                type=str(mat.get("type") or ""),
                name=str(mat.get("name") or ""),
                rfid=str(mat.get("rfid") or ""),
                color=normalize_hex(color_raw, default="") if color_raw else "",
                min_temp=_int(mat.get("minTemp")),
                max_temp=_int(mat.get("maxTemp")),
                pressure=_num(mat.get("pressure")),
                percent=_int(mat.get("percent"), 0),
                selected=bool(_int(mat.get("selected"))),
                edit_status=_int(mat.get("editStatus")),
            )
            box.slots.append(slot)
        box.slots.sort(key=lambda s: s.slot_id)
        boxes.append(box)
    boxes.sort(key=lambda b: b.id)
    return boxes


def parse_material_library(ret_materials: Any) -> list[MaterialLibraryEntry]:
    entries: list[MaterialLibraryEntry] = []
    if not isinstance(ret_materials, list):
        return entries
    for raw in ret_materials:
        if not isinstance(raw, dict):
            continue
        base = raw.get("base") or {}
        kv = raw.get("kvParam") or {}
        if not isinstance(base, dict):
            continue
        entry = MaterialLibraryEntry(
            id=str(base.get("id") or ""),
            brand=str(base.get("brand") or ""),
            type=str(base.get("meterialType") or base.get("materialType") or kv.get("filament_type") or ""),
            name=str(base.get("name") or ""),
            min_temp=_int(base.get("minTemp"), _int(kv.get("nozzle_temperature_range_low"))),
            max_temp=_int(base.get("maxTemp"), _int(kv.get("nozzle_temperature_range_high"))),
            pressure_advance=_num(kv.get("pressure_advance"), 0.0),
            colors=[normalize_hex(c) for c in (base.get("colors") or []) if isinstance(c, str)],
        )
        if entry.name:
            entries.append(entry)
    return entries


def apply_status(snapshot: PrinterSnapshot, msg: dict) -> None:
    """Fold a status/push message from the printer into the snapshot (in place)."""
    if "model" in msg:
        snapshot.model = str(msg["model"])
    if "hostname" in msg:
        snapshot.hostname = str(msg["hostname"])
    if "modelVersion" in msg:
        snapshot.firmware = str(msg["modelVersion"])
    if "deviceState" in msg:
        snapshot.device_state = _int(msg["deviceState"])
    elif "state" in msg and snapshot.device_state is None:
        snapshot.device_state = _int(msg["state"])
    if "feedState" in msg:
        snapshot.feed_state = _int(msg["feedState"])
    if "cfsConnect" in msg:
        snapshot.cfs_connected = bool(_int(msg["cfsConnect"]))
    if "printFileName" in msg:
        snapshot.print_file = str(msg["printFileName"] or "")
    if "printProgress" in msg:
        snapshot.print_progress = _int(msg["printProgress"])
    if "printLeftTime" in msg:
        snapshot.print_left_seconds = _int(msg["printLeftTime"])
    if "layer" in msg:
        snapshot.layer = _int(msg["layer"])
    if "TotalLayer" in msg:
        snapshot.total_layers = _int(msg["TotalLayer"])
    if "nozzleTemp" in msg:
        snapshot.nozzle_temp = _num(msg["nozzleTemp"])
    if "targetNozzleTemp" in msg:
        snapshot.nozzle_target = _num(msg["targetNozzleTemp"])
    if "bedTemp0" in msg:
        snapshot.bed_temp = _num(msg["bedTemp0"])
    if "targetBedTemp0" in msg:
        snapshot.bed_target = _num(msg["targetBedTemp0"])
    if "boxTemp" in msg:
        snapshot.box_temp = _num(msg["boxTemp"])
    if isinstance(msg.get("boxConfig"), dict):
        snapshot.box_config = dict(msg["boxConfig"])
    if isinstance(msg.get("boxsInfo"), dict) and "materialBoxs" in msg["boxsInfo"]:
        snapshot.boxes = parse_boxes(msg["boxsInfo"])
    if isinstance(msg.get("retMaterials"), list):
        snapshot.library = parse_material_library(msg["retMaterials"])
    snapshot.updated_at = time.time()


# =====================================================================================================
# Material matching
# =====================================================================================================

_BASE_TYPES = ("PETG", "PET", "PLA", "ABS", "ASA", "TPU", "PC", "PA", "PVA", "HIPS", "PP", "PPS", "BVOH")


def normalize_filament_type(filament_type: str) -> str:
    """"PLA Silk" / "PLA+" / "PLA-CF" -> "PLA" (what OrcaSlicer's preset lookup wants)."""
    ft = (filament_type or "").strip()
    upper = ft.upper()
    for base in _BASE_TYPES:
        if upper.startswith(base):
            return base
    return ft


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _words(text: str) -> list[str]:
    return [w for w in _norm(text).split(" ") if w]


def match_library_entry(
    library: Iterable[MaterialLibraryEntry], vendor: str, filament_type: str, name: str
) -> Optional[MaterialLibraryEntry]:
    """Pick the printer-library entry that best describes (vendor, type, name).

    Exact (brand, type, name) wins, then name match within the same type, then brand-less name
    containment, then the generic entry of the same type. Returns None when nothing fits.
    """
    entries = list(library)
    if not entries:
        return None
    v, t, n = _norm(vendor), _norm(filament_type), _norm(name)
    base_t = _norm(normalize_filament_type(filament_type))

    def type_ok(e: MaterialLibraryEntry, strict: bool) -> bool:
        if not t:  # no type given: match on name alone
            return True
        et = _norm(e.type)
        if strict:
            return et == t
        return et == t or _norm(normalize_filament_type(e.type)) == base_t

    # 1. exact brand + type + name
    for e in entries:
        if _norm(e.brand) == v and _norm(e.type) == t and _norm(e.name) == n:
            return e
    # 2. same name & type regardless of brand spelling
    for e in entries:
        if type_ok(e, True) and _norm(e.name) == n:
            return e
    # 3. the library name is contained in ours (e.g. "Hyper PLA @K1 Max" vs "Hyper PLA") or vice versa
    scored: list[tuple[int, MaterialLibraryEntry]] = []
    for e in entries:
        if not type_ok(e, False):
            continue
        en = _norm(e.name)
        if not en:
            continue
        score = 0
        if en and (en in n or n in en):
            score += 20
        ew = set(_words(e.name)) - {"generic", _norm(e.type)}
        nw = set(_words(name))
        if ew and ew <= nw:
            score += 10
        if v and _norm(e.brand) == v:
            score += 5
        if score:
            scored.append((score, e))
    if scored:
        scored.sort(key=lambda p: (-p[0], p[1].is_generic, p[1].name))
        return scored[0][1]
    # 4. generic of the same (exact) type, then of the base type (only meaningful when a type is known)
    if not t:
        return None
    for strict in (True, False):
        for e in entries:
            if e.is_generic and type_ok(e, strict):
                return e
    return None


@dataclass
class PresetInfo:
    """The subset of an OrcaSlicer filament preset the plugin cares about (plain data, thread-safe)."""

    name: str
    alias: str = ""
    file: str = ""
    vendor: str = ""
    type: str = ""
    filament_id: str = ""
    inherits: str = ""
    min_temp: int = 0
    max_temp: int = 0
    pressure_advance: float = 0.0
    color: str = ""
    is_system: bool = False
    is_user: bool = False
    is_visible: bool = True
    is_compatible: bool = True

    @property
    def base_name(self) -> str:
        """"Hyper PLA @Creality K1 Max_CFS-C-all" -> "Hyper PLA"."""
        return self.name.split("@", 1)[0].strip()


def match_preset_for_slot(presets: Iterable[PresetInfo], slot: CfsSlot) -> Optional[PresetInfo]:
    """Score filament presets against a CFS slot (port of CrealityPrintAgent::match_filament_preset).

    +20 preset name contains the slot's material name, +10 same vendor (by vendor field or by
    name), -5 per preset-name word the spool never mentioned, type must match the slot's base type.
    System presets win ties, then alphabetical order so the result is deterministic.
    """
    slot_type = _norm(normalize_filament_type(slot.type))
    brand = _norm(slot.name)
    vendor = _norm(slot.vendor)
    spool_words = {"generic"} | set(_words(slot.name)) | set(_words(slot.vendor)) | set(_words(slot.type))
    best: list[tuple[int, int, str, PresetInfo]] = []
    for p in presets:
        if not (p.is_visible and p.is_compatible):
            continue
        if _norm(normalize_filament_type(p.type)) != slot_type:
            continue
        name_n = _norm(p.name)
        score = 0
        if brand and brand in name_n:
            score += 20
        if vendor and (vendor == _norm(p.vendor) or vendor in name_n):
            score += 10
        if score == 0:
            continue
        for w in _words(p.base_name):
            if w not in spool_words:
                score -= 5
        best.append((score, 0 if p.is_system else 1, p.name, p))
    if not best:
        # Fall back to any visible compatible preset of the same base type, preferring generic system ones.
        fallback = [
            p
            for p in presets
            if p.is_visible and p.is_compatible and _norm(normalize_filament_type(p.type)) == slot_type
        ]
        fallback.sort(key=lambda p: (0 if "generic" in _norm(p.name) else 1, 0 if p.is_system else 1, p.name))
        return fallback[0] if fallback else None
    best.sort(key=lambda t: (-t[0], t[1], t[2]))
    return best[0][3]


def spec_from_preset(
    preset: PresetInfo,
    color: str,
    library: Iterable[MaterialLibraryEntry],
    temps_from: str = "preset",
) -> tuple[MaterialSpec, Optional[MaterialLibraryEntry]]:
    """Build the slot record for a slicer filament preset.

    The printer stores a library id (`rfid`), the material's vendor/type/name, a colour, a nozzle
    temperature range and a pressure-advance value. We take the identity from the best library
    match when there is one (so the printer UI shows a known material) and, by default, the numbers
    from the slicer preset because that is what the user actually prints with. `temps_from="library"`
    mirrors Creality Print and uses the library numbers instead.
    """
    entry = match_library_entry(library, preset.vendor, preset.type, preset.base_name)
    spec = MaterialSpec(color=normalize_hex(color) if color else "")
    if entry is not None:
        spec.rfid = entry.id
        spec.vendor = entry.brand
        spec.type = entry.type
        spec.name = entry.name
        spec.min_temp = entry.min_temp
        spec.max_temp = entry.max_temp
        spec.pressure = entry.pressure_advance
    else:
        spec.vendor = preset.vendor or "Generic"
        spec.type = preset.type or "PLA"
        spec.name = preset.base_name or f"{spec.vendor} {spec.type}"
    if temps_from != "library" or entry is None:
        if preset.min_temp:
            spec.min_temp = preset.min_temp
        if preset.max_temp:
            spec.max_temp = preset.max_temp
        if preset.pressure_advance:
            spec.pressure = preset.pressure_advance
    if not spec.min_temp or not spec.max_temp:
        # Last resort so the printer never gets a 0..0 range.
        generic = match_library_entry(library, "Generic", spec.type, f"Generic {spec.type}")
        spec.min_temp = spec.min_temp or (generic.min_temp if generic else 190)
        spec.max_temp = spec.max_temp or (generic.max_temp if generic else 240)
    return spec, entry


# =====================================================================================================
# Minimal RFC 6455 WebSocket client (text frames, client-side masking)
# =====================================================================================================


class WebSocketError(Exception):
    pass


class WebSocketClient:
    """Just enough WebSocket for the Creality printer link.

    K1-family firmware requires TEXT frames and answers its own "heart_beat" text with any reply;
    the class is deliberately tiny so it works inside OrcaSlicer's bundled interpreter with no
    third-party packages.
    """

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, host: str, port: int = WS_PORT, path: str = "/", timeout: float = 5.0, user_agent: str = "OrcaSlicer-creality-cfs"):
        self.host = host
        self.port = port
        self.path = path
        self.timeout = timeout
        self.user_agent = user_agent
        self._sock: Optional[socket.socket] = None
        self._buffer = b""
        self._send_lock = threading.Lock()
        self._fragments: list[bytes] = []
        self._fragment_opcode = 0

    # -- connection -------------------------------------------------------------------------------
    def connect(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {self.user_agent}\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise WebSocketError("connection closed during handshake")
            response += chunk
            if len(response) > 65536:
                sock.close()
                raise WebSocketError("handshake response too large")
        head, _, rest = response.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        status = lines[0].split(" ")
        if len(status) < 2 or status[1] != "101":
            sock.close()
            raise WebSocketError(f"handshake failed: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:]) if k}
        expected = base64.b64encode(hashlib.sha1((key + self.GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            sock.close()
            raise WebSocketError("handshake failed: bad Sec-WebSocket-Accept")
        self._sock = sock
        self._buffer = rest

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def close(self, code: int = 1000) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            self._send_frame(0x8, struct.pack("!H", code))
        except Exception:
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        self._sock = None

    # -- frames -----------------------------------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise WebSocketError("not connected")
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def _read_exact(self, n: int) -> bytes:
        sock = self._sock
        if sock is None:
            raise WebSocketError("not connected")
        while len(self._buffer) < n:
            chunk = sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def recv(self) -> tuple[int, bytes]:
        """Return (opcode, payload) of the next complete data frame; handles ping/close itself.

        Raises socket.timeout when nothing arrives within the socket timeout, WebSocketError on
        close / protocol problems.
        """
        while True:
            b1, b2 = self._read_exact(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                self.close(code)
                raise WebSocketError(f"closed by peer (code {code})")
            if opcode == 0x9:  # ping
                try:
                    self._send_frame(0xA, payload)
                except Exception:
                    pass
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x0:  # continuation
                self._fragments.append(payload)
                if not fin:
                    continue
                data = b"".join(self._fragments)
                op = self._fragment_opcode
                self._fragments, self._fragment_opcode = [], 0
                return op, data
            if not fin:
                self._fragments = [payload]
                self._fragment_opcode = opcode
                continue
            return opcode, payload


# =====================================================================================================
# Printer client (persistent connection, reader thread, request/response, writes)
# =====================================================================================================


def strip_host(print_host: str) -> str:
    """"http://192.168.0.128:4409/" -> "192.168.0.128" (the Creality link ignores the HTTP port)."""
    value = (print_host or "").strip()
    value = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value)
    value = value.split("/", 1)[0]
    if value.startswith("["):  # IPv6 literal
        return value.split("]", 1)[0] + "]"
    return value.split(":", 1)[0]


def fetch_printer_info(host: str, timeout: float = 3.0) -> dict[str, Any]:
    """GET http://<host>/info -> {"model": "K1 Max", "mac": ..., "version": ...}."""
    conn = http.client.HTTPConnection(host, HTTP_PORT, timeout=timeout)
    try:
        conn.request("GET", "/info", headers={"User-Agent": "OrcaSlicer-creality-cfs"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise WebSocketError(f"/info returned HTTP {resp.status}")
        data = json.loads(body.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    finally:
        conn.close()


Listener = Callable[["CrealityPrinterClient", dict], None]


class CrealityPrinterClient:
    """Keeps one WebSocket to a Creality K-series printer and tracks its CFS state.

    * `start()` spawns a reader thread that (re)connects, requests the initial state
      (boxsInfo, boxConfig, reqMaterials), answers heartbeats, folds every message into
      `snapshot` and notifies listeners.
    * `request()` sends a `get` and waits for the keys it expects.
    * `modify_material()` performs a verified slot write.
    """

    INIT_PARAMS = {"boxsInfo": 1, "boxConfig": 1, "reqMaterials": 1}

    def __init__(self, host: str, *, ws_port: int = WS_PORT, poll_interval: float = 15.0, log: logging.Logger = LOG):
        self.host = host
        self.ws_port = ws_port
        self.poll_interval = poll_interval
        self.log = log
        self._snapshot = PrinterSnapshot(host=host)
        self._lock = threading.RLock()
        self._ws: Optional[WebSocketClient] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._listeners: list[Listener] = []
        self._key_events: dict[str, threading.Event] = {}
        self._latest: dict[str, Any] = {}
        self._last_rx = 0.0
        self.reconnect_delay = 3.0
        self.message_log: list[tuple[float, str, str]] = []  # (ts, direction, text) for debugging (bounded)
        self.message_log_limit = 200

    # -- lifecycle --------------------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name=f"creality-cfs-{self.host}", daemon=True)
            self._thread.start()

    def stop(self, join: bool = True) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = self._thread
        if join and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def add_listener(self, fn: Listener) -> None:
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    @property
    def has_listeners(self) -> bool:
        with self._lock:
            return bool(self._listeners)

    def snapshot(self) -> PrinterSnapshot:
        with self._lock:
            return copy.deepcopy(self._snapshot)

    # -- low level ----------------------------------------------------------------------------------
    def _log_msg(self, direction: str, text: str) -> None:
        self.message_log.append((time.time(), direction, text[:4000]))
        if len(self.message_log) > self.message_log_limit:
            del self.message_log[: len(self.message_log) - self.message_log_limit]

    def send(self, method: str, params: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or not ws.connected:
            raise WebSocketError("printer not connected")
        text = json.dumps({"method": method, "params": params}, separators=(",", ":"))
        self._log_msg("tx", text)
        ws.send_text(text)

    def _wait_keys(self, keys: Iterable[str], timeout: float) -> dict[str, Any]:
        keys = list(keys)
        events = []
        with self._lock:
            for k in keys:
                ev = self._key_events.setdefault(k, threading.Event())
                ev.clear()
                events.append((k, ev))
        deadline = time.time() + timeout
        for k, ev in events:
            remaining = deadline - time.time()
            if remaining <= 0 or not ev.wait(remaining):
                break
        with self._lock:
            return {k: self._latest[k] for k in keys if k in self._latest}

    def request(self, params: dict[str, Any], expect: Iterable[str] | None = None, timeout: float = 6.0) -> dict[str, Any]:
        """Send a `get` request and return the expected top-level keys that arrived in time."""
        expect = list(expect) if expect is not None else list(params.keys())
        with self._lock:
            for k in expect:
                self._key_events.setdefault(k, threading.Event()).clear()
        self.send("get", params)
        return self._wait_keys(expect, timeout)

    # -- reader -------------------------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            ws = WebSocketClient(self.host, self.ws_port, timeout=self.poll_interval + 5.0)
            try:
                try:
                    info = fetch_printer_info(self.host)
                except Exception as exc:  # the /info endpoint is optional for the link itself
                    info = {}
                    self.log.debug("GET /info failed for %s: %s", self.host, exc)
                ws.connect()
                self._ws = ws
                with self._lock:
                    if info.get("model"):
                        self._snapshot.model = str(info["model"])
                    if info.get("mac"):
                        self._snapshot.mac = str(info["mac"])
                    self._snapshot.connected = True
                    self._snapshot.last_error = ""
                self.log.info("connected to %s (%s)", self.host, info.get("model") or "model unknown")
                self._notify({"__connected__": True})
                self.send("get", dict(self.INIT_PARAMS))
                next_poll = time.time() + self.poll_interval
                while not self._stop.is_set():
                    try:
                        opcode, payload = ws.recv()
                    except socket.timeout:
                        if time.time() - self._last_rx > self.poll_interval * 3 + 10:
                            raise WebSocketError("no traffic from printer, reconnecting")
                        self.send("get", {"boxsInfo": 1})
                        continue
                    self._last_rx = time.time()
                    if opcode not in (0x1, 0x2):
                        continue
                    text = payload.decode("utf-8", "replace")
                    if "heart_beat" in text and not text.lstrip().startswith("{"):
                        ws.send_text("ok")
                        continue
                    self._log_msg("rx", text)
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        if "heart_beat" in text:
                            ws.send_text("ok")
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if "heart_beat" in msg:
                        ws.send_text("ok")
                    self._handle(msg)
                    if time.time() >= next_poll:
                        next_poll = time.time() + self.poll_interval
                        try:
                            self.send("get", {"boxsInfo": 1})
                        except Exception:
                            pass
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.log.warning("printer link %s dropped: %s", self.host, exc)
                with self._lock:
                    self._snapshot.connected = False
                    self._snapshot.last_error = str(exc)
                self._notify({"__disconnected__": str(exc)})
                self._stop.wait(self.reconnect_delay)
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
                if self._ws is ws:
                    self._ws = None
        with self._lock:
            self._snapshot.connected = False

    def _handle(self, msg: dict) -> None:
        with self._lock:
            apply_status(self._snapshot, msg)
            for k, v in msg.items():
                self._latest[k] = v
                ev = self._key_events.get(k)
                if ev is not None:
                    ev.set()
        self._notify(msg)

    def _notify(self, msg: dict) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(self, msg)
            except Exception:
                self.log.exception("listener failed")

    # -- high level ---------------------------------------------------------------------------------
    def refresh(self, timeout: float = 6.0, include_library: bool = False) -> PrinterSnapshot:
        params: dict[str, Any] = {"boxsInfo": 1, "boxConfig": 1}
        if include_library:
            params["reqMaterials"] = 1
        expect = ["boxsInfo", "boxConfig"] + (["retMaterials"] if include_library else [])
        self.request(params, expect, timeout)
        return self.snapshot()

    def wait_connected(self, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.snapshot().connected:
                return True
            if self._stop.wait(0.1):
                return False
        return self.snapshot().connected

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Connected and the material boxes have been received at least once."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.snapshot()
            if snap.connected and snap.boxes:
                return True
            if self._stop.wait(0.1):
                return False
        snap = self.snapshot()
        return bool(snap.connected and snap.boxes)

    def check_writable(self, snapshot: Optional[PrinterSnapshot] = None) -> Optional[str]:
        """Return a human readable reason when slot writes should be refused, else None."""
        snap = snapshot or self.snapshot()
        if not snap.connected:
            return "printer is not connected"
        if snap.cfs_connected is False:
            return "no CFS is connected to the printer"
        if snap.device_state is None:
            return "printer state unknown yet, try again in a moment"
        if snap.device_state != DEVICE_STATE_IDLE:
            if snap.device_state == DEVICE_STATE_PRINTING:
                return "printer is printing; the CFS slot table is locked while a job runs"
            return f"printer is busy (deviceState={snap.device_state})"
        if snap.feed_state is not None and snap.feed_state not in FEED_STATES_IDLE:
            return "filament is being loaded or unloaded, please wait"
        return None

    def modify_material(
        self,
        box_id: int,
        slot_id: int,
        spec: MaterialSpec,
        *,
        force: bool = False,
        verify_timeout: float = 6.0,
    ) -> WriteResult:
        """Write a slot record and verify it by re-reading boxsInfo.

        The write is refused while the printer is printing / feeding unless `force` is set; this is
        the same interlock Creality Print's UI applies.
        """
        snap = self.snapshot()
        before = snap.find_slot(box_id, slot_id)
        if before is None:
            return WriteResult(False, box_id, slot_id, f"slot {box_id}/{slot_id} does not exist on this printer")
        if before.is_rfid_locked and not spec.is_clear:
            return WriteResult(False, box_id, slot_id, "slot holds a Creality RFID spool and is read-only")
        if not force:
            reason = self.check_writable(snap)
            if reason:
                return WriteResult(False, box_id, slot_id, f"refused: {reason}", before=asdict(before))
        params = spec.to_params(box_id, slot_id)
        with self._lock:
            self._key_events.setdefault("modifyMaterial", threading.Event()).clear()
            self._latest.pop("modifyMaterial", None)
            self._key_events.setdefault("boxsInfo", threading.Event()).clear()
        try:
            self.send("set", params)
        except Exception as exc:
            return WriteResult(False, box_id, slot_id, f"send failed: {exc}", before=asdict(before))
        ack = self._wait_keys(["modifyMaterial"], timeout=2.5)
        code = ack.get("modifyMaterial")
        # Re-read and compare. Some firmwares push boxsInfo on their own, others need the request.
        deadline = time.time() + verify_timeout
        after: Optional[CfsSlot] = None
        verified = False
        while time.time() < deadline:
            try:
                self.request({"boxsInfo": 1}, ["boxsInfo"], timeout=max(0.5, deadline - time.time()))
            except Exception as exc:
                return WriteResult(False, box_id, slot_id, f"verification read failed: {exc}", code, False, asdict(before))
            after = self.snapshot().find_slot(box_id, slot_id)
            if after is not None and _slot_matches(after, spec):
                verified = True
                break
            time.sleep(0.4)
        if verified:
            msg = "slot cleared" if spec.is_clear else f"slot set to {spec.vendor} {spec.name} ({spec.type}) {spec.color}"
            return WriteResult(True, box_id, slot_id, msg, code, True, asdict(before), asdict(after) if after else None)
        detail = f" (printer answered modifyMaterial={code!r})" if code is not None else " (no modifyMaterial acknowledgement)"
        return WriteResult(
            False,
            box_id,
            slot_id,
            "printer did not apply the change" + detail,
            code,
            False,
            asdict(before),
            asdict(after) if after else None,
        )

    def clear_slot(self, box_id: int, slot_id: int, *, force: bool = False) -> WriteResult:
        return self.modify_material(box_id, slot_id, MaterialSpec(), force=force)

    def send_gcode(self, gcode: str) -> None:
        for line in gcode.replace("\r", "").split("\n"):
            line = line.strip()
            if line:
                self.send("set", {"gcodeCmd": line})


def _slot_matches(slot: CfsSlot, spec: MaterialSpec) -> bool:
    if spec.is_clear:
        return not slot.is_loaded or (not slot.vendor and not slot.name and not slot.type)
    if _norm(slot.type) != _norm(spec.type) or _norm(slot.name) != _norm(spec.name):
        return False
    if spec.color and normalize_hex(slot.color, "") != normalize_hex(spec.color):
        return False
    if spec.vendor and _norm(slot.vendor) != _norm(spec.vendor):
        return False
    return True


# A process-wide registry so the printer agent and the manager panel share one connection per host.
_CLIENTS: dict[str, CrealityPrinterClient] = {}
_CLIENTS_LOCK = threading.Lock()


def get_client(host: str, start: bool = True) -> CrealityPrinterClient:
    host = strip_host(host)
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(host)
        if client is None:
            client = CrealityPrinterClient(host)
            _CLIENTS[host] = client
    if start:
        client.start()
    return client


def shutdown_clients() -> None:
    with _CLIENTS_LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for c in clients:
        c.stop(join=False)


# =====================================================================================================
# Project -> CFS synchronisation planning (pure, testable)
# =====================================================================================================


def project_signature(project: Iterable[dict]) -> str:
    """A stable string describing the project's filament list (preset names + colours)."""
    return json.dumps([[f.get("name", ""), normalize_hex(f.get("color", ""), "")] for f in project], separators=(",", ":"))


def plan_project_sync(
    project: list[dict],
    presets: Iterable[PresetInfo],
    snapshot: PrinterSnapshot,
    temps_from: str = "preset",
) -> list[tuple[CfsSlot, MaterialSpec]]:
    """Which CFS slots have to change so that slot i holds project filament i.

    Only slots whose record differs (material name/type/vendor or colour) are returned; RFID-locked
    slots are never touched; extra project filaments beyond the slot count are ignored.
    """
    slots = [s for box in snapshot.cfs_boxes for s in box.slots]
    presets = list(presets)
    plan: list[tuple[CfsSlot, MaterialSpec]] = []
    for i, f in enumerate(project):
        if i >= len(slots):
            break
        slot = slots[i]
        if slot.is_rfid_locked:
            continue
        preset = next((p for p in presets if p.name == f.get("name")), None)
        if preset is None:
            preset = PresetInfo(name=str(f.get("name") or ""), vendor=str(f.get("vendor") or ""), type=str(f.get("type") or ""))
        spec, _entry = spec_from_preset(preset, str(f.get("color") or ""), snapshot.library, temps_from)
        if _slot_matches(slot, spec) and slot.is_loaded:
            continue
        plan.append((slot, spec))
    return plan


# =====================================================================================================
# OrcaSlicer (Bambu-protocol) payload building - what MachineObject::parse_json understands
# =====================================================================================================


def _humidity_level(percent: Optional[float]) -> str:
    """Map a relative humidity reading onto Bambu's 1 (dry) .. 5 (wet) scale used by the AMS widget."""
    if percent is None:
        return "0"
    if percent < 20:
        return "1"
    if percent < 40:
        return "2"
    if percent < 60:
        return "3"
    if percent < 75:
        return "4"
    return "5"


def build_ams_json(snapshot: PrinterSnapshot, preset_for_slot: Callable[[CfsSlot], Optional[PresetInfo]] | None = None) -> dict:
    """Build the `print.ams` object OrcaSlicer's DevFilaSystemParser::ParseV1_0 expects.

    CFS boxes are renumbered 0,1,2.. in id order (the external spool holder, box type 1, is skipped
    exactly like OrcaSlicer's built-in CrealityPrintAgent does) and each physical slot becomes a tray.
    """
    ams_units = []
    ams_exist_bits = 0
    tray_exist_bits = 0
    for index, box in enumerate(snapshot.cfs_boxes):
        ams_exist_bits |= 1 << index
        trays = []
        slots_by_id = {s.slot_id: s for s in box.slots}
        slot_count = max(4, (max(slots_by_id) + 1) if slots_by_id else 4)
        for slot_id in range(slot_count):
            slot = slots_by_id.get(slot_id)
            tray: dict[str, Any] = {"id": str(slot_id), "tag_uid": "0000000000000000"}
            if slot is not None and slot.is_loaded:
                tray_exist_bits |= 1 << (index * 4 + slot_id)
                preset = preset_for_slot(slot) if preset_for_slot else None
                # OrcaSlicer skips trays whose id is EMPTY ("unknown spool") but falls back to the
                # "Generic <type>" preset for an id it does not know, so when the matching preset's
                # id is unavailable publish a non-empty sentinel (never 'P'-prefixed: that shape is
                # reserved for Bambu ids and triggers tray clean-up logic).
                if preset and preset.filament_id:
                    tray["tray_info_idx"] = preset.filament_id
                else:
                    tray["tray_info_idx"] = "CFS-" + (slot.rfid or normalize_filament_type(slot.type) or "UNKNOWN")
                tray["tray_type"] = normalize_filament_type(slot.type) or slot.type
                tray["tray_sub_brands"] = slot.name
                tray["tray_color"] = hex_to_bbl(slot.color) if slot.color else "FFFFFFFF"
                tray["tray_diameter"] = "1.75"
                tray["tray_weight"] = ""
                tray["remain"] = max(0, min(100, slot.percent)) if slot.percent else -1
                if slot.min_temp:
                    tray["nozzle_temp_min"] = str(slot.min_temp)
                if slot.max_temp:
                    tray["nozzle_temp_max"] = str(slot.max_temp)
                if slot.pressure:
                    tray["k"] = round(slot.pressure, 4)
                tray["cols"] = [tray["tray_color"]]
                tray["ctype"] = 0
            else:
                tray["tray_info_idx"] = ""
                tray["tray_type"] = ""
                tray["tray_color"] = "00000000"
                tray["tray_slot_placeholder"] = "1"
            trays.append(tray)
        unit: dict[str, Any] = {"id": str(index), "info": "0002", "tray": trays}
        if box.humidity is not None:
            unit["humidity"] = _humidity_level(box.humidity)
            unit["humidity_raw"] = str(int(box.humidity))
        if box.temp is not None:
            unit["temp"] = f"{box.temp:.1f}"
        ams_units.append(unit)
    return {
        "ams": ams_units,
        "ams_exist_bits": format(ams_exist_bits, "X"),
        "tray_exist_bits": format(tray_exist_bits, "X"),
        "tray_is_bbl_bits": "0",
        "tray_read_done_bits": format(tray_exist_bits, "X"),
        "tray_reading_bits": "0",
        "version": 1,
        "insert_flag": True,
        "power_on_flag": False,
    }


def _gcode_state(snapshot: PrinterSnapshot) -> str:
    if snapshot.device_state == DEVICE_STATE_PRINTING:
        return "RUNNING"
    if snapshot.device_state == 2:
        return "PAUSE" if snapshot.print_left_seconds else "FINISH"
    if snapshot.device_state == 5:
        return "PREPARE"
    return "IDLE"


def build_push_status(snapshot: PrinterSnapshot, sequence_id: int, preset_for_slot=None) -> dict:
    """Full `push_status` message (msg=0) describing the printer in Bambu's dialect."""
    p: dict[str, Any] = {
        "command": "push_status",
        "msg": 0,
        "sequence_id": str(sequence_id),
        "support_mqtt_alive": True,
        "gcode_state": _gcode_state(snapshot),
        "mc_print_stage": "2" if snapshot.device_state == DEVICE_STATE_PRINTING else "1",
        "mc_print_error_code": "0",
        "print_error": 0,
        "home_flag": 0,
        "support_bed_leveling": False,
        "support_send_to_sd": False,
        "ams": build_ams_json(snapshot, preset_for_slot),
    }
    if snapshot.nozzle_temp is not None:
        p["nozzle_temper"] = snapshot.nozzle_temp
    if snapshot.nozzle_target is not None:
        p["nozzle_target_temper"] = snapshot.nozzle_target
    if snapshot.bed_temp is not None:
        p["bed_temper"] = snapshot.bed_temp
    if snapshot.bed_target is not None:
        p["bed_target_temper"] = snapshot.bed_target
    if snapshot.box_temp is not None:
        p["chamber_temper"] = snapshot.box_temp
    if snapshot.print_progress is not None:
        p["mc_percent"] = snapshot.print_progress
    if snapshot.print_left_seconds is not None:
        p["mc_remaining_time"] = max(0, snapshot.print_left_seconds // 60)
    if snapshot.layer is not None:
        p["layer_num"] = snapshot.layer
    if snapshot.total_layers is not None:
        p["total_layer_num"] = snapshot.total_layers
    if snapshot.print_file:
        p["gcode_file"] = os.path.basename(snapshot.print_file)
        p["subtask_name"] = os.path.splitext(os.path.basename(snapshot.print_file))[0]
    return {"print": p}


def build_get_version(snapshot: PrinterSnapshot, sequence_id: int) -> dict:
    return {
        "info": {
            "command": "get_version",
            "sequence_id": str(sequence_id),
            "module": [
                {
                    "name": "ota",
                    "project_name": snapshot.model or "Creality",
                    "sw_ver": snapshot.firmware.split("DWIN sw ver:")[-1].rstrip(";") if snapshot.firmware else "1.0.0",
                    "hw_ver": snapshot.model or "",
                    "sn": snapshot.mac or "",
                }
            ],
        }
    }


def ams_setting_to_spec(
    print_cmd: dict,
    presets: Iterable[PresetInfo],
    library: Iterable[MaterialLibraryEntry],
) -> tuple[Optional[MaterialSpec], Optional[tuple[int, int]]]:
    """Translate OrcaSlicer's `ams_filament_setting` command into a slot write.

    Returns (spec, (box_index, slot_id)) where box_index is the AMS index (0-based CFS box order),
    or (None, None) when the command is malformed. A command with an empty tray_info_idx and type
    is a "clear" (that is what the AMS dialog's Reset button sends).
    """
    try:
        ams_id = int(print_cmd.get("ams_id", 0))
        slot_id = int(print_cmd.get("slot_id", print_cmd.get("tray_id", 0)))
    except (TypeError, ValueError):
        return None, None
    filament_id = str(print_cmd.get("tray_info_idx") or "")
    tray_type = str(print_cmd.get("tray_type") or "")
    color = str(print_cmd.get("tray_color") or "")
    if not filament_id and not tray_type:
        return MaterialSpec(), (ams_id, slot_id)
    preset = None
    for p in presets:
        if p.filament_id and p.filament_id == filament_id:
            preset = p
            break
    temp_min = _int(print_cmd.get("nozzle_temp_min"))
    temp_max = _int(print_cmd.get("nozzle_temp_max"))
    if preset is None:
        preset = PresetInfo(name=f"{tray_type}", type=tray_type, min_temp=temp_min, max_temp=temp_max)
    spec, _entry = spec_from_preset(preset, normalize_hex(color) if color else "", library)
    if temp_min:
        spec.min_temp = temp_min
    if temp_max:
        spec.max_temp = temp_max
    return spec, (ams_id, slot_id)


# =====================================================================================================
# OrcaSlicer vendor preset cache (.opc) reader
# =====================================================================================================
#
# Release builds of OrcaSlicer 2.5 ship (and keep in <datadir>/system) each vendor's presets as a
# binary cache: a 20-byte header (magic "ORCZ", format version, body size, CRC32) framing one cereal
# binary archive. The body is: format version (u32), vendor name, vendor profile version, the option
# dictionary (keys, option types, enum names), the vendor profile map, then the process / filament /
# machine presets, each with its own config diff, `inherits`, `setting_id` and `filament_id`. See
# src/libslic3r/PresetCacheFormat.{hpp,cpp} in OrcaSlicer. This reader mirrors the writer exactly,
# refuses any other format version, and is the only way to learn a system preset's filament_id when
# no JSON profile sources are installed.

OPC_MAGIC = 0x4F52435A
OPC_VERSION = 2

_CO_VECTOR = 0x4000
_CO_FLOAT, _CO_INT, _CO_STRING, _CO_PERCENT, _CO_FLOAT_OR_PERCENT, _CO_POINT, _CO_POINT3, _CO_BOOL, _CO_ENUM = 1, 2, 3, 4, 5, 6, 7, 8, 9
_CO_POINTS_GROUPS, _CO_INTS_GROUPS = 10 + _CO_VECTOR, 11 + _CO_VECTOR


class OpcFormatError(Exception):
    pass


class _BinaryReader:
    """Sequential reader for cereal's BinaryArchive (little endian, u64 size tags, 1-byte bools)."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def _take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.data):
            raise OpcFormatError("unexpected end of preset cache body")
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def u8(self) -> int:
        return self._take(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self._take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self._take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self._take(8))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self._take(8))[0]

    def size(self) -> int:
        n = self.u64()
        if n > 50_000_000:
            raise OpcFormatError("implausible size tag in preset cache")
        return n

    def string(self) -> str:
        n = self.size()
        return self._take(n).decode("utf-8", "replace")

    def strings(self) -> list[str]:
        return [self.string() for _ in range(self.size())]

    def skip(self, n: int) -> None:
        self._take(n)


@dataclass
class OpcPreset:
    name: str
    inherits: str = ""
    filament_id: str = ""
    setting_id: str = ""
    instantiation: str = ""
    vendor: str = ""
    type: str = ""
    min_temp: int = 0
    max_temp: int = 0
    pressure_advance: float = 0.0
    color: str = ""
    compatible_printers: list[str] = field(default_factory=list)


# Filament options the plugin extracts from a cached config diff (everything else is skipped).
_OPC_WANTED = {
    "filament_vendor",
    "filament_type",
    "nozzle_temperature_range_low",
    "nozzle_temperature_range_high",
    "pressure_advance",
    "default_filament_colour",
    "compatible_printers",
}


def _opc_read_option(r: _BinaryReader, opt_type: int) -> Any:
    """Read one option payload of the given ConfigOptionType; returns a Python value."""
    if opt_type in (_CO_ENUM, _CO_ENUM + _CO_VECTOR):
        values = []
        for _ in range(r.u32()):
            idx = r.u16()
            values.append(r.i32() if idx == 0 else idx)
        return values
    if opt_type == _CO_POINTS_GROUPS:
        groups = []
        for _ in range(r.size()):
            m = r.size()
            r.skip(16 * m)
            groups.append(m)
        return groups
    if opt_type == _CO_INTS_GROUPS:
        groups = []
        for _ in range(r.size()):
            m = r.size()
            groups.append([r.i32() for _ in range(m)])
        return groups
    is_vector = bool(opt_type & _CO_VECTOR)
    base = opt_type & ~_CO_VECTOR
    count = r.size() if is_vector else 1
    if base in (_CO_FLOAT, _CO_PERCENT):
        vals = [r.f64() for _ in range(count)]
    elif base == _CO_INT:
        vals = [r.i32() for _ in range(count)]
    elif base == _CO_STRING:
        vals = [r.string() for _ in range(count)]
    elif base == _CO_FLOAT_OR_PERCENT:
        vals = []
        for _ in range(count):
            v = r.f64()
            pct = r.u8()
            vals.append((v, bool(pct)))
    elif base == _CO_POINT:
        vals = [(r.f64(), r.f64()) for _ in range(count)]
    elif base == _CO_POINT3:
        vals = [(r.f64(), r.f64(), r.f64()) for _ in range(count)]
    elif base == _CO_BOOL:
        vals = [bool(r.u8()) for _ in range(count)]
    else:
        raise OpcFormatError(f"unknown option type {opt_type:#x} in preset cache")
    return vals if is_vector else vals[0]


def _opc_skip_vendor_map(r: _BinaryReader) -> list[str]:
    """Consume the VendorProfile map; returns the vendor names."""
    names = []
    for _ in range(r.size()):
        key = r.string()
        names.append(key)
        r.string()  # name
        r.string()  # id
        r.string()  # config_version (Semver saved as a string)
        r.string()  # config_update_url
        r.string()  # changelog_url
        for _m in range(r.size()):  # models
            r.string()  # id
            r.string()  # name
            r.string()  # model_id
            r.u8()  # technology (unsigned char enum)
            r.string()  # family
            for _v in range(r.size()):  # variants
                r.string()
            r.strings()  # default_materials
            r.strings()  # not_support_bed_types
            for _s in range(9):  # bed_model .. hotend_model
                r.string()
        r.strings()  # default_filaments (set)
        r.strings()  # default_sla_materials (set)
    return names


def _opc_read_entries(r: _BinaryReader, keys: list[str], types: list[int], want: bool) -> list[OpcPreset]:
    out: list[OpcPreset] = []
    for _ in range(r.u32()):
        name = r.string()
        r.string()  # sub_path
        preset = OpcPreset(name=name)
        for _o in range(r.u32()):
            idx = r.u16()
            if idx >= len(keys):
                raise OpcFormatError("option index past the end of the dictionary")
            value = _opc_read_option(r, types[idx])
            if want and keys[idx] in _OPC_WANTED:
                key = keys[idx]
                if key == "filament_vendor":
                    preset.vendor = str(_first(value) or "")
                elif key == "filament_type":
                    preset.type = str(_first(value) or "")
                elif key == "nozzle_temperature_range_low":
                    preset.min_temp = _int(_first(value))
                elif key == "nozzle_temperature_range_high":
                    preset.max_temp = _int(_first(value))
                elif key == "pressure_advance":
                    preset.pressure_advance = _num(_first(value))
                elif key == "default_filament_colour":
                    preset.color = normalize_hex(str(_first(value) or ""), "")
                elif key == "compatible_printers":
                    preset.compatible_printers = [str(v) for v in value] if isinstance(value, list) else []
        preset.inherits = r.string()
        r.strings()  # includes
        r.string()  # description
        preset.instantiation = r.string()
        preset.setting_id = r.string()
        preset.filament_id = r.string()
        r.strings()  # renamed_from
        if want:
            out.append(preset)
    return out


def read_opc_filaments(path: str) -> tuple[str, dict[str, OpcPreset]]:
    """Return (vendor_name, {preset name: OpcPreset}) for the filament presets in a .opc file.

    Raises OpcFormatError for anything that is not a version-2 cache with a valid CRC.
    """
    import zlib

    with open(path, "rb") as fh:
        blob = fh.read()
    if len(blob) < 20:
        raise OpcFormatError("file too small")
    magic, version, size, crc = struct.unpack("<IIQI", blob[:20])
    if magic != OPC_MAGIC:
        raise OpcFormatError("not an OrcaSlicer preset cache (bad magic)")
    if version != OPC_VERSION:
        raise OpcFormatError(f"unsupported preset cache format version {version}")
    body = blob[20 : 20 + size]
    if len(body) != size:
        raise OpcFormatError("truncated preset cache")
    if zlib.crc32(body) & 0xFFFFFFFF != crc:
        raise OpcFormatError("preset cache CRC mismatch")
    r = _BinaryReader(body)
    if r.u32() != OPC_VERSION:
        raise OpcFormatError("body version mismatch")
    vendor_name = r.string()
    r.string()  # vendor profile version
    keys = r.strings()
    types = [r.u16() for _ in range(r.size())]
    enum_values = r.strings()
    if len(keys) != len(types) or not enum_values or enum_values[0] != "":
        raise OpcFormatError("corrupt option dictionary")
    _opc_skip_vendor_map(r)
    _opc_read_entries(r, keys, types, want=False)  # process presets
    filaments = _opc_read_entries(r, keys, types, want=True)
    # machine presets and the parse-error counter follow; not needed.
    return vendor_name, {p.name: p for p in filaments}


def resolve_opc_chain(presets: dict[str, OpcPreset], name: str) -> Optional[OpcPreset]:
    """Fold a preset's `inherits` chain into one record (child values win), like OrcaSlicer does."""
    chain: list[OpcPreset] = []
    seen: set[str] = set()
    current = presets.get(name)
    while current is not None and current.name not in seen and len(chain) < 16:
        chain.append(current)
        seen.add(current.name)
        current = presets.get(current.inherits) if current.inherits else None
    if not chain:
        return None
    out = OpcPreset(name=name, inherits=chain[0].inherits, setting_id=chain[0].setting_id, instantiation=chain[0].instantiation)
    for p in chain:  # child first: keep the first non-empty value
        out.filament_id = out.filament_id or p.filament_id
        out.vendor = out.vendor or p.vendor
        out.type = out.type or p.type
        out.min_temp = out.min_temp or p.min_temp
        out.max_temp = out.max_temp or p.max_temp
        out.pressure_advance = out.pressure_advance or p.pressure_advance
        out.color = out.color or p.color
        out.compatible_printers = out.compatible_printers or p.compatible_printers
    return out


class OpcCatalog:
    """All filament presets from every .opc bundle found in the OrcaSlicer system directories."""

    def __init__(self) -> None:
        self._files: dict[str, tuple[float, dict[str, OpcPreset]]] = {}
        self._lock = threading.Lock()
        self.errors: dict[str, str] = {}

    @staticmethod
    def candidate_dirs(hints: Iterable[str] = ()) -> list[str]:
        """Directories that may hold <vendor>.opc files: <datadir>/system plus any hinted paths."""
        dirs: list[str] = []
        # The plugin itself lives in <datadir>/orca_plugins/<dir>/, which pins the data directory.
        here = os.path.dirname(os.path.abspath(__file__))
        for up in range(1, 4):
            base = here
            for _ in range(up):
                base = os.path.dirname(base)
            dirs.append(os.path.join(base, "system"))
        for hint in hints:
            if not hint:
                continue
            p = os.path.abspath(hint)
            for _ in range(5):
                dirs.append(os.path.join(p, "system"))
                parent = os.path.dirname(p)
                if parent == p:
                    break
                p = parent
        seen: set[str] = set()
        out = []
        for d in dirs:
            if d not in seen and os.path.isdir(d):
                seen.add(d)
                out.append(d)
        return out

    def load(self, hints: Iterable[str] = ()) -> dict[str, OpcPreset]:
        """Read (or re-read when changed) every .opc file and return the merged filament catalog."""
        merged: dict[str, OpcPreset] = {}
        for d in self.candidate_dirs(hints):
            try:
                names = sorted(f for f in os.listdir(d) if f.lower().endswith(".opc"))
            except OSError:
                continue
            for fn in names:
                path = os.path.join(d, fn)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                with self._lock:
                    hit = self._files.get(path)
                if hit is None or hit[0] != mtime:
                    try:
                        _vendor, presets = read_opc_filaments(path)
                        hit = (mtime, presets)
                        self.errors.pop(path, None)
                    except Exception as exc:
                        self.errors[path] = str(exc)
                        LOG.warning("could not read preset cache %s: %s", path, exc)
                        hit = (mtime, {})
                    with self._lock:
                        self._files[path] = hit
                for name, p in hit[1].items():
                    merged.setdefault(name, p)
        return merged


# =====================================================================================================
# Preset index (reads OrcaSlicer preset JSON files to recover filament_id, vendor, type, temps)
# =====================================================================================================


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def unserialize_strings(value: Any) -> list[str]:
    """Decode OrcaSlicer's serialized string-list form (`ConfigOptionStrings::serialize`).

    The Python host API hands config values back in their serialized text form: items separated by
    ';', an item quoted with C-style escapes when it contains spaces, quotes, backslashes or
    semicolons (`"Generic PLA @K1";Creality`). Already-decoded lists pass through unchanged.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    text = str(value)
    out: list[str] = []
    i, n = 0, len(text)
    while i <= n:
        if i == n:
            if out or text == "":
                pass
            break
        if text[i] == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                c = text[i]
                if c == "\\" and i + 1 < n:
                    i += 1
                    esc = text[i]
                    buf.append({"n": "\n", "r": "\r", "t": "\t"}.get(esc, esc))
                else:
                    buf.append(c)
                i += 1
            out.append("".join(buf))
            i += 1  # closing quote
            if i < n and text[i] == ";":
                i += 1
                if i == n:
                    out.append("")
            continue
        j = text.find(";", i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        i = j + 1
        if i == n:
            out.append("")
    return out


def unserialize_numbers(value: Any) -> list[float]:
    """Decode `ConfigOptionFloats` / `ConfigOptionInts` serialized form ("190,200" or "0.04")."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [_num(v) for v in value]
    if isinstance(value, (int, float)):
        return [float(value)]
    out = []
    for tok in str(value).replace(";", ",").split(","):
        tok = tok.strip().strip('"')
        if tok in ("", "nil"):
            continue
        if tok.endswith("%"):
            tok = tok[:-1]
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


def _load_preset_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class PresetIndex:
    """Builds PresetInfo records from (name, file) pairs by reading the preset JSON files.

    OrcaSlicer's Python API exposes a preset's config but not its `filament_id`, which lives only in
    the JSON file (and may be inherited from a parent preset). We resolve the `inherits` chain over
    all known files. Results are cached by file mtime.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self.opc = OpcCatalog()

    def _read(self, path: str) -> dict:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return {}
        with self._lock:
            hit = self._cache.get(path)
            if hit and hit[0] == mtime:
                return hit[1]
        data = _load_preset_json(path)
        with self._lock:
            self._cache[path] = (mtime, data)
        return data

    def build(self, presets: list[PresetInfo]) -> list[PresetInfo]:
        """Fill in filament_id / vendor / type / temps / PA from the files; returns the same list."""
        by_name: dict[str, PresetInfo] = {p.name: p for p in presets}
        data_by_name: dict[str, dict] = {}
        for p in presets:
            if p.file:
                data_by_name[p.name] = self._read(p.file)

        def resolve(name: str, key: str, depth: int = 0) -> Any:
            data = data_by_name.get(name)
            if data is None or depth > 12:
                return None
            if key in data and data[key] not in (None, "", [], "null"):
                return data[key]
            parent = data.get("inherits")
            if parent and parent != name:
                if parent not in data_by_name:
                    # Parent file not among the presets we were given (e.g. abstract fdm_filament_*):
                    # try to locate it next to the child.
                    child_file = by_name[name].file if name in by_name else ""
                    if child_file:
                        folder = os.path.dirname(child_file)
                        for candidate in (os.path.join(folder, parent + ".json"), os.path.join(os.path.dirname(folder), "filament", parent + ".json")):
                            if os.path.isfile(candidate):
                                data_by_name[parent] = self._read(candidate)
                                break
                        else:
                            # Search the folder tree once for "<parent>.json".
                            found = _find_file(folder, parent + ".json")
                            if found:
                                data_by_name[parent] = self._read(found)
                return resolve(parent, key, depth + 1) if parent in data_by_name else None
            return None

        for p in presets:
            if not p.file:
                continue
            fid = resolve(p.name, "filament_id")
            if isinstance(fid, str) and fid:
                p.filament_id = fid
            p.inherits = str(data_by_name.get(p.name, {}).get("inherits") or "")
            if not p.vendor:
                p.vendor = str(_first(resolve(p.name, "filament_vendor")) or "")
            if not p.type:
                p.type = str(_first(resolve(p.name, "filament_type")) or "")
            if not p.min_temp:
                p.min_temp = _int(_first(resolve(p.name, "nozzle_temperature_range_low")))
            if not p.max_temp:
                p.max_temp = _int(_first(resolve(p.name, "nozzle_temperature_range_high")))
            if not p.pressure_advance:
                p.pressure_advance = _num(_first(resolve(p.name, "pressure_advance")))
            if not p.color:
                col = _first(resolve(p.name, "default_filament_colour"))
                p.color = normalize_hex(col, "") if isinstance(col, str) and col.strip('"') else ""

        # System presets in release builds have no JSON behind them: take what is missing from the
        # installed vendor caches (.opc), which is OrcaSlicer's own built-in filament configuration.
        missing = [p for p in presets if not p.filament_id or not p.type or not p.vendor]
        if missing:
            hints = [p.file for p in presets if p.file]
            catalog = self.opc.load(hints)
            if catalog:
                hits = 0
                for p in missing:
                    rec = resolve_opc_chain(catalog, p.name)
                    if rec is None and p.inherits:
                        rec = resolve_opc_chain(catalog, p.inherits)
                    if rec is None:
                        continue
                    hits += 1
                    p.filament_id = p.filament_id or rec.filament_id
                    p.vendor = p.vendor or rec.vendor
                    p.type = p.type or rec.type
                    p.min_temp = p.min_temp or rec.min_temp
                    p.max_temp = p.max_temp or rec.max_temp
                    p.pressure_advance = p.pressure_advance or rec.pressure_advance
                    p.color = p.color or rec.color
                LOG.info("preset cache lookup: %d of %d presets completed from %d cached filament profiles", hits, len(missing), len(catalog))
        return presets


_FIND_CACHE: dict[tuple[str, str], str] = {}


def _find_file(start_dir: str, filename: str, max_up: int = 3) -> str:
    key = (start_dir, filename)
    if key in _FIND_CACHE:
        return _FIND_CACHE[key]
    folder = start_dir
    for _ in range(max_up + 1):
        for root, _dirs, files in os.walk(folder):
            if filename in files:
                _FIND_CACHE[key] = os.path.join(root, filename)
                return _FIND_CACHE[key]
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    _FIND_CACHE[key] = ""
    return ""


# =====================================================================================================
# OrcaSlicer integration
# =====================================================================================================

if orca is not None:  # pragma: no cover - requires the OrcaSlicer host

    # The host returns config values in OrcaSlicer's serialized text form (see unserialize_*).
    def _cfg_raw(preset, key: str):
        try:
            return preset.config_value(key)
        except Exception:
            return None

    def _cfg_strings(preset, key: str) -> list[str]:
        return unserialize_strings(_cfg_raw(preset, key))

    def _cfg_numbers(preset, key: str) -> list[float]:
        return unserialize_numbers(_cfg_raw(preset, key))

    def _cfg_string(preset, key: str, default: str = "") -> str:
        values = unserialize_strings(_cfg_raw(preset, key))
        return values[0] if values else default

    def _first_num(values: list[float]) -> float:
        return values[0] if values else 0.0

    def _collect_filament_presets() -> list[PresetInfo]:
        """UI-thread only: snapshot the filament presets into plain data (no file reads here)."""
        out: list[PresetInfo] = []
        bundle = orca.host.preset_bundle()
        filaments = bundle.filaments
        for name in filaments.preset_names():
            preset = filaments.find_preset(name)
            if preset is None or preset.is_default:
                continue
            colors = _cfg_strings(preset, "default_filament_colour")
            info = PresetInfo(
                name=preset.name,
                alias=preset.alias or "",
                file=preset.file or "",
                vendor=_cfg_string(preset, "filament_vendor"),
                type=_cfg_string(preset, "filament_type"),
                min_temp=int(_first_num(_cfg_numbers(preset, "nozzle_temperature_range_low"))),
                max_temp=int(_first_num(_cfg_numbers(preset, "nozzle_temperature_range_high"))),
                pressure_advance=_first_num(_cfg_numbers(preset, "pressure_advance")),
                color=normalize_hex(colors[0], "") if colors else "",
                is_system=bool(preset.is_system),
                is_user=bool(preset.is_user()),
                is_visible=bool(preset.is_visible),
                is_compatible=bool(preset.is_compatible),
            )
            out.append(info)
        return out

    def _full_strings(bundle, key: str) -> list[str]:
        try:
            return unserialize_strings(bundle.full_config_value(key))
        except Exception:
            return []

    def _collect_project_filaments() -> list[dict]:
        """UI-thread only: the filaments currently in the project sidebar, with their colours."""
        bundle = orca.host.preset_bundle()
        names = list(bundle.current_filament_preset_names())
        colors = _full_strings(bundle, "filament_colour")
        types = _full_strings(bundle, "filament_type")
        vendors = _full_strings(bundle, "filament_vendor")
        ids = _full_strings(bundle, "filament_ids")
        out = []
        for i, name in enumerate(names):
            out.append(
                {
                    "index": i,
                    "name": name,
                    "color": normalize_hex(colors[i], "#FFFFFF") if i < len(colors) else "#FFFFFF",
                    "type": types[i] if i < len(types) else "",
                    "vendor": vendors[i] if i < len(vendors) else "",
                    "filament_id": ids[i] if i < len(ids) else "",
                }
            )
        return out

    def _current_print_host() -> str:
        try:
            bundle = orca.host.preset_bundle()
            return strip_host(_cfg_string(bundle.current_printer_preset(), "print_host"))
        except Exception:
            return ""

    def _current_printer_model() -> str:
        try:
            bundle = orca.host.preset_bundle()
            return _cfg_string(bundle.current_printer_preset(), "printer_model")
        except Exception:
            return ""

    class _SharedState:
        """Preset data shared by the agent and the manager (filled on the UI thread, read anywhere)."""

        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.presets: list[PresetInfo] = []
            self.presets_built = False
            self.index = PresetIndex()
            self.queue_on_main: Optional[Callable[[Callable[[], None]], None]] = None
            self.config: dict[str, Any] = {}
            # preset name -> Orca filament_id, learned from the project's filaments. Release builds
            # ship system presets as binary .opc bundles, so this is the only source of ids for
            # them; it grows as the user puts presets into projects and is persisted in the config.
            self.known_ids: dict[str, str] = {}
            self.live_agent = None  # the CrealityCfsAgent instance while OrcaSlicer has it connected

        def learn_project_filament_ids(self) -> None:
            """UI thread: `filament_ids` in the full config carries the id of every project filament."""
            try:
                bundle = orca.host.preset_bundle()
                names = list(bundle.current_filament_preset_names())
                ids = unserialize_strings(bundle.full_config_value("filament_ids"))
            except Exception:
                LOG.debug("could not read project filament ids", exc_info=True)
                return
            with self.lock:
                for name, fid in zip(names, ids):
                    if name and fid and isinstance(fid, str):
                        self.known_ids[name] = fid
                for p in self.presets:
                    if not p.filament_id and p.name in self.known_ids:
                        p.filament_id = self.known_ids[p.name]

        def refresh_presets_from_host(self) -> None:
            """UI thread: re-snapshot presets, then resolve ids in the background."""
            try:
                presets = _collect_filament_presets()
            except Exception:
                LOG.exception("could not read filament presets from the host")
                return
            with self.lock:
                self.presets = presets
                self.presets_built = False
            self.learn_project_filament_ids()
            threading.Thread(target=self._build, args=(presets,), name="creality-cfs-presets", daemon=True).start()

        def _build(self, presets: list[PresetInfo]) -> None:
            try:
                self.index.build(presets)
            except Exception:
                LOG.exception("preset index build failed")
            with self.lock:
                for p in presets:
                    if not p.filament_id and p.name in self.known_ids:
                        p.filament_id = self.known_ids[p.name]
                    elif p.filament_id and p.name not in self.known_ids:
                        self.known_ids[p.name] = p.filament_id
                if self.presets is presets:
                    self.presets_built = True
            LOG.info("indexed %d filament presets (%d with filament_id)", len(presets), sum(1 for p in presets if p.filament_id))

        def get_presets(self) -> list[PresetInfo]:
            with self.lock:
                return list(self.presets)

        def preset_for_slot(self, slot: CfsSlot) -> Optional[PresetInfo]:
            return match_preset_for_slot(self.get_presets(), slot)

    SHARED = _SharedState()

    _PLUGIN_LOG_PATH = _attach_plugin_log_file()
    if _PLUGIN_LOG_PATH:
        LOG.info("plugin log file: %s", _PLUGIN_LOG_PATH)

    DEFAULT_CONFIG = {
        "temps_from": "preset",  # or "library": use the printer library's temperature range / PA
        "allow_write_while_printing": False,
        "poll_interval_seconds": 15,
        "auto_open_panel": False,
        "notify_on_connect": True,
        # Project -> CFS automatic sync: when the filament list in OrcaSlicer's sidebar changes
        # (preset selection or colour), write the changed slots to the printer (filament 1 -> 1A, ...).
        "auto_sync_project_to_cfs": True,
        "auto_sync_debounce_seconds": 2.0,
        "auto_sync_notify": True,
    }

    def _load_config(capability) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        try:
            raw = capability.get_config()
            if raw:
                data = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(data, dict):
                    cfg.update(data)
        except Exception:
            LOG.debug("no stored config for %s", type(capability).__name__)
        with SHARED.lock:
            SHARED.config = cfg
        return cfg

    # -------------------------------------------------------------------------------------------------
    # Printer agent capability
    # -------------------------------------------------------------------------------------------------

    class CrealityCfsAgent(orca.printer_agent.PrinterAgentBase):
        """IPrinterAgent implementation that speaks the Creality port-9999 protocol.

        OrcaSlicer selects it through Printer Settings > Advanced > "Printer Agent". It publishes the
        CFS slots as AMS trays (subscription mode, so the sidebar updates whenever the printer pushes
        a change) and translates the AMS filament-setting command into a slot write.
        """

        def __init__(self) -> None:
            super().__init__()
            self._dev_id = ""
            self._host = ""
            self._client: Optional[CrealityPrinterClient] = None
            self._sequence = 1
            self._on_local_connect = None
            self._on_local_message = None
            self._on_ssdp = None
            self._on_message = None
            self._on_user_message = None
            self._on_printer_connected = None
            self._on_subscribe_failure = None
            self._on_server_err = None
            self._connected_reported = False
            self._announced = False
            self._last_push = 0.0
            self._push_lock = threading.Lock()
            self._cfg = dict(DEFAULT_CONFIG)
            self._autosync: Optional[_AutoSync] = None

        # -- identity / lifecycle -------------------------------------------------------------------
        def get_name(self):
            return AGENT_NAME

        def get_agent_info(self):
            return orca.printer_agent.AgentInfo(AGENT_ID, AGENT_NAME, __version__, "Creality K-series CFS over LAN (port 9999)")

        def on_load(self):
            self._cfg = _load_config(self)
            self._autosync = AUTOSYNC
            AUTOSYNC.start()
            LOG.info("agent loaded (v%s)", __version__)

        def on_unload(self):
            AUTOSYNC.stop()
            self.disconnect_printer()
            SHARED.queue_on_main = None

        def _warn_if_switched_away(self):
            """OrcaSlicer selected another printer agent (typically because the printer preset embedded
            in an opened project has no Printer Agent). Tell the user how to keep live status."""
            if SHARED.live_agent is not None or SHARED.queue_on_main is None:
                return
            if getattr(self, "_switch_warned", False):
                return
            self._switch_warned = True
            LOG.warning("OrcaSlicer switched to another printer agent; filament auto-sync continues through the printer preset's address")
            _notify_main(
                "Creality CFS: the active printer preset no longer selects the 'Creality CFS (LAN)' agent "
                "(projects embed their own printer preset). Filament changes still sync to the CFS; for live "
                "printer status and the sidebar sync button, set Printer Settings > Advanced > Printer Agent and save the project.",
                level="warning",
            )

        def on_lifecycle_event(self, event, ctx):
            try:
                LE = orca.LifecycleEvent
                if event == LE.PresetSelected or event == LE.PresetSaved:
                    SHARED.refresh_presets_from_host()
                if event in (LE.ProjectOpened, LE.NewProject, LE.ProjectClosed):
                    AUTOSYNC.rebase()
                elif event in (LE.PresetSelected, LE.PresetSaved, LE.ProjectDirtyChanged, LE.ObjectChanged):
                    # Something in the project changed: take note now instead of waiting for the next poll.
                    AUTOSYNC.tick_now()
            except Exception:
                LOG.exception("lifecycle handler failed")

        def get_default_config(self):
            return dict(DEFAULT_CONFIG)

        # -- callbacks registered by OrcaSlicer ----------------------------------------------------
        def set_server_callback(self, fn):
            self._on_server_err = fn
            return ORCA_NETWORK_SUCCESS

        def set_on_ssdp_msg_fn(self, fn):
            self._on_ssdp = fn
            return ORCA_NETWORK_SUCCESS

        def set_on_printer_connected_fn(self, fn):
            self._on_printer_connected = fn
            return ORCA_NETWORK_SUCCESS

        def set_on_subscribe_failure_fn(self, fn):
            self._on_subscribe_failure = fn
            return ORCA_NETWORK_SUCCESS

        def set_on_message_fn(self, fn):
            self._on_message = fn
            return ORCA_NETWORK_SUCCESS

        def set_on_user_message_fn(self, fn):
            self._on_user_message = fn
            return ORCA_NETWORK_SUCCESS

        def set_on_local_connect_fn(self, fn):
            self._on_local_connect = fn
            return ORCA_NETWORK_SUCCESS

        def set_on_local_message_fn(self, fn):
            self._on_local_message = fn
            return ORCA_NETWORK_SUCCESS

        def set_queue_on_main_fn(self, fn):
            # OrcaSlicer clears every callback of an agent it switches away from (it passes an empty
            # function). The main-thread hop targets the application's event loop, which outlives any
            # agent selection, and the project->CFS watcher needs it even when another agent is live,
            # so keep the last valid hop instead of dropping it. It is released on unload.
            if fn is not None:
                SHARED.queue_on_main = fn
            else:
                LOG.info("host cleared the main-thread callback (agent switch); keeping the previous hop for the watcher")
            return ORCA_NETWORK_SUCCESS

        # -- connection -------------------------------------------------------------------------------
        def connect_printer(self, params):
            host = strip_host(params.host or "")
            if not params.dev_id or not host:
                LOG.error("connect_printer called without dev_id/host")
                return -1
            self.disconnect_printer()
            self._dev_id = params.dev_id
            self._host = host
            self._connected_reported = False
            self._announced = False
            self._cfg = _load_config(self)
            SHARED.refresh_presets_from_host()  # UI thread: we are called from MachineObject::connect()
            client = get_client(host)
            client.poll_interval = float(self._cfg.get("poll_interval_seconds") or 15)
            client.add_listener(self._on_printer_message)
            self._client = client
            SHARED.live_agent = self
            self._autosync = AUTOSYNC
            AUTOSYNC.start()
            LOG.info("connecting to %s as dev_id=%s", host, self._dev_id)
            snap = client.snapshot()
            if snap.connected:
                self._on_printer_message(client, {"__connected__": True})
            return ORCA_NETWORK_SUCCESS

        def disconnect_printer(self):
            # The project->CFS watcher keeps running (it does not need the agent to be selected);
            # only the status link to OrcaSlicer is dropped.
            if SHARED.live_agent is self:
                SHARED.live_agent = None
                threading.Timer(4.0, self._warn_if_switched_away).start()
            client = self._client
            if client is not None:
                client.remove_listener(self._on_printer_message)
                self._client = None
                if not client.has_listeners:
                    # Keep the link for a while in case the manager panel or a re-select needs it;
                    # a dedicated timer tears it down when nobody is interested anymore.
                    threading.Timer(90.0, _stop_if_idle, args=(client,)).start()
            self._dev_id = ""
            return ORCA_NETWORK_SUCCESS

        def _on_printer_message(self, client: CrealityPrinterClient, msg: dict) -> None:
            """Reader-thread callback: forward connection state and status to OrcaSlicer."""
            if not self._dev_id:
                return
            if "__disconnected__" in msg:
                self._connected_reported = False
                if self._on_local_connect:
                    try:
                        self._on_local_connect(2, self._dev_id, str(msg["__disconnected__"]))  # ConnectStatusLost
                    except Exception:
                        LOG.exception("on_local_connect failed")
                return
            if "__connected__" in msg or not self._connected_reported:
                snap = client.snapshot()
                if not snap.connected:
                    return
                self._connected_reported = True
                self._announce(snap)
                if self._on_local_connect:
                    try:
                        self._on_local_connect(0, self._dev_id, "0")  # ConnectStatusOk
                    except Exception:
                        LOG.exception("on_local_connect failed")
                self._publish_status(force=True)
                return
            interesting = {"boxsInfo", "deviceState", "state", "nozzleTemp", "bedTemp0", "printProgress", "boxConfig"} & set(msg.keys())
            if interesting:
                self._publish_status(force="boxsInfo" in msg)

        def _announce(self, snap: PrinterSnapshot) -> None:
            if self._announced or not self._on_ssdp:
                return
            payload = {
                "dev_name": snap.hostname or snap.model or self._host,
                "dev_id": self._dev_id,
                "dev_ip": self._host,
                "dev_type": _current_printer_model() or snap.model or "Creality",
                "dev_signal": "0",
                "connect_type": "lan",
                "bind_state": "free",
                "sec_link": "secure",
            }
            try:
                self._on_ssdp(json.dumps(payload))
                self._announced = True
            except Exception:
                LOG.exception("ssdp announce failed")

        def _next_seq(self) -> int:
            self._sequence += 1
            return self._sequence

        def _publish(self, message: dict) -> None:
            if not self._on_local_message or not self._dev_id:
                return
            try:
                self._on_local_message(self._dev_id, json.dumps(message))
            except Exception:
                LOG.exception("on_local_message failed")

        def _publish_status(self, force: bool = False) -> None:
            client = self._client
            if client is None:
                return
            with self._push_lock:
                now = time.time()
                if not force and now - self._last_push < 2.0:
                    return
                self._last_push = now
                snap = client.snapshot()
                self._publish(build_push_status(snap, self._next_seq(), SHARED.preset_for_slot))

        # -- commands from OrcaSlicer -------------------------------------------------------------------
        def send_message(self, dev_id, json_str, qos, flag):
            # No cloud relay for Creality LAN printers; treat like a LAN message.
            return self.send_message_to_printer(dev_id, json_str, qos, flag)

        def send_message_to_printer(self, dev_id, json_str, qos, flag):
            client = self._client
            if client is None:
                return -1
            try:
                msg = json.loads(json_str)
            except ValueError:
                LOG.warning("ignoring non-JSON command: %r", json_str[:200])
                return -1
            if not isinstance(msg, dict):
                return -1
            if "pushing" in msg:  # {"pushing": {"command": "pushall"}}
                self._publish_status(force=True)
                return ORCA_NETWORK_SUCCESS
            if "info" in msg and isinstance(msg["info"], dict) and msg["info"].get("command") == "get_version":
                self._publish(build_get_version(client.snapshot(), self._next_seq()))
                return ORCA_NETWORK_SUCCESS
            if "print" in msg and isinstance(msg["print"], dict):
                return self._handle_print_command(msg["print"])
            if "system" in msg:
                return ORCA_NETWORK_SUCCESS  # ledctrl & friends: silently accepted
            return ORCA_NETWORK_ERR_CMD_NOT_SUPPORTED

        def _handle_print_command(self, cmd: dict) -> int:
            client = self._client
            if client is None:
                return -1
            command = str(cmd.get("command") or "")
            if command == "ams_filament_setting":
                presets = SHARED.get_presets()
                library = client.snapshot().library
                spec, pos = ams_setting_to_spec(cmd, presets, library)
                if spec is None or pos is None:
                    return -1
                ams_index, slot_id = pos
                snap = client.snapshot()
                boxes = snap.cfs_boxes
                if ams_index >= len(boxes):
                    LOG.warning("ams_filament_setting for unknown AMS index %d", ams_index)
                    return ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE
                box_id = boxes[ams_index].id
                force = bool(self._cfg.get("allow_write_while_printing"))

                def worker():
                    result = client.modify_material(box_id, slot_id, spec, force=force)
                    LOG.info("ams_filament_setting -> slot %s/%s: %s", box_id, slot_id, result.message)
                    self._publish_status(force=True)
                    if not result.ok:
                        _notify_main(f"Creality CFS: {result.message}", level="error")

                threading.Thread(target=worker, name="creality-cfs-write", daemon=True).start()
                return ORCA_NETWORK_SUCCESS
            if command == "gcode_line":
                gcode = str(cmd.get("param") or "")
                if not gcode.strip():
                    return -1
                try:
                    client.send_gcode(gcode)
                    return ORCA_NETWORK_SUCCESS
                except Exception as exc:
                    LOG.warning("gcode_line failed: %s", exc)
                    return -1
            if command in ("pause", "resume", "stop"):
                params = {"pause": 1} if command == "pause" else {"pause": 0} if command == "resume" else {"stop": 1}
                try:
                    client.send("set", params)
                    return ORCA_NETWORK_SUCCESS
                except Exception as exc:
                    LOG.warning("%s failed: %s", command, exc)
                    return -1
            if command in ("set_bed_temp", "set_nozzle_temp"):
                try:
                    temp = int(cmd.get("temp", 0))
                    if command == "set_bed_temp":
                        client.send("set", {"bedTempControl": {"num": 0, "val": temp}})
                    else:
                        client.send("set", {"nozzleTempControl": temp})
                    return ORCA_NETWORK_SUCCESS
                except Exception as exc:
                    LOG.warning("%s failed: %s", command, exc)
                    return -1
            if command in ("push_status", "get_version"):
                self._publish_status(force=True)
                return ORCA_NETWORK_SUCCESS
            LOG.info("print command not supported by the Creality link: %s", command)
            return ORCA_NETWORK_ERR_CMD_NOT_SUPPORTED

        # -- discovery / binding ----------------------------------------------------------------------
        def start_discovery(self, start, sending):
            if start and self._client is not None:
                snap = self._client.snapshot()
                if snap.connected:
                    self._announce(snap)
            return True

        def bind_detect(self, dev_ip, sec_link, detect):
            snap = self._client.snapshot() if self._client else PrinterSnapshot()
            detect.dev_id = self._dev_id or dev_ip
            detect.model_id = _current_printer_model() or snap.model
            detect.dev_name = snap.hostname or dev_ip
            detect.version = snap.firmware
            detect.connect_type = "lan"
            detect.bind_state = "free"
            return ORCA_NETWORK_SUCCESS

        def check_cert(self):
            return ORCA_NETWORK_SUCCESS

        def ping_bind(self, ping_code):
            return ORCA_NETWORK_SUCCESS

        def bind(self, dev_ip, dev_id, dev_model, sec_link, timezone, improved, update_fn):
            return ORCA_NETWORK_SUCCESS

        def unbind(self, dev_id):
            return ORCA_NETWORK_SUCCESS

        def request_bind_ticket(self):
            return (ORCA_NETWORK_SUCCESS, "")

        def get_user_selected_machine(self):
            return self._dev_id

        def set_user_selected_machine(self, dev_id):
            return ORCA_NETWORK_SUCCESS

        # -- filament sync --------------------------------------------------------------------------
        def get_filament_sync_mode(self):
            # The host asks this on its UI thread after every status message it parses, which makes it
            # a dependable heartbeat for the project->CFS watcher (see _AutoSync.host_hook).
            if self._autosync is not None:
                self._autosync.host_hook()
            return orca.printer_agent.FilamentSyncMode.Subscription

        def fetch_filament_info(self, dev_id, sync_mode):
            # Subscription mode: the sidebar already has the data; re-publish for good measure.
            if self._autosync is not None:
                self._autosync.host_hook()
            self._publish_status(force=True)
            return self._client is not None

        def get_camera_stream_mode(self):
            return orca.printer_agent.CameraStreamMode.None_

        def get_camera_url(self):
            return ""

        # -- print jobs: uploads go through OrcaSlicer's Creality PrintHost, not the agent --------------
        def start_print(self, params, update_fn, cancel_fn, wait_fn):
            return ORCA_NETWORK_ERR_CMD_NOT_SUPPORTED

        def start_local_print_with_record(self, params, update_fn, cancel_fn, wait_fn):
            return ORCA_NETWORK_ERR_CMD_NOT_SUPPORTED

        def start_send_gcode_to_sdcard(self, params, update_fn, cancel_fn, wait_fn):
            return ORCA_NETWORK_ERR_CMD_NOT_SUPPORTED

        def start_local_print(self, params, update_fn, cancel_fn):
            return ORCA_NETWORK_ERR_CMD_NOT_SUPPORTED

        def start_sdcard_print(self, params, update_fn, cancel_fn):
            return ORCA_NETWORK_ERR_CMD_NOT_SUPPORTED

    def _stop_if_idle(client: CrealityPrinterClient) -> None:
        if not client.has_listeners and not _MANAGER_USES.get(client.host):
            LOG.info("closing idle printer link %s", client.host)
            client.stop(join=False)
            with _CLIENTS_LOCK:
                if _CLIENTS.get(client.host) is client:
                    _CLIENTS.pop(client.host, None)

    _MANAGER_USES: dict[str, bool] = {}

    def _notify_main(text: str, level: str = "info") -> None:
        """Show a plater notification from any thread (hops to the UI thread when possible)."""

        def show():
            try:
                levels = orca.host.ui.NotificationLevel
                lvl = {
                    "error": levels.ErrorNotificationLevel,
                    "warning": levels.WarningNotificationLevel,
                }.get(level, levels.RegularNotificationLevel)
                orca.host.ui.push_notification(lvl, text)
            except Exception:
                LOG.info("notification (fallback): %s", text, exc_info=True)

        q = SHARED.queue_on_main
        if q is not None:
            try:
                q(show)
                return
            except Exception:
                pass
        if threading.current_thread() is threading.main_thread():
            show()
        else:
            LOG.info("notification (no UI thread hop available): %s", text)

    class _AutoSync:
        """Keeps the CFS slots in step with OrcaSlicer's sidebar filament list.

        A background thread asks the host to run `_tick` on the UI thread every couple of seconds
        (host objects may only be touched there). The tick snapshots the project's filaments; once the
        list has been stable for the debounce period and differs from what was last applied, the
        differing slots are written from a worker thread. While the printer is busy the change stays
        pending and is applied as soon as it is idle again.
        """

        INTERVAL = 2.0

        def __init__(self) -> None:
            self._stop = threading.Event()
            self._thread: Optional[threading.Thread] = None
            self._last_sig: Optional[str] = None  # signature last applied (or seen at start)
            self._pending_sig: Optional[str] = None
            self._pending_since = 0.0
            self._pending_project: list[dict] = []
            self._blocked_notified = False
            self._writing = False
            self._lock = threading.Lock()
            self.last_tick = 0.0  # when _tick last ran on the UI thread (0 = never)
            self._ticks = 0
            self._ticks_queue = 0
            self._ticks_hook = 0
            self._queue_failures = 0
            self._reported_no_ticks = False
            self._held_client: Optional[CrealityPrinterClient] = None

        def _keepalive(self, _client: CrealityPrinterClient, _msg: dict) -> None:
            """Registered as a listener so the printer link is not closed as idle while we watch."""

        def _resolve_client(self) -> Optional[CrealityPrinterClient]:
            """UI thread: the live agent's link if the plugin agent is selected, else a link to the
            printer named in the current printer preset (OrcaSlicer may have switched agents, e.g.
            because a project's embedded preset carries no Printer Agent)."""
            agent = SHARED.live_agent
            client = agent._client if agent is not None else None
            if client is None:
                host = _current_print_host()
                if not host:
                    return None
                client = get_client(host)
            if client is not self._held_client:
                if self._held_client is not None:
                    self._held_client.remove_listener(self._keepalive)
                client.add_listener(self._keepalive)
                self._held_client = client
            return client

        def _tick_from_queue(self) -> None:
            self._ticks_queue += 1
            self._tick()

        def start(self) -> None:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="creality-cfs-autosync", daemon=True)
            self._thread.start()
            LOG.info("auto-sync watcher armed (queue_on_main available: %s)", SHARED.queue_on_main is not None)

        def stop(self) -> None:
            self._stop.set()

        def _run(self) -> None:
            started = time.time()
            last_report = time.time()
            while not self._stop.wait(self.INTERVAL):
                if time.time() - last_report > 30:
                    last_report = time.time()
                    LOG.info("auto-sync: alive, ticks=%d (queued=%d, host-callback=%d), pending=%s",
                             self._ticks, self._ticks_queue, self._ticks_hook, self._pending_sig is not None)
                q = SHARED.queue_on_main
                if q is not None:
                    try:
                        q(self._tick_from_queue)
                    except Exception:
                        self._queue_failures += 1
                        if self._queue_failures <= 3:
                            LOG.warning("auto-sync: queue_on_main call failed", exc_info=True)
                if not self._reported_no_ticks and self._ticks == 0 and time.time() - started > 12:
                    self._reported_no_ticks = True
                    LOG.warning(
                        "auto-sync: no UI-thread tick in 12s (queue_on_main %s); relying on host callbacks",
                        "missing" if q is None else "not delivering",
                    )

        def rebase(self) -> None:
            """UI thread: a project was opened or created - its filament list is the new baseline.

            Only edits made after this point are synced; opening a project never rewrites the CFS.
            """
            self._last_sig = None
            self._pending_sig = None
            self._pending_project = []
            LOG.info("auto-sync: project changed, taking its filament list as the new baseline")

        def tick_now(self) -> None:
            """UI thread: take note of a change right away (used on PresetSelected).

            The write itself still waits for the debounce period, so stepping through several presets
            in the dropdown results in one write, not one per step.
            """
            self._tick()

        def host_hook(self) -> None:
            """Called from agent methods the host invokes on its UI thread (get_filament_sync_mode,
            fetch_filament_info). Gives the watcher a UI-thread heartbeat even when the queue callback
            is unavailable; rate limited so it stays cheap."""
            if threading.current_thread() is not threading.main_thread():
                return
            if time.time() - self.last_tick < 1.5:
                return
            self._ticks_hook += 1
            self._tick()

        # -- UI thread -------------------------------------------------------------------------------------
        def _tick(self, force_debounce: bool = False) -> None:
            try:
                self._tick_impl(force_debounce)
            except Exception:
                LOG.exception("auto-sync tick failed")

        def _tick_impl(self, force_debounce: bool = False) -> None:
            if self._stop.is_set():
                return
            self.last_tick = time.time()
            self._ticks += 1
            if self._ticks == 1:
                LOG.info("auto-sync: first UI-thread tick (main thread: %s)", threading.current_thread() is threading.main_thread())
            cfg = SHARED.config or dict(DEFAULT_CONFIG)
            if not cfg.get("auto_sync_project_to_cfs", True):
                return
            client = self._resolve_client()
            if client is None:
                return
            try:
                project = _collect_project_filaments()
                SHARED.learn_project_filament_ids()
            except Exception:
                LOG.debug("auto-sync: project snapshot failed", exc_info=True)
                return
            sig = project_signature(project)
            if self._last_sig is None:
                # First look: adopt the current state as the baseline; only later changes sync.
                self._last_sig = sig
                LOG.info("auto-sync: baseline recorded (%d project filaments)", len(project))
                return
            if sig == self._last_sig and self._pending_sig is None:
                return
            now = time.time()
            if sig != self._pending_sig:
                self._pending_sig = sig
                self._pending_since = now
                self._pending_project = project
                self._blocked_notified = False
                LOG.info("auto-sync: project filament list changed, syncing after %.1fs of quiet", float(cfg.get("auto_sync_debounce_seconds") or 2.0))
                if not force_debounce:
                    return
            debounce = float(cfg.get("auto_sync_debounce_seconds") or 2.0)
            if not force_debounce and now - self._pending_since < debounce:
                return
            if sig == self._last_sig:
                self._pending_sig = None
                return
            with self._lock:
                if self._writing:
                    return
            snap = client.snapshot()
            if not snap.connected or not snap.boxes:
                return
            force = bool(cfg.get("allow_write_while_printing"))
            reason = client.check_writable(snap)
            if reason and not force:
                if not self._blocked_notified and cfg.get("auto_sync_notify", True):
                    _notify_main(f"Creality CFS: filament change queued, {reason}.", level="warning")
                    self._blocked_notified = True
                return
            plan = plan_project_sync(self._pending_project, SHARED.get_presets(), snap, str(cfg.get("temps_from") or "preset"))
            self._last_sig = sig
            self._pending_sig = None
            if not plan:
                LOG.info("auto-sync: project filaments changed but the CFS already matches")
                return
            with self._lock:
                self._writing = True
            threading.Thread(target=self._apply, args=(client, plan, force), name="creality-cfs-autosync-write", daemon=True).start()

        # -- worker thread -----------------------------------------------------------------------------------
        def _apply(self, client: CrealityPrinterClient, plan: list[tuple[CfsSlot, MaterialSpec]], force: bool) -> None:
            results = []
            try:
                for slot, spec in plan:
                    result = client.modify_material(slot.box_id, slot.slot_id, spec, force=force)
                    results.append((slot, spec, result))
                    LOG.info("auto-sync %s: %s", slot.label, result.message)
            finally:
                with self._lock:
                    self._writing = False
            agent = SHARED.live_agent
            if agent is not None:
                agent._publish_status(force=True)
            if (SHARED.config or DEFAULT_CONFIG).get("auto_sync_notify", True):
                ok = [f"{s.label} {'cleared' if sp.is_clear else sp.name}" for s, sp, r in results if r.ok]
                bad = [f"{s.label}: {r.message}" for s, _sp, r in results if not r.ok]
                if ok:
                    _notify_main("Creality CFS updated: " + ", ".join(ok))
                if bad:
                    _notify_main("Creality CFS sync failed for " + "; ".join(bad), level="error")

    # One watcher for the whole plugin: started when the plugin loads, independent of which printer
    # agent OrcaSlicer currently has selected.
    AUTOSYNC = _AutoSync()

    # -------------------------------------------------------------------------------------------------
    # Manager panel capability
    # -------------------------------------------------------------------------------------------------

    PANEL_HTML = r"""
<style>
  body { margin: 0; padding: 10px 12px; font-size: 12.5px; }
  h3 { margin: 0 0 2px; font-size: 14px; }
  .muted { color: var(--orca-muted); font-size: 11.5px; }
  .row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin: 8px 0; }
  button { font-size: 12px; padding: 3px 9px; }
  button.quiet { background: transparent; color: var(--orca-fg); border: 1px solid var(--orca-border); }
  button.danger { background: #b23b3b; color: #fff; border-color: #b23b3b; }
  table { width: 100%; border-collapse: collapse; margin-top: 6px; }
  th, td { text-align: left; padding: 4px 4px; border-bottom: 1px solid var(--orca-border); vertical-align: middle; }
  th { font-weight: 600; font-size: 11.5px; color: var(--orca-muted); }
  .swatch { display: inline-block; width: 16px; height: 16px; border-radius: 3px; border: 1px solid var(--orca-border); vertical-align: middle; margin-right: 6px; }
  .slot { font-weight: 600; }
  .pill { display: inline-block; padding: 0 6px; border-radius: 9px; font-size: 10.5px; border: 1px solid var(--orca-border); color: var(--orca-muted); }
  .pill.ok { border-color: #2e9e5b; color: #2e9e5b; }
  .pill.bad { border-color: #b23b3b; color: #b23b3b; }
  .pill.warn { border-color: #c98a1a; color: #c98a1a; }
  select, input[type=text] { font-size: 12px; max-width: 100%; }
  input[type=color] { width: 28px; height: 22px; padding: 0; border: 1px solid var(--orca-border); background: transparent; vertical-align: middle; }
  #log { margin-top: 8px; max-height: 110px; overflow: auto; font-size: 11px; color: var(--orca-muted); white-space: pre-wrap; }
  details { margin-top: 8px; }
  summary { cursor: pointer; color: var(--orca-muted); font-size: 11.5px; }
  .grid { display: grid; grid-template-columns: auto 1fr; gap: 3px 8px; font-size: 11.5px; }
  .hostbox { display:flex; gap:6px; align-items:center; }
  .hostbox input { width: 140px; }
</style>

<h3>Creality CFS Manager</h3>
<div class="muted" id="subtitle">Connecting...</div>

<div class="row">
  <span class="hostbox"><span class="muted">Printer</span> <input id="host" type="text" placeholder="192.168.0.x"/> <button id="connect" class="quiet">Connect</button></span>
  <button id="refresh" class="quiet">Refresh</button>
  <button id="syncall" title="Write project filament 1..N into CFS slots 1A..1D in order">Project &rarr; CFS</button>
</div>

<table>
  <thead><tr><th>Slot</th><th>On printer</th><th>Write</th><th></th></tr></thead>
  <tbody id="rows"></tbody>
</table>

<details>
  <summary>Printer</summary>
  <div class="grid" id="printer"></div>
</details>

<details>
  <summary>Project filaments</summary>
  <div id="project" class="grid"></div>
</details>

<div id="log"></div>

<script>
(function () {
  var state = null;
  var choices = {};   // slot key -> currently chosen write source (persisted across polls)
  var colors = {};    // slot key -> chosen color override

  function esc(v) { var s = document.createElement("span"); s.textContent = (v === undefined || v === null) ? "" : String(v); return s.innerHTML; }
  function key(b, s) { return b + ":" + s; }

  function pill(text, cls) { return '<span class="pill ' + (cls || "") + '">' + esc(text) + '</span>'; }

  function sourceOptions(slotKey, slot) {
    var html = '<option value="">- choose -</option>';
    html += '<optgroup label="Project filaments">';
    (state.project || []).forEach(function (f) {
      var v = "project:" + f.index;
      html += '<option value="' + v + '"' + (choices[slotKey] === v ? " selected" : "") + '>' + esc((f.index + 1) + ". " + f.name) + '</option>';
    });
    html += '</optgroup><optgroup label="Printer material library">';
    (state.library || []).forEach(function (m) {
      var v = "library:" + m.id;
      html += '<option value="' + v + '"' + (choices[slotKey] === v ? " selected" : "") + '>' + esc(m.brand + " " + m.name + " (" + m.type + ")") + '</option>';
    });
    html += '</optgroup>';
    return html;
  }

  function render() {
    if (!state) return;
    var p = state.printer || {};
    var sub = document.getElementById("subtitle");
    if (!p.connected) {
      sub.innerHTML = pill("offline", "bad") + " " + esc(p.host || "") + (p.last_error ? " - " + esc(p.last_error) : "");
    } else {
      var st = p.device_state === 0 ? pill("idle", "ok") : p.device_state === 1 ? pill("printing - writes locked", "warn") : pill("busy (" + p.device_state + ")", "warn");
      sub.innerHTML = esc((p.model || "Creality") + (p.hostname ? " · " + p.hostname : "") + " · " + p.host) + " " + st +
        (p.cfs_connected === false ? " " + pill("no CFS", "bad") : "");
    }
    var hostInput = document.getElementById("host");
    if (document.activeElement !== hostInput && p.host && !hostInput.value) hostInput.value = p.host;

    var rows = document.getElementById("rows");
    var html = "";
    (state.slots || []).forEach(function (s) {
      var k = key(s.box_id, s.slot_id);
      var onPrinter = s.loaded
        ? '<span class="swatch" style="background:' + esc(s.color || "#ffffff") + '"></span>' + esc((s.vendor ? s.vendor + " " : "") + s.name) +
          '<div class="muted">' + esc(s.type) + (s.min_temp ? " · " + s.min_temp + "-" + s.max_temp + "°C" : "") + (s.pressure ? " · PA " + s.pressure : "") +
          (s.percent !== null && s.percent !== undefined && s.percent !== 100 ? " · " + s.percent + "%" : "") +
          (s.selected ? " · active" : "") + (s.rfid_locked ? " · RFID" : "") +
          (s.matched_preset ? '<br/>&rarr; ' + esc(s.matched_preset) : "") + '</div>'
        : '<span class="muted">empty</span>';
      var chosen = choices[k] || "";
      var col = colors[k] || s.suggested_color || s.color || "#ffffff";
      html += '<tr>' +
        '<td class="slot">' + esc(s.label) + '</td>' +
        '<td>' + onPrinter + '</td>' +
        '<td><select data-k="' + k + '" class="src">' + sourceOptions(k, s) + '</select> ' +
          '<input type="color" class="col" data-k="' + k + '" value="' + esc(col) + '" title="Colour to write"/></td>' +
        '<td style="white-space:nowrap">' +
          '<button class="apply" data-b="' + s.box_id + '" data-s="' + s.slot_id + '"' + (s.rfid_locked ? " disabled" : "") + '>Write</button> ' +
          '<button class="clear quiet" data-b="' + s.box_id + '" data-s="' + s.slot_id + '"' + (s.loaded ? "" : " disabled") + '>Clear</button>' +
        '</td></tr>';
    });
    if (!html) html = '<tr><td colspan="4" class="muted">No CFS slots reported yet.</td></tr>';
    rows.innerHTML = html;

    var pr = document.getElementById("printer");
    var items = [["Model", p.model], ["Hostname", p.hostname], ["Firmware", p.firmware], ["State", p.device_state], ["CFS", p.cfs_connected], ["Printing", p.print_file], ["Progress", p.print_progress !== null && p.print_progress !== undefined ? p.print_progress + "%" : ""], ["Layer", p.layer !== null && p.total_layers ? p.layer + "/" + p.total_layers : ""], ["Nozzle", p.nozzle_temp !== null && p.nozzle_temp !== undefined ? p.nozzle_temp + " / " + p.nozzle_target + "°C" : ""], ["Bed", p.bed_temp !== null && p.bed_temp !== undefined ? p.bed_temp + " / " + p.bed_target + "°C" : ""], ["CFS temp", p.box_temp], ["Box config", JSON.stringify(p.box_config || {})]];
    pr.innerHTML = items.filter(function (i) { return i[1] !== "" && i[1] !== null && i[1] !== undefined; }).map(function (i) { return '<div class="muted">' + esc(i[0]) + '</div><div>' + esc(i[1]) + '</div>'; }).join("");

    var proj = document.getElementById("project");
    proj.innerHTML = (state.project || []).map(function (f) {
      return '<div><span class="swatch" style="background:' + esc(f.color) + '"></span>' + esc(f.index + 1) + '</div><div>' + esc(f.name) + ' <span class="muted">' + esc(f.type) + (f.library ? " &rarr; " + esc(f.library) : "") + '</span></div>';
    }).join("");

    var log = document.getElementById("log");
    log.innerHTML = (state.log || []).map(function (l) { return esc(l); }).join("\n");
  }

  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t.classList.contains("src")) choices[t.getAttribute("data-k")] = t.value;
    if (t.classList.contains("col")) colors[t.getAttribute("data-k")] = t.value;
  });
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (t.classList.contains("apply")) {
      var b = +t.getAttribute("data-b"), s = +t.getAttribute("data-s"), k = key(b, s);
      var src = choices[k];
      if (!src) { alert("Choose a project filament or a library material first."); return; }
      orca.postMessage({ command: "apply", box_id: b, slot_id: s, source: src, color: colors[k] || null });
    } else if (t.classList.contains("clear")) {
      var b2 = +t.getAttribute("data-b"), s2 = +t.getAttribute("data-s");
      if (confirm("Clear slot " + (b2 + String.fromCharCode(65 + s2)) + " on the printer?")) orca.postMessage({ command: "clear", box_id: b2, slot_id: s2 });
    }
  });
  document.getElementById("refresh").addEventListener("click", function () { orca.postMessage({ command: "refresh" }); });
  document.getElementById("connect").addEventListener("click", function () { orca.postMessage({ command: "set_host", host: document.getElementById("host").value }); });
  document.getElementById("syncall").addEventListener("click", function () {
    if (confirm("Write the project's filaments into the CFS slots in order (filament 1 -> 1A, 2 -> 1B, ...)?")) orca.postMessage({ command: "sync_project" });
  });

  orca.onMessage(function (m) { if (m && m.command === "state") { state = m; render(); } });
  orca.postMessage({ command: "poll" });
  setInterval(function () { orca.postMessage({ command: "poll" }); }, 2000);
})();
</script>
"""

    class CrealityCfsManager(orca.script.ScriptPluginCapabilityBase):
        """Dock panel to inspect the CFS and write filament records to the printer.

        Works with or without the printer agent being selected: the printer address comes from the
        active printer preset's "Hostname, IP or URL" field (or can be typed into the panel).
        """

        def __init__(self) -> None:
            super().__init__()
            self.panel = None
            self._host = ""
            self._client: Optional[CrealityPrinterClient] = None
            self._log: list[str] = []
            self._cfg = dict(DEFAULT_CONFIG)
            self._project_cache: list[dict] = []
            self._busy = False

        def get_name(self):
            return MANAGER_NAME

        def on_load(self):
            self._cfg = _load_config(self)

        def on_unload(self):
            self._detach()
            shutdown_clients()

        def get_default_config(self):
            return dict(DEFAULT_CONFIG)

        def on_lifecycle_event(self, event, ctx):
            try:
                if event in (orca.LifecycleEvent.PresetSelected, orca.LifecycleEvent.PresetSaved):
                    SHARED.refresh_presets_from_host()
                    if self.panel is not None and self.panel.is_open():
                        self._ensure_client(_current_print_host())
                if event in (orca.LifecycleEvent.ProjectOpened, orca.LifecycleEvent.NewProject, orca.LifecycleEvent.ProjectClosed):
                    AUTOSYNC.rebase()
                elif event == orca.LifecycleEvent.ProjectDirtyChanged:
                    AUTOSYNC.tick_now()
                auto_open = bool(self._cfg.get("auto_open_panel")) or os.environ.get("CREALITY_CFS_AUTO_OPEN") == "1"
                if auto_open and self.panel is None and event in (
                    orca.LifecycleEvent.DeviceSelected,
                    orca.LifecycleEvent.PresetSelected,
                    orca.LifecycleEvent.NewProject,
                    orca.LifecycleEvent.ProjectOpened,
                ):
                    self._open()
            except Exception:
                LOG.exception("lifecycle handler failed")

        # -- panel ------------------------------------------------------------------------------------
        def execute(self):
            self._cfg = _load_config(self)
            return self._open()

        def _open(self):
            if self.panel is not None and self.panel.is_open():
                self.panel.show()
                return orca.ExecutionResult.success("Creality CFS Manager is already open.")
            SHARED.refresh_presets_from_host()
            self._ensure_client(_current_print_host())
            self.panel = orca.host.ui.create_dock_panel(
                html=PANEL_HTML,
                title="Creality CFS",
                width=420,
                height=560,
                on_message=self.on_message,
                on_close=self.on_close,
                dock="right",
            )
            return orca.ExecutionResult.success("Creality CFS Manager opened.")

        def on_close(self):
            self.panel = None
            self._detach()

        def _detach(self):
            if self._client is not None:
                _MANAGER_USES.pop(self._client.host, None)
                client = self._client
                self._client = None
                if not client.has_listeners:
                    threading.Timer(90.0, _stop_if_idle, args=(client,)).start()

        def _ensure_client(self, host: str) -> None:
            host = strip_host(host)
            if not host:
                self._note("No printer address: set 'Hostname, IP or URL' in the printer preset or type it above.")
                return
            if self._client is not None and self._client.host == host:
                return
            self._detach()
            self._host = host
            self._client = get_client(host)
            self._client.poll_interval = float(self._cfg.get("poll_interval_seconds") or 15)
            _MANAGER_USES[host] = True
            self._note(f"Connecting to {host} ...")

        def _note(self, text: str) -> None:
            stamp = time.strftime("%H:%M:%S")
            self._log.append(f"{stamp}  {text}")
            if len(self._log) > 40:
                del self._log[: len(self._log) - 40]
            LOG.info(text)

        # -- messages from the page (UI thread) ------------------------------------------------------------
        def on_message(self, message):
            try:
                command = (message or {}).get("command")
                if command == "poll":
                    self._post_state()
                elif command == "refresh":
                    self._refresh()
                elif command == "set_host":
                    self._ensure_client(str(message.get("host") or ""))
                    self._post_state()
                elif command == "apply":
                    self._apply(message)
                elif command == "clear":
                    self._write(int(message["box_id"]), int(message["slot_id"]), MaterialSpec())
                elif command == "sync_project":
                    self._sync_project()
            except Exception as exc:
                LOG.exception("panel command failed")
                self._note(f"Error: {exc}")
                self._post_state()

        def _refresh(self):
            client = self._client
            if client is None:
                return

            def worker():
                try:
                    client.refresh(include_library=True)
                    self._note("Refreshed from printer.")
                except Exception as exc:
                    self._note(f"Refresh failed: {exc}")

            threading.Thread(target=worker, name="creality-cfs-refresh", daemon=True).start()

        def _spec_for_source(self, source: str, color: Optional[str], library: list[MaterialLibraryEntry]) -> Optional[MaterialSpec]:
            kind, _, ident = source.partition(":")
            if kind == "project":
                idx = int(ident)
                project = self._project_cache
                if idx >= len(project):
                    self._note("Project filament index out of range.")
                    return None
                f = project[idx]
                presets = SHARED.get_presets()
                preset = next((p for p in presets if p.name == f["name"]), None)
                if preset is None:
                    preset = PresetInfo(name=f["name"], vendor=f.get("vendor") or "", type=f.get("type") or "")
                spec, entry = spec_from_preset(preset, color or f["color"], library, str(self._cfg.get("temps_from") or "preset"))
                return spec
            if kind == "library":
                entry = next((e for e in library if e.id == ident), None)
                if entry is None:
                    self._note("Library material not found (refresh?).")
                    return None
                return MaterialSpec(
                    vendor=entry.brand,
                    type=entry.type,
                    name=entry.name,
                    color=normalize_hex(color or (entry.colors[0] if entry.colors else "#FFFFFF")),
                    rfid=entry.id,
                    min_temp=entry.min_temp,
                    max_temp=entry.max_temp,
                    pressure=entry.pressure_advance,
                )
            self._note(f"Unknown source {source!r}.")
            return None

        def _apply(self, message: dict):
            client = self._client
            if client is None:
                return
            library = client.snapshot().library
            spec = self._spec_for_source(str(message.get("source") or ""), message.get("color"), library)
            if spec is None:
                self._post_state()
                return
            self._write(int(message["box_id"]), int(message["slot_id"]), spec)

        def _sync_project(self):
            client = self._client
            if client is None:
                return
            snap = client.snapshot()
            slots = [s for b in snap.cfs_boxes for s in b.slots]
            project = self._project_cache
            if not project:
                self._note("The project has no filaments.")
                return
            pairs = []
            for i, f in enumerate(project):
                if i >= len(slots):
                    self._note(f"Project has {len(project)} filaments but the CFS only has {len(slots)} slots; extra filaments skipped.")
                    break
                spec = self._spec_for_source(f"project:{i}", None, snap.library)
                if spec is not None:
                    pairs.append((slots[i], spec))
            self._write_many(pairs)

        def _write(self, box_id: int, slot_id: int, spec: MaterialSpec):
            client = self._client
            if client is None:
                return
            snap = client.snapshot()
            slot = snap.find_slot(box_id, slot_id)
            if slot is None:
                self._note(f"Slot {box_id}/{slot_id} not found.")
                return
            self._write_many([(slot, spec)])

        def _write_many(self, pairs: list[tuple[CfsSlot, MaterialSpec]]):
            client = self._client
            if client is None or not pairs:
                return
            if self._busy:
                self._note("A write is already in progress.")
                return
            force = bool(self._cfg.get("allow_write_while_printing"))
            reason = client.check_writable()
            if reason and not force:
                self._note(f"Refused: {reason}.")
                self._post_state()
                return
            self._busy = True
            self._note(f"Writing {len(pairs)} slot(s) ...")

            def worker():
                try:
                    for slot, spec in pairs:
                        result = client.modify_material(slot.box_id, slot.slot_id, spec, force=force)
                        self._note(f"{slot.label}: {'OK' if result.ok else 'FAILED'} - {result.message}")
                finally:
                    self._busy = False

            threading.Thread(target=worker, name="creality-cfs-write", daemon=True).start()

        # -- state for the page ---------------------------------------------------------------------------
        def _post_state(self):
            if self.panel is None:
                return
            try:
                self._project_cache = _collect_project_filaments()
                SHARED.learn_project_filament_ids()
            except Exception:
                LOG.debug("project filament snapshot failed", exc_info=True)
            client = self._client
            snap = client.snapshot() if client else PrinterSnapshot(host=self._host)
            presets = SHARED.get_presets()
            slots = []
            for box in snap.cfs_boxes:
                for s in box.slots:
                    matched = match_preset_for_slot(presets, s) if s.is_loaded else None
                    slots.append(
                        {
                            "box_id": s.box_id,
                            "slot_id": s.slot_id,
                            "label": s.label,
                            "loaded": s.is_loaded,
                            "vendor": s.vendor,
                            "name": s.name,
                            "type": s.type,
                            "color": s.color,
                            "min_temp": s.min_temp,
                            "max_temp": s.max_temp,
                            "pressure": s.pressure,
                            "percent": s.percent,
                            "selected": s.selected,
                            "rfid_locked": s.is_rfid_locked,
                            "matched_preset": matched.name if matched else "",
                            "suggested_color": s.color or "",
                        }
                    )
            project = []
            for f in self._project_cache:
                entry = match_library_entry(snap.library, f.get("vendor") or "", f.get("type") or "", f["name"].split("@", 1)[0].strip())
                project.append({**f, "library": f"{entry.brand} {entry.name}" if entry else ""})
            payload = {
                "command": "state",
                "printer": {
                    "host": snap.host,
                    "connected": snap.connected,
                    "model": snap.model,
                    "hostname": snap.hostname,
                    "firmware": snap.firmware,
                    "device_state": snap.device_state,
                    "cfs_connected": snap.cfs_connected,
                    "print_file": os.path.basename(snap.print_file) if snap.print_file else "",
                    "print_progress": snap.print_progress,
                    "layer": snap.layer,
                    "total_layers": snap.total_layers,
                    "nozzle_temp": snap.nozzle_temp,
                    "nozzle_target": snap.nozzle_target,
                    "bed_temp": snap.bed_temp,
                    "bed_target": snap.bed_target,
                    "box_temp": snap.box_temp,
                    "box_config": snap.box_config,
                    "last_error": snap.last_error,
                },
                "slots": slots,
                "library": [
                    {"id": e.id, "brand": e.brand, "name": e.name, "type": e.type}
                    for e in sorted(snap.library, key=lambda e: (e.is_generic, e.brand, e.type, e.name))
                ],
                "project": project,
                "log": list(self._log),
            }
            try:
                self.panel.post(payload)
            except Exception:
                LOG.debug("panel.post failed", exc_info=True)

    @orca.plugin
    class CrealityCfsPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(CrealityCfsAgent)
            orca.register_capability(CrealityCfsManager)
