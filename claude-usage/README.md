# claude-usage

Records your Claude plan usage (the 5-hour and weekly percentages from `/usage`) to a file, so scripts and
loops can read it and stop before a limit.

Two scripts, standard library only:

| File | Job |
|---|---|
| `statusline.py` | Claude Code runs it as the **status line**. It receives usage in JSON on stdin and writes `~/.claude/usage/latest.json`. |
| `usage.py` | Reads that file. `--check 90` exits non-zero at 90%; `--wait-below 90` sleeps until the window resets. |

## Read this first: two limits

1. **Status lines only run in the terminal CLI.** The VS Code extension does not run them, so it never updates
   the file. To feed the file, run Claude Code (`claude`) in a terminal, even briefly.
2. **The figures are only as fresh as the last CLI turn.** `usage.py` treats data older than 30 minutes as
   missing (exit code 3) instead of trusting it. A stale 40% is not evidence you are still at 40%.

Also unconfirmed: your CLI is 2.1.178, and the docs place the usage fields in newer builds (v2.1.251 is the
version I could confirm). If step 3 below shows `payload had no rate_limits`, run `claude update`.

## Setup

### 1. Add the status line to `C:\Users\chris\.claude\settings.json`

The file already exists and has no `statusLine` key. **Merge this key into the existing JSON**; do not replace
the file. Keep the commas right.

```json
"statusLine": {
  "type": "command",
  "command": "C:/Users/chris/github/mfh-analysis/.venv/Scripts/python.exe C:/Users/chris/github/tools/claude-usage/statusline.py"
}
```

**Use forward slashes.** Claude Code on Windows runs this command through Git Bash, which treats backslashes as
escapes: `C:\Users\...` becomes `C:Users...` and fails with "command not found", silently, so the status line
just never appears. (This bit me once; the symptom is `usage.py` saying "No usage recorded yet" after a reply.)

The command uses the venv's Python by absolute path because plain `python` on your PATH resolves to whichever
environment is active. Any Python 3.8+ works; the scripts need nothing installed. If you move the venv, change
this path.

Alternatively run `/statusline` inside `claude` and ask it to use that command.

### 2. Start a CLI session

Open a terminal, run `claude`, and send any message. After the first response the bottom line should read
something like `5h 74% (resets 1h12m) | 7d 15% (resets 118h05m)`.

### 3. Check that it recorded

```
python C:\Users\chris\github\tools\claude-usage\usage.py
```

Expected: `recorded 0 min ago`, then the two percentages.

If it says `payload had no rate_limits (keys: ...)`, your Claude Code version or plan is not sending usage:
run `claude update` and try again. To see exactly what is being sent, set `CLAUDE_USAGE_DEBUG=1` before
starting `claude`, and open `C:\Users\chris\.claude\usage\last-payload.json` (it holds session metadata such as
paths, so do not share it).

## Using it

```
python usage.py                        # print figures
python usage.py --json                 # machine-readable
python usage.py --check 90             # exit 0 = under 90%, 10 = at/over 90%, 3 = no fresh data
python usage.py --check 90 --window five_hour
python usage.py --wait-below 90        # sleep until the window resets, then exit 0
python usage.py --check 90 --max-age 10   # tighter freshness requirement
```

Exit code 3 (no data) should be treated as "do not continue". A loop that cannot see the meter should stop, not
guess.

In a shell loop:

```bash
for step in 1 2 3 4 5 6; do
  python /c/Users/chris/github/tools/claude-usage/usage.py --check 90 || { echo "stopping: usage limit or no data"; break; }
  # ...one unit of work...
done
```

For Claude itself: tell it "before each step, run `usage.py --check 90` and stop if it exits non-zero". That is
the only way the assistant can honour a budget, because it cannot see the meter itself.

## Files

- `~/.claude/usage/latest.json` current figures (overwritten each turn)
- `~/.claude/usage/last-payload.json` only when `CLAUDE_USAGE_DEBUG` is set
- Set `CLAUDE_USAGE_DIR` to store them somewhere else (both scripts honour it).

## Tests

```
cd C:\Users\chris\github\tools\claude-usage
python -m unittest test_claude_usage
```

## Removing it

Delete the `statusLine` key from `settings.json`. Nothing else is installed.
