"""The dedup contract. This is the test the whole project rests on.

The core scenario, spelled out because everything else is downstream of it:

    poll 1 against N fixture entries  -> N new
    poll 2 against the identical data -> 0 new
    poll 3 with one entry appended    -> exactly 1 new, and it is the new one

A false positive here re-announces filings the user already read; a false
negative silently loses a filing. Both are worse than the tool not existing.

Every test writes into a tmp_path, never into the real ~/.docketwatch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docketwatch import state_store
from docketwatch.models import Docket, DocketEntry
from docketwatch.state_store import StateStoreError

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "src" / "docketwatch" / "fixtures"
DOCKET_ID = "fixture-alpha-1"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the store at a tmp dir so no test can touch the real cache."""
    monkeypatch.setattr(state_store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(state_store, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(state_store, "DIGESTS_DIR", tmp_path / "digests")
    return tmp_path


@pytest.fixture
def fixture_docket() -> Docket:
    return Docket.from_dict(json.loads((FIXTURE_DIR / "sample_docket_alpha.json").read_text()))


def _track(docket: Docket):
    return state_store.track_case(
        docket.id,
        {
            "case_name": docket.case_name,
            "docket_number": docket.docket_number,
            "court": docket.court,
            "source": docket.source,
            "note": docket.note,
        },
    )


def _poll(docket_id: str, entries):
    """One full poll: diff, then commit what it found. Returns the new entries."""
    new = state_store.diff(docket_id, entries)
    state_store.mark_seen(docket_id, [e.id for e in new])
    return new


# -- THE dedup test -----------------------------------------------------------


def test_three_polls_first_all_new_second_none_third_exactly_one(fixture_docket):
    _track(fixture_docket)
    entries = fixture_docket.entries
    assert len(entries) == 8, "fixture changed -- update this test's expectations deliberately"

    # Poll 1: everything is new.
    first = _poll(DOCKET_ID, entries)
    assert len(first) == len(entries)
    assert [e.id for e in first] == [e.id for e in entries]

    # Poll 2: the identical docket. Nothing is new.
    second = _poll(DOCKET_ID, entries)
    assert second == []

    # Poll 3: the clerk adds one entry. Exactly that entry is new.
    appended = DocketEntry(
        id="fixture-alpha-e9",
        docket_id=DOCKET_ID,
        date_filed="2026-05-02",
        entry_number=9,
        description="ORDER setting Case Management Conference for 2026-06-15.",
        source="fixture",
        note="Hand-authored sample entry, not a real filing.",
    )
    third = _poll(DOCKET_ID, entries + [appended])
    assert len(third) == 1
    assert third[0].id == "fixture-alpha-e9"

    # And a fourth poll of that same docket is quiet again.
    assert _poll(DOCKET_ID, entries + [appended]) == []
    assert len(state_store.load_case(DOCKET_ID).seen_entry_ids) == 9


# -- diff's own guarantees ----------------------------------------------------


def test_diff_does_not_write(fixture_docket):
    _track(fixture_docket)
    assert len(state_store.diff(DOCKET_ID, fixture_docket.entries)) == 8
    # Called twice with no mark_seen between, it must answer identically.
    assert len(state_store.diff(DOCKET_ID, fixture_docket.entries)) == 8
    assert state_store.load_case(DOCKET_ID).seen_entry_ids == []
    assert state_store.load_case(DOCKET_ID).last_polled_at is None


def test_diff_accepts_raw_dicts(fixture_docket):
    _track(fixture_docket)
    new = state_store.diff(DOCKET_ID, [e.to_dict() for e in fixture_docket.entries])
    assert len(new) == 8
    assert all(isinstance(e, DocketEntry) for e in new)


def test_diff_collapses_duplicate_ids_within_one_fetch(fixture_docket):
    _track(fixture_docket)
    doubled = fixture_docket.entries + fixture_docket.entries
    assert len(state_store.diff(DOCKET_ID, doubled)) == 8


def test_diff_ignores_description_edits_and_keys_only_on_id(fixture_docket):
    """A clerk editing an entry's text must not re-announce it as new."""
    _track(fixture_docket)
    _poll(DOCKET_ID, fixture_docket.entries)
    edited = [DocketEntry.from_dict({**e.to_dict(), "description": e.description + " (corrected)"})
              for e in fixture_docket.entries]
    assert state_store.diff(DOCKET_ID, edited) == []


def test_diff_on_untracked_case_raises(fixture_docket):
    with pytest.raises(StateStoreError, match="not tracked"):
        state_store.diff("never-tracked", fixture_docket.entries)


def test_diff_preserves_order(fixture_docket):
    _track(fixture_docket)
    new = state_store.diff(DOCKET_ID, fixture_docket.entries)
    assert [e.entry_number for e in new] == [1, 2, 3, 4, 5, 6, 7, 8]


# -- mark_seen ----------------------------------------------------------------


def test_mark_seen_is_idempotent(fixture_docket):
    _track(fixture_docket)
    state_store.mark_seen(DOCKET_ID, ["e1", "e2"])
    state_store.mark_seen(DOCKET_ID, ["e2", "e3"])
    assert state_store.load_case(DOCKET_ID).seen_entry_ids == ["e1", "e2", "e3"]


def test_mark_seen_with_nothing_still_records_the_poll(fixture_docket):
    _track(fixture_docket)
    case = state_store.mark_seen(DOCKET_ID, [])
    assert case.seen_entry_ids == []
    assert case.last_polled_at is not None


def test_mark_seen_survives_a_reload(fixture_docket):
    _track(fixture_docket)
    state_store.mark_seen(DOCKET_ID, [e.id for e in fixture_docket.entries])
    assert len(state_store.load_case(DOCKET_ID).seen_entry_ids) == 8
    assert state_store.diff(DOCKET_ID, fixture_docket.entries) == []


# -- registry -----------------------------------------------------------------


def test_track_list_load_untrack(fixture_docket):
    assert state_store.list_cases() == []
    case = _track(fixture_docket)
    assert case.docket_id == DOCKET_ID
    assert case.case_name == fixture_docket.case_name
    assert case.tracked_at
    assert [c.docket_id for c in state_store.list_cases()] == [DOCKET_ID]
    assert state_store.load_case(DOCKET_ID).court == "N.D. Cal."
    assert state_store.untrack_case(DOCKET_ID) is True
    assert state_store.list_cases() == []
    assert state_store.untrack_case(DOCKET_ID) is False


def test_retracking_keeps_the_seen_set(fixture_docket):
    """Re-tracking must not turn every old filing back into news."""
    _track(fixture_docket)
    _poll(DOCKET_ID, fixture_docket.entries)
    _track(fixture_docket)
    assert len(state_store.load_case(DOCKET_ID).seen_entry_ids) == 8
    assert state_store.diff(DOCKET_ID, fixture_docket.entries) == []


def test_untrack_then_retrack_forgets(fixture_docket):
    _track(fixture_docket)
    _poll(DOCKET_ID, fixture_docket.entries)
    state_store.untrack_case(DOCKET_ID)
    _track(fixture_docket)
    assert len(state_store.diff(DOCKET_ID, fixture_docket.entries)) == 8


def test_load_missing_case_raises_with_a_next_step(fixture_docket):
    with pytest.raises(StateStoreError, match="docketwatch track"):
        state_store.load_case("nope")


def test_corrupt_case_file_raises_rather_than_returning_empty(fixture_docket, isolated_store):
    _track(fixture_docket)
    state_store.case_path(DOCKET_ID).write_text("{not json")
    with pytest.raises(StateStoreError, match="unreadable"):
        state_store.load_case(DOCKET_ID)


def test_save_entries_caches_the_timeline_without_marking_them_seen(fixture_docket):
    _track(fixture_docket)
    case = state_store.save_entries(DOCKET_ID, fixture_docket.entries)
    assert len(case.entries) == 8
    assert case.seen_entry_ids == []
    assert len(state_store.diff(DOCKET_ID, fixture_docket.entries)) == 8


def test_provenance_persists_through_the_store(fixture_docket):
    _track(fixture_docket)
    state_store.save_entries(DOCKET_ID, fixture_docket.entries)
    case = state_store.load_case(DOCKET_ID)
    assert case.source == "fixture"
    assert "not a real case" in case.note
    assert all(e.source == "fixture" for e in case.entries)


# -- the id is a filename, so it is a trust boundary --------------------------


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "  ", ".", "..", "x" * 129, "a\0b"])
def test_path_traversal_and_junk_ids_are_rejected(bad):
    with pytest.raises(StateStoreError):
        state_store.case_path(bad)


def test_digest_path_rejects_a_non_date():
    with pytest.raises(StateStoreError):
        state_store.digest_path("not-a-date")
    assert state_store.digest_path("2026-08-20").name == "2026-08-20.md"
