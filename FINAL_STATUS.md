# DocketWatch — final status

Verified end-to-end after the fixes below. `pytest -m "not network"`: **86 passed**.

## What's fully working

- **Dedup core** (`state_store.diff`/`mark_seen`): poll 1 over 8 fixture
  entries → 8 new; poll 2 over the identical docket → 0 new; poll 3 with one
  entry appended → exactly 1 new. Verified both by the test suite and by a
  real `docketwatch track` → `docketwatch poll` → `docketwatch poll` run (see
  below) — the second `poll` reported 0 new entries.
- **CLI**: `track`, `untrack`, `cases`, `fetch`, `timeline`, `search`,
  `explain`, `ask`, `digest`, `poll`, `budget`, `serve` all run against the
  bundled fixtures with no token needed.
- **Web app** (`docketwatch serve`, `http://localhost:8474`): all five
  `/api/` routes return valid JSON (`/api/cases`, `/api/cases/{id}`,
  `/api/cases/{id}/timeline`, `/api/cases/{id}/digest`, `POST /api/ask`).
  The case-detail page now actually renders its Digest and Ask sections
  (previously dead mount points — see fixes below).
- **AI features** (local Ollama, `chains.yaml`'s `digest` chain on
  `gemma4:e4b-mlx`): `explain`, `ask`, and the poll-cycle summary all work
  when Ollama is running locally. Not required for the rest of the app —
  every AI failure path degrades to an honest error, never invented text.
- **Rate budget** (`docketwatch budget`, `sources.Budget`): persists to
  `~/.docketwatch/budget_<date>.json`, enforces 5/min, 50/hour, 125/day, and
  a fixture-mode `poll` no longer gets skipped by a stale live-mode budget
  file (see fixes below).

## What's blocked on a real CourtListener token

Nothing in fixture mode is blocked. Live mode (`DOCKETWATCH_LIVE=1`) is
fully built but genuinely cannot be exercised without a token — CourtListener
has **no anonymous access at all** (confirmed in
`src/docketwatch/spike_courtlistener_auth.py`). Until a token exists:

- `CourtListenerSource`'s response-field mapping (`sources.py`'s
  `_docket_from_api`/`_entry_from_api`) is **ASSUMED**, not verified against
  a real payload.
- `tests/test_sources_live.py` (the one real-network test) self-skips.

To wire up a real token:

```sh
# free account at courtlistener.com, then create an API token
export COURTLISTENER_API_TOKEN=...
export DOCKETWATCH_LIVE=1
docketwatch search "some case name"     # now hits the live API
```

For the launchd background agent, add both env vars to the plist's
`EnvironmentVariables` dict before installing (see `scripts/install-launchd.sh`'s
own printed reminder) — otherwise the agent keeps polling fixtures.

## Stretch / not built

- **Filing search UI** (`web/sections/search.js`): existed as an orphaned,
  broken ES-module file — not referenced by `app.html`, no nav entry, and
  written against `import`/`export` syntax that can't run against `api.js`'s
  UMD module (it would throw on load even if a `<script type="module">` tag
  were added). Deleted rather than half-wired, since building the search
  view out is new feature work, not a bug fix. `docketwatch search` on the
  CLI covers the same need today.
- **Party/counsel data over real dockets**: `Docket.parties` is populated
  from CourtListener's `parties` field (ASSUMED shape, ties to the live-token
  blocker above); the fixtures already carry realistic party data so the UI
  renders correctly against them today.

## Fixes applied this pass

Backend (from the backend-side review):
- `poll.run_poll_cycle` no longer applies the CourtListener rate-budget guard
  to a source that doesn't spend real requests — gated on a new
  `DocketSource.uses_budget` flag (true only for `CourtListenerSource`), so a
  plain fixture-mode `docketwatch poll` is never skipped by a stale live-mode
  budget file from earlier the same day.
- `sources.Budget.consume()` now takes an `fcntl.flock` around its
  read-check-write, closing the race where two overlapping processes (an
  interactive run overlapping a cron/launchd run) could both see room and
  both write, silently under/over-counting the day's real request count.
- `models._provenance` now rejects an explicit `source` value that isn't
  `"fixture"`/`"courtlistener"` (the module's own documented closed set) —
  previously nothing enforced `VALID_SOURCES` at all. A missing `source`
  still defaults to the honest `"unknown"` sentinel, unchanged.
- `TrackedCase` gained a `parties` field, threaded through from
  `cli.py track()` → `state_store.track_case()`, so the case-detail page's
  "Parties & counsel" section (which already read `caseItem.parties`) has
  real data to render instead of being permanently unreachable.
- `cli.py track()`'s printed summary now reports the real
  `len(case.seen_entry_ids)` instead of a hardcoded `"0 marked seen"`, which
  was wrong on every re-track of an already-polled case.

Frontend (from the frontend-side review):
- `web/sections/case-detail.js` now actually populates `#digest-content`
  (calls `DocketWatchApi.getDigest`, renders the markdown) and
  `#ask-content` (a real form calling `DocketWatchApi.ask`) — previously
  these two headed sections rendered nothing at all, with no loading state
  and no error, silently looking finished while being empty.
- Deleted `web/sections/digest.js` and `web/sections/search.js` — orphaned,
  unreferenced, and broken as written (ES `import`/`export` against an
  `api.js` that only exposes a UMD `window.DocketWatchApi`). The digest
  functionality they attempted now lives correctly in `case-detail.js`
  above; `search.js` was a whole unbuilt/unwired feature (see Stretch).

## Skipped (flagged but not fixed, with reasons)

- **Midnight-UTC budget window undercount** (`sources.py`'s per-day budget
  file split): a request just before/after UTC midnight can miss a few
  seconds of the true sliding 1-hour/1-minute window because each day gets
  its own file. Flagged by the review as genuinely minor — never affects the
  125/day cap (correctly bucketed by design), and CourtListener's own HTTP
  429 is still the real backstop. Not fixed; not worth the added complexity
  of reading two files on every check for a single-user local tool.
- **cli.py:66 `str(len(e.recap_documents)) or "0"`**: flagged as dead code
  (`str(0)` is truthy, so the `or "0"` never fires) but not a behavior bug —
  already prints "0" correctly. Left as-is; not worth a diff on its own.

## Commands

```sh
# install the launchd background-poll agent (every 3h, sample fixtures by default)
./scripts/install-launchd.sh
launchctl list | grep com.parth.docketwatch-poll   # check it's loaded
launchctl kickstart -k gui/$(id -u)/com.parth.docketwatch-poll  # run it now
launchctl bootout gui/$(id -u)/com.parth.docketwatch-poll        # uninstall

# wire up a real CourtListener token (see "blocked" section above)
export COURTLISTENER_API_TOKEN=...
export DOCKETWATCH_LIVE=1
```
