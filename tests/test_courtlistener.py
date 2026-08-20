"""Fixture-based, no-network coverage for `docketwatch.sources`.

`CourtListenerSource`, `Budget`, and `get_source()` already live in
`sources.py` (same single-file DocketSource/FixtureSource/live-source shape
as permitpulse's `sources.py` -- there is no separate `courtlistener.py` to
duplicate that split into). `tests/test_sources_live.py` covers the one real
network call; everything here is local: the budget's persistence and ceiling
enforcement, and the API-row -> dataclass mapping, none of which needs a
socket. The one true network test stays in test_sources_live.py rather than
being duplicated here.

    pytest tests/test_courtlistener.py -v
"""

from __future__ import annotations

import json

import pytest

from docketwatch.sources import (
    LIMIT_PER_MINUTE,
    Budget,
    CourtListenerSource,
    DocketDataError,
    FixtureSource,
    RateBudgetError,
    get_source,
    live_enabled,
)

FAKE_TOKEN = "fake-token-for-local-tests"


# -- Budget ---------------------------------------------------------------


def test_budget_starts_with_full_headroom(tmp_path):
    state = Budget(tmp_path).status(now=1_700_000_000)
    assert state["used"] == {"minute": 0, "hour": 0, "day": 0}
    assert state["remaining"]["day"] == state["limits"]["day"]


def test_budget_consume_persists_and_is_counted(tmp_path):
    budget = Budget(tmp_path)
    now = 1_700_000_000.0
    budget.consume(now)
    state = budget.status(now)
    assert state["used"] == {"minute": 1, "hour": 1, "day": 1}
    # persisted, not just in-memory -- a fresh Budget over the same dir sees it too
    assert Budget(tmp_path).status(now)["used"]["day"] == 1


def test_budget_refuses_past_the_minute_ceiling(tmp_path):
    budget = Budget(tmp_path)
    now = 1_700_000_000.0
    for _ in range(LIMIT_PER_MINUTE):
        budget.consume(now)
    with pytest.raises(RateBudgetError, match="minute"):
        budget.consume(now)
    # the refused call must not have been recorded
    assert budget.status(now)["used"]["minute"] == LIMIT_PER_MINUTE


def test_budget_window_rolls_off(tmp_path):
    budget = Budget(tmp_path)
    old = 1_700_000_000.0
    for _ in range(LIMIT_PER_MINUTE):
        budget.consume(old)
    # a minute later the per-minute window is clear again, even though the
    # day total (same file) still carries all of today's requests
    later = old + 61
    state = budget.status(later)
    assert state["used"]["minute"] == 0
    assert state["used"]["day"] == LIMIT_PER_MINUTE
    budget.consume(later)  # must not raise


def test_budget_corrupt_file_raises_clearly(tmp_path):
    budget = Budget(tmp_path)
    now = 1_700_000_000.0
    budget._path(now).parent.mkdir(parents=True, exist_ok=True)
    budget._path(now).write_text("not json")
    with pytest.raises(DocketDataError, match="unreadable"):
        budget.status(now)


# -- CourtListenerSource construction --------------------------------------


def test_no_token_raises_rather_than_falling_back(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    with pytest.raises(DocketDataError, match="anonymous access"):
        CourtListenerSource()
    assert CourtListenerSource.token_present() is False


def test_token_present_reads_the_env_var(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", FAKE_TOKEN)
    assert CourtListenerSource.token_present() is True
    CourtListenerSource()  # must not raise, and must not touch the network


# -- get_source() ------------------------------------------------------------


def test_get_source_defaults_to_fixture(monkeypatch):
    monkeypatch.delenv("DOCKETWATCH_LIVE", raising=False)
    assert live_enabled() is False
    assert isinstance(get_source(), FixtureSource)


def test_get_source_live_without_token_raises(monkeypatch):
    monkeypatch.setenv("DOCKETWATCH_LIVE", "1")
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    assert live_enabled() is True
    with pytest.raises(DocketDataError, match="anonymous access"):
        get_source()


# -- response mapping, no HTTP involved --------------------------------------


def _source(monkeypatch, tmp_path) -> CourtListenerSource:
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", FAKE_TOKEN)
    return CourtListenerSource(budget=Budget(tmp_path))


def test_docket_from_api_maps_assumed_fields(monkeypatch, tmp_path):
    source = _source(monkeypatch, tmp_path)
    row = {
        "id": 12345,
        "case_name": "Doe v. Roe",
        "docket_number": "3:26-cv-00001",
        "court_id": "cand",
        "date_filed": "2026-01-14T00:00:00Z",
        "assigned_to_str": "Hon. Jane Smith",
        "parties": [{"name": "Doe", "type": "plaintiff", "attorneys": []}],
    }
    docket = source._docket_from_api(row, entries=[])
    assert docket.id == "12345"
    assert docket.case_name == "Doe v. Roe"
    assert docket.court_id == "cand"
    assert docket.date_filed == "2026-01-14"
    assert docket.source == "courtlistener"
    assert docket.parties == row["parties"]
    assert "ASSUMED" in docket.note


def test_docket_from_api_requires_an_id(monkeypatch, tmp_path):
    source = _source(monkeypatch, tmp_path)
    with pytest.raises(DocketDataError, match="no id"):
        source._docket_from_api({"case_name": "no id here"})


def test_entry_from_api_maps_recap_documents(monkeypatch, tmp_path):
    source = _source(monkeypatch, tmp_path)
    row = {
        "id": 999,
        "date_filed": "2026-02-01T00:00:00Z",
        "entry_number": 3,
        "description": "ORDER granting motion",
        "recap_documents": [
            {"id": 1, "document_number": "3", "is_available": True, "description": "Order"}
        ],
    }
    entry = source._entry_from_api(row, docket_id="12345")
    assert entry.id == "999"
    assert entry.docket_id == "12345"
    assert entry.entry_type == "order"  # classify_entry_type runs locally, not from the API
    assert len(entry.recap_documents) == 1
    assert entry.recap_documents[0].id == "1"
    assert entry.recap_documents[0].is_available is True


def test_entries_without_ids_are_skipped_in_recap_documents(monkeypatch, tmp_path):
    source = _source(monkeypatch, tmp_path)
    row = {
        "id": 1,
        "date_filed": "2026-02-01",
        "description": "NOTICE of appearance",
        "recap_documents": [{"description": "no id, must be dropped"}],
    }
    entry = source._entry_from_api(row, docket_id="12345")
    assert entry.recap_documents == []
