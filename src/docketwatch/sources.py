"""Where docket records come from.

`DocketSource` is the seam, same shape as permitpulse's `DataSource`:

* `FixtureSource` -- what runs today. Two hand-authored sample dockets
  committed to this repo. Fictional cases, labelled as such in every record's
  `source`/`note`.
* `CourtListenerSource` -- the live CourtListener/RECAP v4 REST API. Needs
  `COURTLISTENER_API_TOKEN`; there is no anonymous access at all (confirmed,
  see `spike_courtlistener_auth.py`).

`get_source()` picks between them on `DOCKETWATCH_LIVE=1`.

Rate limiting is not optional here. The free tier allows 5 requests/minute,
50/hour and 125/day, so every live call goes through `Budget`, which persists
its request log to `~/.docketwatch/budget_<YYYY-MM-DD>.json` and refuses to
spend past a cap rather than letting CourtListener throttle us. There is no
retry loop anywhere in this file, by design.

Polling only. Webhooks need a paid arrangement and are not built toward.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from docketwatch.models import SOURCE_COURTLISTENER, SOURCE_FIXTURE, Docket
from docketwatch.state_store import CACHE_DIR

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
API_BASE = "https://www.courtlistener.com/api/rest/v4"
TOKEN_ENV = "COURTLISTENER_API_TOKEN"

# Free-tier caps, confirmed against CourtListener's published limits.
LIMIT_PER_MINUTE = 5
LIMIT_PER_HOUR = 50
LIMIT_PER_DAY = 125


class DocketDataError(Exception):
    """Raised when a source cannot produce docket records."""


class RateBudgetError(DocketDataError):
    """Raised when a live call would exceed the free-tier rate limit."""


# -- rate budget --------------------------------------------------------------


class Budget:
    """A persisted count of live API calls, enforcing the free-tier caps.

    Stores unix timestamps of every request made today in
    `~/.docketwatch/budget_<YYYY-MM-DD>.json`. Yesterday's file is simply left
    alone -- a new day gets a new file, which is the daily reset.
    """

    def __init__(self, directory: Path = CACHE_DIR) -> None:
        self.directory = directory

    def _path(self, now: float) -> Path:
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        return self.directory / f"budget_{day}.json"

    def _load(self, now: float) -> List[float]:
        path = self._path(now)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise DocketDataError(f"The budget file at {path} is unreadable ({exc}). Delete it.") from exc
        return [float(t) for t in data.get("requests", [])]

    def status(self, now: Optional[float] = None) -> Dict[str, Any]:
        """How much of each window is spent right now. Read-only."""
        now = time.time() if now is None else now
        stamps = self._load(now)
        used = {
            "minute": sum(1 for t in stamps if now - t < 60),
            "hour": sum(1 for t in stamps if now - t < 3600),
            "day": len(stamps),
        }
        return {
            "used": used,
            "limits": {"minute": LIMIT_PER_MINUTE, "hour": LIMIT_PER_HOUR, "day": LIMIT_PER_DAY},
            "remaining": {
                "minute": max(0, LIMIT_PER_MINUTE - used["minute"]),
                "hour": max(0, LIMIT_PER_HOUR - used["hour"]),
                "day": max(0, LIMIT_PER_DAY - used["day"]),
            },
            "budget_file": str(self._path(now)),
        }

    def consume(self, now: Optional[float] = None) -> None:
        """Record one request, or raise `RateBudgetError` if it would exceed a cap."""
        now = time.time() if now is None else now
        # ponytail: a single flock around read-check-write closes the two-process
        # race (interactive run overlapping a cron run) where both read the same
        # stamp list, both see room, and the second writer clobbers the first's
        # count. Per-window sharding would scale further; not needed for a
        # single-user local CLI. Unix-only (fine -- this ships for macOS/launchd).
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.directory / ".budget.lock"
        with open(lock_path, "a+") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                state = self.status(now)
                for window, wait in (("minute", "a minute"), ("hour", "an hour"), ("day", "tomorrow")):
                    if state["remaining"][window] == 0:
                        raise RateBudgetError(
                            f"CourtListener free-tier {window} limit reached "
                            f"({state['used'][window]}/{state['limits'][window]}). Wait {wait}. "
                            "No request was sent."
                        )
                stamps = self._load(now) + [now]
                path = self._path(now)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"requests": stamps}, indent=1))
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)


# -- the seam -----------------------------------------------------------------


class DocketSource(ABC):
    """A place docket records can be read from."""

    source_id: str = "unknown"
    #: True only for a source that spends real, rate-limited requests --
    #: `run_poll_cycle` only consults `Budget` when this is true, so fixture
    #: runs are never gated by a live run's leftover request log.
    uses_budget: bool = False

    @abstractmethod
    def fetch_docket(self, docket_id: str) -> Docket:
        """Return the docket and its entries, or raise `DocketDataError`."""

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> List[Docket]:
        """Return dockets matching `query` (captions only, no entries)."""


# -- fixture source -----------------------------------------------------------


class FixtureSource(DocketSource):
    """Two hand-authored sample dockets committed to this repo.

    These are FICTIONAL cases. No party, judge, docket number or filing in
    them is real. They exist so the CLI, the web app and the tests all have
    something to run against without a CourtListener token.
    """

    source_id = SOURCE_FIXTURE

    def __init__(self, directory: Path = FIXTURE_DIR) -> None:
        self.directory = directory

    def _load_all(self) -> List[Docket]:
        paths = sorted(self.directory.glob("sample_docket_*.json"))
        if not paths:
            raise DocketDataError(f"No sample fixtures found in {self.directory}.")
        dockets = []
        for path in paths:
            try:
                dockets.append(Docket.from_dict(json.loads(path.read_text())))
            except (OSError, ValueError) as exc:
                raise DocketDataError(f"The fixture at {path} is unreadable: {exc}") from exc
        return dockets

    def fetch_docket(self, docket_id: str) -> Docket:
        for docket in self._load_all():
            if docket.id == docket_id:
                return docket
        available = ", ".join(d.id for d in self._load_all())
        raise DocketDataError(
            f"No fixture docket with id '{docket_id}'. Available fixtures: {available}. "
            f"For a real case set {TOKEN_ENV} and DOCKETWATCH_LIVE=1."
        )

    def search(self, query: str, limit: int = 10) -> List[Docket]:
        needle = (query or "").lower().strip()
        if not needle:
            raise DocketDataError("Search needs a non-empty query.")
        hits = [
            d
            for d in self._load_all()
            if needle in d.case_name.lower()
            or needle in d.docket_number.lower()
            or needle in d.court.lower()
        ]
        return hits[:limit]


# -- live CourtListener source -------------------------------------------------


class CourtListenerSource(DocketSource):
    """The live CourtListener/RECAP v4 REST API.

    Auth is mandatory -- `Authorization: Token <token>`. A live GET *and* a
    live OPTIONS against `/dockets/` with no token both returned
    `{"detail":"Authentication credentials were not provided."}`, so there is
    no anonymous discovery path and nothing about the response shape below
    could be verified without a token. Response-field mapping is marked
    ASSUMED where that is the case.

    Every request passes through `Budget` first. One call in, one call out --
    no retries, no backoff loop, no pagination past `limit`.
    """

    source_id = SOURCE_COURTLISTENER
    uses_budget = True

    def __init__(
        self,
        token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        budget: Optional[Budget] = None,
    ) -> None:
        self.token = token or os.environ.get(TOKEN_ENV, "")
        if not self.token:
            raise DocketDataError(
                f"{TOKEN_ENV} is not set. CourtListener allows no anonymous access at all -- "
                "sign up free at https://www.courtlistener.com/ , create a token, and export it. "
                "Until then run without DOCKETWATCH_LIVE=1 to use the sample fixtures."
            )
        self._session = session or requests.Session()
        self._budget = budget or Budget()

    @staticmethod
    def token_present() -> bool:
        return bool(os.environ.get(TOKEN_ENV))

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._budget.consume()  # raises before any socket is opened
        url = f"{API_BASE}/{path.lstrip('/')}"
        headers = {"Authorization": f"Token {self.token}", "Accept": "application/json"}
        try:
            resp = self._session.get(url, params=params or {}, headers=headers, timeout=60)
        except requests.RequestException as exc:
            raise DocketDataError(f"Could not reach CourtListener: {exc}") from exc
        if resp.status_code == 401:
            raise DocketDataError(f"CourtListener rejected the token in {TOKEN_ENV} (HTTP 401).")
        if resp.status_code == 429:
            raise RateBudgetError(
                "CourtListener rate-limited this request (HTTP 429). The free tier is "
                f"{LIMIT_PER_MINUTE}/min, {LIMIT_PER_HOUR}/hour, {LIMIT_PER_DAY}/day. Not retrying."
            )
        try:
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise DocketDataError(
                f"CourtListener response was unusable (HTTP {resp.status_code}): {exc}"
            ) from exc
        if not isinstance(body, dict):
            raise DocketDataError(f"CourtListener returned an unexpected shape: {body!r}")
        return body

    # -- response mapping. ASSUMED field names, verify once a token exists. --

    def _docket_from_api(self, row: Dict[str, Any], entries: Optional[List[Dict[str, Any]]] = None) -> Docket:
        docket_id = str(row.get("id") or "")
        if not docket_id:
            raise DocketDataError(f"CourtListener docket row has no id: {row!r}")
        note = (
            f"Fetched live from CourtListener {API_BASE}/dockets/{docket_id}/ on "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}. Field mapping is ASSUMED "
            "(see spike_courtlistener_auth.py) -- verify against a real response."
        )
        return Docket(
            id=docket_id,
            case_name=row.get("case_name") or "",
            docket_number=row.get("docket_number") or "",
            court=row.get("court") or row.get("court_id") or "",
            court_id=str(row.get("court_id") or ""),
            date_filed=(row.get("date_filed") or "")[:10],
            date_terminated=(row.get("date_terminated") or "")[:10] or None,
            assigned_to=row.get("assigned_to_str") or "",
            nature_of_suit=row.get("nature_of_suit") or "",
            cause=row.get("cause") or "",
            absolute_url=row.get("absolute_url") or "",
            parties=list(row.get("parties") or []),
            entries=[self._entry_from_api(e, docket_id) for e in (entries or [])],
            fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            source=SOURCE_COURTLISTENER,
            note=note,
        )

    def _entry_from_api(self, row: Dict[str, Any], docket_id: str) -> Any:
        from docketwatch.models import DocketEntry, RECAPDocument

        entry_id = str(row.get("id") or "")
        if not entry_id:
            raise DocketDataError(f"CourtListener docket entry has no id: {row!r}")
        docs = [
            RECAPDocument(
                id=str(d.get("id") or ""),
                document_number=str(d.get("document_number") or ""),
                attachment_number=d.get("attachment_number"),
                description=d.get("description") or "",
                page_count=d.get("page_count"),
                is_available=bool(d.get("is_available")),
                file_url=d.get("filepath_local") or None,
                source=SOURCE_COURTLISTENER,
                note="Fetched live from CourtListener. Field mapping ASSUMED.",
            )
            for d in (row.get("recap_documents") or [])
            if d.get("id")
        ]
        return DocketEntry(
            id=entry_id,
            docket_id=docket_id,
            date_filed=(row.get("date_filed") or "")[:10],
            entry_number=row.get("entry_number"),
            description=row.get("description") or "",
            filed_by="",  # not in the docket-entries payload; ASSUMED absent
            recap_documents=docs,
            source=SOURCE_COURTLISTENER,
            note="Fetched live from CourtListener. Field mapping ASSUMED.",
        )

    # -- the two source methods ------------------------------------------

    def fetch_docket(self, docket_id: str) -> Docket:
        """Two requests: the docket, then its entries. Costs 2 of the 5/minute."""
        row = self._get(f"dockets/{docket_id}/")
        entries_body = self._get("docket-entries/", {"docket": docket_id, "order_by": "recap_sequence_number"})
        return self._docket_from_api(row, entries_body.get("results") or [])

    def search(self, query: str, limit: int = 10) -> List[Docket]:
        """One request against `/dockets/` by case name. Costs 1."""
        if not (query or "").strip():
            raise DocketDataError("Search needs a non-empty query.")
        body = self._get("dockets/", {"case_name__icontains": query, "page_size": min(limit, 20)})
        return [self._docket_from_api(r) for r in (body.get("results") or [])][:limit]


# -- source selection ---------------------------------------------------------


def live_enabled() -> bool:
    return os.environ.get("DOCKETWATCH_LIVE") == "1"


def get_source() -> DocketSource:
    """Live CourtListener if `DOCKETWATCH_LIVE=1`, otherwise the fixtures.

    Live mode with no token raises rather than quietly serving fixture data
    and calling it real -- a silent downgrade is exactly the kind of invented
    answer this project refuses to give.
    """
    return CourtListenerSource() if live_enabled() else FixtureSource()
