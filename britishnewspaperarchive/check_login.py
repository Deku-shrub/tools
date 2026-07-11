"""Verify the dedicated Selenium Firefox profile (./selenium_profile) has a
logged-in session on britishnewspaperarchive.co.uk.

Login itself is done manually, once, in a plain (non-automated) Firefox
window pointed at this same profile. Selenium/geckodriver's automation
fingerprints were triggering an endless Cloudflare re-challenge loop on the
login form itself, so this script never drives the login form; it only
loads the site and checks whether the existing session cookie is still
valid.

Usage:
    python check_login.py
"""

import sys
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = "https://www.britishnewspaperarchive.co.uk/"
WAIT_SECONDS = 20
PROFILE_DIR = Path(__file__).parent / "selenium_profile"

LOGGED_IN_SELECTORS = [
    (By.PARTIAL_LINK_TEXT, "MY ACCOUNT"),
    (By.PARTIAL_LINK_TEXT, "My account"),
    (By.PARTIAL_LINK_TEXT, "Log out"),
    (By.PARTIAL_LINK_TEXT, "Sign out"),
]


def find_first(context, selectors, timeout=WAIT_SECONDS):
    last_error = None
    for by, value in selectors:
        try:
            return WebDriverWait(context, timeout).until(
                EC.presence_of_element_located((by, value))
            )
        except TimeoutException as exc:
            last_error = exc
    raise NoSuchElementException(
        f"None of the candidate selectors matched: {selectors}"
    ) from last_error


def build_driver():
    if not PROFILE_DIR.exists():
        sys.exit(
            f"No profile found at {PROFILE_DIR}. Run login.py once (or log in "
            "manually in that profile) before checking login state."
        )

    options = FirefoxOptions()
    options.add_argument("-no-remote")
    options.add_argument("-profile")
    options.add_argument(str(PROFILE_DIR))
    return webdriver.Firefox(options=options)


def main():
    driver = build_driver()
    try:
        driver.get(BASE_URL)
        find_first(driver, LOGGED_IN_SELECTORS)
        print("Logged in.")
    except (NoSuchElementException, TimeoutException) as exc:
        screenshot_path = "check_login_failure.png"
        driver.save_screenshot(screenshot_path)
        sys.exit(
            f"Not logged in: {exc}\nScreenshot saved to {screenshot_path}."
        )


if __name__ == "__main__":
    main()
