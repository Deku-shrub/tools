"""Search and fetch Reddit posts/comments from the Arctic Shift archive
(https://github.com/ArthurHeitmann/arctic_shift) — a free, keyless,
community-run copy of Reddit, kept up to date (new content appears within
minutes to hours). Unlike the official API (client.py) it can:
  - search *comment* text
  - filter by exact date range (--after/--before)
  - page through a subreddit's or user's entire history (no 1000-item cap)

Records come out in the same shape as search.py / listing.py / thread.py
(client.post_record / comment_record), plus `retrieved_on` and
`source: "arctic_shift"`.

Caveats (verified against the live API, 2026-11):
  - Keyword search (--query/--title/--selftext/--body) requires --subreddit
    or --author; a site-wide keyword search is rejected.
  - Scores and comment counts are captured when the archive first saw the
    item (often minutes after posting, so near 0/1) and only sometimes
    refreshed later — see `retrieved_on`. Don't trust them for ranking;
    check the live post on reddit.com.
  - Deleted/removed content often survives here if it was archived first.
  - Comment full-text search is slow server-side and times out over long
    date ranges ("Timeout. Maybe slow down a bit", HTTP 422). This module
    works around that by walking the date range in windows (--window-days,
    default 90 for comment keyword search) and halving any window that
    times out. A window that still times out at MIN_WINDOW_SECONDS is
    skipped and listed in the `gaps` the CLI reports.
  - Rate limits are dynamic (server load × query cost). A 429, or a 422
    that comes back instantly, means the per-minute budget is spent; the
    client sleeps for the server's X-RateLimit-Reset and retries.
  - No uptime guarantee — it's one person's project. Be considerate.

Programmatic use:
    from archive import search_posts, search_comments, get_thread

    posts = search_posts(subreddit="MandelaEffect", query="moonraker")
    comments = search_comments(subreddit="MandelaEffect", body="braces",
                               after="2016-01-01", before="2018-01-01")
    t = get_thread("3vzk53")

CLI use:
    python archive.py posts -r MandelaEffect --query moonraker
    python archive.py posts -r MandelaEffect --title braces --after 2015-01-01 --before 2017-01-01
    python archive.py comments -r MandelaEffect --body "dolly braces" --after 2016-01-01 --json research/dolly_comments.json
    python archive.py comments --author someuser --newest-first --max-results 500
    python archive.py thread https://www.reddit.com/r/MandelaEffect/comments/3vzk53/ --match braces
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from client import (
    comment_record,
    extract_post_id,
    iso_utc,
    keyword_matches,
    parse_date,
    post_record,
    write_json,
)
from thread import _Tree

API_ROOT = "https://arctic-shift.photon-reddit.com"
USER_AGENT = "windows:research-tools:v0.1 (personal research)"
PAGE_LIMIT = 100
EARLIEST = parse_date("2005-06-01")  # Reddit launched June 2005
MIN_WINDOW_SECONDS = 6 * 3600
DEFAULT_WINDOW_DAYS = {"comments_keyword": 90}
MAX_RETRIES = 6
# A 422 "Timeout" faster than this is the server refusing up front (budget
# spent), not the query actually running out of time.
FAST_FAIL_SECONDS = 3


class ArchiveError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _QueryTimeout(ArchiveError):
    pass


def api_get(path, params):
    """One GET to `{API_ROOT}{path}`; returns the `data` field. Sleeps and
    retries on rate limiting; raises _QueryTimeout when the query itself is
    too slow (caller should narrow it)."""
    query = {k: v for k, v in params.items() if v is not None}
    url = f"{API_ROOT}{path}?{urllib.parse.urlencode(query)}"
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.load(resp)
            if payload.get("error"):
                raise ArchiveError(f"{path}: {payload['error']}")
            return payload.get("data") or []
        except urllib.error.HTTPError as exc:
            elapsed = time.monotonic() - started
            body = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(body).get("error") or body[:200]
            except json.JSONDecodeError:
                message = body[:200]
            reset = float(exc.headers.get("X-RateLimit-Reset") or 10)
            budget_spent = exc.code == 429 or (exc.code == 422 and elapsed < FAST_FAIL_SECONDS)
            if budget_spent and attempt < MAX_RETRIES - 1:
                print(f"  rate limited, sleeping {reset + 1:.0f}s...", file=sys.stderr)
                time.sleep(reset + 1)
                continue
            if exc.code == 422 and "timeout" in message.lower():
                raise _QueryTimeout(f"{path}: {message}", exc.code) from exc
            if exc.code in (500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                time.sleep(5 * 2**attempt)
                continue
            raise ArchiveError(f"HTTP {exc.code} from {path}: {message}", exc.code) from exc
        except urllib.error.URLError as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(5 * 2**attempt)
                continue
            raise ArchiveError(f"{path}: {exc.reason}") from exc
    raise ArchiveError(f"Gave up on {path} after {MAX_RETRIES} attempts")


def _fmt(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _walk_range(path, params, lo, hi, newest_first, gaps):
    """Yield every item in [lo, hi) in date order, paging by created_utc and
    halving the remaining range whenever the server times out."""
    seen = set()
    while lo < hi:
        page_params = {**params, "after": int(lo), "before": int(hi), "limit": PAGE_LIMIT,
                       "sort": "desc" if newest_first else "asc"}
        try:
            page = api_get(path, page_params)
        except _QueryTimeout:
            if hi - lo <= MIN_WINDOW_SECONDS:
                print(f"  window {_fmt(lo)} -> {_fmt(hi)} still times out, skipping", file=sys.stderr)
                gaps.append({"after": iso_utc(lo), "before": iso_utc(hi)})
                return
            mid = lo + (hi - lo) / 2
            halves = [(mid, hi), (lo, mid)] if newest_first else [(lo, mid), (mid, hi)]
            for a, b in halves:
                yield from _walk_range(path, params, a, b, newest_first, gaps)
            return

        fresh = [item for item in page if item["id"] not in seen]
        for item in fresh:
            seen.add(item["id"])
            yield item
        if len(page) < PAGE_LIMIT:
            return
        edge = page[-1]["created_utc"]
        if not fresh:
            # A full page of one timestamp we've already seen: step past it.
            edge = edge - 1 if newest_first else edge + 1
        if newest_first:
            hi = edge + 1  # `before` is exclusive; dedupe handles the overlap
        else:
            lo = edge


def _windowed(path, params, *, after, before, window_days, newest_first, gaps):
    lo = parse_date(after) if after else EARLIEST
    hi = parse_date(before) if before else time.time() + 86400
    if not window_days:
        yield from _walk_range(path, params, lo, hi, newest_first, gaps)
        return
    step = window_days * 86400
    edges = []
    start = lo
    while start < hi:
        edges.append((start, min(start + step, hi)))
        start += step
    if newest_first:
        edges.reverse()
    for a, b in edges:
        print(f"  {_fmt(a)[:10]} -> {_fmt(b)[:10]}", file=sys.stderr)
        yield from _walk_range(path, params, a, b, newest_first, gaps)


def _archive_meta(record, d):
    record["retrieved_on"] = iso_utc(d.get("retrieved_on"))
    record["source"] = "arctic_shift"
    return record


def _comment(d, depth=None):
    record = comment_record(d, depth=depth)
    # Arctic Shift's permalinks are malformed (/comments/t3_<id>/<cid>);
    # rebuild a real one.
    if d.get("link_id") and d.get("subreddit"):
        record["url"] = (
            f"https://www.reddit.com/r/{d['subreddit']}/comments/"
            f"{d['link_id'].removeprefix('t3_')}/_/{d['id']}/"
        )
    return _archive_meta(record, d)


def _require_scope(subreddit, author, keyword):
    if keyword and not (subreddit or author):
        raise ValueError("Keyword search needs --subreddit or --author (Arctic Shift has no site-wide search).")
    if not (subreddit or author or keyword):
        raise ValueError("Give at least --subreddit or --author.")


def search_posts(
    *,
    subreddit=None,
    author=None,
    query=None,
    title=None,
    selftext=None,
    url=None,
    after=None,
    before=None,
    max_results=1000,
    newest_first=False,
    window_days=None,
    match=None,
    gaps=None,
):
    """Posts matching all given filters, oldest first unless newest_first.
    `query` searches title + body; `title`/`selftext` one field each; `url`
    is a prefix match on the linked URL. Returns post_record()-shaped dicts."""
    _require_scope(subreddit, author, query or title or selftext)
    params = {"subreddit": subreddit, "author": author, "query": query, "title": title,
              "selftext": selftext, "url": url}
    gaps = [] if gaps is None else gaps
    results = []
    for d in _windowed("/api/posts/search", params, after=after, before=before,
                       window_days=window_days, newest_first=newest_first, gaps=gaps):
        record = _archive_meta(post_record(d), d)
        if match:
            hits = keyword_matches(f"{record['title']} {record['selftext'] or ''}", match)
            if not hits:
                continue
            record["matched"] = hits
        results.append(record)
        if max_results and len(results) >= max_results:
            break
    return results


def search_comments(
    *,
    subreddit=None,
    author=None,
    body=None,
    link_id=None,
    after=None,
    before=None,
    max_results=1000,
    newest_first=False,
    window_days=None,
    match=None,
    gaps=None,
):
    """Comments matching all given filters. `body` is server-side full-text
    search (all words must appear, stemmed); `match` is a strict local
    substring filter on top. Returns comment_record()-shaped dicts."""
    _require_scope(subreddit, author or link_id, body)
    if window_days is None and body:
        window_days = DEFAULT_WINDOW_DAYS["comments_keyword"]
    params = {"subreddit": subreddit, "author": author, "body": body,
              "link_id": f"t3_{extract_post_id(link_id)}" if link_id else None}
    gaps = [] if gaps is None else gaps
    results = []
    for d in _windowed("/api/comments/search", params, after=after, before=before,
                       window_days=window_days, newest_first=newest_first, gaps=gaps):
        record = _comment(d)
        if match:
            hits = keyword_matches(record["body"], match)
            if not hits:
                continue
            record["matched"] = hits
        results.append(record)
        if max_results and len(results) >= max_results:
            break
    return results


def get_thread(post, *, match=None):
    """Same shape as thread.get_thread(), from the archive — includes
    comments since deleted on Reddit, but scores are snapshot values."""
    post_id = extract_post_id(post)
    posts = api_get("/api/posts/ids", {"ids": post_id})
    if not posts:
        raise ArchiveError(f"Post {post_id} not in the archive")
    tree_things = api_get("/api/comments/tree", {"link_id": f"t3_{post_id}", "limit": 9999})
    tree = _Tree()
    tree.add(tree_things)
    by_name = tree.comments
    comments = []
    for c in tree.ordered(f"t3_{post_id}"):
        record = _comment(by_name[f"t1_{c['id']}"], depth=c["depth"])
        if match:
            hits = keyword_matches(record["body"], match)
            if not hits:
                continue
            record["matched"] = hits
        comments.append(record)
    return {
        "post": _archive_meta(post_record(posts[0]), posts[0]),
        "comments": comments,
        "total_fetched": len(by_name),
        "incomplete": bool(tree.pending_ids or tree.pending_continues),
    }


def _build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--subreddit", "-r")
        sp.add_argument("--author", "-u")
        sp.add_argument("--after", help="YYYY-MM-DD (inclusive)")
        sp.add_argument("--before", help="YYYY-MM-DD (exclusive)")
        sp.add_argument("--max-results", type=int, default=1000, help="0 = no limit")
        sp.add_argument("--newest-first", action="store_true")
        sp.add_argument("--window-days", type=float, help="Walk the date range in windows this size")
        sp.add_argument("--match", action="append", help="Strict local substring filter (repeatable, OR'd)")
        sp.add_argument("--json", metavar="PATH")

    posts = sub.add_parser("posts", help="Search posts")
    common(posts)
    posts.add_argument("--query", "-q", help="Keywords in title or body")
    posts.add_argument("--title")
    posts.add_argument("--selftext")
    posts.add_argument("--url", help="Linked-URL prefix, e.g. https://www.youtube.com/watch?v=")

    comments = sub.add_parser("comments", help="Search comments")
    common(comments)
    comments.add_argument("--body", "-q", help="Full-text search of comment bodies")
    comments.add_argument("--post", help="Only comments on this post (URL or ID)")

    thread = sub.add_parser("thread", help="Fetch a post and its archived comment tree")
    thread.add_argument("post", help="Post URL or ID")
    thread.add_argument("--match", action="append")
    thread.add_argument("--json", metavar="PATH")
    return p


def _print_comment(c, indent=""):
    print(f"{indent}u/{c['author']} · r/{c['subreddit']} · {c['created_utc']} · {c['score']} pts (archived)")
    for line in (c["body"] or "").splitlines():
        print(f"{indent}  {line}")
    print(f"{indent}  {c['url']}")
    print()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _build_arg_parser().parse_args()
    gaps = []
    try:
        if args.command == "thread":
            output = get_thread(args.post, match=args.match)
        elif args.command == "posts":
            output = search_posts(
                subreddit=args.subreddit, author=args.author, query=args.query, title=args.title,
                selftext=args.selftext, url=args.url, after=args.after, before=args.before,
                max_results=args.max_results, newest_first=args.newest_first,
                window_days=args.window_days, match=args.match, gaps=gaps,
            )
        else:
            output = search_comments(
                subreddit=args.subreddit, author=args.author, body=args.body, link_id=args.post,
                after=args.after, before=args.before, max_results=args.max_results,
                newest_first=args.newest_first, window_days=args.window_days,
                match=args.match, gaps=gaps,
            )
    except (ArchiveError, ValueError) as exc:
        sys.exit(f"Archive query failed: {exc}")

    if gaps:
        print(f"WARNING: {len(gaps)} window(s) timed out and were skipped: {gaps}", file=sys.stderr)

    if args.json:
        write_json(args.json, {"results": output, "gaps": gaps} if gaps else output)
        n = len(output["comments"]) if args.command == "thread" else len(output)
        print(f"Wrote {n} item(s) to {args.json}")
        return

    if args.command == "thread":
        p = output["post"]
        print(f"{p['title']}\nr/{p['subreddit']} · u/{p['author']} · {p['created_utc']}\n{p['url']}")
        if p["selftext"]:
            print(f"\n{p['selftext']}")
        print(f"\n{len(output['comments'])} comment(s) (of {output['total_fetched']} archived)\n")
        for c in output["comments"]:
            _print_comment(c, "" if args.match else "  " * c["depth"])
        return

    print(f"{len(output)} result(s):\n")
    for r in output:
        if args.command == "comments":
            _print_comment(r)
            continue
        print(f"- {r['title']}")
        print(f"  r/{r['subreddit']} · u/{r['author']} · {r['created_utc']}")
        print(f"  {r['url']}")
        if r["link_url"]:
            print(f"  -> {r['link_url']}")
        if r["selftext"]:
            print(f"  {r['selftext'][:200].replace(chr(10), ' ')}")
        print()


if __name__ == "__main__":
    main()
