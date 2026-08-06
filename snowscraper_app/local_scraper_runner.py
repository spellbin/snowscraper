"""Child-process runner for one user-created BeautifulSoup scraper module."""

import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
import sys

import requests

from .local_scrapers import LocalScraperError, load_module, normalize_scrape_result


MAX_HTML_BYTES = 2 * 1024 * 1024
USER_AGENT = "SnowGUI-LocalScraper/1.0 (+https://www.snowscraper.ca)"
MAX_DIAGNOSTIC_CHARS = 8192


class _CappedDiagnosticLog:
    """File-like sink that prevents accidental print loops filling Pi memory."""

    def __init__(self, limit=MAX_DIAGNOSTIC_CHARS):
        self.limit = limit
        self.value = ""

    def write(self, text):
        self.value = (self.value + str(text))[-self.limit:]
        return len(str(text))

    def flush(self):
        return None


def _read_fixture(path: Path) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise LocalScraperError(f"cannot read fixture {path}: {exc}") from exc
    if len(content) > MAX_HTML_BYTES:
        raise LocalScraperError(f"fixture exceeds {MAX_HTML_BYTES} bytes")
    return content


def _fetch_html(url: str, timeout_seconds: float) -> bytes:
    """Download a bounded HTML response without loading an unlimited body."""
    try:
        with requests.get(
            url,
            timeout=(5.0, timeout_seconds),
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            stream=True,
        ) as response:
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").lower()
            if content_type and "html" not in content_type:
                raise LocalScraperError(
                    f"source returned {content_type!r}, expected an HTML page"
                )
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_HTML_BYTES:
                    raise LocalScraperError(
                        f"source HTML exceeds the {MAX_HTML_BYTES}-byte Pi safety limit"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except LocalScraperError:
        raise
    except requests.RequestException as exc:
        raise LocalScraperError(f"HTML request failed: {exc}") from exc


def _load_user_code(code_path: Path):
    spec = importlib.util.spec_from_file_location(
        f"snowscraper_local_{code_path.parent.name}", code_path
    )
    if spec is None or spec.loader is None:
        raise LocalScraperError(f"cannot import {code_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise LocalScraperError(f"cannot import scraper.py: {exc}") from exc
    scrape = getattr(module, "scrape", None)
    if not callable(scrape):
        raise LocalScraperError("scraper.py must define def scrape(soup):")
    return scrape


def execute(module_directory, fixture_path=None) -> dict:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise LocalScraperError(
            "BeautifulSoup is not installed; run: sudo pip3 install beautifulsoup4"
        ) from exc
    manifest = load_module(module_directory)
    html = (
        _read_fixture(Path(fixture_path))
        if fixture_path is not None
        else _fetch_html(manifest.source_url, manifest.timeout_seconds)
    )
    soup = BeautifulSoup(html, "html.parser")
    diagnostics = _CappedDiagnosticLog()
    # Capture prints made while importing user code as well as prints made by
    # scrape(). Otherwise a harmless beginner debugging statement at module
    # scope would corrupt the single JSON document written to stdout.
    with contextlib.redirect_stdout(diagnostics):
        scrape = _load_user_code(manifest.code_path)
    try:
        with contextlib.redirect_stdout(diagnostics):
            result = scrape(soup)
    except Exception as exc:
        raise LocalScraperError(f"scrape(soup) raised {type(exc).__name__}: {exc}") from exc
    if diagnostics.value.strip():
        print(diagnostics.value.strip(), file=sys.stderr)
    current = normalize_scrape_result(result)
    return {
        "current": current,
        "source": {
            "provider": "local_beautifulsoup",
            "module": manifest.module_id,
            "url": manifest.source_url,
        },
        "local_scraper": {"module_id": manifest.module_id},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("module_directory")
    parser.add_argument("--fixture")
    args = parser.parse_args(argv)
    try:
        payload = execute(args.module_directory, args.fixture)
    except LocalScraperError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
