"""Characterization tests for behavior extracted from the original snowgui.py.

These tests intentionally focus on stable inputs and outputs rather than GUI
pixels or live services.  Hardware, network, and systemd integration require a
Raspberry Pi or deployment environment; their pure decision logic is exercised
here without making external calls.
"""

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from snowscraper_app import alarms, avalanche, brightness, resorts, storage, system


class StorageTests(unittest.TestCase):
    """Atomic helpers must preserve the exact text and JSON formats callers expect."""

    def test_atomic_text_and_json_writes_replace_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            text_path = Path(temp_dir) / "nested" / "setting.conf"
            storage.atomic_write_text("first", str(text_path))
            storage.atomic_write_text("second", str(text_path))
            self.assertEqual(text_path.read_text(encoding="utf-8"), "second")

            json_path = Path(temp_dir) / "state.json"
            storage.atomic_write_json({"snow": 12}, str(json_path), indent=2)
            self.assertEqual(json.loads(json_path.read_text()), {"snow": 12})


class BrightnessTests(unittest.TestCase):
    """Persisted profile indices remain clamped to the two historical profiles."""

    def test_read_index_clamps_and_falls_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "brightness.conf"
            path.write_text("99")
            self.assertEqual(brightness.read_brightness_index(str(path)), 1)
            path.write_text("-5")
            self.assertEqual(brightness.read_brightness_index(str(path)), 0)
            path.write_text("not-a-number")
            self.assertEqual(brightness.read_brightness_index(str(path), default=1), 1)

    def test_state_exposes_original_profile_values(self):
        with mock.patch.object(brightness, "read_brightness_index", return_value=0):
            state = brightness.BrightnessState()
        self.assertEqual(state.name, "Full")
        self.assertEqual(state.scale, 1.0)
        state._apply_index(1)
        self.assertEqual(state.name, "Dim")
        self.assertEqual(state.scale, 0.35)


class AvalancheNormalizationTests(unittest.TestCase):
    """Provider-specific payloads must keep producing the common forecast shape."""

    def test_avalanche_canada_danger_and_summary(self):
        payload = {
            "report": {
                "highlights": "<p>Storm slab remains reactive.</p>",
                "dangerRatings": [
                    {
                        "ratings": {
                            "alp": {"rating": {"display": "High"}},
                            "tln": {"rating": {"display": "Considerable"}},
                            "btl": {"rating": {"display": "Moderate"}},
                        }
                    }
                ],
            }
        }
        self.assertEqual(
            avalanche._extract_danger(payload),
            {
                "alpine": "High",
                "treeline": "Considerable",
                "below_treeline": "Moderate",
            },
        )
        self.assertIn("Storm slab remains reactive.", avalanche._extract_summary(payload))

    def test_latest_nwac_product_uses_zone_and_published_time(self):
        products = [
            {
                "id": "10",
                "published_time": "2026-01-01T10:00:00Z",
                "forecast_zone": [{"zone_id": "2", "name": "Stevens Pass"}],
            },
            {
                "id": "11",
                "published_time": "2026-01-01T12:00:00Z",
                "forecast_zone": [{"zone_id": "2", "name": "Stevens Pass"}],
            },
        ]
        self.assertEqual(
            avalanche._pick_latest_nwac_product_id(products, zone_id="2"),
            11,
        )

    def test_caic_tie_prefers_product_with_fewer_zones(self):
        products = [
            {
                "id": "20",
                "published_time": "2026-01-01T12:00:00Z",
                "forecast_zone": [
                    {"name": "Ten Mile Range"},
                    {"name": "Gore Range"},
                ],
            },
            {
                "id": "21",
                "published_time": "2026-01-01T12:00:00Z",
                "forecast_zone": [{"name": "Ten Mile Range"}],
            },
        ]
        self.assertEqual(
            avalanche._pick_latest_caic_product_id(products, "Ten Mile Range"),
            21,
        )


class ResortSelectionTests(unittest.TestCase):
    """Country/region filters retain metadata order and legacy catch-all labels."""

    META = {
        "Sun Peaks": {"country": "Canada", "region": "British Columbia"},
        "Stevens Pass": {"country": "USA", "region": "Washington"},
        "Mystery Hill": {"country": "", "region": ""},
    }

    def test_filter_lists_and_active_resorts(self):
        self.assertEqual(
            resorts.get_countries(self.META),
            ["All Countries", "Canada", "Other", "USA"],
        )
        self.assertEqual(
            resorts.get_regions(self.META, "Canada"),
            ["All Regions", "British Columbia"],
        )
        self.assertEqual(
            resorts.get_active_resorts("USA", "Washington", self.META),
            ["Stevens Pass"],
        )
        self.assertEqual(
            resorts.get_active_resorts("Other", "Other", self.META),
            ["Mystery Hill"],
        )

    def test_selected_region_accepts_legacy_all_resorts_label(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "region.conf"
            path.write_text("All Resorts")
            self.assertEqual(
                resorts._read_selected_region(str(path)),
                resorts.ALL_REGIONS_LABEL,
            )

    def test_ski_hill_dev_mode_retains_stub_values_without_network(self):
        hill = resorts.skiHill("Test Hill", "", 0, 0, 0)
        with mock.patch.object(resorts, "DEV_MODE", True):
            hill.getSnow()
        self.assertEqual((hill.newSnow, hill.weekSnow, hill.baseSnow), (1, 3, 120))


class AlarmPersistenceTests(unittest.TestCase):
    """Alarm files and the in-memory cache remain synchronized after extraction."""

    def test_save_then_force_reload_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            alarm_path = str(Path(temp_dir) / "alarm.conf")
            original_cache = alarms._alarm_cfg_cache
            try:
                alarms._alarm_cfg_cache = None
                with mock.patch.object(alarms, "ALARM_CONF_FILE", alarm_path):
                    config = alarms._default_alarm_cfg()
                    config["active"] = True
                    config["triggered_snow"] = "10"
                    alarms.save_alarm_cfg(config)
                    loaded = alarms.load_alarm_cfg(force_reload=True)
                self.assertTrue(loaded["active"])
                self.assertEqual(loaded["triggered_snow"], "10")
            finally:
                alarms._alarm_cfg_cache = original_cache


class LedMappingTests(unittest.TestCase):
    """The LED module is exercised with an in-memory stand-in for rpi_ws281x."""

    @staticmethod
    def _fake_ws281x_module():
        module = types.ModuleType("rpi_ws281x")

        class FakeStrip:
            def __init__(self, count, *args, **kwargs):
                self.count = count
                self.colors = [(0, 0, 0)] * count

            def begin(self):
                pass

            def numPixels(self):
                return self.count

            def setPixelColor(self, index, color):
                self.colors[index] = color

            def show(self):
                pass

        module.PixelStrip = FakeStrip
        module.Color = lambda red, green, blue: (red, green, blue)
        module.ws = types.SimpleNamespace(WS2811_STRIP_GRB=1)
        return module

    def test_color_anchors_and_delta_speed_match_original_mapping(self):
        fake_module = self._fake_ws281x_module()
        sys.modules.pop("snowscraper_app.leds", None)
        with mock.patch.dict(sys.modules, {"rpi_ws281x": fake_module}):
            leds = importlib.import_module("snowscraper_app.leds")
            controller = leds.SnowLEDs()
            self.assertEqual(controller._color_for_cm(1), (168, 216, 255))
            self.assertEqual(controller._color_for_cm(10), (128, 0, 255))
            self.assertEqual(controller._color_for_cm(20), (255, 0, 0))
            self.assertEqual(controller._breath_period_for_delta(1), 8.0)
            self.assertEqual(controller._breath_period_for_delta(10), 1.5)
            controller.clear()


class SystemContractTests(unittest.TestCase):
    """Appliance paths and service names are behavior-sensitive deployment contracts."""

    def test_update_and_heartbeat_constants_are_unchanged(self):
        self.assertEqual(system.REPO_URL, "https://github.com/spellbin/snowscraper.git")
        self.assertEqual(system.LOCAL_REPO_PATH, "/home/pi/snowscraper")
        self.assertEqual(system.SERVICE_NAME, "snowscraper.service")
        self.assertEqual(system.UPDATER_UNIT, "snowgui-updater")
        self.assertEqual(system.HEARTBEAT_RAM_FILE, "/run/heartbeat.txt")

    def test_systemd_update_builds_and_launches_transient_unit(self):
        completed = types.SimpleNamespace(stdout="Running as unit test.service\n", stderr="")
        with mock.patch.object(system.os, "geteuid", return_value=0):
            with mock.patch.object(
                system.subprocess,
                "run",
                return_value=completed,
            ) as run:
                self.assertTrue(system._systemd_run_update("v1.2.3"))
        launched_command = run.call_args_list[-1].args[0]
        self.assertEqual(launched_command[0], "systemd-run")
        self.assertIn("VER=v1.2.3", launched_command)


if __name__ == "__main__":
    unittest.main()
