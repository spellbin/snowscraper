# Creating a local BeautifulSoup snow scraper

This guide is written for someone who is comfortable editing a few lines of
text but may never have written a Python program. You do not need to understand
the SnowScraper GUI, make network requests, parse JSON, or use Playwright.

## What a local module does

A local module teaches one Raspberry Pi how to read snow numbers from a public
HTML page. SnowScraper downloads that page, parses it with BeautifulSoup, runs
your small `scrape(soup)` function in a separate process, checks every returned
value, and attaches the result to the existing display and LED behavior.

Local modules can:

- override the current Snow API reading for an existing resort; or
- add a local-only resort to the Country / Region / Resort picker.

Local modules cannot render JavaScript. They are deliberately designed for
ordinary server-rendered HTML because Chromium and Playwright are too heavy for
a Raspberry Pi Zero 2 W. If **View Page Source** does not contain the snow
number, that page is not suitable for this lightweight system.

## Safety and resource limits

- A module is disabled until you explicitly enable it.
- Testing never enables a module.
- A module runs in a child process, not inside the touchscreen event loop.
- HTTP and execution time are bounded by `timeout_seconds` (3–60 seconds).
- HTML responses larger than 2 MiB are rejected.
- Results must contain known fields, sensible centimetre values, and at least
  one actual measurement.
- An existing resort falls back to the normal Snow API by default when its
  local module fails.
- A module is regular Python code and is **not a security sandbox**. Install
  modules only from people you trust, or inspect the short `scraper.py` first.

Respect the website's terms of use and robots policy. SnowScraper refreshes
periodically, not continuously, but a site owner can still prohibit scraping.

## Five-minute quick start

From `/home/pi/snowscraper`, run:

    python3 -c "from bs4 import BeautifulSoup; print('BeautifulSoup is ready')"

If that reports `ModuleNotFoundError`, install the one lightweight parser
dependency and repeat the check:

    sudo pip3 install beautifulsoup4

Then start the guided creator:

    python3 scraperctl.py create

The guided questions are:

1. **Module ID** — a short lowercase folder name, for example `my_mountain`.
2. **Resort name** — the exact touchscreen display name. Use an existing name
   exactly to override it, or a new name to add a local-only resort.
3. **Public snow-report URL** — the HTML page containing the measurements.
4. **Country and region** — where a new resort appears in the picker.
5. **Latitude and longitude** — optional, but recommended for avalanche lookup
   on a new resort.

Creation prints the exact path and next commands. The resulting directory is:

    conf/local_scrapers/my_mountain/
    ├── module.ini
    ├── scraper.py
    ├── README.md
    └── fixtures/
        └── sample.html

It has no `ENABLED` marker yet, so the running GUI ignores it.

First verify the unmodified template:

    python3 scraperctl.py test my_mountain --sample

Expected values are 5, 8, 31, and 142 cm. Then edit `scraper.py`, replace its
four example CSS selectors, and test the real site:

    python3 scraperctl.py test my_mountain

Only after the real values are correct:

    python3 scraperctl.py enable my_mountain

For a newly added resort, restart the service so the picker reloads metadata:

    sudo systemctl restart snowscraper.service

## Finding a good CSS selector

Open the source URL in Firefox or Chrome on a normal computer:

1. Find the visible snow number.
2. Right-click it and select **Inspect**.
3. Look for the smallest element containing just that measurement.
4. Prefer a meaningful class, ID, or `data-*` attribute.

Given this HTML:

    <span class="snowfall-24h">12 cm</span>

the selector and extraction are:

    day_snow_cm = number_from_text(soup.select_one(".snowfall-24h"))

Given:

    <div id="base-depth"><strong>185</strong> cm</div>

use:

    base_snow_cm = number_from_text(soup.select_one("#base-depth"))

Given:

    <li data-period="7-day">43 cm</li>

use:

    week_snow_cm = number_from_text(soup.select_one("[data-period='7-day']"))

Avoid `nth-child` and long parent chains. They describe today's layout rather
than the meaning of the number and tend to break during routine redesigns.

## Required Python function

`scraper.py` must define exactly this callable entry point:

    def scrape(soup):
        return {
            "newSnow": ...,
            "daySnow": ...,
            "weekSnow": ...,
            "baseSnow": ...,
        }

The framework provides `soup`; do not call `requests.get()` yourself. You may
split complicated parsing into helper functions in the same file, but keep the
entry point named `scrape`.

### Return-field meanings

| Field | Meaning | Allowed value |
|---|---|---|
| `newSnow` | Snow since the resort's latest report | centimetres or `None` |
| `daySnow` | Resort's published 24-hour/daily total | centimetres or `None` |
| `weekSnow` | Published seven-day total | centimetres or `None` |
| `baseSnow` | Current base depth | centimetres or `None` |
| `date` | Optional observation date | `YYYY-MM-DD`; today if omitted |

`None` means “not published or not found.” A genuine displayed zero is `0`.
Never replace a missing selector with zero: that would turn “we do not know”
into “the resort verified no snow,” which can change alarms and display output.

At least one of the four measurements must be a real number. Values are rounded
to whole centimetres and must fall between 0 and 10,000 cm.

### Inches

The helper conversion is already imported by the template:

    new_snow_inches = number_from_text(soup.select_one(".new-snow"))
    new_snow_cm = inches_to_cm(new_snow_inches)

Do not multiply strings directly; always extract the number first.

### Values stored in HTML attributes

BeautifulSoup attributes are available through `.get()`:

    element = soup.select_one("[data-base-cm]")
    base_snow_cm = number_from_text(element.get("data-base-cm")) if element else None

### Dates

Usually omit `date` and let SnowScraper use today's local date. If the page has
an explicit observation date, return it only after converting to `YYYY-MM-DD`:

    return {
        "date": "2026-12-18",
        "newSnow": new_snow_cm,
        ...
    }

## Understanding `module.ini`

The generated file is heavily commented. Its important settings are:

- `resort_name`: exact display name and attachment key.
- `source_url`: one public HTML page fetched by the framework.
- `country` / `region`: picker placement for a new local-only resort.
- `latitude` / `longitude`: avalanche lookup coordinates for a new resort.
- `timeout_seconds`: 20 by default; allowed range is 3–60.
- `fallback_to_snow_api`: when true, a failed override uses the normal API.

For an existing resort, keep `fallback_to_snow_api = true`. For a completely
new local-only resort, set it to false; there is no central API record to fall
back to.

## Enable, disable, list, and test

Show every module and its state:

    python3 scraperctl.py list

Enable a tested module:

    python3 scraperctl.py enable my_mountain

Disable it without deleting or changing any code:

    python3 scraperctl.py disable my_mountain

Test its real URL while enabled or disabled:

    python3 scraperctl.py test my_mountain

Test a saved HTML page without network access:

    python3 scraperctl.py test my_mountain --fixture /path/to/report.html

Test the included known-good example:

    python3 scraperctl.py test my_mountain --sample

Enablement is just an empty file named `ENABLED` in the module folder. The CLI
manages it so there is no Boolean spelling or JSON syntax to get wrong.

## Existing resort versus local-only resort

### Override an existing resort

Use the exact existing resort name. When enabled, current readings come from
the local module. If it fails and `fallback_to_snow_api = true`, SnowScraper
logs the module error and loads the normal Snow API current reading instead.
Disabling the module immediately restores normal API behavior.

### Add a new local-only resort

Use a new resort name and fill in country, region, and coordinates. When the
module is enabled and SnowScraper restarts, metadata merging appends that resort
to the picker. Its current report, display values, LED color, health status, and
daily local log use the same `skiHill` path as built-in resorts.

The central Snow API does not know a local-only resort. SnowScraper therefore
builds the ordinary local daily log from successful readings and uses that log
as the history-chart fallback when the server history endpoint is unavailable.
The chart starts sparse and grows one daily observation at a time. Other devices
do not see this resort unless the module is copied to them.

## Troubleshooting

### “returned no snow values; check the CSS selectors”

Every selector returned no element or no number. Confirm the snow value exists
in **View Page Source**, not only in the browser's live Elements panel. If it is
created by JavaScript, BeautifulSoup cannot see it.

### “expected an HTML page”

The URL returned JSON, an image, a download, or an anti-bot page. Use the public
HTML snow-report URL. This framework intentionally does not add browser
automation.

### HTTP 403 or 429

The website rejected automated access or rate-limited the Pi. Do not bypass the
site's policy. Disable the module and use the normal Snow API where available.

### Import or syntax error

Run:

    python3 -m py_compile conf/local_scrapers/my_mountain/scraper.py

The error prints the exact line. Compare the file with
`templates/local_scraper/scraper.py`, paying special attention to quotes,
commas, indentation, and the final return dictionary.

### Wrong number extracted

`number_from_text` extracts the first number. Select the smallest element that
contains only the desired measurement. If an element says “24 hours: 12 cm,”
the first number is 24; select the nested element around `12 cm` instead.

### Module works in `--sample` but fails live

The framework is working, but the real selector, URL, content type, or site
policy differs. Run the live test and read the final error. Save the real page's
source as a fixture so selector work does not repeatedly contact the website.

### SnowScraper still shows API values

Run `python3 scraperctl.py list` and confirm the module says `ENABLED`. Confirm
`resort_name` exactly matches the selected resort. Check `logs/snowgui.log` for
`[LocalScraper]`. A failed existing-resort module deliberately falls back to the
Snow API when configured to do so.

## Updates, backups, and sharing

`conf/local_scrapers/` is ignored by Git. The built-in updater's force checkout
therefore preserves modules, their fixtures, and `ENABLED` markers. Git also
does not back them up. Copy the complete module folder somewhere safe before
replacing an SD card.

To share a module, share only its module folder after removing private fixtures
or notes. Recipients should inspect `scraper.py`, copy the folder under their own
`conf/local_scrapers/`, run both sample and live tests, and enable it themselves.
Never distribute credentials or scrape pages that require a customer login.

## Removing a module

Disable it first and confirm normal behavior:

    python3 scraperctl.py disable my_mountain

Then move its folder out of `conf/local_scrapers`. Moving it to a backup is safer
than deleting it immediately. Restart SnowScraper if it was a local-only resort.
