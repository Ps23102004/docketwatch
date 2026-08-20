"""What we know about the CourtListener API, what is blocked, and what is a guess.

Run it:

    python -m docketwatch.spike_courtlistener_auth

It prints the auth status of this machine and, if a token exists, makes ONE
live request to confirm the token works and to dump the real field names --
which is the only way anything below marked ASSUMED gets promoted to
CONFIRMED.

-----------------------------------------------------------------------------
CONFIRMED (checked against the live API, 2026-08-20)
-----------------------------------------------------------------------------
* Base URL: https://www.courtlistener.com/api/rest/v4/
* Auth header: `Authorization: Token <token>`
* There is NO anonymous access, at all. A live GET against
  /api/rest/v4/dockets/ with no token returns:
      {"detail":"Authentication credentials were not provided."}
  A live OPTIONS against the same path returns the identical body. OPTIONS is
  normally how a DRF API self-describes its fields, so with no token there is
  no way to discover field names -- not by request, not by introspection.
* Free-tier rate limits: 5 requests/minute, 50/hour, 125/day. `sources.Budget`
  enforces all three locally before any socket is opened, and no code path in
  this project retries a throttled request.
* Webhooks require a PAID arrangement. Not available on the free tier. This
  project is polling-only by design and nothing is built toward webhooks.

-----------------------------------------------------------------------------
BLOCKED right now
-----------------------------------------------------------------------------
COURTLISTENER_API_TOKEN is not set in this environment. Until it is:

* `docketwatch fetch --live`, `search --live` and `poll --live` cannot run.
* Every field name in the ASSUMED list below is unverified.
* No real case is being tracked. The only dockets this repo contains are the
  two hand-authored fixtures in `fixtures/`, which are FICTIONAL and labelled
  `"source": "fixture"` in every record.

To unblock: sign up free at https://www.courtlistener.com/ , create an API
token in your profile, then

    export COURTLISTENER_API_TOKEN=...
    export DOCKETWATCH_LIVE=1
    python -m docketwatch.spike_courtlistener_auth

The live code path in `sources.CourtListenerSource` is already written and
activates the moment that variable exists -- nothing else needs changing.

-----------------------------------------------------------------------------
ASSUMED -- verify once a token exists
-----------------------------------------------------------------------------
These are best guesses at CourtListener's own naming, taken from its public
documentation and from how DRF APIs are usually shaped. NONE of them has been
seen in a real response from this machine. They are wired into
`sources.CourtListenerSource._docket_from_api` / `._entry_from_api`, so if any
is wrong, that is the one place to fix.

  Docket (GET /dockets/{id}/):
      id, case_name, docket_number, court, court_id, date_filed,
      date_terminated, assigned_to_str, nature_of_suit, cause, absolute_url
      -- `parties` may not be embedded on the docket object at all; it may
         need a separate /parties/?docket= request, which costs another
         request against the 5/minute budget.

  Docket entries (GET /docket-entries/?docket={id}):
      results[], and per result: id, date_filed, entry_number, description,
      recap_documents[]
      -- `order_by=recap_sequence_number` is assumed to be an accepted
         ordering parameter.
      -- pagination is assumed to be DRF-standard (`results`, `next`,
         `page_size`). This project fetches one page only.

  RECAP documents (embedded in a docket entry):
      id, document_number, attachment_number, description, page_count,
      is_available, filepath_local
      -- `is_available` false is assumed to mean RECAP holds metadata but not
         the PDF.

  Search (GET /dockets/?case_name__icontains=...):
      the `__icontains` filter suffix is assumed to be enabled on case_name.
      If it is not, the fallback is the /search/ endpoint with `type=r`.
"""

from __future__ import annotations

import json
import os
import sys

TOKEN_ENV = "COURTLISTENER_API_TOKEN"
PROBE_URL = "https://www.courtlistener.com/api/rest/v4/dockets/"


def main() -> int:
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(f"{TOKEN_ENV} is NOT set.")
        print("BLOCKED: no live call is possible -- CourtListener allows no anonymous access.")
        print("Sign up at https://www.courtlistener.com/ , create a token, then export it.")
        print("Everything marked ASSUMED in this module's docstring stays unverified.")
        return 1

    import requests

    print(f"{TOKEN_ENV} is set. Making ONE live request (of the 5/minute free-tier budget)...")
    resp = requests.get(
        PROBE_URL,
        params={"page_size": 1},
        headers={"Authorization": f"Token {token}", "Accept": "application/json"},
        timeout=60,
    )
    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        return 1

    body = resp.json()
    results = body.get("results") or []
    print(f"top-level keys: {sorted(body)}")
    if results:
        print("REAL docket field names (promote these from ASSUMED to CONFIRMED):")
        print(json.dumps(sorted(results[0]), indent=1))
    else:
        print("Authenticated, but the response carried no results to read field names from.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
