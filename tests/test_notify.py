"""Notification tests. `subprocess.run` is always mocked -- this sandbox may
not have a working `osascript` binary, and `fire()` must behave identically
whether or not one exists: the OS notification is best-effort and swallows
everything, the markdown digest write is not.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from llm_ladder.digest import LensResult

from docketwatch import notify, state_store
from docketwatch.models import DocketEntry, TrackedCase

CASE = TrackedCase(
    docket_id="fixture-alpha-1",
    case_name="Redwood Analytics, Inc. v. Harbor Systems LLC",
    docket_number="3:26-cv-00417",
    court="N.D. Cal.",
    source="fixture",
)

ENTRIES = [
    DocketEntry(
        id="fixture-alpha-e9",
        docket_id="fixture-alpha-1",
        date_filed="2026-05-02",
        entry_number=9,
        description="ORDER setting Case Management Conference for 2026-06-15.",
        source="fixture",
    )
]


@pytest.fixture(autouse=True)
def isolated_digests(tmp_path, monkeypatch):
    """Point notify at a tmp digests dir -- never the real ~/.docketwatch."""
    monkeypatch.setattr(state_store, "DIGESTS_DIR", tmp_path / "digests")
    return tmp_path


def test_fire_writes_the_digest_and_calls_osascript(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr(notify.subprocess, "run", run)

    notify.fire(CASE, ENTRIES, None)

    run.assert_called_once()
    args = run.call_args.args[0]
    assert args[0] == "osascript"
    assert "-e" in args

    path = state_store.digest_path()
    text = path.read_text()
    assert CASE.case_name in text
    assert "1 new filing" in text
    assert "Case Management Conference" in text
    assert "no AI summary" in text


def test_fire_survives_a_broken_osascript_binary(monkeypatch):
    """subprocess.run blows up exactly like a missing binary would -- fire() must not raise."""
    monkeypatch.setattr(notify.subprocess, "run", MagicMock(side_effect=FileNotFoundError("no osascript")))

    notify.fire(CASE, ENTRIES, None)  # must not raise

    # The digest write is not best-effort -- it still happened.
    assert state_store.digest_path().is_file()


def test_fire_renders_an_ai_verdict_when_one_is_given(monkeypatch):
    monkeypatch.setattr(notify.subprocess, "run", MagicMock())
    summary = LensResult(
        lens_verdict="2 of 2 models agree that a hearing was scheduled.",
        consensus=["Both models flagged the new hearing date."],
        disagreements=["model-a thought it was routine; model-b flagged it as significant."],
    )

    notify.fire(CASE, ENTRIES, summary)

    text = state_store.digest_path().read_text()
    assert "VERDICT: 2 of 2 models agree" in text
    assert "CONSENSUS:" in text
    assert "hearing date" in text
    assert "DISAGREEMENTS:" in text


def test_fire_explains_a_skipped_verdict_via_the_note(monkeypatch):
    monkeypatch.setattr(notify.subprocess, "run", MagicMock())
    summary = LensResult(note="needs >=2 distinct models to compare")

    notify.fire(CASE, ENTRIES, summary)

    text = state_store.digest_path().read_text()
    assert "needs >=2 distinct models to compare" in text


def test_fire_appends_across_multiple_calls(monkeypatch):
    monkeypatch.setattr(notify.subprocess, "run", MagicMock())
    notify.fire(CASE, ENTRIES, None)
    notify.fire(CASE, ENTRIES, None)
    text = state_store.digest_path().read_text()
    assert text.count(CASE.case_name) == 2
