"""Second pass: per-person structured extraction from a study's corpus.jsonl
with Claude, every field backed by a verbatim evidence quote.

For each distinct author_hash that wrote at least one query-matching item,
the model gets all of that person's items in the matching threads (plus, for
replies, the parent item as CONTEXT) and fills a fixed JSON schema:
facts (onset, diagnoses, surgery, tension location, triggers, linked
symptoms, interventions + outcomes, confounders — each with claim_type and
provenance) and a score against the study's pattern. The instructions and
pattern definitions live in <study>/extraction.md — edit them there.

Every evidence quote is then checked in code against the text of the item
it cites (whitespace-normalised exact substring). Facts whose quote doesn't
verify are kept but marked quote_verified: false, and the summary counts
only verified ones, so nothing the model invented gets counted.

Runs through the Message Batches API (50% price; most batches finish
within an hour, max 24 h):

    python extract.py studies/neck_tension --dry-run     # build prompts, estimate cost, no API calls
    python extract.py studies/neck_tension --submit      # create the batch (id saved to the study)
    python extract.py studies/neck_tension --collect     # poll until done, write extracted.jsonl
    python extract.py studies/neck_tension --summary     # distinct-people counts from extracted.jsonl

Auth: ANTHROPIC_API_KEY (or an `ant auth login` profile). Note the corpus —
other people's public posts, usernames already replaced by hashes — is sent
to the Anthropic API.

Selection-bias reminder: counts are of distinct people who posted, who skew
toward the most affected. Treat any pattern recurrence as a hypothesis.
"""

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TOKENS = 16000
BATCH_MAX_REQUESTS = 100_000
# Batch pricing for claude-opus-5-5 (half of $4 / $20 per MTok), for --dry-run only.
INPUT_USD_PER_MTOK = 2.0
OUTPUT_USD_PER_MTOK = 10.0
EST_OUTPUT_TOKENS = 2500

FACT_FIELDS = [
    "onset_timing", "illness_trigger", "diagnosis", "surgery_history",
    "tension_location", "trigger_type", "pain_or_tension",
    "morning_surge", "heat_intolerance", "rash", "glucose_dips", "sleep",
    "intervention", "medication_confounder",
]
PATTERN_ELEMENTS = ["load_dependent", "heat_relieved", "ring_distribution", "morning_surge", "glucose_dips_separate"]

_QUOTED = {
    "evidence_quote": {"type": "string"},
    "source_id": {"type": "string"},
}
SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": FACT_FIELDS},
                    "value": {"type": "string"},
                    "outcome": {"type": ["string", "null"], "enum": ["helped", "no_effect", "worse", "mixed", "unclear", None]},
                    "provenance": {"type": ["string", "null"], "enum": ["clinician", "self_reported", "unclear", None]},
                    "claim_type": {"type": "string",
                                   "enum": ["experience", "clinician_statement", "speculation", "question", "secondhand"]},
                    **_QUOTED,
                },
                "required": ["field", "value", "outcome", "provenance", "claim_type", "evidence_quote", "source_id"],
                "additionalProperties": False,
            },
        },
        "pattern": {
            "type": "object",
            "properties": {
                element: {
                    "type": "object",
                    "properties": {
                        "verdict": {"type": "string", "enum": ["yes", "no", "mixed", "not_mentioned"]},
                        "evidence_quote": {"type": ["string", "null"]},
                        "source_id": {"type": ["string", "null"]},
                    },
                    "required": ["verdict", "evidence_quote", "source_id"],
                    "additionalProperties": False,
                }
                for element in PATTERN_ELEMENTS
            },
            "required": PATTERN_ELEMENTS,
            "additionalProperties": False,
        },
        "flags": {
            "type": "object",
            "properties": {
                "promotional": {"type": "boolean"},
                "possibly_ai_written": {"type": "boolean"},
                "note": {"type": ["string", "null"]},
            },
            "required": ["promotional", "possibly_ai_written", "note"],
            "additionalProperties": False,
        },
    },
    "required": ["relevant", "facts", "pattern", "flags"],
    "additionalProperties": False,
}


def _ws(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def _item_text(r):
    return f"{r['title']}\n\n{r['body'] or ''}" if r["type"] == "post" else (r["body"] or "")


def load_people(study):
    """{author_hash: {"items": [...own items...], "context": {id: item}}} for
    everyone who wrote at least one query-matching item."""
    by_id, rows = {}, []
    with (study / "corpus.jsonl").open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            by_id[r["id"]] = r
            rows.append(r)
    matched_threads = defaultdict(set)  # author -> threads where they matched
    for r in rows:
        if r["author_hash"] and r["query_ids"]:
            matched_threads[r["author_hash"]].add(r["thread_id"])
    people = {}
    for r in rows:
        author = r["author_hash"]
        if author in matched_threads and r["thread_id"] in matched_threads[author]:
            person = people.setdefault(author, {"items": [], "context": {}})
            person["items"].append(r)
            parent = by_id.get(r["parent_id"]) if r["parent_id"] else None
            if parent and parent["author_hash"] != author:
                person["context"][parent["id"]] = parent
            if r["type"] == "comment" and r["thread_id"] in by_id and by_id[r["thread_id"]]["author_hash"] != author:
                person["context"][r["thread_id"]] = by_id[r["thread_id"]]
    return people


def render_person(person):
    parts = []
    for r in sorted(person["context"].values(), key=lambda r: r["created_utc"] or ""):
        parts.append(f'<item id="{r["id"]}" role="CONTEXT" type="{r["type"]}" subreddit="{r["subreddit"]}">\n'
                     f"{_item_text(r)}\n</item>")
    for r in sorted(person["items"], key=lambda r: r["created_utc"] or ""):
        parts.append(f'<item id="{r["id"]}" role="PERSON" type="{r["type"]}" subreddit="{r["subreddit"]}" '
                     f'created="{r["created_utc"]}" replying_to="{r["parent_id"] or ""}">\n{_item_text(r)}\n</item>')
    return "\n\n".join(parts)


def build_requests(study, people):
    system = (study / "extraction.md").read_text(encoding="utf-8")
    requests = []
    for author, person in sorted(people.items()):
        requests.append(Request(
            custom_id=author[:64],
            params=MessageCreateParamsNonStreaming(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": render_person(person)}],
            ),
        ))
    return requests


def verify(extraction, person):
    """Mark each quote verified iff it's an exact (whitespace-normalised)
    substring of the PERSON item it cites."""
    own = {r["id"]: _ws(_item_text(r)) for r in person["items"]}

    def check(quote, source_id):
        return bool(quote) and source_id in own and _ws(quote) in own[source_id]

    for fact in extraction["facts"]:
        fact["quote_verified"] = check(fact["evidence_quote"], fact["source_id"])
    for element in extraction["pattern"].values():
        element["quote_verified"] = (element["verdict"] == "not_mentioned"
                                     or check(element["evidence_quote"], element["source_id"]))
    return extraction


def _state_path(study):
    return study / "extract_batch.json"


def dry_run(study, people, requests):
    chars = sum(len(r["params"]["messages"][0]["content"]) for r in requests)
    system_chars = len((study / "extraction.md").read_text(encoding="utf-8"))
    in_tokens = chars / 4 + len(requests) * 3000  # + system prompt and schema per request
    out_tokens = len(requests) * EST_OUTPUT_TOKENS
    cost = in_tokens / 1e6 * INPUT_USD_PER_MTOK + out_tokens / 1e6 * OUTPUT_USD_PER_MTOK
    sizes = sorted(len(p["items"]) for p in people.values())
    print(f"{len(requests):,} people (requests); items per person: median {sizes[len(sizes)//2]}, max {sizes[-1]}")
    print(f"~{in_tokens/1e6:.1f}M input + ~{out_tokens/1e6:.1f}M output tokens "
          f"≈ ${cost:,.0f} on {MODEL} batch pricing (rough: chars/4, {EST_OUTPUT_TOKENS} output tokens/person)")
    sample = study / "extract_sample_prompt.txt"
    sample.write_text(requests[0]["params"]["messages"][0]["content"], encoding="utf-8")
    print(f"Sample prompt written to {sample} (system prompt: {system_chars:,} chars from extraction.md)")


def submit(study, requests):
    if _state_path(study).exists():
        sys.exit(f"A batch is already recorded in {_state_path(study)} — --collect it, or delete the file to resubmit.")
    client = anthropic.Anthropic()
    batch_ids = []
    for start in range(0, len(requests), BATCH_MAX_REQUESTS):
        batch = client.messages.batches.create(requests=requests[start:start + BATCH_MAX_REQUESTS])
        batch_ids.append(batch.id)
        print(f"Created batch {batch.id} ({len(requests[start:start + BATCH_MAX_REQUESTS]):,} requests)")
    _state_path(study).write_text(json.dumps({"batch_ids": batch_ids, "model": MODEL}), encoding="utf-8")


def collect(study, people):
    state = json.loads(_state_path(study).read_text(encoding="utf-8"))
    client = anthropic.Anthropic()
    by_custom_id = {author[:64]: (author, person) for author, person in people.items()}
    out_path = study / "extracted.jsonl"
    failed = Counter()
    with out_path.open("w", encoding="utf-8") as out:
        for batch_id in state["batch_ids"]:
            while (batch := client.messages.batches.retrieve(batch_id)).processing_status != "ended":
                print(f"{batch_id}: {batch.processing_status}, {batch.request_counts.processing:,} processing...",
                      file=sys.stderr)
                time.sleep(60)
            for result in client.messages.batches.results(batch_id):
                if result.custom_id not in by_custom_id:
                    continue
                author, person = by_custom_id[result.custom_id]
                if result.result.type != "succeeded":
                    failed[result.result.type] += 1
                    continue
                msg = result.result.message
                if msg.stop_reason != "end_turn":
                    failed[f"stop_{msg.stop_reason}"] += 1
                    continue
                text = next((b.text for b in msg.content if b.type == "text"), "")
                try:
                    extraction = json.loads(text)
                except json.JSONDecodeError:
                    failed["bad_json"] += 1
                    continue
                record = {
                    "author_hash": author,
                    "subreddits": sorted({r["subreddit"] for r in person["items"]}),
                    "item_ids": [r["id"] for r in person["items"]],
                    "query_ids": sorted({q for r in person["items"] for q in r["query_ids"]}),
                    "model": msg.model,
                    **verify(extraction, person),
                }
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Wrote {out_path}" + (f"; failed: {dict(failed)} (re-run those by resubmitting)" if failed else ""))


def summary(study):
    rows = [json.loads(line) for line in (study / "extracted.jsonl").open(encoding="utf-8")]
    relevant = [r for r in rows if r["relevant"] and not r["flags"]["promotional"]]
    print(f"{len(rows):,} people extracted; {len(relevant):,} relevant and non-promotional "
          f"({sum(r['flags']['promotional'] for r in rows)} promotional, "
          f"{sum(r['flags']['possibly_ai_written'] for r in rows)} possibly AI-written)\n")
    print("Pattern elements (distinct people, verified quotes only):")
    for element in PATTERN_ELEMENTS:
        verdicts = Counter(r["pattern"][element]["verdict"] for r in relevant if r["pattern"][element]["quote_verified"])
        print(f"  {element:24} yes {verdicts['yes']:5}  mixed {verdicts['mixed']:5}  no {verdicts['no']:5}")
    full = Counter(sum(r["pattern"][e]["verdict"] == "yes" and r["pattern"][e]["quote_verified"] for e in PATTERN_ELEMENTS)
                   for r in relevant)
    print("\nPeople by number of pattern elements matched (yes):")
    for n in range(len(PATTERN_ELEMENTS), 0, -1):
        print(f"  {n}/{len(PATTERN_ELEMENTS)}: {full[n]:,}")
    interventions = defaultdict(Counter)
    for r in relevant:
        for fact in r["facts"]:
            if fact["field"] == "intervention" and fact["quote_verified"] and fact["claim_type"] == "experience":
                interventions[_ws(fact["value"]).lower()][fact["outcome"]] += 1
    print("\nMost-mentioned interventions (first-hand, verified; free-text values, not normalised):")
    for value, outcomes in sorted(interventions.items(), key=lambda kv: -sum(kv[1].values()))[:25]:
        print(f"  {sum(outcomes.values()):4}  {value[:50]:50} {dict(outcomes)}")
    unverified = sum(not f["quote_verified"] for r in rows for f in r["facts"])
    total = sum(len(r["facts"]) for r in rows)
    print(f"\n{unverified:,}/{total:,} facts had quotes that didn't verify and were excluded above.")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("study", help="Study folder containing corpus.jsonl and extraction.md")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--submit", action="store_true")
    mode.add_argument("--collect", action="store_true")
    mode.add_argument("--summary", action="store_true")
    args = p.parse_args()
    study = Path(args.study)

    if args.summary:
        summary(study)
        return
    people = load_people(study)
    if not people:
        sys.exit("No query-matching items with a known author in corpus.jsonl.")
    if args.collect:
        collect(study, people)
        return
    requests = build_requests(study, people)
    if args.dry_run:
        dry_run(study, people, requests)
    else:
        submit(study, requests)


if __name__ == "__main__":
    main()
