#!/usr/bin/env python3
"""Beginner-friendly manager for local BeautifulSoup scraper modules.

Run ``python3 scraperctl.py --help`` for commands. User modules live in the
gitignored ``conf/local_scrapers`` directory, so SnowScraper's updater preserves
them. The CLI intentionally performs one small action at a time and prints the
next command a first-time module author should run.
"""

import argparse
from pathlib import Path
import shutil
import sys

from snowscraper_app.local_scrapers import (
    APP_ROOT,
    ENABLED_MARKER_NAME,
    LocalScraperError,
    MODULE_ID_RE,
    discover_modules,
    load_module,
    module_root,
    run_local_scraper,
)
from snowscraper_app.storage import atomic_write_text


TEMPLATE_ROOT = APP_ROOT / "templates" / "local_scraper"


def _one_line(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not value or "\n" in value or "\r" in value:
        raise LocalScraperError(f"{label} must be a non-empty single line")
    return value


def _prompt(value, label: str, default=None, required=True) -> str:
    if value is not None:
        return str(value).strip()
    suffix = f" [{default}]" if default not in (None, "") else ""
    answer = input(f"{label}{suffix}: ").strip()
    if answer:
        return answer
    if default is not None:
        return str(default)
    if required:
        raise LocalScraperError(f"{label} is required")
    return ""


def _coordinate(value: str, label: str, minimum: float, maximum: float) -> str:
    """Validate an optional coordinate before creating any module files."""
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        number = float(value)
    except ValueError as exc:
        raise LocalScraperError(f"{label} must be a decimal number") from exc
    if not minimum <= number <= maximum:
        raise LocalScraperError(f"{label} must be between {minimum:g} and {maximum:g}")
    return value


def _create(args) -> int:
    print("Create a local BeautifulSoup scraper")
    print("The new module starts DISABLED so it cannot affect the display before testing.\n")
    module_id = _prompt(args.module_id, "Short module ID (lowercase, no spaces)")
    if not MODULE_ID_RE.fullmatch(module_id):
        raise LocalScraperError(
            f"module ID must match {MODULE_ID_RE.pattern}; example: my_mountain"
        )
    resort_name = _one_line(
        _prompt(args.name, "Resort name shown on SnowScraper"), "resort name"
    )
    source_url = _one_line(
        _prompt(args.url, "Public snow-report page URL"), "source URL"
    )
    if not source_url.startswith(("https://", "http://")):
        raise LocalScraperError("source URL must begin with https:// or http://")
    country = _prompt(args.country, "Country", default="", required=False)
    region = _prompt(args.region, "Province/state/region", default="", required=False)
    if country:
        country = _one_line(country, "country")
    if region:
        region = _one_line(region, "region")
    latitude = _coordinate(
        _prompt(args.latitude, "Latitude (optional)", default="", required=False),
        "latitude",
        -90,
        90,
    )
    longitude = _coordinate(
        _prompt(args.longitude, "Longitude (optional)", default="", required=False),
        "longitude",
        -180,
        180,
    )

    root = module_root(args.root)
    destination = root / module_id
    if destination.exists():
        raise LocalScraperError(f"module already exists: {destination}")
    if not TEMPLATE_ROOT.is_dir():
        raise LocalScraperError(f"template directory is missing: {TEMPLATE_ROOT}")

    destination.mkdir(parents=True)
    shutil.copy2(TEMPLATE_ROOT / "scraper.py", destination / "scraper.py")
    module_readme = (TEMPLATE_ROOT / "README.md").read_text(encoding="utf-8")
    atomic_write_text(
        module_readme.replace("YOUR_MODULE_ID", module_id),
        str(destination / "README.md"),
    )
    (destination / "fixtures").mkdir()
    shutil.copy2(
        TEMPLATE_ROOT / "fixtures" / "sample.html",
        destination / "fixtures" / "sample.html",
    )
    manifest = (TEMPLATE_ROOT / "module.ini.template").read_text(encoding="utf-8")
    replacements = {
        "{{RESORT_NAME}}": resort_name,
        "{{SOURCE_URL}}": source_url,
        "{{COUNTRY}}": country,
        "{{REGION}}": region,
        "{{LATITUDE}}": latitude,
        "{{LONGITUDE}}": longitude,
        "{{FALLBACK_TO_SNOW_API}}": "false" if args.no_fallback else "true",
    }
    for marker, replacement in replacements.items():
        manifest = manifest.replace(marker, replacement)
    atomic_write_text(manifest, str(destination / "module.ini"))
    # Validate the generated manifest before telling the user creation
    # succeeded. A partial folder remains visible for straightforward repair.
    load_module(destination)

    print(f"\nCreated: {destination}")
    print("Next steps:")
    print(f"  1. Read {destination / 'README.md'}")
    print(f"  2. Edit {destination / 'scraper.py'}")
    print(f"  3. Test sample HTML: python3 scraperctl.py test {module_id} --sample")
    print(f"  4. Test live page:   python3 scraperctl.py test {module_id}")
    print(f"  5. Enable:          python3 scraperctl.py enable {module_id}")
    print("  6. Restart SnowScraper so a new local-only resort appears in the picker.")
    return 0


def _list(args) -> int:
    modules, errors = discover_modules(args.root)
    root = module_root(args.root)
    print(f"Local scraper directory: {root}")
    if not modules and not errors:
        print("No modules yet. Create one with: python3 scraperctl.py create")
        return 0
    if modules:
        print("\nSTATUS    MODULE ID                 RESORT")
        print("--------  ------------------------  ------------------------------")
        for module in modules:
            status = "ENABLED" if module.enabled else "disabled"
            print(f"{status:<8}  {module.module_id:<24}  {module.resort_name}")
    if errors:
        print("\nModules needing attention:")
        for error in errors:
            print(f"  - {error}")
        return 1
    return 0


def _set_enabled(args, enabled: bool) -> int:
    directory = module_root(args.root) / args.module_id
    module = load_module(directory)
    marker = directory / ENABLED_MARKER_NAME
    if enabled:
        atomic_write_text("enabled\n", str(marker))
        print(f"Enabled '{module.module_id}' for {module.resort_name}.")
        print("Restart SnowScraper if this module adds a new resort to the picker.")
    else:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        print(f"Disabled '{module.module_id}'. The local scraper will no longer run.")
        print("Restart SnowScraper to remove a local-only resort from the picker.")
    return 0


def _test(args) -> int:
    module = load_module(module_root(args.root) / args.module_id)
    fixture = args.fixture
    if args.sample:
        fixture = module.directory / "fixtures" / "sample.html"
    mode = f"fixture {fixture}" if fixture else f"live page {module.source_url}"
    print(f"Testing '{module.module_id}' against {mode}...")
    payload = run_local_scraper(module, fixture_path=fixture)
    current = payload["current"]
    print("\nPASS — normalized values:")
    for field in ("date", "newSnow", "daySnow", "weekSnow", "baseSnow"):
        value = current.get(field)
        suffix = " cm" if field != "date" and value is not None else ""
        print(f"  {field:<10} {value}{suffix}")
    if not module.enabled:
        print(f"\nThe module is still disabled. Enable it with:")
        print(f"  python3 scraperctl.py enable {module.module_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, test, enable, and disable local BeautifulSoup snow scrapers."
    )
    parser.add_argument(
        "--root",
        help="advanced: alternate module directory (default: conf/local_scrapers)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a guided module from the template")
    create.add_argument("module_id", nargs="?")
    create.add_argument("--name", help="resort display name")
    create.add_argument("--url", help="public HTML snow-report URL")
    create.add_argument("--country")
    create.add_argument("--region")
    create.add_argument("--latitude")
    create.add_argument("--longitude")
    create.add_argument(
        "--no-fallback",
        action="store_true",
        help="do not fall back to Snow API if the local module fails",
    )
    create.set_defaults(handler=_create)

    listing = commands.add_parser("list", help="show modules and enabled state")
    listing.set_defaults(handler=_list)

    enable = commands.add_parser("enable", help="enable a tested module")
    enable.add_argument("module_id")
    enable.set_defaults(handler=lambda args: _set_enabled(args, True))

    disable = commands.add_parser("disable", help="disable a module without deleting it")
    disable.add_argument("module_id")
    disable.set_defaults(handler=lambda args: _set_enabled(args, False))

    test = commands.add_parser("test", help="run and validate one module")
    test.add_argument("module_id")
    source = test.add_mutually_exclusive_group()
    source.add_argument("--sample", action="store_true", help="use its included sample HTML")
    source.add_argument("--fixture", help="use an HTML file instead of the live website")
    test.set_defaults(handler=_test)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (LocalScraperError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
