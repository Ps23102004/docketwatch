"""The one test file that talks to the real CourtListener API.

Marked `network`, so the default `pytest -m "not network"` skips it and the
rest of the suite needs no network at all. It also self-skips when
`COURTLISTENER_API_TOKEN` is absent or the host is unreachable -- CourtListener
allows no anonymous access whatsoever, so with no token there is nothing to
assert and a hard failure would be noise, not signal.

Each test here spends real requests from a 5/min, 50/hour, 125/day free-tier
budget. Keep it to one request per test and do not add retries.

    pytest tests/test_sources_live.py -v -m network
"""

from __future__ import annotations

import os

import pytest

from docketwatch.sources import TOKEN_ENV, CourtListenerSource, DocketDataError

pytestmark = pytest.mark.network

requires_token = pytest.mark.skipif(
    not os.environ.get(TOKEN_ENV),
    reason=f"{TOKEN_ENV} is not set -- CourtListener has no anonymous access, nothing to test",
)


def test_no_token_raises_rather_than_falling_back_to_fixtures(monkeypatch):
    """Live mode without credentials must fail loudly, never serve sample data as real."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(DocketDataError, match="anonymous access"):
        CourtListenerSource()


@requires_token
def test_live_search_returns_real_dockets():
    try:
        hits = CourtListenerSource().search("insurance", limit=3)
    except DocketDataError as exc:
        pytest.skip(f"CourtListener unreachable or unusable right now: {exc}")
    assert hits, "search returned nothing -- widen the query before assuming a bug"
    for docket in hits:
        assert docket.id
        assert docket.source == "courtlistener"
        assert docket.note
