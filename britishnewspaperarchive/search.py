"""Run searches against britishnewspaperarchive.co.uk's advanced search.

Drives the real advanced-search form (https://britishnewspaperarchive.co.uk/search/advanced)
with Selenium, using the same dedicated, persistent Firefox profile as
check_login.py (./selenium_profile). Log in there manually first — see
check_login.py's docstring.

Pagination reuses the site's own "&page=N" (0-indexed) query param on the
results URL, so subsequent pages are just direct navigations rather than
re-submitting the form.

Programmatic use:
    from search import build_driver, search

    driver = build_driver()
    results = search(
        driver,
        free="Moonraker",
        date_from="1979-06-01",
        date_to="1979-12-31",
        sort_order="dayEarly",
        max_pages=2,
    )
    for r in results:
        print(r["headline"], r["published"], r["newspaper"])

CLI use:
    python search.py --free Moonraker --date-from 1979-06-01 --date-to 1979-12-31 --sort-order dayEarly
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

BASE_URL = "https://britishnewspaperarchive.co.uk/"
ADVANCED_SEARCH_URL = "https://britishnewspaperarchive.co.uk/search/advanced"
WAIT_SECONDS = 20
PROFILE_DIR = Path(__file__).parent / "selenium_profile"

CONTENT_TYPES = {
    "Advertisement",
    "Advertising",
    "Article",
    "Detailed Lists, Results and Guides",
    "Family Notices",
    "FamilyNotice",
    "Illustrated",
    "Miscellaneous",
    "News",
    "Table",
}
ACCESS_TYPES = {"Free To View", "Subscriber Access"}
SORT_ORDERS = {"score", "dayEarly", "dayRecent"}

PUBLISHED_RE = re.compile(r"Published:\s*(.+)")


def build_driver():
    if not PROFILE_DIR.exists():
        sys.exit(
            f"No profile found at {PROFILE_DIR}. Log in manually once in that "
            "profile first (see check_login.py)."
        )
    options = FirefoxOptions()
    options.add_argument("-no-remote")
    options.add_argument("-profile")
    options.add_argument(str(PROFILE_DIR))
    return webdriver.Firefox(options=options)


def _select_option_containing(select_el, text):
    """Select the first <option> whose visible text contains `text`
    (case-insensitive). Needed for fields like NewspaperTitle where options
    are labelled "Name (startYear - endYear)"."""
    needle = text.strip().lower()
    for option in select_el.options:
        if needle in option.text.strip().lower():
            option.click()
            return
    raise NoSuchElementException(f"No option containing {text!r} in {select_el._el}")


def _set_date(form, prefix, iso_date):
    """iso_date: 'YYYY-MM-DD' string."""
    year, month, day = iso_date.split("-")
    Select(form.find_element(By.ID, f"{prefix}Day")).select_by_value(str(int(day)))
    Select(form.find_element(By.ID, f"{prefix}Month")).select_by_value(str(int(month)))
    Select(form.find_element(By.ID, f"{prefix}Year")).select_by_value(year)


def _set_checkbox_group(form, name, values):
    for value in values:
        checkbox = form.find_element(
            By.CSS_SELECTOR, f"input[name='{name}'][value='{value}']"
        )
        if not checkbox.is_selected():
            checkbox.click()


def fill_and_submit(
    driver,
    *,
    free=None,
    some=None,
    phrase=None,
    not_words=None,
    exact=False,
    place=None,
    newspaper_title=None,
    date_from=None,
    date_to=None,
    content_types=None,
    access_types=None,
    front_page=False,
    public_tag=None,
    sort_order="score",
):
    """Fill the advanced search form and submit it. Returns the base results
    URL (no &page= suffix) that the site redirected to."""
    if content_types:
        unknown = set(content_types) - CONTENT_TYPES
        if unknown:
            raise ValueError(f"Unknown content_types: {unknown}")
    if access_types:
        unknown = set(access_types) - ACCESS_TYPES
        if unknown:
            raise ValueError(f"Unknown access_types: {unknown}")
    if sort_order not in SORT_ORDERS:
        raise ValueError(f"Unknown sort_order: {sort_order!r}, expected one of {SORT_ORDERS}")

    driver.get(ADVANCED_SEARCH_URL)
    form = WebDriverWait(driver, WAIT_SECONDS).until(
        EC.presence_of_element_located((By.ID, "searchAdvanced"))
    )

    if free:
        form.find_element(By.ID, "FreeSearch").send_keys(free)
    if some:
        form.find_element(By.ID, "SomeSearch").send_keys(some)
    if not_words:
        form.find_element(By.ID, "NotSearch").send_keys(not_words)
    if phrase:
        form.find_element(By.ID, "PhraseSearch").send_keys(phrase)
    if exact:
        form.find_element(By.CSS_SELECTOR, "input[name='ExactSearch']").click()

    if place:
        _select_option_containing(Select(form.find_element(By.ID, "Place")), place)
    if newspaper_title:
        _select_option_containing(
            Select(form.find_element(By.ID, "NewspaperTitle")), newspaper_title
        )

    if date_from:
        _set_date(form, "DateFrom", date_from)
    if date_to:
        _set_date(form, "DateTo", date_to)

    if content_types:
        _set_checkbox_group(form, "ContentType", content_types)
    if access_types:
        _set_checkbox_group(form, "AccessType", access_types)
    if front_page:
        form.find_element(By.CSS_SELECTOR, "input[name='FrontPage']").click()

    if public_tag:
        form.find_element(By.ID, "PublicTag").send_keys(public_tag)

    Select(form.find_element(By.ID, "SortOrder")).select_by_value(sort_order)

    form.find_element(By.ID, "submit").click()

    WebDriverWait(driver, WAIT_SECONDS).until(
        lambda d: "/search/results/" in d.current_url
    )
    url = driver.current_url
    return re.sub(r"[&?]page=\d+", "", url)


def _text_or_none(el, selector):
    try:
        return el.find_element(By.CSS_SELECTOR, selector).text.strip()
    except NoSuchElementException:
        return None


def scrape_results_page(driver):
    """Scrape all result cards on the current results page."""
    results = []
    for card in driver.find_elements(By.CSS_SELECTOR, "article.bna-card"):
        title_link = card.find_element(By.CSS_SELECTOR, "h4.bna-card__title a")
        meta_text = _text_or_none(card, ".bna-card__meta") or ""
        published_match = PUBLISHED_RE.search(meta_text)

        newspaper_name = None
        newspaper_url = None
        try:
            newspaper_link = card.find_element(
                By.CSS_SELECTOR, ".bna-card__meta a[href*='newspaperTitle=']"
            )
            newspaper_name = newspaper_link.text.strip()
            newspaper_url = newspaper_link.get_attribute("href")
        except NoSuchElementException:
            pass

        item_id = None
        try:
            item_id = card.find_element(
                By.CSS_SELECTOR, "[data-item-id]"
            ).get_attribute("data-item-id")
        except NoSuchElementException:
            pass

        thumbnail_url = None
        try:
            thumbnail_url = card.find_element(
                By.CSS_SELECTOR, ".bna-card__media__image"
            ).get_attribute("src")
        except NoSuchElementException:
            pass

        results.append(
            {
                "item_id": item_id,
                "headline": title_link.text.strip(),
                "url": urljoin(BASE_URL, title_link.get_attribute("href")),
                "snippet": _text_or_none(card, ".bna-card__body__description"),
                "published": published_match.group(1).strip() if published_match else None,
                "newspaper": newspaper_name,
                "newspaper_url": urljoin(BASE_URL, newspaper_url) if newspaper_url else None,
                "thumbnail_url": thumbnail_url,
            }
        )
    return results


def search(driver, *, max_pages=1, **form_kwargs):
    """Fill the advanced search form, submit, and scrape up to `max_pages`
    pages of results (site pagination is 0-indexed via &page=N)."""
    base_url = fill_and_submit(driver, **form_kwargs)
    all_results = []
    for page in range(max_pages):
        separator = "&" if "?" in base_url else "?"
        driver.get(f"{base_url}{separator}page={page}")
        WebDriverWait(driver, WAIT_SECONDS).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "article.bna-card")
            or "no results" in d.page_source.lower()
        )
        page_results = scrape_results_page(driver)
        if not page_results:
            break
        all_results.extend(page_results)
    return all_results


def _build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--free", help="AND of all words (FreeSearch)")
    p.add_argument("--some", help="OR of words (SomeSearch)")
    p.add_argument("--phrase", help="Exact phrase (PhraseSearch)")
    p.add_argument("--not-words", dest="not_words", help="Exclude words (NotSearch)")
    p.add_argument("--exact", action="store_true", help="Disable stemming (ExactSearch)")
    p.add_argument("--place")
    p.add_argument("--newspaper-title")
    p.add_argument("--date-from", help="YYYY-MM-DD")
    p.add_argument("--date-to", help="YYYY-MM-DD")
    p.add_argument("--content-type", action="append", dest="content_types", choices=sorted(CONTENT_TYPES))
    p.add_argument("--access-type", action="append", dest="access_types", choices=sorted(ACCESS_TYPES))
    p.add_argument("--front-page", action="store_true")
    p.add_argument("--public-tag")
    p.add_argument("--sort-order", default="score", choices=sorted(SORT_ORDERS))
    p.add_argument("--max-pages", type=int, default=1)
    p.add_argument("--json", metavar="PATH", help="Write results as JSON to this file instead of printing")
    return p


def main():
    args = _build_arg_parser().parse_args()
    driver = build_driver()
    try:
        results = search(
            driver,
            free=args.free,
            some=args.some,
            phrase=args.phrase,
            not_words=args.not_words,
            exact=args.exact,
            place=args.place,
            newspaper_title=args.newspaper_title,
            date_from=args.date_from,
            date_to=args.date_to,
            content_types=args.content_types,
            access_types=args.access_types,
            front_page=args.front_page,
            public_tag=args.public_tag,
            sort_order=args.sort_order,
            max_pages=args.max_pages,
        )
    except (NoSuchElementException, TimeoutException) as exc:
        screenshot_path = "search_failure.png"
        driver.save_screenshot(screenshot_path)
        sys.exit(f"Search failed: {exc}\nScreenshot saved to {screenshot_path}.")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"Wrote {len(results)} result(s) to {args.json}")
    else:
        print(f"{len(results)} result(s):\n")
        for r in results:
            print(f"- {r['headline']}")
            print(f"  {r['published']} — {r['newspaper']}")
            print(f"  {r['url']}")
            if r["snippet"]:
                print(f"  {r['snippet'][:200]}")
            print()


if __name__ == "__main__":
    main()
