"""Search Reddit posts (or subreddits/users) via the official API's
/search endpoint, site-wide or within one subreddit.

Query syntax — Reddit's search supports field operators inside the query
string itself, combinable with AND / OR / NOT and parentheses:
    title:braces            title only
    selftext:"steel teeth"  post body only (quotes = phrase)
    author:someuser         by user
    subreddit:MandelaEffect (or use --subreddit, which also restricts paging)
    site:youtube.com        link posts to a domain
    url:moonraker           text in the linked URL
    flair:"Visual"          by post flair
    self:yes / self:no      text posts / link posts only

What it can't do:
  - Search comment text. Reddit's API search only covers posts (title +
    body). To find a word in comments, pull candidate threads with this and
    scan them with `thread.py --match`.
  - Date ranges. `--time` only offers rolling windows (past day/week/...),
    so --after/--before filter locally on created_utc. With --sort new,
    paging stops as soon as results get older than --after; with other
    sorts every page up to --max-results is fetched and filtered.
  - Go deep: ~1000 results max per query (see client.py). Split broad
    queries by subreddit or by --time window if you hit that.

Search relevance is fuzzy (stemming, partial matches), so --match does a
strict local substring filter on title + body, like the BNA tool's
keyword_matches().

Programmatic use:
    from search import search

    posts = search("dolly braces", subreddit="MandelaEffect", sort="new", max_results=200)
    for p in posts:
        print(p["created_utc"], p["score"], p["title"], p["url"])

CLI use:
    python search.py "dolly braces" --subreddit MandelaEffect --sort top --time all
    python search.py 'title:moonraker (braces OR "steel teeth")' --sort new --after 2015-01-01 --json research/moonraker.json
    python search.py "mandela effect" --kind sr
"""

import argparse
import sys

from client import (
    RedditAPIError,
    in_date_range,
    iso_utc,
    keyword_matches,
    paginate,
    parse_date,
    post_record,
    write_json,
)

SORTS = {"relevance", "hot", "top", "new", "comments"}
TIMES = {"hour", "day", "week", "month", "year", "all"}
KINDS = {"link": "t3", "sr": "t5", "user": "t2"}


def _subreddit_record(d):
    return {
        "name": d.get("display_name"),
        "title": d.get("title"),
        "subscribers": d.get("subscribers"),
        "created_utc": iso_utc(d.get("created_utc")),
        "over_18": d.get("over18"),
        "description": d.get("public_description"),
        "url": f"https://www.reddit.com{d['url']}" if d.get("url") else None,
    }


def _user_record(d):
    return {
        "name": d.get("name"),
        "created_utc": iso_utc(d.get("created_utc")),
        "link_karma": d.get("link_karma"),
        "comment_karma": d.get("comment_karma"),
        "url": f"https://www.reddit.com/user/{d['name']}" if d.get("name") else None,
    }


def search(
    query,
    *,
    subreddit=None,
    sort="relevance",
    time_filter="all",
    kind="link",
    max_results=100,
    after=None,
    before=None,
    match=None,
    include_nsfw=True,
):
    """Returns a list of normalised records (post_record() for kind="link").
    `after`/`before` are 'YYYY-MM-DD' (or ISO) strings; `match` is a list of
    keywords of which at least one must appear in title + body."""
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {SORTS}")
    if time_filter not in TIMES:
        raise ValueError(f"time_filter must be one of {TIMES}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {set(KINDS)}")

    path = f"/r/{subreddit}/search" if subreddit else "/search"
    params = {
        "q": query,
        "sort": sort,
        "t": time_filter,
        "type": kind,
        "restrict_sr": 1 if subreddit else None,
        "include_over_18": "on" if include_nsfw else None,
    }

    if kind == "sr":
        return [_subreddit_record(d) for d in paginate(path, params, max_results)]
    if kind == "user":
        return [_user_record(d) for d in paginate(path, params, max_results)]

    after_epoch = parse_date(after) if after else None
    before_epoch = parse_date(before) if before else None
    # When filtering by date or keyword, max_results caps what's *fetched*
    # less usefully than what's *kept* — so page through Reddit's full
    # ~1000 and stop once enough survive the filters.
    filtering = after_epoch or before_epoch or match
    results = []
    for d in paginate(path, params, None if filtering else max_results):
        created = d.get("created_utc") or 0
        if sort == "new" and after_epoch is not None and created < after_epoch:
            break  # everything from here on is older
        if not in_date_range(created, after_epoch, before_epoch):
            continue
        record = post_record(d)
        if match:
            hits = keyword_matches(f"{record['title']} {record['selftext'] or ''}", match)
            if not hits:
                continue
            record["matched"] = hits
        results.append(record)
        if len(results) >= max_results:
            break
    return results


def _build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("query")
    p.add_argument("--subreddit", "-r", help="Restrict to one subreddit (no r/ prefix needed)")
    p.add_argument("--sort", default="relevance", choices=sorted(SORTS))
    p.add_argument("--time", dest="time_filter", default="all", choices=sorted(TIMES))
    p.add_argument("--kind", default="link", choices=sorted(KINDS), help="link = posts, sr = subreddits, user = users")
    p.add_argument("--max-results", type=int, default=100)
    p.add_argument("--after", help="Only posts created on/after this date, YYYY-MM-DD (local filter)")
    p.add_argument("--before", help="Only posts created before this date, YYYY-MM-DD (local filter)")
    p.add_argument("--match", action="append", help="Keep only posts whose title/body contains this (repeatable, OR'd)")
    p.add_argument("--no-nsfw", action="store_true")
    p.add_argument("--json", metavar="PATH", help="Write results as JSON to this file instead of printing")
    return p


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _build_arg_parser().parse_args()
    subreddit = args.subreddit.removeprefix("r/") if args.subreddit else None
    try:
        results = search(
            args.query,
            subreddit=subreddit,
            sort=args.sort,
            time_filter=args.time_filter,
            kind=args.kind,
            max_results=args.max_results,
            after=args.after,
            before=args.before,
            match=args.match,
            include_nsfw=not args.no_nsfw,
        )
    except RedditAPIError as exc:
        sys.exit(f"Search failed: {exc}")

    if args.json:
        write_json(args.json, results)
        print(f"Wrote {len(results)} result(s) to {args.json}")
        return

    print(f"{len(results)} result(s):\n")
    for r in results:
        if args.kind == "link":
            print(f"- {r['title']}")
            print(f"  r/{r['subreddit']} · u/{r['author']} · {r['created_utc']} · {r['score']} pts · {r['num_comments']} comments")
            print(f"  {r['url']}")
            if r["link_url"]:
                print(f"  -> {r['link_url']}")
            if r["selftext"]:
                print(f"  {r['selftext'][:200].replace(chr(10), ' ')}")
        elif args.kind == "sr":
            print(f"- r/{r['name']} ({r['subscribers'] or 0:,} subscribers) — {r['title']}")
            print(f"  {r['url']}")
        else:
            print(f"- u/{r['name']} (created {r['created_utc']})")
        print()


if __name__ == "__main__":
    main()
