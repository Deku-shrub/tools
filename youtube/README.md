# youtube

Search YouTube and fetch video comments via the official **YouTube Data API
v3** (JSON REST, stdlib `urllib` only — no extra dependencies, no browser).

## Setup

1. Create/select a project at https://console.cloud.google.com/
2. **APIs & Services → Library** → enable "YouTube Data API v3"
3. **APIs & Services → Credentials** → Create Credentials → API key
4. Set it as an environment variable (never commit it):
   - PowerShell: `$env:YOUTUBE_API_KEY = "your-key"`
   - bash: `export YOUTUBE_API_KEY="your-key"`

No OAuth consent screen is needed — search and public comments only require
a plain API key.

## Files

- [client.py](client.py) — low-level API wrapper (auth, pagination, error
  handling, video-ID extraction). See its docstring for quota costs.
- [search.py](search.py) — search videos/channels/playlists (`search.list`),
  optionally enriched with view/like/comment counts.
- [comments.py](comments.py) — fetch a video's comments and replies
  (`commentThreads.list` + `comments.list`).

## Usage

```powershell
python search.py "moonraker trailer" --max-results 10 --with-stats
python comments.py "https://youtu.be/dQw4w9WgXcQ" --max-results 200 --order time --json out.json
```

Both scripts are also importable as libraries — see the `search()` /
`get_comments()` functions and each module's docstring for details.

## Quota

The free tier is 10,000 units/day. `search.list` costs 100 units/call
(~100 searches/day); comment and video-detail calls cost 1 unit each
(thousands/day). A `quotaExceeded` error resets at midnight Pacific time.
