"""Save interesting search results for offline analysis and follow-up.

Each finding gets its own subfolder under ./findings/ containing:
  - data.json       structured source data (everything search.py scraped)
                     plus your note, tags, and the query that produced it
  - page_image.png  a screenshot of the article's page in the site's
                     canvas-based viewer, captured via the logged-in browser
                     session. A plain image fetch (thumbnail CDN, or
                     canvas.toDataURL()) is blocked by CORS/canvas-tainting
                     for this cross-origin, tile-rendered viewer; a
                     WebDriver element screenshot is a compositor-level
                     capture that sidesteps both.

findings/index.json accumulates a flat list of every saved finding, for
quickly scanning what's been collected so far.

Usage (programmatic, typically right after search.py's search()):
    from search import build_driver, search
    from findings import save_finding

    driver = build_driver()
    results = search(driver, free="Moonraker", date_from="1979-06-01", date_to="1979-12-31")
    save_finding(driver, results[0], note="Possible braces reference", tags=["braces", "phase1"])
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

FINDINGS_DIR = Path(__file__).parent / "findings"
INDEX_PATH = FINDINGS_DIR / "index.json"
VIEWER_WAIT_SECONDS = 20
VIEWER_RENDER_SETTLE_SECONDS = 2


def _slugify(text, max_len=60):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug[:max_len] or "finding"


def _capture_viewer_page_image(driver, article_url, dest_path):
    """Navigate to the article's viewer page and screenshot its canvas
    (the rendered newspaper page), returning the image-credit text if any
    (e.g. "Image © Reach PLC.") for attribution."""
    driver.get(article_url)
    canvas = WebDriverWait(driver, VIEWER_WAIT_SECONDS).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "canvas"))
    )
    time.sleep(VIEWER_RENDER_SETTLE_SECONDS)  # let the tiled viewer finish painting
    dest_path.write_bytes(canvas.screenshot_as_png)

    try:
        return driver.find_element(By.XPATH, "//*[contains(text(), '©')]").text.strip()
    except NoSuchElementException:
        return None


def _load_index():
    if INDEX_PATH.exists():
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    return []


def _save_index(index):
    FINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def save_finding(driver, result, *, note="", tags=None, query=None, download_image=True):
    """Save one search-result dict (as produced by search.scrape_results_page)
    as a finding: structured data + page image + your annotation.

    Returns the folder the finding was saved to.
    """
    tags = list(tags or [])
    finding_id = result.get("item_id") or _slugify(result.get("headline", "finding"))
    folder = FINDINGS_DIR / _slugify(finding_id)
    folder.mkdir(parents=True, exist_ok=True)

    record = {
        "finding_id": finding_id,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "note": note,
        "tags": tags,
        "query": query,
        "source": result,
    }

    if download_image and result.get("url"):
        try:
            credit = _capture_viewer_page_image(driver, result["url"], folder / "page_image.png")
            record["page_image_file"] = "page_image.png"
            if credit:
                record["image_credit"] = credit
        except (WebDriverException, TimeoutException) as exc:
            record["image_error"] = str(exc)

    (folder / "data.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    index = _load_index()
    index = [f for f in index if f.get("finding_id") != finding_id]  # replace if re-saved
    index.append(
        {
            "finding_id": finding_id,
            "folder": str(folder.relative_to(FINDINGS_DIR.parent)),
            "headline": result.get("headline"),
            "published": result.get("published"),
            "newspaper": result.get("newspaper"),
            "url": result.get("url"),
            "note": note,
            "tags": tags,
            "saved_at": record["saved_at"],
        }
    )
    _save_index(index)

    return folder


def list_findings():
    """Return the flat index of everything saved so far."""
    return _load_index()
