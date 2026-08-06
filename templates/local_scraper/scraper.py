"""BeautifulSoup scraper template.

For most resorts, the only lines you need to change are the four CSS selectors
inside scrape(). Start with the included fixtures/sample.html, then use your web
browser's Inspect tool to find equivalent selectors on the real snow-report page.

The SnowScraper framework already downloads the configured source_url, enforces
a timeout and response-size limit, parses the HTML with BeautifulSoup, and runs
this file in a separate process. Do not add requests or Playwright here.
"""

from snowscraper_app.local_scrapers import inches_to_cm, number_from_text


def scrape(soup):
    """Return the current snow measurements found in ``soup``.

    Every snow field must be centimetres or None. None means the source did not
    publish a value; it is different from a verified zero. The framework adds
    today's date when ``date`` is omitted.
    """

    # CSS selector examples:
    #   ".new-snow"                 element with class="new-snow"
    #   "#base-depth"               element with id="base-depth"
    #   "[data-snow='seven-day']"   element with a matching data attribute
    #   ".conditions .depth"        .depth inside .conditions
    #
    # number_from_text extracts the first number from an element. A selector
    # that finds nothing becomes None instead of an incorrect zero.
    new_snow_cm = number_from_text(soup.select_one("[data-snow='new']"))
    day_snow_cm = number_from_text(soup.select_one("[data-snow='day']"))
    week_snow_cm = number_from_text(soup.select_one("[data-snow='week']"))
    base_snow_cm = number_from_text(soup.select_one("[data-snow='base']"))

    # If the website reports inches, convert after extracting the number:
    # new_snow_cm = inches_to_cm(
    #     number_from_text(soup.select_one(".new-snow-inches"))
    # )

    return {
        "newSnow": new_snow_cm,
        "daySnow": day_snow_cm,
        "weekSnow": week_snow_cm,
        "baseSnow": base_snow_cm,
    }

