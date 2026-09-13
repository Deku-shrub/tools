"""Search YouTube (videos, channels, or playlists) via the YouTube Data API
v3's search.list endpoint.

search.list is the API's most expensive call (100 quota units, vs 1 for
almost everything else — see client.py's docstring), so default
--max-results is kept modest (25) and pagination stops as soon as you have
enough rather than always fetching a full page ahead.

search.list's snippet does NOT include view/like/comment counts — pass
--with-stats to enrich video results with one extra (cheap, 1-unit)
videos.list call, batched up to 50 IDs at a time.

Programmatic use:
    from search import search

    results = search("moonraker trailer", max_results=10, with_stats=True)
    for r in results:
        print(r["title"], r.get("view_count"), r["url"])

CLI use:
    python search.py "moonraker trailer" --max-results 10 --with-stats
    python search.py "some channel" --kind channel --json out.json
"""

import argparse
import json
import sys
from pathlib import Path

from client import YouTubeAPIError, api_get, paginate

KINDS = {"video", "channel", "playlist"}
ORDERS = {"relevance", "date", "rating", "title", "videoCount", "viewCount"}
DURATIONS = {"any", "short", "medium", "long"}


def _watch_url(kind, item_id):
    if kind == "video":
        return f"https://www.youtube.com/watch?v={item_id}"
    if kind == "channel":
        return f"https://www.youtube.com/channel/{item_id}"
    if kind == "playlist":
        return f"https://www.youtube.com/playlist?list={item_id}"
    return None


def search(
    query,
    *,
    kind="video",
    order="relevance",
    max_results=25,
    published_after=None,
    published_before=None,
    channel_id=None,
    region_code=None,
    video_duration="any",
    safe_search="moderate",
    with_stats=False,
    api_key=None,
):
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if order not in ORDERS:
        raise ValueError(f"order must be one of {ORDERS}")
    if video_duration != "any" and video_duration not in DURATIONS:
        raise ValueError(f"video_duration must be one of {DURATIONS}")
    if video_duration != "any" and kind != "video":
        raise ValueError("video_duration only applies to kind='video'")

    params = {
        "part": "snippet",
        "q": query,
        "type": kind,
        "order": order,
        "safeSearch": safe_search,
    }
    if published_after:
        params["publishedAfter"] = published_after
    if published_before:
        params["publishedBefore"] = published_before
    if channel_id:
        params["channelId"] = channel_id
    if region_code:
        params["regionCode"] = region_code
    if video_duration != "any":
        params["videoDuration"] = video_duration

    results = []
    for item in paginate("search", params, max_results=max_results, api_key=api_key):
        item_id = item["id"].get(f"{kind}Id")
        snippet = item["snippet"]
        results.append(
            {
                "id": item_id,
                "kind": kind,
                "title": snippet.get("title"),
                "description": snippet.get("description"),
                "channel_title": snippet.get("channelTitle"),
                "channel_id": snippet.get("channelId"),
                "published_at": snippet.get("publishedAt"),
                "thumbnail_url": (snippet.get("thumbnails", {}).get("high") or {}).get("url"),
                "url": _watch_url(kind, item_id),
            }
        )

    if with_stats and kind == "video" and results:
        _attach_video_stats(results, api_key=api_key)

    return results


def _attach_video_stats(results, api_key=None):
    """videos.list accepts up to 50 comma-separated IDs per call — batch
    accordingly rather than one call per video."""
    by_id = {r["id"]: r for r in results}
    ids = list(by_id)
    for i in range(0, len(ids), 50):
        batch = ids[i : i + 50]
        payload = api_get(
            "videos",
            {"part": "statistics,contentDetails", "id": ",".join(batch)},
            api_key=api_key,
        )
        for item in payload.get("items", []):
            stats = item.get("statistics", {})
            target = by_id[item["id"]]
            target["view_count"] = int(stats["viewCount"]) if "viewCount" in stats else None
            target["like_count"] = int(stats["likeCount"]) if "likeCount" in stats else None
            target["comment_count"] = int(stats["commentCount"]) if "commentCount" in stats else None
            target["duration"] = item.get("contentDetails", {}).get("duration")


def _build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("query")
    p.add_argument("--kind", default="video", choices=sorted(KINDS))
    p.add_argument("--order", default="relevance", choices=sorted(ORDERS))
    p.add_argument("--max-results", type=int, default=25)
    p.add_argument("--published-after", help="RFC3339, e.g. 2024-01-01T00:00:00Z")
    p.add_argument("--published-before", help="RFC3339, e.g. 2024-06-01T00:00:00Z")
    p.add_argument("--channel-id")
    p.add_argument("--region-code", help="ISO 3166-1 alpha-2, e.g. GB")
    p.add_argument("--video-duration", default="any", choices=sorted(DURATIONS))
    p.add_argument("--safe-search", default="moderate", choices=["moderate", "none", "strict"])
    p.add_argument("--with-stats", action="store_true", help="Enrich video results with view/like/comment counts")
    p.add_argument("--api-key", help="Overrides the YOUTUBE_API_KEY environment variable")
    p.add_argument("--json", metavar="PATH", help="Write results as JSON to this file instead of printing")
    return p


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _build_arg_parser().parse_args()
    try:
        results = search(
            args.query,
            kind=args.kind,
            order=args.order,
            max_results=args.max_results,
            published_after=args.published_after,
            published_before=args.published_before,
            channel_id=args.channel_id,
            region_code=args.region_code,
            video_duration=args.video_duration,
            safe_search=args.safe_search,
            with_stats=args.with_stats,
            api_key=args.api_key,
        )
    except YouTubeAPIError as exc:
        sys.exit(f"Search failed ({exc.reason or exc.status}): {exc}")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {len(results)} result(s) to {args.json}")
        return

    print(f"{len(results)} result(s):\n")
    for r in results:
        print(f"- {r['title']}")
        print(f"  {r['channel_title']} — {r['published_at']}")
        if r.get("view_count") is not None:
            print(f"  {r['view_count']:,} views · {r.get('like_count') or 0:,} likes")
        print(f"  {r['url']}")
        print()


if __name__ == "__main__":
    main()
