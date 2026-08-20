"""The record shapes DocketWatch passes around.

Four dataclasses -- `RECAPDocument`, `DocketEntry`, `Docket`, `TrackedCase` --
each with `to_dict()`/`from_dict()` and a `source`/`note` provenance pair.
`to_dict()` is the wire format: the CLI's `--json`, the cached JSON under
`~/.docketwatch/`, and every `/api/` response body all emit exactly these
keys, so the frontend can code against this file.

Provenance, house rules:

* `source` is `"fixture"` (hand-authored sample data committed to this repo)
  or `"courtlistener"` (fetched live from the v4 REST API). Nothing else.
* `note` is plain English about where the record came from. It is never
  empty on a record that reached a user.

Field names marked "assumed" in comments below are NOT confirmed against the
real API -- see `spike_courtlistener_auth.py`. CourtListener refuses
anonymous requests entirely, so no field name can be verified until a token
exists. Nothing here invents a *value*; the shapes are a best guess at the
API's own naming and are flagged as such.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

#: The only two legal values of a `source` field, anywhere in this project.
SOURCE_FIXTURE = "fixture"
SOURCE_COURTLISTENER = "courtlistener"
VALID_SOURCES = (SOURCE_FIXTURE, SOURCE_COURTLISTENER)

#: The closed set `classify_entry_type` returns.
ENTRY_TYPES = ("motion", "order", "notice", "opinion", "stipulation", "other")

# A PACER entry leads with the kind of document it is and only then names
# whatever it acts on, so the EARLIEST keyword wins -- that one rule settles
# both "ORDER granting 14 Motion to Dismiss" (an order) and "STIPULATION and
# Proposed Order" (a stipulation), which no fixed priority ordering can.
# This tuple's order is only the tie-break when two keywords start at the
# same offset, and it runs most- to least-specific for that case.
_TYPE_KEYWORDS = (
    ("opinion", ("opinion", "memorandum decision", "findings of fact")),
    ("order", ("order", "judgment", "decree", "granting", "denying")),
    ("stipulation", ("stipulation", "stipulated", "joint agreement")),
    ("motion", ("motion", "petition to", "application for", "moves to")),
    ("notice", ("notice", "notification", "summons", "certificate of service", "praecipe")),
)


def classify_entry_type(description: Optional[str]) -> str:
    """Bucket a docket-entry description into one of `ENTRY_TYPES`.

    Pure and deterministic -- keyword matching on the description text, no
    network and no model. Whole-word matching, so "notice" does not fire on
    "unnoticed". The earliest-starting keyword wins, because a docket entry
    names its own type first and whatever it acts on second. Returns "other"
    when nothing matches, never a guess.

    >>> classify_entry_type("ORDER granting 14 Motion to Dismiss")
    'order'
    >>> classify_entry_type("STIPULATION and Proposed Order Extending Time")
    'stipulation'
    >>> classify_entry_type("MOTION to Dismiss for Failure to State a Claim")
    'motion'
    >>> classify_entry_type("")
    'other'
    """
    text = (description or "").lower()
    if not text.strip():
        return "other"
    best_pos, best_type = len(text), "other"
    for entry_type, keywords in _TYPE_KEYWORDS:
        for keyword in keywords:
            match = re.search(r"\b" + re.escape(keyword) + r"\b", text)
            # Strictly-earlier only, so an equal start keeps the earlier
            # (more specific) rank -- "opinion and order" stays an opinion.
            if match and match.start() < best_pos:
                best_pos, best_type = match.start(), entry_type
    return best_type


def _provenance(data: Dict[str, Any]) -> Dict[str, str]:
    """Pull `source`/`note` off a dict, defaulting loudly rather than silently."""
    source = data.get("source") or "unknown"
    return {"source": source, "note": data.get("note") or ""}


@dataclass
class RECAPDocument:
    """One PDF (or unavailable placeholder) hanging off a docket entry.

    Field names follow CourtListener's v4 `recap_documents` sub-objects.
    ASSUMED, verify once a token exists.
    """

    id: str
    document_number: str = ""
    attachment_number: Optional[int] = None
    description: str = ""
    page_count: Optional[int] = None
    #: False means RECAP has the metadata but not the PDF -- it is NOT free to read.
    is_available: bool = False
    file_url: Optional[str] = None
    source: str = SOURCE_FIXTURE
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RECAPDocument":
        if not data.get("id"):
            raise ValueError("RECAPDocument is missing its id")
        known = {f: data.get(f) for f in cls.__dataclass_fields__ if f in data}
        known["id"] = str(known["id"])
        known.update(_provenance(data))
        return cls(**known)


@dataclass
class DocketEntry:
    """One numbered line on a docket: what was filed, when, by whom.

    `id` is the stable dedup key -- `state_store.diff()` compares on it and
    nothing else. It is CourtListener's own docket-entry id where one exists.
    `entry_type` is derived locally by `classify_entry_type`, never sent by
    the API.
    """

    id: str
    docket_id: str
    date_filed: str = ""  # YYYY-MM-DD
    entry_number: Optional[int] = None
    description: str = ""
    entry_type: str = "other"
    filed_by: str = ""
    recap_documents: List[RECAPDocument] = field(default_factory=list)
    source: str = SOURCE_FIXTURE
    note: str = ""

    def __post_init__(self) -> None:
        # Classify on construction so no consumer has to remember to; an
        # explicit non-default entry_type from cached JSON is left alone.
        if self.entry_type in ("", "other", None):
            self.entry_type = classify_entry_type(self.description)

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "recap_documents": [d.to_dict() for d in self.recap_documents],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DocketEntry":
        if not data.get("id"):
            raise ValueError("DocketEntry is missing its id -- it cannot be deduped without one")
        known = {f: data.get(f) for f in cls.__dataclass_fields__ if f in data}
        known["id"] = str(known["id"])
        known["docket_id"] = str(known.get("docket_id") or "")
        known["recap_documents"] = [
            RECAPDocument.from_dict(d) for d in (data.get("recap_documents") or [])
        ]
        known.update(_provenance(data))
        return cls(**known)


@dataclass
class Docket:
    """A case: its caption, court, and the entries fetched so far.

    `parties` is a list of plain dicts (`{"name", "type", "attorneys": [...]}`)
    rather than a dataclass -- party/attorney structure varies enough between
    courts that pinning it down before seeing real API output would be
    inventing a shape. ASSUMED, verify once a token exists.
    """

    id: str
    case_name: str = ""
    docket_number: str = ""
    court: str = ""  # human-readable, e.g. "N.D. Cal."
    court_id: str = ""  # CourtListener slug, e.g. "cand"
    date_filed: str = ""  # YYYY-MM-DD
    date_terminated: Optional[str] = None
    assigned_to: str = ""
    nature_of_suit: str = ""
    cause: str = ""
    absolute_url: str = ""
    parties: List[Dict[str, Any]] = field(default_factory=list)
    entries: List[DocketEntry] = field(default_factory=list)
    fetched_at: str = ""  # ISO-8601 Z
    source: str = SOURCE_FIXTURE
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Docket":
        if not data.get("id"):
            raise ValueError("Docket is missing its id")
        known = {f: data.get(f) for f in cls.__dataclass_fields__ if f in data}
        known["id"] = str(known["id"])
        known["parties"] = list(data.get("parties") or [])
        known["entries"] = [DocketEntry.from_dict(e) for e in (data.get("entries") or [])]
        known.update(_provenance(data))
        return cls(**known)


@dataclass
class TrackedCase:
    """A case the user asked to watch, plus the dedup state for it.

    `seen_entry_ids` is the whole point: it is what makes the second poll of
    an unchanged docket report zero new entries. `state_store` owns writing
    it; nothing else should.
    """

    docket_id: str
    case_name: str = ""
    docket_number: str = ""
    court: str = ""
    tracked_at: str = ""  # ISO-8601 Z
    last_polled_at: Optional[str] = None
    seen_entry_ids: List[str] = field(default_factory=list)
    entries: List[DocketEntry] = field(default_factory=list)
    source: str = SOURCE_FIXTURE
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "seen_entry_ids": list(self.seen_entry_ids),
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrackedCase":
        if not data.get("docket_id"):
            raise ValueError("TrackedCase is missing its docket_id")
        known = {f: data.get(f) for f in cls.__dataclass_fields__ if f in data}
        known["docket_id"] = str(known["docket_id"])
        known["seen_entry_ids"] = [str(i) for i in (data.get("seen_entry_ids") or [])]
        known["entries"] = [DocketEntry.from_dict(e) for e in (data.get("entries") or [])]
        known.update(_provenance(data))
        return cls(**known)
