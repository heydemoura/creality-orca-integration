"""Tests for the Creality CFS Sync plugin's protocol layer.

Run with either `python3 -m pytest tests` or `python3 -m unittest discover tests`.
The plugin module is imported directly from ../plugin (no OrcaSlicer needed).
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugin"))
sys.path.insert(0, HERE)

import creality_cfs as cfs  # noqa: E402
from fake_printer import FakePrinter, load_fixture  # noqa: E402


def snapshot_from_fixture() -> cfs.PrinterSnapshot:
    data = load_fixture()
    snap = cfs.PrinterSnapshot(host="fixture")
    cfs.apply_status(snap, {**data["status"], "boxsInfo": data["boxsInfo"], "boxConfig": data["boxConfig"], "retMaterials": data["retMaterials"]})
    return snap


class ColourTests(unittest.TestCase):
    def test_normalize_variants(self):
        self.assertEqual(cfs.normalize_hex("#0ff1e1e"), "#FF1E1E")  # Creality slot colour
        self.assertEqual(cfs.normalize_hex("#ff1e1e"), "#FF1E1E")
        self.assertEqual(cfs.normalize_hex("ff1e1e"), "#FF1E1E")
        self.assertEqual(cfs.normalize_hex("26A69AFF"), "#26A69A")  # Orca RRGGBBAA
        self.assertEqual(cfs.normalize_hex("#FF26A69A"), "#26A69A")  # Creality cloud AARRGGBB
        self.assertEqual(cfs.normalize_hex("0x26a69a"), "#26A69A")
        self.assertEqual(cfs.normalize_hex("", default="#ABCDEF"), "#ABCDEF")
        self.assertEqual(cfs.normalize_hex("garbage", default=""), "")

    def test_conversions(self):
        self.assertEqual(cfs.hex_to_creality("#26A69A"), "#026a69a")
        self.assertEqual(cfs.hex_to_bbl("#26a69a"), "26A69AFF")
        # round trip through the printer spelling
        self.assertEqual(cfs.normalize_hex(cfs.hex_to_creality("#9EA7AE")), "#9EA7AE")


class ParsingTests(unittest.TestCase):
    def test_parse_live_capture(self):
        snap = snapshot_from_fixture()
        self.assertEqual(snap.model, "K1 Max")
        self.assertEqual(snap.hostname, "K1Max-1E54")
        self.assertEqual(len(snap.boxes), 2)
        external, box = snap.boxes
        self.assertEqual(external.type, cfs.BOX_TYPE_EXTERNAL)
        self.assertFalse(external.is_cfs)
        self.assertTrue(box.is_cfs and box.is_active)
        self.assertEqual([s.label for s in box.slots], ["1A", "1B", "1C", "1D"])
        self.assertEqual([s.color for s in box.slots], ["#FF1E1E", "#000000", "#FFFFFF", "#9EA7AE"])
        self.assertTrue(all(s.is_loaded for s in box.slots))
        self.assertTrue(box.slots[3].selected)
        self.assertEqual(box.slots[0].rfid, "00001")
        self.assertEqual(box.temp, 29.0)
        self.assertEqual(box.humidity, 50.0)
        self.assertEqual(len(snap.library), 40)
        self.assertEqual(snap.box_config["cAutoFeed"], 1)

    def test_printing_state_blocks_writes(self):
        snap = snapshot_from_fixture()  # captured while printing: deviceState == 1
        self.assertEqual(snap.device_state, 1)
        self.assertFalse(snap.is_idle)
        client = cfs.CrealityPrinterClient("fixture")
        snap.connected = True
        self.assertIn("printing", client.check_writable(snap))
        snap.device_state = 0
        self.assertIsNone(client.check_writable(snap))
        snap.feed_state = 1
        self.assertIn("loaded or unloaded", client.check_writable(snap))

    def test_empty_and_rfid_slots(self):
        boxes = cfs.parse_boxes(
            {
                "materialBoxs": [
                    {
                        "id": 1,
                        "state": 1,
                        "type": 0,
                        "materials": [
                            {"id": 0, "state": 0, "vendor": "", "type": "", "name": "", "color": ""},
                            {"id": 1, "state": 2, "vendor": "Creality", "type": "PLA", "name": "Hyper PLA", "color": "#0ff0000", "rfid": "01001"},
                        ],
                    }
                ]
            }
        )
        self.assertFalse(boxes[0].slots[0].is_loaded)
        self.assertTrue(boxes[0].slots[1].is_loaded)
        self.assertTrue(boxes[0].slots[1].is_rfid_locked)

    def test_material_library_parse(self):
        lib = cfs.parse_material_library(load_fixture()["retMaterials"])
        hyper = next(e for e in lib if e.name == "Hyper PLA")
        self.assertEqual((hyper.id, hyper.brand, hyper.type, hyper.min_temp, hyper.max_temp, hyper.pressure_advance), ("01001", "Creality", "PLA", 190, 240, 0.04))


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.lib = snapshot_from_fixture().library

    def test_library_matching(self):
        cases = [
            (("Creality", "PLA", "Hyper PLA @K1 Max_CFS-C-all"), "01001"),
            (("Creality", "PLA", "Hyper PLA"), "01001"),
            (("Generic", "PLA", "Generic PLA"), "00001"),
            (("Generic", "PLA", "Generic PLA Silk"), "00002"),
            (("Generic", "PLA-CF", "Generic PLA-CF"), "00006"),
            (("eSUN", "PETG", "eSUN PETG+"), "00003"),  # unknown brand -> generic of same type
            (("Polymaker", "PLA", "PolyTerra PLA"), "00001"),
            (("Creality", "ABS", "CR-ABS"), "07001"),
            (("Creality", "TPU", "HP-TPU"), "10001"),
        ]
        for (vendor, ftype, name), expected in cases:
            with self.subTest(name=name):
                entry = cfs.match_library_entry(self.lib, vendor, ftype, name)
                self.assertIsNotNone(entry)
                self.assertEqual(entry.id, expected)

    def test_library_matching_unknown_type(self):
        self.assertIsNone(cfs.match_library_entry(self.lib, "X", "WOOD-METAL", "Odd stuff"))

    def test_library_matching_without_type_is_name_only(self):
        # The CLI's `set --library "Generic PLA"` form gives no vendor/type.
        self.assertEqual(cfs.match_library_entry(self.lib, "", "", "Generic PLA").id, "00001")
        self.assertEqual(cfs.match_library_entry(self.lib, "", "", "Creality Hyper PLA").id, "01001")
        self.assertEqual(cfs.match_library_entry(self.lib, "", "", "hyper petg").id, "06002")
        self.assertIsNone(cfs.match_library_entry(self.lib, "", "", "no such material"))

    def test_normalize_filament_type(self):
        self.assertEqual(cfs.normalize_filament_type("PLA Silk"), "PLA")
        self.assertEqual(cfs.normalize_filament_type("PETG-CF"), "PETG")
        self.assertEqual(cfs.normalize_filament_type("PA6-CF"), "PA")
        self.assertEqual(cfs.normalize_filament_type("TPU"), "TPU")

    def test_preset_matching_prefers_least_specific_system_preset(self):
        presets = [
            cfs.PresetInfo(name="Generic PLA @K1 Max_CFS-C-all", type="PLA", vendor="Generic", filament_id="GFL99", is_system=True),
            cfs.PresetInfo(name="Generic PLA Silk @K1 Max_CFS-C-all", type="PLA", vendor="Generic", filament_id="GFL98", is_system=True),
            cfs.PresetInfo(name="Generic PLA @K1 Max_CFS-C-all (tuned)", type="PLA", vendor="Generic", filament_id="GFL99", is_user=True),
            cfs.PresetInfo(name="Hyper PLA @K1 Max_CFS-C-all", type="PLA", vendor="Creality", filament_id="OFHYP", is_system=True),
            cfs.PresetInfo(name="Generic PETG @K1 Max_CFS-C-all", type="PETG", vendor="Generic", filament_id="GFG99", is_system=True),
        ]
        slot = cfs.CfsSlot(1, 0, state=1, vendor="Generic", type="PLA", name="Generic PLA", color="#FF0000")
        self.assertEqual(cfs.match_preset_for_slot(presets, slot).name, "Generic PLA @K1 Max_CFS-C-all")
        slot = cfs.CfsSlot(1, 1, state=1, vendor="Creality", type="PLA", name="Hyper PLA", color="#FF0000")
        self.assertEqual(cfs.match_preset_for_slot(presets, slot).filament_id, "OFHYP")
        slot = cfs.CfsSlot(1, 2, state=1, vendor="Sunlu", type="PETG", name="Sunlu PETG", color="#FF0000")
        self.assertEqual(cfs.match_preset_for_slot(presets, slot).filament_id, "GFG99")  # type fallback
        slot = cfs.CfsSlot(1, 3, state=1, vendor="Sunlu", type="ASA", name="ASA", color="#FF0000")
        self.assertIsNone(cfs.match_preset_for_slot(presets, slot))

    def test_spec_from_preset_uses_preset_numbers_and_library_identity(self):
        preset = cfs.PresetInfo(name="Hyper PLA @K1 Max_CFS-C-all", vendor="Creality", type="PLA", min_temp=200, max_temp=235, pressure_advance=0.035)
        spec, entry = cfs.spec_from_preset(preset, "#123456", self.lib)
        self.assertEqual(entry.id, "01001")
        self.assertEqual((spec.rfid, spec.vendor, spec.name, spec.type), ("01001", "Creality", "Hyper PLA", "PLA"))
        self.assertEqual((spec.min_temp, spec.max_temp, spec.pressure), (200, 235, 0.035))
        self.assertEqual(spec.color, "#123456")
        spec_lib, _ = cfs.spec_from_preset(preset, "#123456", self.lib, temps_from="library")
        self.assertEqual((spec_lib.min_temp, spec_lib.max_temp, spec_lib.pressure), (190, 240, 0.04))

    def test_spec_from_unknown_preset_falls_back_to_custom(self):
        preset = cfs.PresetInfo(name="Mystery WOODFILL @K1", vendor="Mystery", type="WOODFILL")
        spec, entry = cfs.spec_from_preset(preset, "#654321", self.lib)
        self.assertIsNone(entry)
        self.assertEqual((spec.vendor, spec.type, spec.name, spec.rfid), ("Mystery", "WOODFILL", "Mystery WOODFILL", ""))
        self.assertTrue(spec.min_temp and spec.max_temp)

    def test_modify_material_params_match_creality_print(self):
        spec = cfs.MaterialSpec(vendor="Generic", type="PLA", name="Generic PLA", color="#ff1e1e", rfid="00001", min_temp=190, max_temp=240, pressure=0.02)
        params = spec.to_params(1, 0)["modifyMaterial"]
        self.assertEqual(params["boxId"], 1)
        self.assertEqual(params["id"], 0)
        self.assertEqual(params["color"], "#0ff1e1e")
        self.assertAlmostEqual(params["minTemp"], 190, places=6)
        self.assertIsInstance(params["minTemp"], float)
        clear = cfs.MaterialSpec().to_params(1, 2)["modifyMaterial"]
        self.assertEqual(clear, {"boxId": 1, "id": 2, "rfid": "", "type": "", "vendor": "", "name": "", "color": "", "minTemp": 0, "maxTemp": 0, "pressure": 0})


class OrcaPayloadTests(unittest.TestCase):
    def test_ams_payload_shape(self):
        snap = snapshot_from_fixture()
        presets = [cfs.PresetInfo(name="Generic PLA @K1 Max_CFS-C-all", type="PLA", vendor="Generic", filament_id="GFL99", is_system=True)]
        payload = cfs.build_push_status(snap, 7, lambda slot: cfs.match_preset_for_slot(presets, slot))
        p = payload["print"]
        self.assertEqual(p["command"], "push_status")
        self.assertEqual(p["msg"], 0)
        self.assertEqual(p["gcode_state"], "RUNNING")
        self.assertEqual(p["mc_percent"], 1)
        ams = p["ams"]
        self.assertEqual(ams["ams_exist_bits"], "1")
        self.assertEqual(ams["tray_exist_bits"], "F")
        self.assertEqual(len(ams["ams"]), 1)  # the external spool holder is not an AMS
        unit = ams["ams"][0]
        self.assertEqual(unit["id"], "0")
        self.assertEqual(len(unit["tray"]), 4)
        tray = unit["tray"][0]
        self.assertEqual(tray["tray_info_idx"], "GFL99")
        self.assertEqual(tray["tray_type"], "PLA")
        self.assertEqual(tray["tray_color"], "FF1E1EFF")
        self.assertEqual(tray["nozzle_temp_min"], "190")
        self.assertEqual(unit["humidity_raw"], "50")

    def test_ams_payload_uses_sentinel_id_when_preset_id_unknown(self):
        snap = snapshot_from_fixture()
        ams = cfs.build_ams_json(snap)  # no preset lookup at all
        ids = [t["tray_info_idx"] for t in ams["ams"][0]["tray"]]
        self.assertEqual(ids, ["CFS-00001"] * 4)
        self.assertTrue(all(i and not i.startswith("P") for i in ids))
        presets = [cfs.PresetInfo(name="Generic PLA @K1 Max_CFS-C-all", type="PLA", vendor="Generic", is_system=True)]  # known preset, unknown id
        ams = cfs.build_ams_json(snap, lambda slot: cfs.match_preset_for_slot(presets, slot))
        self.assertEqual(ams["ams"][0]["tray"][0]["tray_info_idx"], "CFS-00001")

    def test_ams_payload_with_empty_slots(self):
        snap = snapshot_from_fixture()
        snap.boxes[1].slots[1].state = 0
        snap.boxes[1].slots[1].vendor = snap.boxes[1].slots[1].name = snap.boxes[1].slots[1].type = snap.boxes[1].slots[1].color = ""
        ams = cfs.build_ams_json(snap)
        self.assertEqual(ams["tray_exist_bits"], "D")
        self.assertEqual(ams["ams"][0]["tray"][1]["tray_slot_placeholder"], "1")

    def test_get_version_payload(self):
        snap = snapshot_from_fixture()
        info = cfs.build_get_version(snap, 3)["info"]
        self.assertEqual(info["command"], "get_version")
        self.assertEqual(info["module"][0]["name"], "ota")
        self.assertEqual(info["module"][0]["sw_ver"], "2.3.5.34")

    def test_ams_setting_translation(self):
        lib = snapshot_from_fixture().library
        presets = [cfs.PresetInfo(name="Hyper PLA @K1 Max_CFS-C-all", vendor="Creality", type="PLA", filament_id="OFHYP", min_temp=190, max_temp=240, pressure_advance=0.04)]
        cmd = {"command": "ams_filament_setting", "ams_id": 0, "slot_id": 2, "tray_id": 2, "tray_info_idx": "OFHYP", "setting_id": "", "tray_color": "12345678", "nozzle_temp_min": 200, "nozzle_temp_max": 230, "tray_type": "PLA"}
        spec, pos = cfs.ams_setting_to_spec(cmd, presets, lib)
        self.assertEqual(pos, (0, 2))
        self.assertEqual((spec.rfid, spec.name, spec.color, spec.min_temp, spec.max_temp), ("01001", "Hyper PLA", "#123456", 200, 230))
        clear, pos = cfs.ams_setting_to_spec({"command": "ams_filament_setting", "ams_id": 0, "slot_id": 1, "tray_info_idx": "", "tray_type": "", "tray_color": "FFFFFF00"}, presets, lib)
        self.assertTrue(clear.is_clear)
        self.assertEqual(pos, (0, 1))


class SerializedConfigTests(unittest.TestCase):
    """The host API hands back ConfigOption::serialize() text, not Python lists."""

    def test_strings(self):
        self.assertEqual(cfs.unserialize_strings("OFDSrzZ8"), ["OFDSrzZ8"])
        self.assertEqual(cfs.unserialize_strings("OFDSrzZ8;GFL99"), ["OFDSrzZ8", "GFL99"])
        self.assertEqual(cfs.unserialize_strings('"Generic PLA @K1 Max_CFS-C-all";"Hyper PLA @K1"'), ["Generic PLA @K1 Max_CFS-C-all", "Hyper PLA @K1"])
        self.assertEqual(cfs.unserialize_strings('"a\\"b";c'), ['a"b', "c"])
        self.assertEqual(cfs.unserialize_strings('#26A69A;#FF0000'), ["#26A69A", "#FF0000"])
        self.assertEqual(cfs.unserialize_strings('""'), [""])
        self.assertEqual(cfs.unserialize_strings(""), [])
        self.assertEqual(cfs.unserialize_strings(None), [])
        self.assertEqual(cfs.unserialize_strings(["x", "y"]), ["x", "y"])
        self.assertEqual(cfs.unserialize_strings("192.168.0.128:4409"), ["192.168.0.128:4409"])

    def test_numbers(self):
        self.assertEqual(cfs.unserialize_numbers("190"), [190.0])
        self.assertEqual(cfs.unserialize_numbers("190,200"), [190.0, 200.0])
        self.assertEqual(cfs.unserialize_numbers("0.04"), [0.04])
        self.assertEqual(cfs.unserialize_numbers("nil"), [])
        self.assertEqual(cfs.unserialize_numbers("100%"), [100.0])
        self.assertEqual(cfs.unserialize_numbers(None), [])
        self.assertEqual(cfs.unserialize_numbers(["1", 2]), [1.0, 2.0])


class HostHelpersTests(unittest.TestCase):
    def test_strip_host(self):
        self.assertEqual(cfs.strip_host("192.168.0.128:4409"), "192.168.0.128")
        self.assertEqual(cfs.strip_host("http://192.168.0.128/"), "192.168.0.128")
        self.assertEqual(cfs.strip_host("https://k1max.local:443/path"), "k1max.local")
        self.assertEqual(cfs.strip_host("  k1max  "), "k1max")
        self.assertEqual(cfs.strip_host(""), "")


class PresetIndexTests(unittest.TestCase):
    def test_resolves_inherited_filament_id(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fil = os.path.join(tmp, "filament")
            os.makedirs(fil)
            with open(os.path.join(fil, "fdm_filament_pla.json"), "w") as fh:
                json.dump({"name": "fdm_filament_pla", "filament_type": ["PLA"], "nozzle_temperature_range_low": ["190"], "nozzle_temperature_range_high": ["230"]}, fh)
            with open(os.path.join(fil, "CR-PLA @K1 Max_CFS-C-all.json"), "w") as fh:
                json.dump({"name": "CR-PLA @K1 Max_CFS-C-all", "inherits": "fdm_filament_pla", "filament_id": "OFp4ETDP", "filament_vendor": ["Creality"], "nozzle_temperature_range_high": ["240"], "pressure_advance": "0.04"}, fh)
            user = os.path.join(tmp, "user")
            os.makedirs(user)
            with open(os.path.join(user, "My CR-PLA.json"), "w") as fh:
                json.dump({"name": "My CR-PLA", "inherits": "CR-PLA @K1 Max_CFS-C-all", "filament_settings_id": ["My CR-PLA"]}, fh)
            presets = [
                cfs.PresetInfo(name="CR-PLA @K1 Max_CFS-C-all", file=os.path.join(fil, "CR-PLA @K1 Max_CFS-C-all.json"), is_system=True),
                cfs.PresetInfo(name="My CR-PLA", file=os.path.join(user, "My CR-PLA.json"), is_user=True),
            ]
            cfs.PresetIndex().build(presets)
            self.assertEqual(presets[0].filament_id, "OFp4ETDP")
            self.assertEqual((presets[0].vendor, presets[0].type, presets[0].min_temp, presets[0].max_temp, presets[0].pressure_advance), ("Creality", "PLA", 190, 240, 0.04))
            self.assertEqual(presets[1].filament_id, "OFp4ETDP")  # inherited through the user preset
            self.assertEqual(presets[1].type, "PLA")


class AutoSyncPlanningTests(unittest.TestCase):
    """Project filament list -> CFS slot writes (the automatic sync's decision logic)."""

    def setUp(self):
        self.snap = snapshot_from_fixture()  # 4 slots, all Generic PLA: red, black, white, grey
        self.presets = [
            cfs.PresetInfo(name="Generic PLA @K1 Max_CFS-C-all", vendor="Generic", type="PLA", filament_id="OFDSrzZ8", min_temp=190, max_temp=240, pressure_advance=0.04, is_system=True),
            cfs.PresetInfo(name="Hyper PLA @K1 Max_CFS-C-all", vendor="Creality", type="PLA", filament_id="OFCZsqXg", min_temp=190, max_temp=240, pressure_advance=0.04, is_system=True),
            cfs.PresetInfo(name="Generic PETG @K1 Max_CFS-C-all", vendor="Generic", type="PETG", filament_id="OFYPdQJh", min_temp=220, max_temp=270, pressure_advance=0.08, is_system=True),
        ]

    def test_signature_tracks_names_and_colours_only(self):
        a = cfs.project_signature([{"name": "X", "color": "#ff0000", "type": "PLA"}])
        b = cfs.project_signature([{"name": "X", "color": "#FF0000", "type": "PETG"}])
        c = cfs.project_signature([{"name": "X", "color": "#00ff00"}])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_no_writes_when_project_matches_printer(self):
        project = [
            {"name": "Generic PLA @K1 Max_CFS-C-all", "color": "#FF1E1E"},
            {"name": "Generic PLA @K1 Max_CFS-C-all", "color": "#000000"},
            {"name": "Generic PLA @K1 Max_CFS-C-all", "color": "#FFFFFF"},
            {"name": "Generic PLA @K1 Max_CFS-C-all", "color": "#9EA7AE"},
        ]
        self.assertEqual(cfs.plan_project_sync(project, self.presets, self.snap), [])

    def test_only_changed_slots_are_written(self):
        project = [
            {"name": "Generic PLA @K1 Max_CFS-C-all", "color": "#FF1E1E"},   # unchanged
            {"name": "Hyper PLA @K1 Max_CFS-C-all", "color": "#000000"},     # material changed
            {"name": "Generic PLA @K1 Max_CFS-C-all", "color": "#123456"},   # colour changed
            {"name": "Generic PETG @K1 Max_CFS-C-all", "color": "#9EA7AE"},  # type changed
            {"name": "Generic PLA @K1 Max_CFS-C-all", "color": "#FFFFFF"},   # 5th filament: no slot
        ]
        plan = cfs.plan_project_sync(project, self.presets, self.snap)
        self.assertEqual([s.label for s, _ in plan], ["1B", "1C", "1D"])
        specs = {s.label: spec for s, spec in plan}
        self.assertEqual((specs["1B"].name, specs["1B"].rfid, specs["1B"].color), ("Hyper PLA", "01001", "#000000"))
        self.assertEqual((specs["1C"].name, specs["1C"].color), ("Generic PLA", "#123456"))
        self.assertEqual((specs["1D"].type, specs["1D"].rfid, specs["1D"].min_temp, specs["1D"].max_temp), ("PETG", "00003", 220, 270))

    def test_rfid_locked_and_empty_slots(self):
        self.snap.boxes[1].slots[0].state = cfs.SLOT_STATE_RFID
        empty = self.snap.boxes[1].slots[1]
        empty.state = 0
        empty.vendor = empty.name = empty.type = empty.color = ""
        project = [
            {"name": "Hyper PLA @K1 Max_CFS-C-all", "color": "#000000"},
            {"name": "Generic PLA @K1 Max_CFS-C-all", "color": "#000000"},  # same material as the empty slot's old record
        ]
        plan = cfs.plan_project_sync(project, self.presets, self.snap)
        self.assertEqual([s.label for s, _ in plan], ["1B"])  # 1A locked, 1B empty -> gets filled

    def test_library_temps_option(self):
        project = [{"name": "Hyper PLA @K1 Max_CFS-C-all", "color": "#000000"}]
        hyper = [p for p in self.presets if p.name.startswith("Hyper")]
        hyper[0].min_temp, hyper[0].max_temp, hyper[0].pressure_advance = 205, 225, 0.031
        (slot, spec), = cfs.plan_project_sync(project, hyper, self.snap, temps_from="preset")
        self.assertEqual((spec.min_temp, spec.max_temp, spec.pressure), (205, 225, 0.031))
        (slot, spec), = cfs.plan_project_sync(project, hyper, self.snap, temps_from="library")
        self.assertEqual((spec.min_temp, spec.max_temp, spec.pressure), (190, 240, 0.04))


class OpcReaderTests(unittest.TestCase):
    """OrcaSlicer's binary vendor preset cache is the source of system preset ids in release builds."""

    def _filaments(self):
        return [
            {"name": "fdm_filament_common", "instantiation": "false", "config": {
                "filament_type": ("strings", ["PLA"]), "nozzle_temperature_range_low": ("ints", [190]),
                "filament_cost": ("floats", [20.0]), "filament_retract_lift_above": ("floats", [float("nan")]),
                "slow_down_layer_time": ("ints", [4]), "filament_wipe": ("bools", [True]),
                "filament_z_hop_types": ("enums", ["Normal Lift"]), "fan_speedup_overhangs": ("bool", True),
                "compatible_printers_condition": ("string", ""), "filament_shrink": ("percents", [100.0]),
                "filament_retract_length": ("floats_or_percents", [(0.8, False), (10.0, True)]),
                "bed_exclude_area": ("points", [(0.0, 0.0), (1.0, 2.0)]), "extruder_offset": ("points", [(0.0, 0.0)]),
                "wipe_tower_x": ("floats", [15.0]), "nozzle_height": ("float", 4.5), "flush_volumes": ("ints_groups", [[1, 2], [3]]),
                "head_wrap_detect_zone": ("points_groups", [[(0.0, 0.0), (1.0, 1.0)]]), "curr_bed_type": ("enum", "Textured PEI Plate"),
                "printer_notes": ("string", "hello world"), "thumbnail_size": ("point3", (1.0, 2.0, 3.0)), "layer_height": ("percent", 0.2),
                "wall_count": ("int", 2), "unknown_enum": ("enum", 7),
            }},
            {"name": "fdm_filament_pla", "inherits": "fdm_filament_common", "instantiation": "false", "config": {
                "nozzle_temperature_range_high": ("ints", [230]), "default_filament_colour": ("strings", ["#FFFFFF"]),
            }},
            {"name": "Generic PLA @Test", "inherits": "fdm_filament_pla", "filament_id": "GFL98", "setting_id": "GFSA00", "config": {
                "filament_vendor": ("strings", ["Generic"]), "pressure_advance": ("floats", [0.03]),
                "compatible_printers": ("strings", ["Test 0.4 nozzle"]), "nozzle_temperature_range_high": ("ints", [240]),
            }},
            {"name": "Hyper PLA @Test", "inherits": "fdm_filament_pla", "filament_id": "OFHYP1", "config": {
                "filament_vendor": ("strings", ["Creality"]), "default_filament_colour": ("strings", ["#FF0000"]),
            }},
            {"name": "My tuned PLA @Test", "inherits": "Hyper PLA @Test", "config": {"pressure_advance": ("floats", [0.02])}},
        ]

    def test_round_trip_and_chain(self):
        import tempfile
        from fake_opc import build_opc

        blob = build_opc("Test", self._filaments(), processes=[{"name": "0.20mm @Test", "config": {"wall_count": ("int", 3), "layer_height": ("percent", 0.2)}}],
                         machines=[{"name": "Test 0.4 nozzle", "config": {"printer_notes": ("string", "x"), "extruder_offset": ("points", [(0, 0)])}}],
                         enum_values=["Normal Lift", "Textured PEI Plate"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Test.opc")
            with open(path, "wb") as fh:
                fh.write(blob)
            vendor, presets = cfs.read_opc_filaments(path)
        self.assertEqual(vendor, "Test")
        self.assertEqual(set(presets), {"fdm_filament_common", "fdm_filament_pla", "Generic PLA @Test", "Hyper PLA @Test", "My tuned PLA @Test"})
        self.assertEqual(presets["Generic PLA @Test"].filament_id, "GFL98")
        self.assertEqual(presets["Generic PLA @Test"].compatible_printers, ["Test 0.4 nozzle"])
        self.assertEqual(presets["fdm_filament_common"].type, "PLA")
        generic = cfs.resolve_opc_chain(presets, "Generic PLA @Test")
        self.assertEqual((generic.filament_id, generic.vendor, generic.type, generic.min_temp, generic.max_temp, generic.pressure_advance, generic.color),
                         ("GFL98", "Generic", "PLA", 190, 240, 0.03, "#FFFFFF"))
        tuned = cfs.resolve_opc_chain(presets, "My tuned PLA @Test")
        self.assertEqual((tuned.filament_id, tuned.vendor, tuned.max_temp, tuned.pressure_advance, tuned.color), ("OFHYP1", "Creality", 230, 0.02, "#FF0000"))
        self.assertIsNone(cfs.resolve_opc_chain(presets, "nope"))

    def test_rejects_wrong_version_and_crc(self):
        import tempfile
        from fake_opc import build_opc

        with tempfile.TemporaryDirectory() as tmp:
            bad_version = os.path.join(tmp, "v.opc")
            with open(bad_version, "wb") as fh:
                fh.write(build_opc("Test", self._filaments()[:1], version=3))
            with self.assertRaises(cfs.OpcFormatError):
                cfs.read_opc_filaments(bad_version)
            bad_crc = os.path.join(tmp, "c.opc")
            with open(bad_crc, "wb") as fh:
                fh.write(build_opc("Test", self._filaments()[:1], corrupt_crc=True))
            with self.assertRaises(cfs.OpcFormatError):
                cfs.read_opc_filaments(bad_crc)
            not_opc = os.path.join(tmp, "x.opc")
            with open(not_opc, "wb") as fh:
                fh.write(b"ZCRO" + b"\0" * 40)
            with self.assertRaises(cfs.OpcFormatError):
                cfs.read_opc_filaments(not_opc)

    def test_preset_index_completes_system_presets_from_cache(self):
        import tempfile
        from fake_opc import build_opc

        with tempfile.TemporaryDirectory() as tmp:
            system = os.path.join(tmp, "system")
            os.makedirs(system)
            with open(os.path.join(system, "Test.opc"), "wb") as fh:
                fh.write(build_opc("Test", self._filaments(), enum_values=["Normal Lift", "Textured PEI Plate"]))
            user_dir = os.path.join(tmp, "user", "default", "filament")
            os.makedirs(user_dir)
            # A user preset saved as JSON that inherits a system preset: id must come through the cache.
            with open(os.path.join(user_dir, "Mine.json"), "w") as fh:
                json.dump({"name": "Mine", "inherits": "Hyper PLA @Test", "filament_settings_id": ["Mine"]}, fh)
            presets = [
                cfs.PresetInfo(name="Generic PLA @Test", is_system=True),               # no file: release build
                cfs.PresetInfo(name="Hyper PLA @Test", is_system=True, vendor="Creality"),
                cfs.PresetInfo(name="Mine", file=os.path.join(user_dir, "Mine.json"), is_user=True),
                cfs.PresetInfo(name="Unknown @Elsewhere", is_system=True),
            ]
            cfs.PresetIndex().build(presets)
        self.assertEqual((presets[0].filament_id, presets[0].vendor, presets[0].type, presets[0].min_temp, presets[0].max_temp), ("GFL98", "Generic", "PLA", 190, 240))
        self.assertEqual((presets[1].filament_id, presets[1].type, presets[1].color), ("OFHYP1", "PLA", "#FF0000"))
        self.assertEqual((presets[2].filament_id, presets[2].vendor, presets[2].type), ("OFHYP1", "Creality", "PLA"))
        self.assertEqual(presets[3].filament_id, "")

    def test_real_bundle_when_available(self):
        """Smoke test against a real OrcaSlicer install when one is around (skipped otherwise)."""
        candidates = [
            os.path.expanduser("~/.config/OrcaSlicer-cfs-test/system/Creality.opc"),
            os.path.expanduser("~/.config/OrcaSlicer/system/Creality.opc"),
        ]
        path = next((c for c in candidates if os.path.isfile(c)), None)
        if path is None:
            self.skipTest("no OrcaSlicer vendor cache installed")
        vendor, presets = cfs.read_opc_filaments(path)
        self.assertEqual(vendor, "Creality")
        self.assertGreater(len(presets), 100)
        generic = cfs.resolve_opc_chain(presets, "Generic PLA @K1 Max_CFS-C-all")
        self.assertIsNotNone(generic)
        self.assertTrue(generic.filament_id)
        self.assertEqual(generic.type, "PLA")


class ClientAgainstFakePrinterTests(unittest.TestCase):
    """End-to-end over a real TCP WebSocket against the fake firmware."""

    def _client(self, printer: FakePrinter) -> cfs.CrealityPrinterClient:
        client = cfs.CrealityPrinterClient("127.0.0.1", ws_port=printer.port, poll_interval=2.0)
        client.reconnect_delay = 0.2
        client.start()
        self.addCleanup(client.stop)
        self.assertTrue(client.wait_ready(5.0), client.snapshot().last_error)
        deadline = time.time() + 5
        while time.time() < deadline and not client.snapshot().library:
            time.sleep(0.05)
        return client

    def test_connect_and_read(self):
        with FakePrinter(heartbeat_interval=0.3) as printer:
            client = self._client(printer)
            snap = client.snapshot()
            self.assertTrue(snap.connected)
            self.assertEqual(snap.model, "K1 Max")
            self.assertEqual(len(snap.cfs_boxes), 1)
            self.assertEqual(len(snap.library), 40)
            self.assertEqual(snap.device_state, 0)
            # Heartbeats must be answered with "ok" and must not break JSON handling.
            time.sleep(0.8)
            self.assertTrue(client.snapshot().connected)
            snap = client.refresh(include_library=True)
            self.assertEqual(snap.cfs_boxes[0].slots[0].name, "Generic PLA")

    def test_write_and_verify(self):
        with FakePrinter() as printer:
            client = self._client(printer)
            spec = cfs.MaterialSpec(vendor="Creality", type="PLA", name="Hyper PLA", color="#00FF00", rfid="01001", min_temp=200, max_temp=235, pressure=0.035)
            result = client.modify_material(1, 2, spec)
            self.assertTrue(result.ok, result.message)
            self.assertTrue(result.verified)
            self.assertEqual(result.response_code, 0)
            sent = [m for m in printer.received if m.get("method") == "set" and "modifyMaterial" in m["params"]]
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["params"]["modifyMaterial"]["color"], "#000ff00")
            slot = printer.slot(1, 2)
            self.assertEqual((slot["name"], slot["minTemp"], slot["maxTemp"], slot["pressure"]), ("Hyper PLA", 200, 235, 0.035))
            after = client.snapshot().find_slot(1, 2)
            self.assertEqual(after.color, "#00FF00")
            # clear it again
            result = client.clear_slot(1, 2)
            self.assertTrue(result.ok, result.message)
            self.assertFalse(client.snapshot().find_slot(1, 2).is_loaded)

    def test_write_refused_while_printing(self):
        with FakePrinter(device_state=1) as printer:
            client = self._client(printer)
            result = client.modify_material(1, 0, cfs.MaterialSpec(vendor="Generic", type="PLA", name="Generic PLA", color="#111111", rfid="00001", min_temp=190, max_temp=240))
            self.assertFalse(result.ok)
            self.assertIn("printing", result.message)
            self.assertFalse(any("modifyMaterial" in m.get("params", {}) for m in printer.received))
            # force overrides the interlock
            result = client.modify_material(1, 0, cfs.MaterialSpec(vendor="Generic", type="PLA", name="Generic PLA", color="#111111", rfid="00001", min_temp=190, max_temp=240), force=True)
            self.assertTrue(result.ok, result.message)

    def test_write_without_ack_still_verifies_by_reading_back(self):
        with FakePrinter(ack_modify=False, push_after_modify=False) as printer:
            client = self._client(printer)
            result = client.modify_material(1, 1, cfs.MaterialSpec(vendor="Generic", type="PETG", name="Generic PETG", color="#ABCDEF", rfid="00003", min_temp=220, max_temp=270))
            self.assertTrue(result.ok, result.message)
            self.assertIsNone(result.response_code)
            self.assertEqual(printer.slot(1, 1)["type"], "PETG")

    def test_rfid_slot_is_read_only(self):
        with FakePrinter() as printer:
            printer.slot(1, 3)["state"] = 2
            client = self._client(printer)
            result = client.modify_material(1, 3, cfs.MaterialSpec(vendor="Generic", type="PLA", name="Generic PLA", color="#111111"))
            self.assertFalse(result.ok)
            self.assertIn("RFID", result.message)

    def test_gcode_and_reconnect(self):
        with FakePrinter() as printer:
            client = self._client(printer)
            client.send_gcode("M104 S200\nM140 S60\n")
            time.sleep(0.3)
            self.assertEqual(printer.gcode, ["M104 S200", "M140 S60"])
            # Drop every socket on the server side; the client must come back by itself.
            for c in list(printer._clients):
                c.close()
            deadline = time.time() + 5
            seen_down = False
            while time.time() < deadline:
                snap = client.snapshot()
                if not snap.connected:
                    seen_down = True
                if seen_down and snap.connected:
                    break
                time.sleep(0.05)
            self.assertTrue(client.snapshot().connected)

    def test_listener_receives_box_updates(self):
        with FakePrinter() as printer:
            client = self._client(printer)
            got = []
            client.add_listener(lambda c, m: got.append(m) if "boxsInfo" in m else None)
            printer.slot(1, 0)["color"] = "#0123456"
            printer.broadcast({"boxsInfo": printer.boxs_info})
            deadline = time.time() + 3
            while time.time() < deadline and not got:
                time.sleep(0.05)
            self.assertTrue(got)
            self.assertEqual(client.snapshot().find_slot(1, 0).color, "#123456")


if __name__ == "__main__":
    unittest.main()
