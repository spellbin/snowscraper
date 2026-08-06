# Your local SnowScraper module

This folder belongs to you. It lives under `conf/local_scrapers`, which is
ignored by Git, so normal SnowScraper updates preserve it.

The module starts disabled. Work through these steps in order:

1. Open `module.ini` and verify the resort name and source URL.
2. Run the known-good example:

       python3 scraperctl.py test YOUR_MODULE_ID --sample

   It should print `PASS` and the values 5, 8, 31, and 142 cm. This proves the
   framework and BeautifulSoup installation work before you edit anything.

3. Open the real source URL on a laptop or desktop browser. Right-click the
   displayed snow number and choose **Inspect**. Identify a stable `class`, `id`,
   or `data-*` attribute on that number.

4. Edit only the selector strings in `scraper.py`. Avoid long chains such as
   `body > div:nth-child(4) > div:nth-child(2)` because harmless page layout
   changes break them. Prefer selectors such as `.new-snow`, `#base-depth`, or
   `[data-testid='snow-24h']`.

5. Test the live page while the module is still disabled:

       python3 scraperctl.py test YOUR_MODULE_ID

6. Check every value carefully. A wrong powder-alarm value is worse than no
   value. Missing measurements should be `None`, never a made-up `0`.

7. Enable only after the live test passes:

       python3 scraperctl.py enable YOUR_MODULE_ID

8. Restart the app if this is a new local-only resort:

       sudo systemctl restart snowscraper.service

Disable instantly without deleting your work:

    python3 scraperctl.py disable YOUR_MODULE_ID

For the complete field contract, troubleshooting guide, inches example,
fallback behavior, and backup instructions, read `docs/LOCAL_SCRAPERS.md` in
the SnowScraper repository.

