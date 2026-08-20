"""The tracked-case registry and the dedup state behind it.

One JSON file per tracked case at `~/.docketwatch/cases/<docket_id>.json`,
holding a serialized `TrackedCase`. No database, no index file -- the
directory listing *is* the registry.

This module is the one every other module leans on, so its interface is meant
to stay put:

    track_case(docket_id, metadata)     -> TrackedCase
    untrack_case(docket_id)             -> bool
    list_cases()                        -> list[TrackedCase]
    load_case(docket_id)                -> TrackedCase
    diff(docket_id, fetched_entries)    -> list[DocketEntry]   (pure, no writes)
    mark_seen(docket_id, entry_ids)     -> TrackedCase          (writes)

`diff` and `mark_seen` are deliberately separate. `diff` answers "what's new?"
without side effects, so a caller can show the user new entries and only
commit them as seen once that succeeded. A poll is `diff` then `mark_seen`.

Dedup is on `DocketEntry.id` and nothing else -- not entry number, not date,
not description text. Entry numbers get reused across attachments and
descriptions get edited by clerks; the id is the only stable identity.

Fails loudly: a missing case raises `StateStoreError`, an unreadable file
raises `StateStoreError`. Nothing here ever returns invented data.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from docketwatch.models import DocketEntry, TrackedCase

CACHE_DIR = Path(os.environ.get("DOCKETWATCH_HOME") or (Path.home() / ".docketwatch"))
CASES_DIR = CACHE_DIR / "cases"
DIGESTS_DIR = CACHE_DIR / "digests"

# A docket id becomes a filename, so it is a trust boundary: only these
# characters, and never a path separator or a dot-segment.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class StateStoreError(Exception):
    """Raised when the tracked-case store cannot satisfy a request."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_id(docket_id: str) -> str:
    docket_id = str(docket_id or "").strip()
    if not _SAFE_ID.match(docket_id) or docket_id in (".", ".."):
        raise StateStoreError(
            f"'{docket_id}' is not a usable docket id -- letters, digits, dot, dash and "
            "underscore only, up to 128 characters."
        )
    return docket_id


def case_path(docket_id: str) -> Path:
    """Where `docket_id`'s state file lives. Validates the id."""
    return CASES_DIR / f"{_check_id(docket_id)}.json"


def _write(case: TrackedCase) -> TrackedCase:
    path = case_path(case.docket_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(case.to_dict(), indent=1))
    tmp.replace(path)  # atomic: a crash mid-write never leaves a half-file
    return case


def is_tracked(docket_id: str) -> bool:
    return case_path(docket_id).is_file()


def track_case(docket_id: str, metadata: Optional[Dict[str, Any]] = None) -> TrackedCase:
    """Start tracking `docket_id`, or refresh an already-tracked case's metadata.

    `metadata` carries the case's caption fields (`case_name`, `docket_number`,
    `court`, `source`, `note`, and optionally `entries`). Re-tracking never
    clears `seen_entry_ids` -- that would re-announce every old filing as new.
    """
    docket_id = _check_id(docket_id)
    metadata = dict(metadata or {})

    existing = load_case(docket_id) if is_tracked(docket_id) else None
    payload = {
        "docket_id": docket_id,
        "case_name": metadata.get("case_name", existing.case_name if existing else ""),
        "docket_number": metadata.get("docket_number", existing.docket_number if existing else ""),
        "court": metadata.get("court", existing.court if existing else ""),
        "tracked_at": existing.tracked_at if existing else _now(),
        "last_polled_at": existing.last_polled_at if existing else None,
        "seen_entry_ids": list(existing.seen_entry_ids) if existing else [],
        "entries": metadata.get("entries", [e.to_dict() for e in existing.entries] if existing else []),
        "source": metadata.get("source", existing.source if existing else "fixture"),
        "note": metadata.get("note", existing.note if existing else ""),
    }
    if payload["entries"] and isinstance(payload["entries"][0], DocketEntry):
        payload["entries"] = [e.to_dict() for e in payload["entries"]]
    return _write(TrackedCase.from_dict(payload))


def untrack_case(docket_id: str) -> bool:
    """Stop tracking. True if a case was removed, False if it wasn't tracked."""
    path = case_path(docket_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


def load_case(docket_id: str) -> TrackedCase:
    """Read one tracked case. Raises rather than returning an empty shell."""
    path = case_path(docket_id)
    if not path.is_file():
        raise StateStoreError(
            f"'{docket_id}' is not tracked. Run `docketwatch track {docket_id}` first."
        )
    try:
        return TrackedCase.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError) as exc:
        raise StateStoreError(
            f"The tracked-case file at {path} is unreadable ({exc}). Delete it and re-track."
        ) from exc


def list_cases() -> List[TrackedCase]:
    """Every tracked case, oldest-tracked first. Empty list means none tracked."""
    if not CASES_DIR.is_dir():
        return []
    cases = [load_case(p.stem) for p in sorted(CASES_DIR.glob("*.json"))]
    return sorted(cases, key=lambda c: (c.tracked_at, c.docket_id))


def diff(docket_id: str, fetched_entries: Iterable[Any]) -> List[DocketEntry]:
    """Entries in `fetched_entries` whose id isn't in the case's seen set.

    Pure: reads state, writes none. Accepts `DocketEntry` objects or raw
    dicts. Within one call, duplicate ids collapse to the first occurrence --
    a single fetch that lists an entry twice must not report it twice.
    Order is the order given, which for a docket is chronological.
    """
    seen = set(load_case(docket_id).seen_entry_ids)
    new: List[DocketEntry] = []
    for raw in fetched_entries:
        entry = raw if isinstance(raw, DocketEntry) else DocketEntry.from_dict(raw)
        if entry.id in seen:
            continue
        seen.add(entry.id)  # dedup within this batch too
        new.append(entry)
    return new


def mark_seen(docket_id: str, entry_ids: Iterable[str]) -> TrackedCase:
    """Record `entry_ids` as seen and stamp `last_polled_at`. Idempotent.

    Call it after a `diff` has been acted on. Passing ids already seen is
    harmless; passing an empty iterable still updates `last_polled_at`,
    which is the honest record that a poll happened and found nothing.
    """
    case = load_case(docket_id)
    seen = list(case.seen_entry_ids)
    known = set(seen)
    for entry_id in entry_ids:
        entry_id = str(entry_id)
        if entry_id not in known:
            known.add(entry_id)
            seen.append(entry_id)
    case.seen_entry_ids = seen
    case.last_polled_at = _now()
    return _write(case)


def save_entries(docket_id: str, entries: Iterable[Any]) -> TrackedCase:
    """Replace the case's cached entry list (the timeline the UI reads).

    Separate from `mark_seen` on purpose: caching what a docket looks like
    and recording what the user has been told about it are different facts.
    """
    case = load_case(docket_id)
    case.entries = [e if isinstance(e, DocketEntry) else DocketEntry.from_dict(e) for e in entries]
    return _write(case)


def digest_path(day: Optional[str] = None) -> Path:
    """`~/.docketwatch/digests/<YYYY-MM-DD>.md` for `day` (default: today, UTC)."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise StateStoreError(f"'{day}' is not a YYYY-MM-DD date.")
    return DIGESTS_DIR / f"{day}.md"


def append_digest(text: str, day: Optional[str] = None) -> Path:
    """Append `text` to today's digest file and return its path.

    Append, never overwrite: `notify.fire` writes a block into this same file
    every time it announces new filings, and that record is not reproducible
    from anything else. A `--write` render is, so the render is what gives
    way. Every writer of `digest_path()` goes through here.
    """
    path = digest_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    return path
