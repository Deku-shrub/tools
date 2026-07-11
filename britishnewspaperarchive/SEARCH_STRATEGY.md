# BNA Search Strategy — Moonraker / Dolly / Braces

Goal: find contemporary (1979-era, and later broadcast-reaction) British/Irish press
that either supports or contradicts the "Dolly wore braces" claim, and — more
valuably — surfaces sources nobody has checked yet: the syndicated novelisation
run, the Royal Premiere coverage, and the December 1982 ITV premiere.

Written against the Advanced Search form fields at
`https://britishnewspaperarchive.co.uk/search/advanced` (`FreeSearch`,
`SomeSearch`, `PhraseSearch`, `NotSearch`, `Place`, `NewspaperTitle`,
`DateFrom*/DateTo*`, `ContentType[]`, `PublicTag`, `SortOrder`).

## Field cheat-sheet

| Field | Behaviour | When to use |
|---|---|---|
| `FreeSearch` | AND of all words | Broad first pass |
| `SomeSearch` | OR of words | Casting a wide net for variant wording |
| `PhraseSearch` | Exact phrase | Once you have a specific quote/caption to chase |
| `NotSearch` | Excludes | Strip modern retrospective noise once you know the pattern |
| `ExactSearch` checkbox | Disables stemming (won't match "fishing" for "fish") | Turn on when a word keeps pulling irrelevant plurals/tenses |
| `ContentType[]` | Advertisement / Article / Illustrated / News / etc. | `Illustrated` is the fastest way to find pages with a photo of Dolly to eyeball |
| `SortOrder` | `dayEarly` sorts oldest first | Use for "what's the earliest print mention" questions — this is how the 2000 Usenet thread was contextualised, do the same for print |
| `PublicTag` | BNA staff-curated tags | Worth a blind try: `Moonraker`, `James Bond` — low cost, occasionally surfaces curated clippings you won't find by keyword |

OCR quality on 1979 regional papers is inconsistent — plan on re-running any
zero-result search with `ExactSearch` off, and with the alternate period
phrasing below.

### Engine quirk: only `FreeSearch` is actually required

Discovered while running Phase 1 Q1 (2026-07-11): despite the name,
`PhraseSearch` is **not** AND'ed against other fields. Reverse-engineered
from the site's composed query string (`basicsearch=`): `FreeSearch` terms
alone get a `+` (required) prefix; `PhraseSearch` and `SomeSearch` terms are
always unprefixed, which in the underlying Lucene-style query means
"optional, boosts relevance if present" — not "must match". A query built
from `PhraseSearch: Moonraker` + `SomeSearch: Dolly braces teeth` (no
`FreeSearch`) returned 9 results, none of which mentioned Moonraker
anywhere on the page — they matched purely on "braces"/"teeth" from an
unrelated Poly Styrene wire story.

**Consequence for every phase below:** wherever a query pairs a
"must-have" `PhraseSearch` term with a `SomeSearch` OR-group (Phase 1 Q1/Q2,
Phase 3 Q1, Phase 4 Q1), put the must-have term in `FreeSearch` instead —
`search.py`'s `search(free=..., some=...)` does this correctly. Reserve
`PhraseSearch` for standalone exact-phrase searches (Q3, Q4, Phase 2, Phase
6) where nothing else needs to be required alongside it. Always still open
the actual page image to confirm relevance — `SomeSearch`/`PhraseSearch`
terms only rank, they don't filter, so an OR-group term can dominate a
result even when `FreeSearch` is present.

(Also: the form carries field values over from your previous search
server-side ("Keep filters"). `search.py`'s `fill_and_submit()` now clicks
"Clear" before every fill, so this isn't a concern when using the tool —
but it means clicking around the form manually between searches without
clearing can silently mix queries.)

**Update:** `FreeSearch` + `SomeSearch` together are *also* unreliable, just
differently broken. Isolated by testing `free="Moonraker"` against
`some=` values of increasing length over the same date range: `"braces"`
alone → 12 (page-1 baseline, no filtering), `"teeth"` alone → 12,
`"braces teeth"` → 7, `"Dolly braces"` → 1, `"Dolly braces teeth"` → 0.
A true OR can only ever match *more* documents as you add words —
count going down as words are added means `SomeSearch`'s words are
effectively AND'ed together once a required `FreeSearch` clause is also
present (the opposite of standalone `SomeSearch` behaviour, which — per
the very first Phase 1 attempt — genuinely is OR-like when it's the only
populated field). In short: don't trust multi-word `SomeSearch` for
anything except a single word, regardless of what else is populated.

**Adopted methodology going forward:** fetch the `FreeSearch`-required
result set alone (reliable), then rank/filter locally against the OR-group
keywords using `search.keyword_matches()` — full control, no dependence on
the engine's undocumented combinator behaviour. This is slower (more
pages fetched) but the only approach verified to not silently drop
relevant hits.

## Phase 1 — Direct hits (do these first, ~10 min)

Straight shots at the claim itself.

1. `PhraseSearch: Moonraker` + `SomeSearch: Dolly braces teeth` — `DateFrom` 1/6/1979 → `DateTo` 31/12/1979
2. `PhraseSearch: Dolly` + `SomeSearch: braces overbite orthodontic dental` — same date range
3. `PhraseSearch: no braces` and separately `PhraseSearch: without braces` — same range (the anti-braces control; Canby-style asides could exist in a UK paper too)
4. `PhraseSearch: Blanche Ravalec` — **no date restriction**. She's a minor credit, so this may only surface via a premiere party photo caption, a career-retrospective piece, or an obituary-adjacent "where are they now." Any hit is worth reading in full regardless of date.

Run all four with `ContentType: Illustrated` checked as a second pass — a
captioned photo is stronger evidence than prose either way.

## Phase 2 — The syndication trace (highest-value, not yet tried)

The Leicester Chronicle piece (06/07/1979) is explicitly a **syndicated**
novelisation extract — "adapted from Christopher Wood's screenplay... distributed
by United Artists." Syndicated wire features often ran in dozens of regional
papers with locally-edited captions or introductions. If any editor's version
described Dolly differently, that's a second, independent contemporary source —
and it directly tests the UA-prints theory, since UA is the named distributor
of the syndication too.

1. `PhraseSearch: adapted from Christopher Wood` — no newspaper filter, `DateFrom` 1/6/1979 → `DateTo` 30/9/1979. This should enumerate every paper that ran the serialisation.
2. `PhraseSearch: a petite blonde` — the exact Dolly description already found in Leicester Chronicle. If other papers ran the same syndicated copy verbatim, this catches them; if one paper's version differs, that's the interesting case.
3. For each title Phase 2.1 turns up, also pull their instalment immediately before and after the Dolly-mentioning one — syndicated serials sometimes split character introductions across parts, so a "braces" clause could sit in an adjacent instalment rather than the one already found.
4. Specifically target the missing **Part One**: `NewspaperTitle: Leicester Chronicle`, `DateFrom` 22/6/1979 → `DateTo` 29/6/1979, `FreeSearch: Moonraker`. (Leicester Chronicle's BNA run ends in 1979, so this is close to the edge of its coverage — don't assume a later re-check will still find it if BNA's OCR indexing has gaps near the title's end date.)

## Phase 3 — Royal Premiere coverage (26/06/1979)

Untried angle. Local/regional papers covering a Leicester Square premiere often
ran cast photos with captions describing each actor visually — this is exactly
the kind of throwaway caption that could confirm or kill the braces claim
independent of any review.

1. `PhraseSearch: Royal Premiere` + `SomeSearch: Moonraker Leicester Square` — `DateFrom` 25/6/1979 → `DateTo` 3/7/1979, `ContentType: Illustrated`
2. `SomeSearch: Moonraker Roger Moore premiere` — same range, no `ContentType` filter, to catch text-only coverage too
3. Check `Place: London, London, England` as a filter if the above is noisy — but be aware nationals aren't well represented in BNA for 1979 (see title list below), so don't over-filter by place and miss regional papers whose reporter attended.

## Phase 4 — Review sweep, wider net

Standard contemporary-review search, cast wide because you don't yet know
which regional critic (if any) echoed Lichtenwalter's "braces galore" line
independently.

1. `PhraseSearch: Moonraker` + `SomeSearch: review Bond film` — `DateFrom` 28/6/1979 → `DateTo` 30/9/1979, `SortOrder: dayEarly`
2. Repeat narrowed to specific titles known to run film criticism and to cover 1979 in BNA's index (see shortlist below), one at a time if the all-titles pass is too noisy.
3. Try period-appropriate slang alongside clinical terms: `braces`, `brace`, `train tracks`, `metal teeth`, `steel teeth`, `tin teeth` — 1979 British tabloid prose is more likely to use a slangy aside than the clinical "orthodontic."

**Newspaper Title shortlist** (nationals/regionals confirmed in BNA's title
list to run daily/weekly through 1979, worth targeting individually if the
all-titles search is too noisy): Daily Express, Daily Mirror, Daily Star,
Sunday Express, Sunday Mirror, The Scotsman, Aberdeen Press and Journal,
Belfast Telegraph, Birmingham Daily Post, Coventry Evening Telegraph, Dundee
Courier, Hull Daily Mail, Liverpool Echo, Manchester Evening News, Newcastle
Journal, Nottingham Evening Post, Yorkshire Post, Irish Independent, Evening
Herald (Dublin).

Note: **TV Times stops at 1980** in BNA's index, so it cannot help with the
December 1982 ITV premiere (Phase 5) — don't waste a search on it for that.

## Phase 5 — ITV premiere reaction (27/12/1982) and later broadcasts

Your document flags this as "ground zero" for the 1982–1986 UK viewer
testimony. BNA won't have the broadcast itself, but it may have viewer
letters, listings previews, or "what's on tonight" write-ups that describe the
film — occasionally these recap plot/characters in enough detail to mention a
visual gag.

1. `FreeSearch: Moonraker` — `DateFrom` 20/12/1982 → `DateTo` 3/1/1983, `SortOrder: dayEarly` — scan listings pages and any Boxing Day/post-Christmas TV review columns.
2. Repeat for the other Kaleidoscope-confirmed broadcast dates already in your grid: 25/12/1985 and 13/02/1988, ±5 days each, same approach — a viewer letters page mentioning the film weeks later is plausible for a Christmas broadcast.
3. `PhraseSearch: Moonraker` + `NotSearch: cinema film premiere 1979` — general date range 1980–1999 — to surface any reader-nostalgia or "do you remember" pieces that discuss the film independent of a specific broadcast, which is where a Mandela-Effect-style claim might first leak into print before 2000.

## Phase 6 — Earliest-mention sweep

Once Phases 1–5 turn up any hit using the word "braces" in connection with
Moonraker/Dolly, re-run that exact phrase with **no date restriction** and
`SortOrder: dayEarly`. The goal is a chronological ladder of every print
mention, the print analogue of what you already did for the 2000 Usenet
thread — if the earliest print use pre-dates the 2000 thread, that's a
significant new anchor point.

## Verification notes

- Always open the actual clipping/page image, not just the OCR snippet — BNA's
  OCR on 1979 halftone reproductions is unreliable around small captions, and
  "braces" can be an OCR misread of unrelated text.
- Record `clippingId` and full newspaper/date/page for anything relevant,
  matching the citation style already used in the briefing document
  (newspapers.com-style clipping URLs), so it can be cross-referenced later.
- For the bot: this site has previously served a Cloudflare interactive
  challenge to non-browser clients (see `login.py`'s handling of this) —
  keep automated search requests to a human-plausible pace and stay within
  your own logged-in session's normal usage rather than bulk-scraping, both
  to avoid re-triggering Cloudflare and to stay within BNA's terms for a
  personal subscriber account.
