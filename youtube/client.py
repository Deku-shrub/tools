"""Thin wrapper around the YouTube Data API v3 (JSON REST, no OAuth) used by
search.py and comments.py.

Setup — one-time, free:
    1. Create/select a project at https://console.cloud.google.com/
    2. Enable "YouTube Data API v3" for it (APIs & Services > Library).
    3. Create an API key (APIs & Services > Credentials > Create Credentials
       > API key). No OAuth consent screen needed — search and public
       comments only require a plain API key.
    4. Set it as an environment variable so it never appears in source
       control or shell history:

           PowerShell:  $env:YOUTUBE_API_KEY = "your-key"
           bash:        export YOUTUBE_API_KEY="your-key"

Quota — the free tier is 10,000 units/day, and costs are wildly uneven:
    search.list            100 units/call  (expensive — ~100 searches/day)
    videos.list               1 unit/call  (cheap — used for --with-stats)
    commentThreads.list        1 unit/call  (cheap)
    comments.list               1 unit/call  (cheap — used to page full reply threads)
A quotaExceeded error surfaces as a YouTubeAPIError with reason
"quotaExceeded"; the quota resets at midnight Pacific time.

This module talks to the API with stdlib `urllib` only (no
google-api-python-client dependency) — one function per concern:

    api_get(endpoint, params)                 one request, returns parsed JSON
    paginate(endpoint, params, max_results)   generator over `items`, follows nextPageToken
    extract_video_id(url_or_id)               accepts a bare ID or any youtube.com/youtu.be URL
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://www.googleapis.com/youtube/v3"

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
URL_ID_PATTERNS = [
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/live/([A-Za-z0-9_-]{11})"),
]


class YouTubeAPIError(RuntimeError):
    """Raised for any non-2xx response. `.reason` is the API's short error
    code (e.g. "quotaExceeded", "commentsDisabled", "videoNotFound",
    "keyInvalid") when the API returned one, else None."""

    def __init__(self, message, status=None, reason=None):
        super().__init__(message)
        self.status = status
        self.reason = reason


def get_api_key(api_key=None):
    api_key = api_key or os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise SystemExit(
            "No API key. Set the YOUTUBE_API_KEY environment variable or "
            "pass --api-key (see client.py's module docstring for how to "
            "get one)."
        )
    return api_key


def extract_video_id(url_or_id):
    """Accepts a bare 11-char video ID or any watch/share/shorts/embed/live
    URL and returns the bare ID. Raises ValueError if none can be found."""
    candidate = url_or_id.strip()
    if VIDEO_ID_RE.match(candidate):
        return candidate
    for pattern in URL_ID_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract a video ID from {url_or_id!r}")


def api_get(endpoint, params, api_key=None):
    """One request to `{API_ROOT}/{endpoint}`. Returns the parsed JSON body.
    Raises YouTubeAPIError on any non-2xx response."""
    query = {**params, "key": get_api_key(api_key)}
    url = f"{API_ROOT}/{endpoint}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        reason = None
        message = f"HTTP {exc.code} from {endpoint}"
        try:
            error = json.loads(body).get("error", {})
            message = error.get("message", message)
            errors_list = error.get("errors") or []
            if errors_list:
                reason = errors_list[0].get("reason")
        except json.JSONDecodeError:
            pass
        raise YouTubeAPIError(message, status=exc.code, reason=reason) from exc


def paginate(endpoint, params, max_results=None, api_key=None):
    """Yield items from `endpoint`, following nextPageToken until either the
    API runs out of pages or `max_results` items have been yielded (None =
    all of them). Each call requests min(50, remaining) via `maxResults`,
    the API's own per-page cap."""
    params = dict(params)
    yielded = 0
    while True:
        page_size = 50 if max_results is None else min(50, max_results - yielded)
        if page_size <= 0:
            return
        params["maxResults"] = page_size
        payload = api_get(endpoint, params, api_key=api_key)
        for item in payload.get("items", []):
            yield item
            yielded += 1
            if max_results is not None and yielded >= max_results:
                return
        next_token = payload.get("nextPageToken")
        if not next_token:
            return
        params["pageToken"] = next_token
