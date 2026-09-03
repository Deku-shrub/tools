"""Interactively explore an archived site on the Wayback Machine with
Selenium + Firefox.

This is not a crawler — it launches a real Firefox window on the given
start URL and gives you a handful of small helper functions to drive it by
hand from a Python REPL: list the links on the page you're looking at,
follow one by index, jump back/forward, or jump straight to an arbitrary
page/timestamp of the same original site.

Every helper takes the Selenium `driver` as its first argument so you can
freely mix them with raw `driver.*` calls.

Usage — run it and get dropped straight into a REPL with `driver` in scope:

    python explore.py
    >>> links(driver)
    [ 0]  Join                                                         -> http://www.pcgroup.org.uk/join.html
    [ 1]  Troubleshoot                                                 -> http://www.pcgroup.org.uk/troubleshoot.html
    ...
    >>> open_link(driver, 0)
    >>> current(driver)
    >>> back(driver)
    >>> goto(driver, "press.html")            # same timestamp as current page
    >>> goto(driver, "press.html", "20010101000000")   # a different capture
    >>> history()
    >>> close(driver)

Or import it as a library from your own script / `python -i`:

    from explore import build_driver, start, links, open_link, back, goto

    driver = build_driver()
    start(driver)
    links(driver)
    open_link(driver, 2)

Link classification (the marker column in `links()` output):
    (space)  archived_page    a captured page on web.archive.org — safe to open_link()
    *        external         not a wayback capture (off-site, or the live site)
    #        wayback_calendar a "pick a snapshot" link (fuzzy/asterisk timestamp)
    ~        asset            embedded resource link (css/img/js capture) — hidden by default
    @        mailto           a mailto: link
    !        javascript       a javascript: pseudo-link (e.g. popup handlers) — not navigable
    ?        wayback_other    some other web.archive.org URL (e.g. archive.org UI chrome)
"""

import argparse
import code
import re
import sys
import time
from urllib.parse import urljoin

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions

START_URL = "https://web.archive.org/web/20000817045448/http://www.pcgroup.org.uk/"
ORIGINAL_SITE_ROOT = "http://www.pcgroup.org.uk/"
WINDOW_SIZE = (1280, 900)

# The archive.org playback toolbar (donate banner, calendar, "About this
# capture" etc.) is injected into the live DOM by the site's own JS, inside
# a container with this id — not present in the raw HTML, only visible once
# a real browser has run the page's scripts. Its links are filtered out of
# links() by default since they're archive.org UI chrome, not page content.
TOOLBAR_CONTAINER_ID = "wm-ipp-base"

# https://web.archive.org/web/<timestamp><modifier>/<original-url>
# `modifier` (e.g. "cs_", "im_", "js_", "oe_", "id_") marks an embedded-asset
# capture rather than a navigable page; it's absent for ordinary page links.
WAYBACK_URL_RE = re.compile(
    r"^https?://web\.archive\.org/web/(?P<timestamp>\d{1,14})(?P<mod>[a-z_]{0,3})/(?P<original>.+)$"
)

_history = []  # list of the dicts current() has returned, in visit order
_last_links = []  # cache for open_link(driver, <index>), set by links()


def build_driver(headless=False):
    options = FirefoxOptions()
    if headless:
        options.add_argument("-headless")
    driver = webdriver.Firefox(options=options)
    driver.set_window_size(*WINDOW_SIZE)
    return driver


def start(driver, url=START_URL):
    """Navigate to the start URL (or any URL) and record it as visit #0."""
    _history.clear()
    driver.get(url)
    return _record_visit(driver)


def _parse_wayback_url(url):
    m = WAYBACK_URL_RE.match(url)
    if not m:
        return None
    return {
        "timestamp": m.group("timestamp"),
        "modifier": m.group("mod") or None,
        "original_url": m.group("original"),
    }


def current(driver):
    """Print and return info about the page currently loaded."""
    url = driver.current_url
    parsed = _parse_wayback_url(url)
    info = {
        "url": url,
        "title": driver.title,
        "timestamp": parsed["timestamp"] if parsed else None,
        "original_url": parsed["original_url"] if parsed else None,
    }
    print(
        f"[{info['timestamp'] or '?'}] {info['title']!r}\n"
        f"  archived: {info['url']}\n"
        f"  original: {info['original_url'] or '(not a wayback capture URL)'}"
    )
    return info


def _record_visit(driver):
    info = current(driver)
    _history.append(info)
    return info


def _is_in_toolbar(el):
    try:
        el.find_element(By.XPATH, f"ancestor::*[@id='{TOOLBAR_CONTAINER_ID}']")
        return True
    except NoSuchElementException:
        return False


def _classify(href, current_timestamp):
    """Classify a raw href read from the DOM and work out the actual
    web.archive.org URL to navigate to for it (`nav_href`).

    Wayback's replay JS ("wombat") rewrites every in-page link's href
    attribute to look like the *original* site URL again (cosmetic, so
    copy-pasting a link looks normal) — it does NOT leave the
    /web/<timestamp>/ prefix in the DOM. Real navigation on click is
    handled by wombat's own JS, which Selenium's driver.get() bypasses
    entirely, so for anything that isn't already an absolute
    web.archive.org URL we have to reconstruct the replay URL ourselves,
    landing on the same capture timestamp as the current page.
    """
    if href.startswith("javascript:"):
        return "javascript", None, href
    if href.startswith("mailto:") or "/mailto:" in href:
        return "mailto", None, href
    parsed = _parse_wayback_url(href)
    if parsed:
        if parsed["modifier"]:
            return "asset", parsed, href
        return "archived_page", parsed, href
    if href.startswith("http://") or href.startswith("https://"):
        nav_href = f"https://web.archive.org/web/{current_timestamp}/{href}"
        parsed = {"timestamp": current_timestamp, "modifier": None, "original_url": href}
        return "archived_page", parsed, nav_href
    return "external", None, href


_MARKERS = {
    "archived_page": " ",
    "external": "*",
    "wayback_calendar": "#",
    "asset": "~",
    "mailto": "@",
    "javascript": "!",
    "wayback_other": "?",
}


def _current_timestamp(driver):
    parsed = _parse_wayback_url(driver.current_url)
    return parsed["timestamp"] if parsed else None


def links(driver, show_toolbar=False, show_assets=False):
    """List the <a> links on the current page, classified (see module
    docstring for the marker legend). Caches the list so open_link(driver,
    <index>) can refer back to it."""
    global _last_links
    current_timestamp = _current_timestamp(driver)
    results = []
    for el in driver.find_elements(By.TAG_NAME, "a"):
        href = el.get_attribute("href")
        if not href:
            continue
        in_toolbar = _is_in_toolbar(el)
        if in_toolbar and not show_toolbar:
            continue
        kind, parsed, nav_href = _classify(href, current_timestamp)
        if kind == "asset" and not show_assets:
            continue
        text = (el.text or el.get_attribute("title") or el.get_attribute("aria-label") or "").strip()
        results.append(
            {
                "text": text or "(no text)",
                "href": nav_href,  # what open_link() actually navigates to
                "raw_href": href,  # what was literally on the <a> in the DOM
                "kind": kind,
                "timestamp": parsed["timestamp"] if parsed else None,
                "original_url": parsed["original_url"] if parsed else None,
                "in_toolbar": in_toolbar,
            }
        )
    _last_links = results
    for i, r in enumerate(results):
        marker = _MARKERS.get(r["kind"], "?")
        label = r["original_url"] or r["href"]
        print(f"[{i:>2}]{marker} {r['text'][:60]:<60} -> {label}")
    if not results:
        print("(no links found — try links(driver, show_toolbar=True) or show_assets=True)")
    return results


def open_link(driver, target):
    """Follow a link from the most recent links() call — pass its index —
    or navigate directly by passing an href string (used as-is, so pass a
    full https://web.archive.org/web/<timestamp>/<url> URL, not a bare
    original-site URL — see goto() for that)."""
    if isinstance(target, int):
        if not _last_links:
            raise RuntimeError("Call links(driver) first to populate the link list.")
        href = _last_links[target]["href"]
    else:
        href = target
    driver.get(href)
    return _record_visit(driver)


def back(driver):
    driver.back()
    return _record_visit(driver)


def forward(driver):
    driver.forward()
    return _record_visit(driver)


def goto(driver, original_path_or_url, timestamp=None):
    """Jump straight to a page of the archived site, bypassing link-clicking.

    `original_path_or_url` can be:
      - a path relative to the site root, e.g. "press.html" or "members/members.html"
      - a full original URL, e.g. "http://www.pcgroup.org.uk/press.html"
      - a full https://web.archive.org/... URL, used as-is

    `timestamp` (a wayback "YYYYMMDDhhmmss" string) defaults to the
    timestamp of the page currently loaded, so `goto(driver, "press.html")`
    stays within the same capture date you're already looking at.
    """
    if original_path_or_url.startswith("https://web.archive.org/"):
        driver.get(original_path_or_url)
        return _record_visit(driver)

    if timestamp is None:
        parsed = _parse_wayback_url(driver.current_url)
        if not parsed:
            raise ValueError(
                "Current page isn't a wayback capture URL — pass timestamp= explicitly."
            )
        timestamp = parsed["timestamp"]

    original_url = urljoin(ORIGINAL_SITE_ROOT, original_path_or_url)
    driver.get(f"https://web.archive.org/web/{timestamp}/{original_url}")
    return _record_visit(driver)


def history():
    """Print and return every page visited so far, in order."""
    for i, h in enumerate(_history):
        print(f"[{i:>2}] [{h['timestamp'] or '?'}] {h['title']!r} - {h['original_url'] or h['url']}")
    return list(_history)


def screenshot(driver, path=None):
    path = path or f"screenshot_{int(time.time())}.png"
    driver.save_screenshot(path)
    print(f"Saved {path}")
    return path


def close(driver):
    driver.quit()


HELP = """\
Helpers in scope: build_driver, start, current, links, open_link, back,
forward, goto, history, screenshot, close. `driver` is already open and
pointed at the start URL.

  links(driver)                 list links on the current page
  open_link(driver, 3)          follow link #3 from the last links() call
  open_link(driver, "https://...")   or navigate to an href directly
  back(driver) / forward(driver)
  goto(driver, "press.html")    jump to another page, same capture date
  goto(driver, "press.html", "20010101000000")   ...or a different capture
  current(driver)               show info about where you are now
  history()                     list every page visited this session
  screenshot(driver)            save a PNG of the current page
  close(driver)                 quit the browser (or just Ctrl-D to exit
                                 and leave Firefox running)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=START_URL, help="Start URL (a web.archive.org capture URL)")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    driver = build_driver(headless=args.headless)
    try:
        start(driver, args.url)
    except WebDriverException as exc:
        driver.quit()
        sys.exit(f"Failed to load start URL: {exc}")

    print(HELP)
    if not sys.flags.interactive:
        # Drop into a real Python REPL with the helpers + driver already in
        # scope, so `python explore.py` alone is enough — no need for `-i`.
        code.interact(banner="", local=dict(globals(), driver=driver))
    return driver


if __name__ == "__main__":
    driver = main()
