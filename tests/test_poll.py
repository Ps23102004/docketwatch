"""Poll-cycle tests: fetch -> diff -> summarize -> notify -> mark_seen, plus
the rate-budget short-circuit. `courtlistener`/state_store's real functions
run against a tmp-dir store (fast, no mocking of the dedup logic itself);
`ai.summarize_new_filings` and `notify.fire` are mocked -- neither a live
model nor a real osascript call belongs in this suite.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from docketwatch import poll, state_store
from docketwatch.models import Docket, DocketEntry
from docketwatch.sources import Budget, DocketDataError, DocketSource

TODAY = "2026-08-20"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the store at a tmp dir so no test can touch the real cache."""
    monkeypatch.setattr(state_store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(state_store, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(state_store, "DIGESTS_DIR", tmp_path / "digests")
    return tmp_path


def _docket(docket_id: str, entry_ids) -> Docket:
    return Docket(
        id=docket_id,
        case_name=f"Case {docket_id}",
        docket_number="1:26-cv-00001",
        court="N.D. Cal.",
        source="fixture",
        note="test fixture",
        entries=[
            DocketEntry(id=f"{docket_id}-{n}", docket_id=docket_id, entry_number=i, description=f"Entry {n}",
                        source="fixture")
            for i, n in enumerate(entry_ids, start=1)
        ],
    )


def _track(docket_id: str, entry_ids=()):
    state_store.track_case(docket_id, {"case_name": f"Case {docket_id}", "source": "fixture", "note": "t"})
    if entry_ids:
        # Seed as already-seen so the next fetch reports no new entries.
        seeded = _docket(docket_id, entry_ids)
        state_store.save_entries(docket_id, seeded.entries)
        state_store.mark_seen(docket_id, [e.id for e in seeded.entries])


class FakeSource(DocketSource):
    """Serves canned dockets and records fetch order. No network."""

    source_id = "fixture"

    def __init__(self, dockets: dict, budget: Budget = None, call_log: list = None, fail_on=(),
                 uses_budget: bool = False):
        self.dockets = dockets
        self.budget = budget
        self.call_log = call_log if call_log is not None else []
        self.fail_on = set(fail_on)
        # True only in the budget-guard tests below, where this fake stands in
        # for a metered live source (it consumes `budget` in fetch_docket just
        # like CourtListenerSource does) -- everywhere else it behaves like the
        # real FixtureSource, which never spends the rate budget.
        self.uses_budget = uses_budget

    def fetch_docket(self, docket_id: str) -> Docket:
        self.call_log.append(f"fetch:{docket_id}")
        if docket_id in self.fail_on:
            raise DocketDataError(f"simulated failure for {docket_id}")
        if self.budget is not None:
            self.budget.consume()
            self.budget.consume()  # a real fetch is 2 requests: docket + entries
        return self.dockets[docket_id]

    def search(self, query: str, limit: int = 10):
        raise NotImplementedError


def _wrap(call_log, name, real):
    def wrapper(*args, **kwargs):
        call_log.append(name)
        return real(*args, **kwargs)

    return wrapper


# -- call order and content ----------------------------------------------------


def test_new_entries_trigger_summarize_then_notify_then_mark_seen(monkeypatch):
    _track("case-1")
    docket = _docket("case-1", ["e1", "e2"])
    call_log = []
    source = FakeSource({"case-1": docket}, call_log=call_log)

    monkeypatch.setattr(poll.state_store, "diff", _wrap(call_log, "diff", state_store.diff))
    monkeypatch.setattr(poll.state_store, "save_entries", _wrap(call_log, "save_entries", state_store.save_entries))
    monkeypatch.setattr(poll.state_store, "mark_seen", _wrap(call_log, "mark_seen", state_store.mark_seen))

    fake_summary = object()
    summarize = MagicMock(return_value=fake_summary)
    notify_fire = MagicMock()
    monkeypatch.setattr(poll, "summarize_new_filings", _wrap(call_log, "summarize", summarize))
    monkeypatch.setattr(poll.notify, "fire", _wrap(call_log, "notify", notify_fire))

    result = poll.run_poll_cycle(source=source)

    assert call_log == ["fetch:case-1", "diff", "save_entries", "summarize", "notify", "mark_seen"]
    assert result.polled == ["case-1"]
    assert result.skipped == []
    outcome = result.outcomes["case-1"]
    assert [e.id for e in outcome.new_entries] == ["case-1-e1", "case-1-e2"]
    assert outcome.summary is fake_summary
    assert outcome.summary_error is None

    summarize.assert_called_once()
    called_case, called_entries = summarize.call_args.args
    assert called_case.docket_id == "case-1"
    assert [e.id for e in called_entries] == ["case-1-e1", "case-1-e2"]

    notify_fire.assert_called_once_with(called_case, called_entries, fake_summary)
    assert state_store.load_case("case-1").seen_entry_ids == ["case-1-e1", "case-1-e2"]
    assert state_store.load_case("case-1").last_polled_at is not None


def test_no_new_entries_skips_summarize_and_notify_but_still_marks_polled(monkeypatch):
    _track("case-1", entry_ids=["e1"])  # already seen
    docket = _docket("case-1", ["e1"])  # fetch returns the identical entry
    source = FakeSource({"case-1": docket})

    summarize = MagicMock()
    notify_fire = MagicMock()
    monkeypatch.setattr(poll, "summarize_new_filings", summarize)
    monkeypatch.setattr(poll.notify, "fire", notify_fire)

    result = poll.run_poll_cycle(source=source)

    summarize.assert_not_called()
    notify_fire.assert_not_called()
    assert result.polled == ["case-1"]
    assert result.outcomes["case-1"].new_entries == []
    assert state_store.load_case("case-1").last_polled_at is not None  # the poll still happened


def test_summary_failure_does_not_block_notification_or_mark_seen(monkeypatch):
    from docketwatch.ai import SummaryError

    _track("case-1")
    docket = _docket("case-1", ["e1"])
    source = FakeSource({"case-1": docket})

    monkeypatch.setattr(poll, "summarize_new_filings", MagicMock(side_effect=SummaryError("chain broken")))
    notify_fire = MagicMock()
    monkeypatch.setattr(poll.notify, "fire", notify_fire)

    result = poll.run_poll_cycle(source=source)

    outcome = result.outcomes["case-1"]
    assert outcome.summary is None
    assert "chain broken" in outcome.summary_error
    notify_fire.assert_called_once()
    assert notify_fire.call_args.args[2] is None  # notified with no summary
    assert state_store.load_case("case-1").seen_entry_ids == ["case-1-e1"]


def test_fetch_error_on_one_case_does_not_stop_the_cycle(monkeypatch):
    _track("case-1")
    _track("case-2")
    source = FakeSource({"case-2": _docket("case-2", ["e1"])}, fail_on=["case-1"])
    monkeypatch.setattr(poll, "summarize_new_filings", MagicMock(return_value=None))
    monkeypatch.setattr(poll.notify, "fire", MagicMock())

    result = poll.run_poll_cycle(source=source)

    assert result.outcomes["case-1"].error is not None
    assert "simulated failure" in result.outcomes["case-1"].error
    assert "case-1" not in result.polled
    assert "case-2" in result.polled
    # A failed fetch never marks anything seen for that case.
    assert state_store.load_case("case-1").seen_entry_ids == []


# -- budget guard ---------------------------------------------------------------


def test_budget_guard_stops_the_cycle_and_reports_skipped(tmp_path, monkeypatch):
    _track("case-1")
    _track("case-2")
    _track("case-3")
    budget = Budget(directory=tmp_path)  # fresh: 5/min, 50/hour, 125/day
    dockets = {f"case-{i}": _docket(f"case-{i}", ["e1"]) for i in (1, 2, 3)}
    source = FakeSource(dockets, budget=budget, uses_budget=True)  # each fetch consumes 2 of the 5/minute
    monkeypatch.setattr(poll, "summarize_new_filings", MagicMock(return_value=None))
    monkeypatch.setattr(poll.notify, "fire", MagicMock())

    result = poll.run_poll_cycle(source=source, budget=budget)

    # 5/minute budget, 2 per case: case-1 and case-2 fit (4 used, 1 left),
    # case-3 needs 2 more and only 1 remains -- it gets skipped, not errored.
    assert result.polled == ["case-1", "case-2"]
    assert result.skipped == ["case-3"]
    assert result.budget_stopped is True
    assert "case-3" not in result.outcomes
    assert source.call_log == ["fetch:case-1", "fetch:case-2"]  # case-3 was never even fetched


def test_budget_already_exhausted_skips_everything_and_exits_cleanly(tmp_path, monkeypatch):
    _track("case-1")
    _track("case-2")
    budget = Budget(directory=tmp_path)
    # Pre-spend the minute window down to 1 remaining (< the 2-per-case cost).
    for _ in range(4):
        budget.consume()

    source = FakeSource({}, budget=budget, uses_budget=True)  # would raise KeyError if ever asked to fetch
    monkeypatch.setattr(poll, "summarize_new_filings", MagicMock())
    monkeypatch.setattr(poll.notify, "fire", MagicMock())

    result = poll.run_poll_cycle(source=source, budget=budget)

    assert result.polled == []
    assert result.skipped == ["case-1", "case-2"]
    assert result.budget_stopped is True
    assert source.call_log == []  # the guard fired before any fetch was attempted


def test_run_poll_cycle_accepts_a_docket_id_filter(monkeypatch):
    _track("case-1")
    _track("case-2")
    source = FakeSource({"case-1": _docket("case-1", ["e1"])})
    monkeypatch.setattr(poll, "summarize_new_filings", MagicMock(return_value=None))
    monkeypatch.setattr(poll.notify, "fire", MagicMock())

    result = poll.run_poll_cycle(docket_ids=["case-1"], source=source)

    assert result.polled == ["case-1"]
    assert "case-2" not in result.outcomes
