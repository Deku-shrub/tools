"""Query the Wayback Machine's CDX API for snapshot metadata — no browser
needed. This answers "what/when was captured" questions (list every
snapshot of a domain, every URL ever captured under a path, every capture
of one exact URL with its HTTP status) far more cheaply than driving
Firefox through explore.py; use explore.py once you actually want to look
at a specific page's rendered content.

CLI:
    # every snapshot under a domain (and its subdomains), one date range
    python cdx.py pcgroup.org.uk --match domain --from 1999 --to 2001

    # every URL ever captured under a path prefix
    python cdx.py "pcgroup.org.uk/newsletters/" --match prefix

    # every capture of one exact URL, with status codes
    python cdx.py "http://www.pcgroup.org.uk/threads/" --match exact

    # every captured URL under a domain whose path contains any of these
    # substrings (client-side filter — CDX has no substring search)
    python cdx.py pcgroup.org.uk --match domain --contains forum board ubb

    python cdx.py pcgroup.org.uk --match domain --json out.json

Library:
    from cdx import query, contains_any

    rows = query("pcgroup.org.uk", match="domain", date_from="19990101", date_to="20011231")
    forum_like = contains_any(rows, ["forum", "board", "ubb", "cgi-bin"])
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
DEFAULT_FIELDS = ("timestamp", "original", "statuscode", "mimetype", "length")


def query(url, match="exact", date_from=None, date_to=None, collapse=None, fields=DEFAULT_FIELDS, limit=None):
    """Query the CDX API. `match` is one of exact/prefix/host/domain (see
    module docstring). Returns a list of dicts, one per snapshot, keyed by
    `fields`. Raises urllib.error.HTTPError / URLError on request failure."""
    params = {
        "url": url,
        "matchType": match,
        "output": "json",
        "fl": ",".join(fields),
    }
    if collapse:
        params["collapse"] = collapse
    if date_from:
        params["from"] = date_from
    if date_to:
        params["to"] = date_to
    if limit:
        params["limit"] = str(limit)

    full_url = f"{CDX_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if not data:
        return []
    header, *rows = data
    return [dict(zip(header, row)) for row in rows]


def contains_any(rows, substrings, field="original"):
    """Client-side filter: rows whose `field` contains any of `substrings`
    (case-insensitive). CDX itself has no substring search."""
    needles = [s.lower() for s in substrings]
    return [r for r in rows if any(n in r.get(field, "").lower() for n in needles)]


def wayback_url(row):
    """Build the https://web.archive.org/web/... replay URL for a CDX row."""
    return f"https://web.archive.org/web/{row['timestamp']}/{row['original']}"


def _norm_date(d, end=False):
    if d is None:
        return None
    d = d.replace("-", "")
    if len(d) == 4:
        d += "1231" if end else "0101"
    return d


def _build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("url")
    p.add_argument("--match", default="exact", choices=["exact", "prefix", "host", "domain"])
    p.add_argument("--from", dest="date_from", help="YYYY, YYYY-MM-DD, or YYYYMMDD")
    p.add_argument("--to", dest="date_to", help="YYYY, YYYY-MM-DD, or YYYYMMDD")
    p.add_argument("--contains", nargs="+", help="Client-side substring filter on the URL")
    p.add_argument("--no-collapse", action="store_true", help="Show every capture, not just one per unique URL")
    p.add_argument("--limit", type=int)
    p.add_argument("--json", metavar="PATH", help="Write results as JSON to this file instead of printing")
    return p


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _build_arg_parser().parse_args()
    try:
        rows = query(
            args.url,
            match=args.match,
            date_from=_norm_date(args.date_from),
            date_to=_norm_date(args.date_to, end=True),
            collapse=None if args.no_collapse else "urlkey",
            limit=args.limit,
        )
    except urllib.error.URLError as exc:
        sys.exit(f"CDX request failed: {exc}")

    if args.contains:
        rows = contains_any(rows, args.contains)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        print(f"Wrote {len(rows)} row(s) to {args.json}")
        return

    print(f"{len(rows)} snapshot(s):\n")
    for r in rows:
        print(f"[{r['timestamp']}] {r.get('statuscode') or '-':>3} {r['original']}")


if __name__ == "__main__":
    main()
