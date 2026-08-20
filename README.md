# DocketWatch

DocketWatch tracks federal court cases and tells you what changed since you last looked. You point it at a CourtListener/RECAP docket id, it caches the docket's entries under `~/.docketwatch/`, and every poll after that diffs the live docket against the entry ids you have already seen — so the second poll of an unchanged case reports nothing, and a case with one new filing reports exactly that one filing. It classifies each entry as a motion, order, notice, opinion or stipulation, renders a timeline and a markdown digest, and can explain an entry or answer a question about the case using a local Ollama model. Everything runs on your machine: flat JSON files, no database, no cloud, and no deployment.

## Run it

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e "$HOME/Developer/llm-ladder"   # local sibling package, not on PyPI
pip install -e ".[dev]"
docketwatch --help
```

Out of the box it runs against two hand-authored sample dockets in `src/docketwatch/fixtures/`. **Those are fictional cases** — no party, judge, docket number or filing in them is real, and every record says so in its `source`/`note` fields.

```sh
docketwatch search redwood            # find a case in the active source
docketwatch track fixture-alpha-1     # start watching it
docketwatch poll                      # what's new since last time
docketwatch timeline fixture-alpha-1  # the whole docket
docketwatch digest --write            # markdown to ~/.docketwatch/digests/<date>.md
docketwatch serve                     # web app + /api/ on http://localhost:8474
```

Add `--json` to any data command to get exactly the JSON the web API serves.

## Live mode

```sh
export COURTLISTENER_API_TOKEN=...    # free account at courtlistener.com
export DOCKETWATCH_LIVE=1
```

CourtListener allows **no anonymous access at all**, so without a token the live path is blocked and DocketWatch says so rather than quietly serving sample data as if it were real. The free tier is 5 requests/minute, 50/hour and 125/day; `docketwatch budget` shows what today has spent, and every live call is refused locally before it can be throttled. Polling only — webhooks need a paid arrangement. See `src/docketwatch/spike_courtlistener_auth.py` for what is confirmed, what is blocked, and which field names are still assumptions awaiting a token.

## Tests

```sh
pytest -m "not network"    # the default safe run, no network needed
pytest -m network          # hits the real API; self-skips without a token
```
