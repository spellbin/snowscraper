"""Contract tests for user-created BeautifulSoup scraper modules."""

import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import scraperctl
from snowscraper_app import local_scrapers, resorts


MANIFEST = """[local_scraper]
resort_name = {resort}
source_url = https://example.test/snow
country = Canada
region = British Columbia
latitude = 50.1
longitude = -120.2
timeout_seconds = 5
fallback_to_snow_api = {fallback}
"""

SCRAPER_CODE = """from snowscraper_app.local_scrapers import number_from_text
print("scraper.py imported")
def scrape(soup):
    return {
        "newSnow": number_from_text(soup.select_one(".new")),
        "daySnow": number_from_text(soup.select_one(".day")),
        "weekSnow": None,
        "baseSnow": number_from_text(soup.select_one(".base")),
    }
"""

FIXTURE = """<!doctype html><html><body>
<span class="new">4 cm</span><span class="day">7 cm</span>
<span class="base">130 cm</span></body></html>
"""


def write_module(
    root: Path,
    module_id="test_peak",
    resort="Test Peak",
    *,
    enabled=False,
    fallback=True,
):
    directory = root / module_id
    directory.mkdir(parents=True)
    (directory / "module.ini").write_text(
        MANIFEST.format(resort=resort, fallback=str(fallback).lower()),
        encoding="utf-8",
    )
    (directory / "scraper.py").write_text(SCRAPER_CODE, encoding="utf-8")
    fixture = directory / "fixture.html"
    fixture.write_text(FIXTURE, encoding="utf-8")
    if enabled:
        (directory / "ENABLED").write_text("enabled\n", encoding="utf-8")
    return local_scrapers.load_module(directory), fixture


class LocalScraperContractTests(unittest.TestCase):
    def test_helpers_and_result_validation_preserve_missing_vs_zero(self):
        self.assertEqual(local_scrapers.number_from_text("12.5 cm"), 12.5)
        self.assertEqual(local_scrapers.inches_to_cm(10), 25)
        self.assertIsNone(local_scrapers.number_from_text(None))
        current = local_scrapers.normalize_scrape_result({
            "newSnow": 0,
            "daySnow": None,
            "weekSnow": 12.6,
            "baseSnow": 140,
        })
        self.assertEqual(current["newSnow"], 0)
        self.assertIsNone(current["daySnow"])
        self.assertEqual(current["weekSnow"], 13)
        with self.assertRaises(local_scrapers.LocalScraperError):
            local_scrapers.normalize_scrape_result({"newSnow": None})
        with self.assertRaises(local_scrapers.LocalScraperError):
            local_scrapers.normalize_scrape_result({"newSnow": -1})
        with self.assertRaises(local_scrapers.LocalScraperError):
            local_scrapers.normalize_scrape_result({"newSnow": 1, "surprise": 2})

    def test_discovery_enablement_and_metadata_attachment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            disabled, _ = write_module(root, enabled=False)
            modules, errors = local_scrapers.discover_modules(root)
            self.assertEqual(errors, [])
            self.assertFalse(modules[0].enabled)
            self.assertIsNone(local_scrapers.find_enabled_module("Test Peak", root))
            self.assertEqual(
                local_scrapers.merge_enabled_module_metadata({"API Peak": {}}, root),
                {"API Peak": {}},
            )

            disabled.enabled_path.write_text("enabled\n", encoding="utf-8")
            enabled = local_scrapers.find_enabled_module("test peak", root)
            self.assertEqual(enabled.module_id, "test_peak")
            merged = local_scrapers.merge_enabled_module_metadata({"API Peak": {}}, root)
            self.assertEqual(list(merged), ["API Peak", "Test Peak"])
            self.assertEqual(merged["Test Peak"]["country"], "Canada")
            self.assertEqual(merged["Test Peak"]["lat"], 50.1)

            attached = local_scrapers.merge_enabled_module_metadata(
                {"test peak": {"country": "Canonical country"}}, root
            )
            self.assertEqual(list(attached), ["test peak"])
            self.assertEqual(attached["test peak"]["country"], "Canonical country")
            self.assertEqual(attached["test peak"]["local_scraper_id"], "test_peak")

    @unittest.skipUnless(importlib.util.find_spec("bs4"), "beautifulsoup4 not installed")
    def test_runner_parses_fixture_in_an_isolated_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module, fixture = write_module(Path(temp_dir), enabled=True)
            payload = local_scrapers.run_local_scraper(module, fixture_path=fixture)
            self.assertEqual(payload["current"]["newSnow"], 4)
            self.assertEqual(payload["current"]["daySnow"], 7)
            self.assertIsNone(payload["current"]["weekSnow"])
            self.assertEqual(payload["current"]["baseSnow"], 130)
            self.assertEqual(payload["source"]["provider"], "local_beautifulsoup")

    def test_resort_fetch_uses_module_and_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module, _ = write_module(Path(temp_dir), enabled=True, fallback=True)
            local_payload = {
                "current": {"newSnow": 4, "daySnow": 7, "weekSnow": None, "baseSnow": 130},
                "source": {"provider": "local_beautifulsoup", "url": module.source_url},
            }
            with mock.patch.object(resorts, "find_enabled_module", return_value=module):
                with mock.patch.object(resorts, "run_local_scraper", return_value=local_payload):
                    with mock.patch.object(resorts, "_snow_api_get") as api_get:
                        self.assertIs(resorts.fetch_current_snow("Test Peak"), local_payload)
            api_get.assert_not_called()

            api_payload = {"current": {"newSnow": 2}}
            with mock.patch.object(resorts, "find_enabled_module", return_value=module):
                with mock.patch.object(
                    resorts,
                    "run_local_scraper",
                    side_effect=local_scrapers.LocalScraperError("selector broke"),
                ):
                    with mock.patch.object(resorts, "_snow_api_get", return_value=api_payload):
                        self.assertEqual(resorts.fetch_current_snow("Test Peak"), api_payload)

    def test_local_only_history_uses_existing_daily_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module, _ = write_module(Path(temp_dir), enabled=True, fallback=False)
            log_path = Path(temp_dir) / "snow_log.json"
            log_path.write_text(json.dumps({
                "Test Peak": {
                    "history": [{"date": "2026-08-06", "daySnow": 7, "baseSnow": 130}]
                }
            }), encoding="utf-8")
            with mock.patch.object(resorts, "SNOW_LOG_FILE", str(log_path)):
                with mock.patch.object(resorts, "find_enabled_module", return_value=module):
                    with mock.patch.object(
                        resorts,
                        "_snow_api_get",
                        side_effect=resorts.SnowApiError("not in API"),
                    ):
                        payload = resorts.fetch_snow_history("Test Peak")
            self.assertEqual(payload["history"][0]["daySnow"], 7)
            self.assertEqual(payload["source"]["provider"], "local_log")


class ScraperCtlTests(unittest.TestCase):
    def test_guided_commands_create_test_enable_and_disable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common = ["--root", str(root)]
            created = scraperctl.main(common + [
                "create", "beginner_peak",
                "--name", "Beginner Peak",
                "--url", "https://example.test/report",
                "--country", "Canada",
                "--region", "British Columbia",
                "--latitude", "50.2",
                "--longitude=-120.3",
                "--no-fallback",
            ])
            self.assertEqual(created, 0)
            module = local_scrapers.load_module(root / "beginner_peak")
            self.assertFalse(module.enabled)
            generated_readme = (module.directory / "README.md").read_text(encoding="utf-8")
            self.assertIn("scraperctl.py test beginner_peak --sample", generated_readme)
            self.assertNotIn("YOUR_MODULE_ID", generated_readme)
            sample_payload = {
                "current": {
                    "date": "2026-08-06",
                    "newSnow": 5,
                    "daySnow": 8,
                    "weekSnow": 31,
                    "baseSnow": 142,
                }
            }
            with mock.patch.object(
                scraperctl, "run_local_scraper", return_value=sample_payload
            ):
                self.assertEqual(
                    scraperctl.main(common + ["test", "beginner_peak", "--sample"]),
                    0,
                )
            self.assertEqual(scraperctl.main(common + ["enable", "beginner_peak"]), 0)
            self.assertTrue(local_scrapers.load_module(module.directory).enabled)
            self.assertEqual(scraperctl.main(common + ["list"]), 0)
            self.assertEqual(scraperctl.main(common + ["disable", "beginner_peak"]), 0)
            self.assertFalse(local_scrapers.load_module(module.directory).enabled)


if __name__ == "__main__":
    unittest.main()
