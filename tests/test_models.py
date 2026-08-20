"""Model shapes, provenance, and entry classification.

`to_dict()` is a published contract -- the CLI's `--json`, the cache files and
every `/api/` response emit it verbatim -- so these tests assert on exact key
sets, not just round-tripping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docketwatch.models import (
    ENTRY_TYPES,
    Docket,
    DocketEntry,
    RECAPDocument,
    TrackedCase,
    classify_entry_type,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "src" / "docketwatch" / "fixtures"


# -- classify_entry_type ------------------------------------------------------


@pytest.mark.parametrize(
    "description,expected",
    [
        ("MOTION to Dismiss for Failure to State a Claim", "motion"),
        ("ORDER granting 5 Stipulation to Extend Fact Discovery", "order"),
        ("MEMORANDUM OPINION AND ORDER denying 3 Motion to Dismiss", "opinion"),
        ("NOTICE of Appearance by Cleo Barrington", "notice"),
        ("STIPULATION and Proposed Order Extending Time", "stipulation"),
        ("COMPLAINT for Trademark Infringement", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_classify_entry_type(description, expected):
    assert classify_entry_type(description) == expected


def test_classify_is_case_insensitive_and_closed():
    for text in ("order granting X", "ORDER GRANTING X", "OrDeR granting X"):
        assert classify_entry_type(text) == "order"
    assert classify_entry_type("something entirely unrelated") in ENTRY_TYPES


def test_order_beats_motion_when_both_words_appear():
    # A clerk's entry for a ruling names the motion it rules on. It is still
    # a ruling, and mislabeling it would put the wrong icon on the timeline.
    assert classify_entry_type("ORDER granting 14 Motion to Dismiss") == "order"


def test_notice_does_not_fire_on_a_substring():
    assert classify_entry_type("Defendant went unnoticed by the clerk") == "other"


# -- to_dict() key contracts --------------------------------------------------


def test_recap_document_dict_shape():
    doc = RECAPDocument(id="d1", document_number="3", page_count=18, note="n")
    assert set(doc.to_dict()) == {
        "id", "document_number", "attachment_number", "description",
        "page_count", "is_available", "file_url", "source", "note",
    }
    assert RECAPDocument.from_dict(doc.to_dict()) == doc


def test_docket_entry_dict_shape_and_roundtrip():
    entry = DocketEntry(
        id="e1",
        docket_id="c1",
        date_filed="2026-02-09",
        entry_number=3,
        description="MOTION to Dismiss",
        recap_documents=[RECAPDocument(id="d1")],
        note="n",
    )
    payload = entry.to_dict()
    assert set(payload) == {
        "id", "docket_id", "date_filed", "entry_number", "description",
        "entry_type", "filed_by", "recap_documents", "source", "note",
    }
    assert payload["entry_type"] == "motion"  # classified on construction
    assert isinstance(payload["recap_documents"][0], dict)
    assert DocketEntry.from_dict(payload) == entry


def test_docket_dict_shape_and_roundtrip():
    docket = Docket(id="c1", case_name="A v. B", entries=[DocketEntry(id="e1", docket_id="c1")])
    payload = docket.to_dict()
    assert set(payload) == {
        "id", "case_name", "docket_number", "court", "court_id", "date_filed",
        "date_terminated", "assigned_to", "nature_of_suit", "cause",
        "absolute_url", "parties", "entries", "fetched_at", "source", "note",
    }
    assert Docket.from_dict(payload) == docket


def test_tracked_case_dict_shape_and_roundtrip():
    case = TrackedCase(docket_id="c1", seen_entry_ids=["e1"], entries=[DocketEntry(id="e1", docket_id="c1")])
    payload = case.to_dict()
    assert set(payload) == {
        "docket_id", "case_name", "docket_number", "court", "tracked_at",
        "last_polled_at", "seen_entry_ids", "entries", "source", "note",
    }
    assert TrackedCase.from_dict(payload) == case


def test_to_dict_is_json_serializable():
    docket = Docket(
        id="c1",
        entries=[DocketEntry(id="e1", docket_id="c1", recap_documents=[RECAPDocument(id="d1")])],
    )
    assert json.loads(json.dumps(docket.to_dict()))["entries"][0]["recap_documents"][0]["id"] == "d1"


# -- provenance / fails-loudly ------------------------------------------------


def test_missing_id_raises_rather_than_defaulting():
    for cls, payload in (
        (DocketEntry, {"docket_id": "c1"}),
        (Docket, {"case_name": "A v. B"}),
        (TrackedCase, {"case_name": "A v. B"}),
        (RECAPDocument, {"description": "x"}),
    ):
        with pytest.raises(ValueError):
            cls.from_dict(payload)


def test_unknown_source_is_labelled_unknown_not_guessed():
    entry = DocketEntry.from_dict({"id": "e1", "docket_id": "c1"})
    assert entry.source == "unknown"


# -- the shipped fixtures -----------------------------------------------------


@pytest.mark.parametrize("name", ["sample_docket_alpha.json", "sample_docket_beta.json"])
def test_fixture_parses_and_is_labelled_fictional(name):
    docket = Docket.from_dict(json.loads((FIXTURE_DIR / name).read_text()))
    assert docket.source == "fixture"
    assert "not a real case" in docket.note
    assert 5 <= len(docket.entries) <= 8
    assert docket.parties and all(p.get("attorneys") for p in docket.parties)
    for entry in docket.entries:
        assert entry.source == "fixture"
        assert entry.note
        assert entry.docket_id == docket.id
        assert entry.entry_type in ENTRY_TYPES
        for doc in entry.recap_documents:
            assert doc.source == "fixture"


def test_fixtures_span_several_weeks_and_vary_in_type():
    for name in ("sample_docket_alpha.json", "sample_docket_beta.json"):
        docket = Docket.from_dict(json.loads((FIXTURE_DIR / name).read_text()))
        dates = sorted(e.date_filed for e in docket.entries)
        assert dates[0] < dates[-1]
        assert len({e.entry_type for e in docket.entries}) >= 3
