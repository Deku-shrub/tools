"""Bulk-collect a study's subreddits from the Arctic Shift archive, run its
query set locally, and write a deduplicated JSONL corpus of matching
threads (post + every comment) with structure intact.

Why bulk + local matching instead of server-side keyword search (verified
against the live API, 2026-11):
  - The server's phrase/OR syntax is unreliable: `"neck tension" | "tight
    neck"` and the `OR` form both returned r/POTS posts containing neither
    phrase.
  - Keyword search is expensive server-side: after a few dozen queries the
    server refuses even single-word searches ("Timeout. Maybe slow down a
    bit"), so ~400 query × subreddit combinations is impractical.
  - Plain paging by subreddit + date is cheap (~0.6 s per 100 items), and
    once the data is local every query runs over every item instantly —
    re-tune queries freely without touching the server again.

A study is a folder holding queries.json (see studies/neck_tension/). Each
query is a list of terms that must ALL appear (case-insensitive substring;
multi-word terms are exact phrases) in a post's title+body or a comment's
body.

Pipeline — every step is resumable; interrupt and re-run at any time:

  1. download  All posts of each subreddit → cache/bulk/posts_<sub>.jsonl.gz
               (~600k posts for the neck_tension study ≈ 1-2 h). With
               --bulk-comments, all comments too → comments_<sub>.jsonl.gz
               (~7.7M ≈ a day; catches mentions in threads whose post
               didn't match). Run per subreddit with --subreddit if you like.
  2. match     Every query over every downloaded item.
  3. threads   Full archived comment tree of every thread containing a
               match → cache/threads/<post_id>.json
  4. corpus    corpus.jsonl: every post + comment of those threads. Items
               matching nothing are kept (query_ids = []) as thread context.

Record format (one JSON object per line):
    id, type (post|comment), parent_id (null for posts), thread_id,
    depth (0 = top-level comment; null for posts), subreddit, created_utc,
    permalink, title (post's own, or thread title for comments), body
    (full, untruncated), link_url, score, flair, author_hash, is_op,
    query_ids, thread_query_ids (union over the thread), links,
    promo_flags, retrieved_utc (when Arctic Shift archived it — scores are
    as of then), collected_utc, thread_incomplete

Usernames are never written to disk: they're replaced by author_hash =
HMAC-SHA256(secret, username) as soon as they're downloaded. Plain sha256
wouldn't protect anyone — Reddit usernames are public, so anyone could hash
a candidate name and look it up. The secret is generated once into
<study>/.hash_key (git-ignored); keep it for stable hashes across runs.

Usage:
    python collect.py studies/neck_tension                      # all steps
    python collect.py studies/neck_tension --subreddit TMJ      # one sub's download first
    python collect.py studies/neck_tension --bulk-comments --subreddit TMJ
    python collect.py studies/neck_tension --no-download        # re-match + corpus only (after editing queries)
    python collect.py studies/neck_tension --stats              # per-query hit counts, no fetching
"""

import argparse
import gzip
import hashlib
import hmac
import json
import re
import secrets
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from archive import EARLIEST, ArchiveError, api_get
from client import iso_utc
from thread import _Tree

PAGE_LIMIT = 100
POSTS_BY_ID_BATCH = 100
POST_FIELDS = ("id", "subreddit", "created_utc", "title", "selftext", "author", "score", "num_comments",
               "link_flair_text", "permalink", "url", "is_self", "over_18", "retrieved_on")
COMMENT_FIELDS = ("id", "subreddit", "created_utc", "body", "author", "score", "link_id", "parent_id",
                  "author_flair_text", "retrieved_on")

URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)
NEUTRAL_DOMAINS = (
    "reddit.com", "redd.it", "imgur.com", "wikipedia.org", "nih.gov", "doi.org",
    "nature.com", "thelancet.com", "bmj.com", "jamanetwork.com", "nejm.org",
    "sciencedirect.com", "springer.com", "wiley.com", "medrxiv.org", "biorxiv.org",
    "nhs.uk", "cdc.gov", "who.int", "mayoclinic.org", "clevelandclinic.org",
    "youtube.com", "youtu.be", "giphy.com",
)
PROMO_PATTERNS = {
    "prolotherapy": re.compile(r"\bprolo(therapy)?\b", re.I),
    "instability_clinic": re.compile(r"\b(cci|craniocervical instability|upper cervical)\b.{0,80}\b(clinic|doctor|specialist)\b", re.I),
    "dm_me": re.compile(r"\b(dm|pm|message) me\b", re.I),
    "discount_code": re.compile(r"\b(discount|promo|coupon|affiliate) code\b|\buse code\b", re.I),
    "my_clinic": re.compile(r"\b(my|our) (clinic|practice|program|protocol|course)\b", re.I),
}


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").replace("’", "'").lower())


def _term_pattern(term):
    """Whole-word match ("ldn" must not hit "couldn't"); a trailing * allows
    any suffix ("rash*" matches rash, rashes); spaces match any whitespace."""
    wildcard = term.endswith("*")
    body = r"\s+".join(re.escape(w) for w in term.rstrip("*").split())
    return re.compile(rf"(?<![a-z0-9]){body}" + ("" if wildcard else r"(?![a-z0-9])"))


class Study:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.config = json.loads((self.folder / "queries.json").read_text(encoding="utf-8"))
        self.cache = self.folder / "cache"
        for sub in ("bulk", "threads"):
            (self.cache / sub).mkdir(parents=True, exist_ok=True)
        self.queries = {}  # query_id -> [normalised terms]
        self._patterns = {}  # query_id -> [compiled term regexes]
        for cluster, spec in self.config["clusters"].items():
            for i, terms in enumerate(spec["queries"], 1):
                qid = f"{cluster}_{i:02d}"
                self.queries[qid] = [_norm(t) for t in terms]
                self._patterns[qid] = [_term_pattern(t) for t in self.queries[qid]]
        key_path = self.folder / ".hash_key"
        if not key_path.exists():
            key_path.write_text(secrets.token_hex(32), encoding="utf-8")
        self.hash_key = bytes.fromhex(key_path.read_text(encoding="utf-8").strip())

    def matching_queries(self, text):
        haystack = _norm(text)
        return [qid for qid, patterns in self._patterns.items() if all(p.search(haystack) for p in patterns)]

    def author_hash(self, author):
        if not author or author in ("[deleted]", "[removed]"):
            return None
        return hmac.new(self.hash_key, author.lower().encode(), hashlib.sha256).hexdigest()

    def slim(self, d, fields):
        """Keep only `fields`, swapping author for author_hash."""
        out = {k: d.get(k) for k in fields if k != "author"}
        out["author_hash"] = self.author_hash(d.get("author"))
        return out


# ---------------------------------------------------------------- download

def _bulk_path(study, kind, subreddit):
    return study.cache / "bulk" / f"{kind}_{subreddit}.jsonl.gz"


def _state_path(study, kind, subreddit):
    return study.cache / "bulk" / f"{kind}_{subreddit}.state.json"


def download(study, kind, subreddit):
    """Page through every `kind` (posts|comments) of a subreddit, oldest
    first, appending to a gzipped JSONL. Resumes from the saved cursor."""
    state_path = _state_path(study, kind, subreddit)
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "after": int(EARLIEST), "edge_ids": [], "count": 0, "done": False}
    if state["done"]:
        # Pick up anything posted since the last complete run.
        state["done"] = False
    fields = POST_FIELDS if kind == "posts" else COMMENT_FIELDS
    page_n = 0
    with gzip.open(_bulk_path(study, kind, subreddit), "at", encoding="utf-8") as out:
        while True:
            params = {"subreddit": subreddit, "after": state["after"], "limit": PAGE_LIMIT, "sort": "asc"}
            try:
                page = api_get(f"/api/{kind}/search", params)
            except ArchiveError as exc:
                _log(f"  r/{subreddit} {kind}: {exc} - pausing 60s")
                time.sleep(60)
                continue
            edge_ids = set(state["edge_ids"])
            fresh = [d for d in page if d["id"] not in edge_ids]
            for d in fresh:
                out.write(json.dumps(study.slim(d, fields), ensure_ascii=False) + "\n")
            out.flush()
            state["count"] += len(fresh)
            if len(page) < PAGE_LIMIT:
                state["done"] = True
            else:
                last_ts = page[-1]["created_utc"]
                if not fresh:
                    # A whole page sharing one already-seen timestamp: step past it.
                    state["after"], state["edge_ids"] = last_ts + 1, []
                else:
                    # `after` is inclusive-ish; remember the IDs at the edge
                    # second so the overlap isn't written twice.
                    state["after"] = last_ts
                    state["edge_ids"] = [d["id"] for d in page if d["created_utc"] == last_ts]
            state_path.write_text(json.dumps(state))
            page_n += 1
            if page_n % 50 == 0 or state["done"]:
                _log(f"  r/{subreddit} {kind}: {state['count']:,} so far, up to "
                     f"{iso_utc(state['after'])[:10]}" + (" - done" if state["done"] else ""))
            if state["done"]:
                return state["count"]


def iter_bulk(study, kind, subreddit):
    """Yield each downloaded item once (a resumed download can overlap)."""
    path = _bulk_path(study, kind, subreddit)
    if not path.exists():
        return
    seen = set()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn last line from an interrupted write
            if d["id"] not in seen:
                seen.add(d["id"])
                yield d


# ------------------------------------------------------------------- match

def match_bulk(study, subreddits):
    """Returns ({thread_id: subreddit}, Counter(query_id -> items), Counter(query_id -> threads))."""
    threads = {}
    item_hits, thread_hits = Counter(), defaultdict(set)
    for subreddit in subreddits:
        for d in iter_bulk(study, "posts", subreddit):
            for qid in study.matching_queries(f"{d.get('title') or ''} {d.get('selftext') or ''}"):
                threads[d["id"]] = subreddit
                item_hits[qid] += 1
                thread_hits[qid].add(d["id"])
        for d in iter_bulk(study, "comments", subreddit):
            qids = study.matching_queries(d.get("body"))
            if qids:
                thread_id = (d.get("link_id") or "").removeprefix("t3_")
                if thread_id:
                    threads[thread_id] = subreddit
                for qid in qids:
                    item_hits[qid] += 1
                    thread_hits[qid].add(thread_id)
    return threads, item_hits, Counter({q: len(t) for q, t in thread_hits.items()})


# ----------------------------------------------------------------- threads

def fetch_threads(study, thread_ids):
    todo = sorted(t for t in thread_ids if not (study.cache / "threads" / f"{t}.json").exists())
    _log(f"Threads: {len(thread_ids):,} matching, {len(todo):,} to fetch")
    for start in range(0, len(todo), POSTS_BY_ID_BATCH):
        batch = todo[start:start + POSTS_BY_ID_BATCH]
        try:
            posts = {p["id"]: p for p in api_get("/api/posts/ids", {"ids": ",".join(batch)})}
        except ArchiveError as exc:
            _log(f"  post batch failed, will retry next run: {exc}")
            continue
        for i, post_id in enumerate(batch, start + 1):
            if post_id not in posts:
                continue
            try:
                things = api_get("/api/comments/tree", {"link_id": f"t3_{post_id}", "limit": 9999})
            except ArchiveError as exc:
                _log(f"  {post_id} comments failed, will retry next run: {exc}")
                continue
            tree = _Tree()
            tree.add(things)
            comments = []
            for c in tree.ordered(f"t3_{post_id}"):
                slim = study.slim(tree.comments[f"t1_{c['id']}"], COMMENT_FIELDS)
                slim["depth"] = c["depth"]
                comments.append(slim)
            record = {"post": study.slim(posts[post_id], POST_FIELDS), "comments": comments,
                      "incomplete": bool(tree.pending_ids or tree.pending_continues)}
            (study.cache / "threads" / f"{post_id}.json").write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8")
            if i % 100 == 0 or i == len(todo):
                _log(f"  [{i:,}/{len(todo):,}] threads fetched")


# ------------------------------------------------------------------ corpus

def _links_and_flags(text):
    links = URL_RE.findall(text or "")
    flags = {name for name, pattern in PROMO_PATTERNS.items() if pattern.search(text or "")}
    for link in links:
        host = re.sub(r"^https?://(www\.)?", "", link.lower()).split("/")[0]
        if not host.endswith(NEUTRAL_DOMAINS):
            flags.add(f"external_link:{host}")
    return links, sorted(flags)


def build_corpus(study, thread_ids):
    collected = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    n_items = n_matched = n_threads = 0
    with (study.folder / "corpus.jsonl").open("w", encoding="utf-8") as out:
        for thread_id in sorted(thread_ids):
            cache_path = study.cache / "threads" / f"{thread_id}.json"
            if not cache_path.exists():
                continue
            thread = json.loads(cache_path.read_text(encoding="utf-8"))
            post = thread["post"]
            op_hash = post.get("author_hash")
            body = post.get("selftext") or ""
            link_url = None if post.get("is_self") else post.get("url")
            links, flags = _links_and_flags(f"{body} {link_url or ''}")
            records = [{
                "id": post["id"], "type": "post", "parent_id": None, "thread_id": post["id"], "depth": None,
                "subreddit": post.get("subreddit"), "created_utc": iso_utc(post.get("created_utc")),
                "permalink": f"https://www.reddit.com{post['permalink']}" if post.get("permalink") else None,
                "title": post.get("title"), "body": body, "link_url": link_url,
                "score": post.get("score"), "flair": post.get("link_flair_text"),
                "author_hash": op_hash, "is_op": True,
                "query_ids": study.matching_queries(f"{post.get('title') or ''} {body}"),
                "links": links, "promo_flags": flags,
                "retrieved_utc": iso_utc(post.get("retrieved_on")), "collected_utc": collected,
            }]
            for c in thread["comments"]:
                links, flags = _links_and_flags(c.get("body"))
                records.append({
                    "id": c["id"], "type": "comment",
                    "parent_id": (c.get("parent_id") or "").split("_", 1)[-1] or None,
                    "thread_id": post["id"], "depth": c.get("depth"),
                    "subreddit": c.get("subreddit"), "created_utc": iso_utc(c.get("created_utc")),
                    "permalink": f"https://www.reddit.com/r/{c.get('subreddit')}/comments/{post['id']}/_/{c['id']}/",
                    "title": post.get("title"), "body": c.get("body"), "link_url": None,
                    "score": c.get("score"), "flair": c.get("author_flair_text"),
                    "author_hash": c.get("author_hash"),
                    "is_op": bool(op_hash) and c.get("author_hash") == op_hash,
                    "query_ids": study.matching_queries(c.get("body")),
                    "links": links, "promo_flags": flags,
                    "retrieved_utc": iso_utc(c.get("retrieved_on")), "collected_utc": collected,
                })
            thread_qids = sorted({q for r in records for q in r["query_ids"]})
            if not thread_qids:
                continue
            n_threads += 1
            for r in records:
                r["thread_query_ids"] = thread_qids
                r["thread_incomplete"] = thread.get("incomplete", False)
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
                n_items += 1
                n_matched += bool(r["query_ids"])
    return n_items, n_matched, n_threads


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("study", help="Study folder containing queries.json")
    p.add_argument("--subreddit", action="append", help="Limit download to these subreddits (repeatable)")
    p.add_argument("--bulk-comments", action="store_true", help="Also download every comment (slow, big)")
    p.add_argument("--no-download", action="store_true", help="Skip downloading; match + threads + corpus only")
    p.add_argument("--no-threads", action="store_true", help="Skip fetching thread trees")
    p.add_argument("--stats", action="store_true", help="Print per-query hit counts and stop")
    args = p.parse_args()

    study = Study(args.study)
    all_subs = study.config["subreddits"]
    unknown = set(args.subreddit or []) - set(all_subs)
    if unknown:
        sys.exit(f"Not in queries.json subreddits: {sorted(unknown)}")

    if not (args.no_download or args.stats):
        for subreddit in args.subreddit or all_subs:
            _log(f"Downloading r/{subreddit} posts")
            download(study, "posts", subreddit)
            if args.bulk_comments:
                _log(f"Downloading r/{subreddit} comments")
                download(study, "comments", subreddit)

    # Matching always covers everything downloaded so far, so a narrowed
    # run never shrinks the corpus.
    threads, item_hits, thread_hits = match_bulk(study, all_subs)
    print(f"{len(threads):,} matching thread(s)")
    if args.stats:
        for qid, terms in study.queries.items():
            print(f"  {qid:22} {item_hits[qid]:6,} items {thread_hits[qid]:6,} threads  {terms}")
        return
    if not args.no_threads:
        fetch_threads(study, set(threads))
    n_items, n_matched, n_threads = build_corpus(study, set(threads))
    print(f"corpus.jsonl: {n_items:,} items ({n_matched:,} directly matching a query) across {n_threads:,} threads")


if __name__ == "__main__":
    main()
