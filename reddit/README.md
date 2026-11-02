# reddit

Search and browse Reddit from two sources, using stdlib `urllib` only (no
extra dependencies, no browser):

- **[archive.py](archive.py): the Arctic Shift archive.** Free, needs no key, and
  works today. It searches comment text, filters by exact date range, and
  covers full history. Start here.
- **The official OAuth API** (search.py, listing.py, thread.py) gives live
  scores and current state, but needs an approved API app (see Setup).
  Self-service app creation is closed under Reddit's Responsible Builder
  Policy, and personal-use approvals are reportedly rare.

## archive.py (Arctic Shift)

```powershell
python archive.py posts -r MandelaEffect --query moonraker
python archive.py posts -r MandelaEffect --title braces --after 2015-01-01 --before 2017-01-01
python archive.py comments -r MandelaEffect --body braces --match dolly --after 2016-01-01 --before 2018-01-01 --json research/dolly.json
python archive.py comments --author someuser --newest-first
python archive.py thread 3vzk53 --match braces
```

- Keyword search needs `-r` or `--author`. Site-wide keyword search isn't
  supported.
- Comment-text search is slow on the server, so the script walks the
  date range in 90-day windows, halves any window that times out, and
  waits out rate limits. Expect minutes for multi-year pulls (about 5½ minutes for two
  years of r/MandelaEffect). Windows that can't be fetched are reported
  as `gaps`.
- Scores are snapshots taken when the item was archived, so check
  `retrieved_on` and don't rank by them. Deleted or removed content often
  survives in the archive.

## Studies: bulk collection + structured extraction

For research across many queries, [collect.py](collect.py) downloads whole
subreddits from Arctic Shift. It does this because server-side keyword search is
unreliable (phrase/OR syntax returns non-matches) and gets throttled after a few
dozen queries. It then runs every query locally and writes a JSONL corpus
of the matching threads with structure intact. [extract.py](extract.py)
then has Claude fill a per-person schema, and code checks every evidence
quote against its source.

A study is a folder in `studies/` holding:
- `queries.json`: subreddits and query clusters. Each query is a list of
  whole-word terms that must all appear. Phrases are allowed, and a trailing `*` allows suffixes.
- `extraction.md`: the extraction instructions and pattern definitions.

```powershell
python collect.py studies/neck_tension             # download posts, fetch matching threads, write corpus.jsonl
python collect.py studies/neck_tension --stats     # hit counts per query (after editing queries.json)
python collect.py studies/neck_tension --no-download   # re-match and rebuild the corpus without downloading again
python collect.py studies/neck_tension --bulk-comments # also download every comment (about a day for 7.7M)
python extract.py studies/neck_tension --dry-run   # prompts + cost estimate
python extract.py studies/neck_tension --submit    # then --collect, then --summary
```

Usernames are replaced with a keyed hash (HMAC) at download time and never
stored. Corpus and cache files are git-ignored, because they contain other people's health
posts.

## Official API

The API is required rather than "nice to have". Reddit's network-security layer
blocks plain HTTP clients *and* Selenium-driven Firefox on the public
`.json` endpoints, even with valid logged-in cookies. The OAuth API is
the supported route.

## Setup

1. Logged in, go to https://www.reddit.com/prefs/apps → **create another
   app...** → type **script**, redirect URI `http://localhost:8080`.
   (If Reddit sends you to an API-access request form instead, new apps
   currently need approval under its Responsible Builder policy. File the
   request, describing non-commercial personal research.)
2. Set the client ID (under the app name) and secret (never commit them):
   - PowerShell (persistent): `setx REDDIT_CLIENT_ID "..."` and
     `setx REDDIT_CLIENT_SECRET "..."`, then open a new terminal
   - bash: `export REDDIT_CLIENT_ID=... REDDIT_CLIENT_SECRET=...`
3. Optional: `REDDIT_USERNAME` + `REDDIT_PASSWORD` to act as your account
   (needed only for private subs you're in; fails with 2FA). Without them
   it uses app-only auth, which covers all public content.

## Files

- [client.py](client.py): auth/token cache, rate limiting, pagination,
  record normalisation. See its docstring for limits.
- [search.py](search.py): post search, site-wide or per-subreddit, with
  Reddit's `title:`/`author:`/`site:`… operators, plus local date and
  keyword filters. Also finds subreddits and users (`--kind sr|user`).
- [listing.py](listing.py): browse `r/<sub>` feeds (hot/new/top/…) or
  `u/<user>` posts/comments history.
- [thread.py](thread.py): a post plus its *full* comment tree (expands
  "load more" and "continue this thread"), with `--match` for searching
  comment text.

## Usage

```powershell
python search.py "dolly braces" -r MandelaEffect --sort top --time all
python search.py 'title:moonraker (braces OR "steel teeth")' --sort new --after 2015-01-01 --json research/moonraker.json
python listing.py r/MandelaEffect --sort top --time year --max-results 200
python listing.py u/someuser --what comments --match moonraker
python thread.py https://www.reddit.com/r/MandelaEffect/comments/abc123/ --match braces
python thread.py abc123 --sort old --json research/thread_abc123.json
```

All scripts are importable as libraries (`search()`, `listing()`,
`get_thread()`). `research/` is git-ignored for saved output.

## Limits

- ~100 requests/minute; the client sleeps on the rate-limit headers.
- Every listing (search included) stops at ~1000 items. Split broad
  pulls by subreddit or `--time` window.
- API search covers post titles and bodies only, not comments. To search comments, pull
  candidate threads, then run `thread.py --match`.
- No true date-range search: `--after`/`--before` filter locally.
