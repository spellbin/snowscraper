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

from snowscraper_app import alarms, avalanche, brightness, health, resorts, storage, system


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

    def test_resort_point_uses_api_backed_metadata_loader(self):
        api_only = {
            "API-only Resort": {
                "slug": "API_only_Resort",
                "lat": 51.25,
                "lon": -120.75,
            }
        }
        with mock.patch.object(resorts, "load_resort_meta", return_value=api_only):
            self.assertEqual(
                avalanche._get_resort_point("API-only Resort"),
                (51.25, -120.75),
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
        self.assertEqual(hill.daySnow, 1)


class ResortSnowApiTests(unittest.TestCase):
    """Snow API transport, payload, fallback, and null semantics."""

    def setUp(self):
        resorts.clear_resort_meta_cache()

    def tearDown(self):
        resorts.clear_resort_meta_cache()

    def test_urls_use_https_api_root_and_canonical_slug(self):
        with mock.patch.dict(
            resorts.os.environ,
            {"SNOW_API_BASE_URL": "https://snow.example/api/snow/"},
        ):
            self.assertEqual(
                resorts.snow_api_url("current", "O'Brien-Hill"),
                "https://snow.example/api/snow/current/O%27Brien-Hill",
            )
            self.assertEqual(
                resorts.snow_history_url("Sun Peaks"),
                "https://snow.example/api/snow/history30/Sun_Peaks",
            )

    def test_url_preserves_the_canonical_val_disere_slug(self):
        with mock.patch.dict(
            resorts.os.environ,
            {"SNOW_API_BASE_URL": "https://snow.example/api/snow"},
        ):
            self.assertEqual(
                resorts.snow_api_url("current", "VAL D'ISÈRE"),
                "https://snow.example/api/snow/current/VAL_D%27IS%C3%88RE",
            )

    def test_api_metadata_normalizes_and_preserves_server_order(self):
        payload = {
            "resorts": [
                {
                    "name": "Sun Peaks",
                    "slug": "Sun_Peaks",
                    "country": "CA",
                    "region": "BC",
                    "lat": 50.883,
                    "lon": -119.885,
                },
                {
                    "name": "Whistler",
                    "slug": "Whistler",
                    "country": "CA",
                    "region": "BC",
                    "lat": 50.113,
                    "lon": -122.954,
                },
            ]
        }
        with mock.patch.object(resorts, "_snow_api_get", return_value=payload):
            meta = resorts.load_resort_meta(force_refresh=True)
        self.assertEqual(list(meta), ["Sun Peaks", "Whistler"])
        self.assertEqual(meta["Sun Peaks"]["slug"], "Sun_Peaks")

    def test_transport_uses_json_headers_timeout_and_configured_base(self):
        payload = {"current": {"newSnow": 3}}
        response = mock.Mock()
        response.content = b"{}"
        response.json.return_value = payload
        with mock.patch.dict(
            resorts.os.environ,
            {
                "SNOW_API_BASE_URL": "https://snow.example/api/snow",
                "SNOW_API_TIMEOUT_SECONDS": "4.5",
            },
        ):
            with mock.patch.object(
                resorts.requests,
                "get",
                return_value=response,
            ) as get:
                self.assertEqual(
                    resorts._snow_api_get("current", "Sun Peaks"),
                    payload,
                )
        get.assert_called_once_with(
            "https://snow.example/api/snow/current/Sun_Peaks",
            timeout=4.5,
            headers={
                "User-Agent": resorts.SNOW_API_USER_AGENT,
                "Accept": "application/json",
            },
        )
        response.raise_for_status.assert_called_once_with()

    def test_metadata_uses_bundled_fallback_after_api_failure(self):
        fallback = {
            "Sun Peaks": {
                "slug": "Sun_Peaks",
                "country": "CA",
                "region": "BC",
                "lat": 50.883,
                "lon": -119.885,
            }
        }
        with mock.patch.object(
            resorts,
            "_snow_api_get",
            side_effect=resorts.SnowApiError("offline"),
        ):
            with mock.patch.object(
                resorts,
                "_load_local_resort_meta",
                return_value=fallback,
            ):
                self.assertEqual(
                    resorts.load_resort_meta(force_refresh=True),
                    fallback,
                )
        self.assertEqual(resorts._meta_cache_source, "offline")

    def test_failed_refresh_retains_last_full_api_universe(self):
        canonical = {
            "resorts": [
                {
                    "name": "Sun Peaks",
                    "slug": "Sun_Peaks",
                    "country": "CA",
                    "region": "BC",
                    "lat": 50.883,
                    "lon": -119.885,
                },
                {
                    "name": "API-only Resort",
                    "slug": "API_only_Resort",
                    "country": "CA",
                    "region": "BC",
                    "lat": 51.0,
                    "lon": -120.0,
                },
            ]
        }
        with mock.patch.object(resorts, "_snow_api_get", return_value=canonical):
            first = resorts.load_resort_meta(force_refresh=True)
        with mock.patch.object(
            resorts,
            "_snow_api_get",
            side_effect=resorts.SnowApiError("temporary outage"),
        ):
            with mock.patch.object(
                resorts,
                "_load_local_resort_meta",
                return_value={"Sun Peaks": {}},
            ):
                refreshed = resorts.load_resort_meta(force_refresh=True)
        self.assertIs(refreshed, first)
        self.assertIn("API-only Resort", refreshed)
        self.assertEqual(resorts._meta_cache_source, "api")

    def test_current_payload_preserves_null_and_verified_zero(self):
        payload = {
            "current": {
                "date": "2026-08-06",
                "newSnow": None,
                "daySnow": 0,
                "weekSnow": None,
                "baseSnow": 120,
            },
            "freshness": {"level": "suspect"},
            "source": {"provider": "Fixture", "url": "https://example.test"},
        }
        hill = resorts.skiHill("Sun Peaks", "", 8, 20, 100)
        with mock.patch.object(resorts, "_load_resort_json", return_value=payload):
            with mock.patch.object(resorts, "log_snow_data"):
                with mock.patch.object(
                    resorts.health_reporter, "record_snow_fetch_success"
                ) as report_success:
                    hill.getSnow()
        self.assertIsNone(hill.newSnow)
        self.assertEqual(hill.daySnow, 0)
        self.assertIsNone(hill.weekSnow)
        self.assertEqual(hill.baseSnow, 120)
        self.assertEqual(hill.freshness, {"level": "suspect"})
        report_success.assert_called_once_with("Sun Peaks")

    def test_current_request_failure_keeps_last_successful_values(self):
        hill = resorts.skiHill("Sun Peaks", "", 8, 20, 100)
        hill.daySnow = 7
        with mock.patch.object(
            resorts,
            "_load_resort_json",
            side_effect=resorts.SnowApiError("network down"),
        ):
            with mock.patch.object(
                resorts.health_reporter, "record_snow_fetch_failure"
            ) as report_failure:
                with self.assertRaises(resorts.SnowApiError):
                    hill.getSnow()
        self.assertEqual(
            (hill.newSnow, hill.daySnow, hill.weekSnow, hill.baseSnow),
            (8, 7, 20, 100),
        )
        report_failure.assert_called_once()
        self.assertEqual(report_failure.call_args.args[1], "Sun Peaks")

    def test_history_requires_the_contract_history_list(self):
        with mock.patch.object(
            resorts,
            "_snow_api_get",
            return_value={"history": [{"date": "2026-08-06", "daySnow": 4}]},
        ):
            payload = resorts.fetch_snow_history("Sun Peaks")
        self.assertEqual(payload["history"][0]["daySnow"], 4)

        with mock.patch.object(resorts, "_snow_api_get", return_value={"history": None}):
            with self.assertRaises(resorts.SnowApiError):
                resorts.fetch_snow_history("Sun Peaks")


class RemoteHealthReporterTests(unittest.TestCase):
    """Anonymous reporting remains persistent, optional, minimal, and fail-soft."""

    def test_reporter_generates_persistent_id_and_sends_minimal_payload(self):
        env = {
            "SNOWSCRAPER_HEARTBEAT_URL": "https://backend.example/api/v1/snowscraper/heartbeat",
            "SNOWSCRAPER_HEARTBEAT_TIMEOUT_SECONDS": "4.5",
        }
        response = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "health.json"
            with mock.patch.dict(health.os.environ, env, clear=True):
                reporter = health.RemoteHealthReporter(state_path=state_path)
            reporter.set_app_version("2.3.0")
            reporter.record_snow_fetch_success("Sun Peaks")

            with mock.patch.object(health.requests, "post", return_value=response) as post:
                self.assertTrue(reporter.send_once())

            payload = post.call_args.kwargs["json"]
            self.assertRegex(payload["scraper_id"], r"^ss_[0-9a-f]{32}$")
            self.assertNotIn("hostname", payload)
            self.assertEqual(payload["app_version"], "2.3.0")
            self.assertEqual(payload["selected_resort"], "Sun Peaks")
            self.assertIsNotNone(payload["last_snow_fetch_at"])
            self.assertIsNone(payload["last_error"])
            self.assertIsInstance(payload["uptime_seconds"], int)
            self.assertTrue(payload["reported_at"].endswith("Z"))
            self.assertEqual(post.call_args.kwargs["timeout"], 4.5)
            self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

            # Recreating the reporter simulates a git update/restart. The
            # pseudonymous identity must survive through the ignored state file.
            second = health.RemoteHealthReporter(state_path=state_path)
            self.assertEqual(second.scraper_id, reporter.scraper_id)
            self.assertTrue(second.reporting_enabled)
        response.raise_for_status.assert_called_once_with()

    def test_fetch_failure_is_reported_without_erasing_last_success_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reporter = health.RemoteHealthReporter(
                state_path=Path(temp_dir) / "health.json"
            )
            reporter.record_snow_fetch_success("Whistler")
            successful_at = reporter.payload()["last_snow_fetch_at"]
            reporter.record_snow_fetch_failure("timeout", "Whistler")
            payload = reporter.payload()
            self.assertEqual(payload["last_snow_fetch_at"], successful_at)
            self.assertEqual(payload["last_error"], "timeout")

    def test_customer_opt_out_persists_and_stops_network_reporting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "health.json"
            reporter = health.RemoteHealthReporter(state_path=state_path)
            self.assertTrue(reporter.set_reporting_enabled(False))
            with mock.patch.object(health.requests, "post") as post:
                self.assertFalse(reporter.send_once())
            post.assert_not_called()

            restarted = health.RemoteHealthReporter(state_path=state_path)
            self.assertFalse(restarted.reporting_enabled)
            self.assertEqual(restarted.scraper_id, reporter.scraper_id)

    def test_malformed_state_is_replaced_without_blocking_startup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "health.json"
            state_path.write_text("not-json", encoding="utf-8")
            reporter = health.RemoteHealthReporter(state_path=state_path)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["scraper_id"], reporter.scraper_id)
            self.assertTrue(persisted["reporting_enabled"])


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
