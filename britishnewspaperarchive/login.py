"""Log in to britishnewspaperarchive.co.uk with Selenium + Firefox.

Password is read from the "newspaper" environment variable so it never
appears in source control.

Runs against a dedicated Firefox profile stored in ./selenium_profile,
separate from your everyday Firefox profile. It's persistent (not wiped
between runs) so cookies picked up from a real login stick around —
including any Cloudflare clearance cookie — making later runs less likely
to hit a challenge. It's created automatically on first run.

Since this profile always logs in explicitly with the credentials below,
no attempt is made to carry over your real browser's session — the script
always drives the actual login form.

If a Cloudflare challenge appears anyway, the script gives up rather than
trying to solve it (see FAIL on "Cloudflare challenge...").

Usage (PowerShell):
    $env:newspaper = "your-password"
    python login.py

Usage (bash):
    export newspaper="your-password"
    python login.py
"""

import os
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

LOGIN_URL = "https://www.britishnewspaperarchive.co.uk/account/login"
EMAIL = "chrismmonteiro1@gmail.com"
WAIT_SECONDS = 20
CHALLENGE_SILENT_WAIT_SECONDS = 15
CHALLENGE_MANUAL_TIMEOUT_SECONDS = 300
PROFILE_DIR = Path(__file__).parent / "selenium_profile"

# Multiple candidate selectors are tried for each element because the
# site's exact markup couldn't be inspected ahead of time (it blocks
# non-browser HTTP clients). Adjust these if the site's markup differs.
COOKIE_ACCEPT_SELECTORS = [
    (By.ID, "onetrust-accept-btn-handler"),
    (By.CSS_SELECTOR, "button[aria-label='Accept all cookies']"),
    (By.XPATH, "//button[contains(., 'Accept')]"),
]
EMAIL_FIELD_SELECTORS = [
    (By.ID, "Email"),
    (By.ID, "email"),
    (By.NAME, "email"),
    (By.CSS_SELECTOR, "input[type='email']"),
]
PASSWORD_FIELD_SELECTORS = [
    (By.ID, "Password"),
    (By.ID, "password"),
    (By.NAME, "password"),
    (By.CSS_SELECTOR, "input[type='password']"),
]
REMEMBER_ME_SELECTORS = [
    (By.ID, "RememberMe"),
    (By.ID, "rememberMe"),
    (By.NAME, "RememberMe"),
    (By.NAME, "rememberMe"),
    (By.CSS_SELECTOR, "input[type='checkbox']"),
]
SUBMIT_BUTTON_SELECTORS = [
    (By.CSS_SELECTOR, "button[type='submit']"),
    (By.XPATH, "//button[contains(., 'Log in')]"),
    (By.XPATH, "//button[contains(., 'Sign in')]"),
    (By.XPATH, "//input[@type='submit']"),
]
LOGGED_IN_SELECTORS = [
    (By.PARTIAL_LINK_TEXT, "MY ACCOUNT"),
    (By.PARTIAL_LINK_TEXT, "My account"),
    (By.PARTIAL_LINK_TEXT, "Log out"),
    (By.PARTIAL_LINK_TEXT, "Sign out"),
]

# Signals that a Cloudflare interactive/JS challenge is showing instead of
# the real page.
CHALLENGE_SELECTORS = [
    (By.ID, "challenge-running"),
    (By.ID, "cf-challenge-running"),
    (By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']"),
    (By.CSS_SELECTOR, "div.cf-turnstile"),
]
CHALLENGE_TITLES = {"just a moment...", "attention required! | cloudflare"}


def find_first(context, selectors, timeout=WAIT_SECONDS):
    """Return the first element found for any of the given selectors.

    `context` is anything with a `find_element(by, value)` method — a
    WebDriver or a WebElement (to search within a subtree, e.g. a form).
    """
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


def dismiss_cookie_banner(driver):
    try:
        button = find_first(driver, COOKIE_ACCEPT_SELECTORS, timeout=5)
        button.click()
    except NoSuchElementException:
        pass


def is_on_challenge_page(driver):
    if driver.title.strip().lower() in CHALLENGE_TITLES:
        return True
    for by, value in CHALLENGE_SELECTORS:
        try:
            driver.find_element(by, value)
            return True
        except NoSuchElementException:
            continue
    return False


def wait_for_challenge_to_clear(driver):
    """Give Cloudflare's silent JS check a few seconds. If a challenge is
    still showing after that, pause and let a human solve it by hand in the
    visible browser window — this script never attempts to solve it itself.
    Because the profile is persistent, solving it once should carry over
    (via the resulting clearance cookie) to future runs.
    """
    for _ in range(CHALLENGE_SILENT_WAIT_SECONDS):
        if not is_on_challenge_page(driver):
            return
        time.sleep(1)
    if not is_on_challenge_page(driver):
        return

    print(
        "Cloudflare challenge shown — solve it manually in the browser window. "
        f"Waiting up to {CHALLENGE_MANUAL_TIMEOUT_SECONDS}s..."
    )
    deadline = time.monotonic() + CHALLENGE_MANUAL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not is_on_challenge_page(driver):
            print("Challenge cleared, continuing.")
            return
        time.sleep(2)

    screenshot_path = "cloudflare_challenge.png"
    driver.save_screenshot(screenshot_path)
    driver.quit()
    sys.exit(
        "Cloudflare challenge still showing after "
        f"{CHALLENGE_MANUAL_TIMEOUT_SECONDS}s — giving up. "
        f"Screenshot saved to {screenshot_path}."
    )


def main():
    password = os.environ.get("newspaper")
    if not password:
        sys.exit(
            "Environment variable 'newspaper' is not set. "
            "Set it to your account password before running this script."
        )

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    options = FirefoxOptions()
    # -no-remote: run standalone even while your everyday Firefox is open,
    # instead of handing off to it (which tears down this session).
    options.add_argument("-no-remote")
    options.add_argument("-profile")
    options.add_argument(str(PROFILE_DIR))
    options.set_preference("signon.rememberSignons", False)
    options.set_preference("app.update.auto", False)
    options.set_preference("app.update.disabledForTesting", True)
    options.set_preference("browser.sessionstore.resume_from_crash", False)

    driver = webdriver.Firefox(options=options)
    try:
        driver.get(LOGIN_URL)
        wait_for_challenge_to_clear(driver)
        dismiss_cookie_banner(driver)

        email_field = find_first(driver, EMAIL_FIELD_SELECTORS)
        email_field.clear()
        email_field.send_keys(EMAIL)

        password_field = find_first(driver, PASSWORD_FIELD_SELECTORS)
        password_field.clear()
        password_field.send_keys(password)

        # Scoped to the login form itself: a page-wide search for
        # "button[type=submit]" / "input[type=checkbox]" can match
        # unrelated elements elsewhere on the page (e.g. this site's
        # header search button).
        login_form = password_field.find_element(By.XPATH, "./ancestor::form[1]")

        try:
            remember_me = find_first(login_form, REMEMBER_ME_SELECTORS, timeout=3)
            if not remember_me.is_selected():
                remember_me.click()
        except NoSuchElementException:
            print("No 'remember me' checkbox found — skipping.")

        submit_button = find_first(login_form, SUBMIT_BUTTON_SELECTORS)
        submit_button.click()

        wait_for_challenge_to_clear(driver)
        find_first(driver, LOGGED_IN_SELECTORS)
        print("Login successful.")
    except (NoSuchElementException, TimeoutException) as exc:
        screenshot_path = "login_failure.png"
        driver.save_screenshot(screenshot_path)
        sys.exit(
            f"Login flow failed: {exc}\n"
            f"Saved a screenshot to {screenshot_path} for debugging."
        )


if __name__ == "__main__":
    main()
