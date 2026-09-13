"""Fetch comments for a YouTube video via the YouTube Data API v3
(commentThreads.list for top-level comments, comments.list to page through
replies beyond the handful returned inline).

commentThreads.list with part=snippet,replies returns each top-level
comment plus up to its first ~5 replies inline — cheap (1 quota unit per
call, see client.py's docstring) but incomplete for heavily-replied
threads. Pass --full-replies to fetch every reply on a thread whose
totalReplyCount exceeds what came back inline (one extra comments.list
call per such thread).

Comments can be disabled for a video, or a video can simply not exist —
both surface as a YouTubeAPIError ("commentsDisabled" / "videoNotFound")
rather than an empty list, and are reported explicitly below so neither
looks like "no comments yet".

Programmatic use:
    from comments import get_comments

    for c in get_comments("dQw4w9WgXcQ", max_results=200, order="time"):
        print(c["author"], c["like_count"], c["text"])

CLI use:
    python comments.py dQw4w9WgXcQ --max-results 200 --order time
    python comments.py "https://youtu.be/dQw4w9WgXcQ" --full-replies --json out.json
"""

import argparse
import json
import sys
from pathlib import Path

from client import YouTubeAPIError, extract_video_id, paginate

ORDERS = {"time", "relevance"}


def _comment_record(comment_item, *, video_id, parent_id=None):
    snippet = comment_item["snippet"]
    return {
        "comment_id": comment_item["id"],
        "video_id": video_id,
        "parent_id": parent_id,
        "author": snippet.get("authorDisplayName"),
        "author_channel_id": (snippet.get("authorChannelId") or {}).get("value"),
        "text": snippet.get("textDisplay"),
        "like_count": snippet.get("likeCount"),
        "published_at": snippet.get("publishedAt"),
        "updated_at": snippet.get("updatedAt"),
    }


def _fetch_all_replies(parent_id, video_id, api_key=None):
    return [
        _comment_record(item, video_id=video_id, parent_id=parent_id)
        for item in paginate(
            "comments",
            {"part": "snippet", "parentId": parent_id, "textFormat": "plainText"},
            api_key=api_key,
        )
    ]


def get_comments(
    video_id_or_url,
    *,
    max_results=100,
    order="time",
    full_replies=False,
    api_key=None,
):
    """`max_results` caps the number of top-level threads fetched; replies
    (inline, or all of them with full_replies=True) are additional and not
    counted against it."""
    if order not in ORDERS:
        raise ValueError(f"order must be one of {ORDERS}")
    video_id = extract_video_id(video_id_or_url)

    params = {
        "part": "snippet,replies",
        "videoId": video_id,
        "order": order,
        "textFormat": "plainText",
    }

    results = []
    for thread in paginate("commentThreads", params, max_results=max_results, api_key=api_key):
        top_snippet = thread["snippet"]
        top_comment = top_snippet["topLevelComment"]
        results.append(_comment_record(top_comment, video_id=video_id))

        total_replies = top_snippet.get("totalReplyCount", 0)
        inline_replies = thread.get("replies", {}).get("comments", [])
        if full_replies and total_replies > len(inline_replies):
            results.extend(_fetch_all_replies(top_comment["id"], video_id, api_key=api_key))
        else:
            results.extend(
                _comment_record(reply, video_id=video_id, parent_id=top_comment["id"])
                for reply in inline_replies
            )

    return results


def _build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("video", help="Video ID or any YouTube URL (watch/share/shorts/embed/live)")
    p.add_argument("--max-results", type=int, default=100, help="Top-level threads to fetch (replies are extra)")
    p.add_argument("--order", default="time", choices=sorted(ORDERS))
    p.add_argument("--full-replies", action="store_true", help="Fetch every reply on a thread, not just the first ~5")
    p.add_argument("--api-key", help="Overrides the YOUTUBE_API_KEY environment variable")
    p.add_argument("--json", metavar="PATH", help="Write results as JSON to this file instead of printing")
    return p


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _build_arg_parser().parse_args()
    try:
        results = get_comments(
            args.video,
            max_results=args.max_results,
            order=args.order,
            full_replies=args.full_replies,
            api_key=args.api_key,
        )
    except ValueError as exc:
        sys.exit(str(exc))
    except YouTubeAPIError as exc:
        if exc.reason == "commentsDisabled":
            sys.exit("Comments are disabled for this video.")
        if exc.reason == "videoNotFound":
            sys.exit("Video not found.")
        sys.exit(f"Fetching comments failed ({exc.reason or exc.status}): {exc}")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {len(results)} comment(s) to {args.json}")
        return

    print(f"{len(results)} comment(s):\n")
    for c in results:
        indent = "  ↳ " if c["parent_id"] else ""
        print(f"{indent}{c['author']} ({c['like_count']} likes) — {c['published_at']}")
        print(f"{indent}  {c['text']}")
        print()


if __name__ == "__main__":
    main()
