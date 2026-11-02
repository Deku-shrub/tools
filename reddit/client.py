"""Thin wrapper around Reddit's official OAuth API (oauth.reddit.com, JSON
REST, stdlib `urllib` only) used by search.py, listing.py and thread.py.

Setup — one-time, free for non-commercial use:
    1. Logged in to Reddit, go to https://www.reddit.com/prefs/apps and
       "create another app...". Pick type **script**, any name, redirect
       URI http://localhost:8080 (unused, but required). If Reddit instead
       points you at its Developer Platform / API access request form, you
       have to file that request first (Reddit has been gating new API apps
       behind its "Responsible Builder" policy).
    2. The client ID is the short string under the app's name; the secret
       is labelled "secret".
    3. Set them as environment variables (persist with `setx`, then open a
       new terminal):

           PowerShell:  $env:REDDIT_CLIENT_ID = "..."; $env:REDDIT_CLIENT_SECRET = "..."
           bash:        export REDDIT_CLIENT_ID=... REDDIT_CLIENT_SECRET=...

       Optional: REDDIT_USERNAME + REDDIT_PASSWORD switch from app-only auth
       to acting as your account (password grant — the app must be a script
       app you own, and it fails if the account has 2FA enabled). Only
       needed for things like private subreddits you're a member of;
       public search/browsing works fine app-only.

Rate limit — ~100 requests/minute per OAuth client. Every response carries
X-Ratelimit-Remaining/-Reset headers; api_get() sleeps until the window
resets when they run out, and backs off on 429/5xx, so long pulls just run
slower instead of failing.

Listing cap — Reddit stops serving any listing (search, /new, a user's
history, ...) after ~1000 items, however you page it. For deeper history,
narrow the query (per-subreddit, per-time-window) rather than paging further.

The access token (valid 1 hour) is cached in ./.token.json so consecutive
runs don't re-authenticate.

    api_get(path, params)                      one request, returns parsed JSON
    paginate(path, params, max_results)        generator over listing children (`data` dicts)
    extract_post_id(url_or_id)                 accepts a bare ID, t3_ fullname, or any post URL
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_ROOT = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
TOKEN_CACHE = Path(__file__).parent / ".token.json"
PAGE_LIMIT = 100
MAX_RETRIES = 4

POST_ID_RE = re.compile(r"^(?:t3_)?([a-z0-9]{4,10})$", re.IGNORECASE)
URL_ID_PATTERNS = [
    re.compile(r"/comments/([a-z0-9]{4,10})", re.IGNORECASE),
    re.compile(r"redd\.it/([a-z0-9]{4,10})", re.IGNORECASE),
]

_token = None


class RedditAPIError(RuntimeError):
    """Raised for any non-2xx response. `.status` is the HTTP code."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _credentials():
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not secret:
        raise SystemExit(
            "No API credentials. Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET "
            "(see client.py's module docstring for how to get them)."
        )
    return client_id, secret, os.environ.get("REDDIT_USERNAME"), os.environ.get("REDDIT_PASSWORD")


def user_agent():
    # Reddit asks for "<platform>:<app id>:<version> (by /u/<username>)" and
    # throttles generic agents hard.
    username = os.environ.get("REDDIT_USERNAME") or "unknown"
    return os.environ.get("REDDIT_USER_AGENT") or f"windows:research-tools:v0.1 (by /u/{username})"


def _fetch_token():
    client_id, secret, username, password = _credentials()
    if username and password:
        form = {"grant_type": "password", "username": username, "password": password}
    else:
        form = {"grant_type": "client_credentials"}
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Authorization": f"Basic {basic}", "User-Agent": user_agent()},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RedditAPIError(
            f"Token request failed: HTTP {exc.code} — check REDDIT_CLIENT_ID/SECRET", exc.code
        ) from exc
    if "access_token" not in payload:
        # Bad username/password comes back as 200 {"error": "invalid_grant"}.
        raise RedditAPIError(f"Token request failed: {payload}")
    token = {
        "access_token": payload["access_token"],
        "expires_at": time.time() + payload.get("expires_in", 3600) - 60,
        "client_id": client_id,
        "username": username if password else None,
    }
    TOKEN_CACHE.write_text(json.dumps(token), encoding="utf-8")
    return token


def _get_token(force_refresh=False):
    global _token
    client_id, _, username, password = _credentials()
    wanted_user = username if password else None
    if not force_refresh and _token is None and TOKEN_CACHE.exists():
        try:
            _token = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _token = None
    if (
        force_refresh
        or _token is None
        or _token.get("expires_at", 0) < time.time()
        or _token.get("client_id") != client_id
        or _token.get("username") != wanted_user
    ):
        _token = _fetch_token()
    return _token["access_token"]


def _respect_rate_limit(headers):
    try:
        remaining = float(headers.get("X-Ratelimit-Remaining", "10"))
        reset = float(headers.get("X-Ratelimit-Reset", "0"))
    except ValueError:
        return
    if remaining < 1 and reset > 0:
        print(f"Rate limit reached, sleeping {reset:.0f}s...", file=sys.stderr)
        time.sleep(reset + 1)


def api_get(path, params=None):
    """One GET to `{API_ROOT}{path}`. Returns the parsed JSON body. Raises
    RedditAPIError on any non-2xx response (after retries for 429/5xx)."""
    query = {k: v for k, v in (params or {}).items() if v is not None}
    query["raw_json"] = 1  # don't HTML-escape &, <, > in text fields
    url = f"{API_ROOT}{path}?{urllib.parse.urlencode(query)}"
    refreshed = False
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"bearer {_get_token(force_refresh=refreshed)}", "User-Agent": user_agent()},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                _respect_rate_limit(resp.headers)
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and not refreshed:
                refreshed = True  # cached token revoked/expired early
                continue
            if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                backoff = 5 * 2**attempt
                print(f"HTTP {exc.code} from {path}, retrying in {backoff}s...", file=sys.stderr)
                time.sleep(backoff)
                continue
            hint = {
                403: " (private/quarantined/banned subreddit or suspended user)",
                404: " (no such subreddit/user/post)",
            }.get(exc.code, "")
            raise RedditAPIError(f"HTTP {exc.code} from {path}{hint}", exc.code) from exc
    raise RedditAPIError(f"Gave up on {path} after {MAX_RETRIES} attempts")


def paginate(path, params=None, max_results=100):
    """Yield the `data` dict of each child in a Listing, following `after`
    until `max_results` items (None = until Reddit's ~1000-item cap) or the
    listing runs out. Each yielded dict gets `_kind` (t1 comment, t3 post,
    t5 subreddit, t2 user) added."""
    params = dict(params or {})
    yielded = 0
    while max_results is None or yielded < max_results:
        remaining = PAGE_LIMIT if max_results is None else max_results - yielded
        params["limit"] = min(PAGE_LIMIT, remaining)
        listing = api_get(path, params)
        children = listing["data"]["children"]
        for child in children:
            item = dict(child["data"])
            item["_kind"] = child["kind"]
            yield item
            yielded += 1
            if max_results is not None and yielded >= max_results:
                return
        after = listing["data"].get("after")
        if not after or not children:
            return
        params["after"] = after


def extract_post_id(url_or_id):
    """Accepts a bare post ID ("1abcde"), a "t3_" fullname, or any
    reddit.com/.../comments/<id>/... or redd.it/<id> URL; returns the bare
    ID. Share links (/r/x/s/<code>) are opaque redirects — open them in a
    browser and copy the resulting URL instead."""
    candidate = url_or_id.strip()
    match = POST_ID_RE.match(candidate)
    if match and "/" not in candidate:
        return match.group(1).lower()
    for pattern in URL_ID_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1).lower()
    raise ValueError(f"Could not extract a post ID from {url_or_id!r}")


def iso_utc(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_date(value):
    """'YYYY-MM-DD' or full ISO 8601 -> UTC epoch seconds."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def post_record(d):
    """Normalise a t3 (post) `data` dict to the fields worth keeping."""
    return {
        "id": d.get("id"),
        "subreddit": d.get("subreddit"),
        "title": d.get("title"),
        "author": d.get("author"),
        "created_utc": iso_utc(d.get("created_utc")),
        "score": d.get("score"),
        "upvote_ratio": d.get("upvote_ratio"),
        "num_comments": d.get("num_comments"),
        "flair": d.get("link_flair_text"),
        "is_self": d.get("is_self"),
        "over_18": d.get("over_18"),
        "selftext": d.get("selftext") or None,
        "link_url": None if d.get("is_self") else d.get("url"),
        "url": f"https://www.reddit.com{d['permalink']}" if d.get("permalink") else None,
        "removed": d.get("removed_by_category"),
        "edited": iso_utc(d["edited"]) if d.get("edited") else None,
    }


def comment_record(d, depth=None):
    """Normalise a t1 (comment) `data` dict."""
    return {
        "id": d.get("id"),
        "parent_id": d.get("parent_id"),
        "link_id": d.get("link_id"),
        "subreddit": d.get("subreddit"),
        "author": d.get("author"),
        "created_utc": iso_utc(d.get("created_utc")),
        "score": d.get("score"),
        "depth": d.get("depth") if depth is None else depth,
        "body": d.get("body"),
        "is_submitter": d.get("is_submitter"),
        "edited": iso_utc(d["edited"]) if d.get("edited") else None,
        "url": f"https://www.reddit.com{d['permalink']}" if d.get("permalink") else None,
        # Only present on comments from user/comment listings, not threads.
        "post_title": d.get("link_title"),
    }


def keyword_matches(text, keywords):
    """Case-insensitive substring match; returns the keywords that hit."""
    haystack = (text or "").lower()
    return [kw for kw in keywords if kw.lower() in haystack]


def in_date_range(created_utc_epoch, after_epoch=None, before_epoch=None):
    if after_epoch is not None and created_utc_epoch < after_epoch:
        return False
    if before_epoch is not None and created_utc_epoch >= before_epoch:
        return False
    return True


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
