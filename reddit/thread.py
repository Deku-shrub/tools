"""Fetch a Reddit post and its full comment tree via the official API.

GET /comments/<id> returns the post plus the first ~200-500 comments; the
rest of a big thread is hidden behind two kinds of stub:
  - "load more comments" — a `more` object listing child IDs, expanded via
    /api/morechildren (up to 100 IDs per call)
  - "continue this thread" — a `more` with no IDs, marking a reply chain
    cut off for depth; expanded by re-fetching /comments/<id>?comment=<parent>
Both are expanded by default (one API call each, capped by
--max-more-calls) so the result is every visible comment, not just the
first screenful. --no-expand skips that for a quick look.

Comments come back flattened in thread (depth-first) order, each with
`depth` and `parent_id` (t3_ = top-level, t1_ = reply), so the JSON can be
re-nested or scanned linearly.

--match keeps only comments containing any of the given keywords (case-
insensitive), the way to search comment text that search.py can't reach.

Programmatic use:
    from thread import get_thread

    t = get_thread("https://www.reddit.com/r/MandelaEffect/comments/abc123/...")
    print(t["post"]["title"], len(t["comments"]))
    for c in t["comments"]:
        print("  " * c["depth"], c["author"], c["body"][:80])

CLI use:
    python thread.py https://www.reddit.com/r/X/comments/abc123/title/
    python thread.py abc123 --sort old --match braces --match "steel teeth"
    python thread.py abc123 --json research/thread_abc123.json
"""

import argparse
import sys
import textwrap

from client import (
    RedditAPIError,
    api_get,
    comment_record,
    extract_post_id,
    keyword_matches,
    post_record,
    write_json,
)

SORTS = {"confidence", "top", "new", "controversial", "old", "qa"}
MORECHILDREN_BATCH = 100


class _Tree:
    """Collects comments from however many API responses, then emits them
    in depth-first thread order."""

    def __init__(self):
        self.comments = {}  # fullname -> raw comment data
        self.children = {}  # parent fullname -> [child fullnames], in API order
        self.pending_ids = []  # comment IDs from "load more" stubs
        self.pending_continues = []  # parent fullnames from "continue this thread" stubs

    def add(self, things):
        for thing in things:
            kind, d = thing["kind"], thing["data"]
            if kind == "more":
                if d.get("children"):
                    self.pending_ids.extend(d["children"])
                elif d.get("count", 0) == 0 and d.get("parent_id", "").startswith("t1_"):
                    self.pending_continues.append(d["parent_id"])
                continue
            if kind != "t1":
                continue
            # Already-seen comments still get their replies walked: a
            # "continue this thread" refetch re-returns the parent with the
            # previously cut-off replies nested under it.
            if d["name"] not in self.comments:
                self.comments[d["name"]] = d
                self.children.setdefault(d["parent_id"], []).append(d["name"])
            replies = d.get("replies")
            if isinstance(replies, dict):
                self.add(replies["data"]["children"])

    def ordered(self, root):
        out = []
        stack = [(name, 0) for name in reversed(self.children.get(root, []))]
        while stack:
            name, depth = stack.pop()
            out.append(comment_record(self.comments[name], depth=depth))
            stack.extend((child, depth + 1) for child in reversed(self.children.get(name, [])))
        return out


def get_thread(post, *, sort="confidence", expand=True, max_more_calls=100, match=None):
    """Returns {"post": post_record, "comments": [...], "incomplete": bool}.
    `incomplete` is True if --max-more-calls ran out before every stub
    was expanded."""
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {SORTS}")
    post_id = extract_post_id(post)
    link_id = f"t3_{post_id}"

    post_listing, comment_listing = api_get(f"/comments/{post_id}", {"sort": sort, "limit": 500})
    post_data = post_listing["data"]["children"][0]["data"]
    tree = _Tree()
    tree.add(comment_listing["data"]["children"])

    calls = 0
    while expand and (tree.pending_ids or tree.pending_continues) and calls < max_more_calls:
        calls += 1
        if tree.pending_ids:
            batch = tree.pending_ids[:MORECHILDREN_BATCH]
            del tree.pending_ids[:MORECHILDREN_BATCH]
            response = api_get(
                "/api/morechildren",
                {"api_type": "json", "link_id": link_id, "children": ",".join(batch), "sort": sort},
            )
            tree.add(response["json"]["data"]["things"])
        else:
            parent = tree.pending_continues.pop(0)
            _, sub_listing = api_get(
                f"/comments/{post_id}", {"comment": parent.removeprefix("t1_"), "sort": sort, "limit": 500}
            )
            tree.add(sub_listing["data"]["children"])

    comments = tree.ordered(link_id)
    if match:
        kept = []
        for c in comments:
            hits = keyword_matches(c["body"], match)
            if hits:
                kept.append({**c, "matched": hits})
        comments = kept

    return {
        "post": post_record(post_data),
        "comments": comments,
        "total_fetched": len(tree.comments),
        "incomplete": bool(tree.pending_ids or tree.pending_continues),
    }


def _build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("post", help="Post URL, redd.it link, t3_ fullname, or bare ID")
    p.add_argument("--sort", default="confidence", choices=sorted(SORTS), help="confidence = Reddit's 'best'")
    p.add_argument("--no-expand", action="store_true", help="Don't expand 'load more' / 'continue thread' stubs")
    p.add_argument("--max-more-calls", type=int, default=100)
    p.add_argument("--match", action="append", help="Keep only comments containing this (repeatable, OR'd)")
    p.add_argument("--json", metavar="PATH", help="Write the thread as JSON to this file instead of printing")
    return p


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _build_arg_parser().parse_args()
    try:
        thread = get_thread(
            args.post,
            sort=args.sort,
            expand=not args.no_expand,
            max_more_calls=args.max_more_calls,
            match=args.match,
        )
    except (RedditAPIError, ValueError) as exc:
        sys.exit(f"Thread fetch failed: {exc}")

    if args.json:
        write_json(args.json, thread)
        print(f"Wrote post + {len(thread['comments'])} comment(s) to {args.json}")
        return

    p = thread["post"]
    print(p["title"])
    print(f"r/{p['subreddit']} · u/{p['author']} · {p['created_utc']} · {p['score']} pts · {p['num_comments']} comments")
    print(p["url"])
    if p["link_url"]:
        print(f"-> {p['link_url']}")
    if p["selftext"]:
        print()
        print(textwrap.fill(p["selftext"], 100, replace_whitespace=False))
    label = "matching comment(s)" if args.match else "comment(s)"
    print(f"\n{len(thread['comments'])} {label} (of {thread['total_fetched']} fetched)"
          + (" — INCOMPLETE, raise --max-more-calls" if thread["incomplete"] else "") + "\n")

    for c in thread["comments"]:
        indent = "  " * c["depth"] if not args.match else ""
        print(f"{indent}u/{c['author']} · {c['score']} pts · {c['created_utc']}" + (" [OP]" if c["is_submitter"] else ""))
        for line in (c["body"] or "").splitlines() or [""]:
            print(textwrap.fill(line, 100, initial_indent=indent + "  ", subsequent_indent=indent + "  ") if line else "")
        if args.match:
            print(f"  {c['url']}")
        print()


if __name__ == "__main__":
    main()
