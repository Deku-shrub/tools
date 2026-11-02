"""Browse a subreddit's feed or a user's post/comment history via the
official API — the "scroll through it" counterpart to search.py.

Targets:
    r/<name>     subreddit feed: --sort hot | new | top | rising | controversial
    u/<name>     user history: --what submitted (posts) | comments | overview (both)
                 with --sort new | top | hot | controversial

--time (hour..all) applies to top/controversial. Like search, every listing
is capped at ~1000 items by Reddit (client.py), so a prolific user's full
history may not be reachable — --sort top --time all and --sort new give
two different 1000-item windows.

--after/--before/--match filter locally (see search.py). For comments,
--match checks the comment body; for posts, title + body.

Programmatic use:
    from listing import listing

    items = listing("u/someuser", what="comments", sort="new", max_results=500)
    items = listing("r/MandelaEffect", sort="top", time_filter="all", max_results=200)

CLI use:
    python listing.py r/MandelaEffect --sort top --time all --max-results 200
    python listing.py u/someuser --what comments --match braces --json research/someuser.json
"""

import argparse
import re
import sys

from client import (
    RedditAPIError,
    comment_record,
    in_date_range,
    keyword_matches,
    paginate,
    parse_date,
    post_record,
    write_json,
)

SUB_SORTS = {"hot", "new", "top", "rising", "controversial"}
USER_SORTS = {"new", "top", "hot", "controversial"}
USER_WHATS = {"submitted", "comments", "overview"}
TIMES = {"hour", "day", "week", "month", "year", "all"}

TARGET_RE = re.compile(r"(?:^|/)(r|u|user)/([A-Za-z0-9_-]+)")


def parse_target(target):
    """'r/x', '/r/x', 'u/x', '/user/x' or a full reddit URL -> ('r'|'u', name)."""
    match = TARGET_RE.search(target.strip())
    if not match:
        raise ValueError(f"Target must look like r/<subreddit> or u/<user>, got {target!r}")
    prefix, name = match.groups()
    return ("u" if prefix in ("u", "user") else "r"), name


def listing(
    target,
    *,
    sort="new",
    time_filter="all",
    what="overview",
    max_results=100,
    after=None,
    before=None,
    match=None,
):
    kind, name = parse_target(target)
    if kind == "r":
        if sort not in SUB_SORTS:
            raise ValueError(f"subreddit sort must be one of {SUB_SORTS}")
        path = f"/r/{name}/{sort}"
        params = {"t": time_filter if sort in ("top", "controversial") else None}
    else:
        if sort not in USER_SORTS:
            raise ValueError(f"user sort must be one of {USER_SORTS}")
        if what not in USER_WHATS:
            raise ValueError(f"what must be one of {USER_WHATS}")
        path = f"/user/{name}/{what}"
        params = {"sort": sort, "t": time_filter if sort in ("top", "controversial") else None}

    after_epoch = parse_date(after) if after else None
    before_epoch = parse_date(before) if before else None
    filtering = after_epoch or before_epoch or match
    results = []
    for d in paginate(path, params, None if filtering else max_results):
        created = d.get("created_utc") or 0
        if sort == "new" and after_epoch is not None and created < after_epoch:
            break
        if not in_date_range(created, after_epoch, before_epoch):
            continue
        if d["_kind"] == "t1":
            record = {"type": "comment", **comment_record(d)}
            text = record["body"]
        else:
            record = {"type": "post", **post_record(d)}
            text = f"{record['title']} {record['selftext'] or ''}"
        if match:
            hits = keyword_matches(text, match)
            if not hits:
                continue
            record["matched"] = hits
        results.append(record)
        if len(results) >= max_results:
            break
    return results


def _build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("target", help="r/<subreddit> or u/<user> (or a URL to one)")
    p.add_argument("--sort", default="new")
    p.add_argument("--time", dest="time_filter", default="all", choices=sorted(TIMES))
    p.add_argument("--what", default="overview", choices=sorted(USER_WHATS), help="User targets only")
    p.add_argument("--max-results", type=int, default=100)
    p.add_argument("--after", help="YYYY-MM-DD (local filter)")
    p.add_argument("--before", help="YYYY-MM-DD (local filter)")
    p.add_argument("--match", action="append", help="Keep only items containing this (repeatable, OR'd)")
    p.add_argument("--json", metavar="PATH", help="Write results as JSON to this file instead of printing")
    return p


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _build_arg_parser().parse_args()
    try:
        results = listing(
            args.target,
            sort=args.sort,
            time_filter=args.time_filter,
            what=args.what,
            max_results=args.max_results,
            after=args.after,
            before=args.before,
            match=args.match,
        )
    except (RedditAPIError, ValueError) as exc:
        sys.exit(f"Listing failed: {exc}")

    if args.json:
        write_json(args.json, results)
        print(f"Wrote {len(results)} item(s) to {args.json}")
        return

    print(f"{len(results)} item(s):\n")
    for r in results:
        if r["type"] == "comment":
            print(f"- [comment] r/{r['subreddit']} · u/{r['author']} · {r['created_utc']} · {r['score']} pts")
            print(f"  on: {r['post_title']}")
            print(f"  {(r['body'] or '')[:300].replace(chr(10), ' ')}")
        else:
            print(f"- {r['title']}")
            print(f"  r/{r['subreddit']} · u/{r['author']} · {r['created_utc']} · {r['score']} pts · {r['num_comments']} comments")
            if r["selftext"]:
                print(f"  {r['selftext'][:200].replace(chr(10), ' ')}")
        print(f"  {r['url']}")
        print()


if __name__ == "__main__":
    main()
