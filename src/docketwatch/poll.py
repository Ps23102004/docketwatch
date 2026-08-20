"""Ties fetch -> diff -> summarize -> notify -> mark_seen into one poll cycle.

`run_poll_cycle()` is what `docketwatch poll` calls. Each tracked case is
fetched from the active source (fixture or live CourtListener), diffed
against what's already been seen, and -- only when there's something new --
summarized by `ai.summarize_new_filings` and announced by `notify.fire`.
`mark_seen` only commits after that, so a crash mid-cycle never marks an
entry seen without having told anyone about it.

CourtListener's free tier is rate-limited (`sources.Budget`); `fetch_docket`
costs 2 requests (docket + entries). Before fetching each case this checks
the shared budget file and, if fewer than 2 requests remain in any window,
stops the cycle right there and reports every untouched case as `skipped`
rather than letting the source raise mid-loop. That is not an error -- it's
the budget doing its job -- so callers should treat `skipped` as routine and
exit 0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from docketwatch import notify, state_store
from docketwatch.ai import SummaryError, summarize_new_filings
from docketwatch.ai_backend import AIBackendError
from docketwatch.models import DocketEntry
from docketwatch.sources import Budget, DocketDataError, DocketSource, get_source
from docketwatch.state_store import StateStoreError

logger = logging.getLogger(__name__)

#: fetch_docket() issues one request for the docket, one for its entries.
COST_PER_CASE = 2


@dataclass
class CasePollOutcome:
    docket_id: str
    new_entries: List[DocketEntry] = field(default_factory=list)
    summary: Optional[object] = None  # ai.summarize_new_filings' LensResult, or None
    summary_error: Optional[str] = None
    error: Optional[str] = None


@dataclass
class PollCycleResult:
    polled: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    outcomes: Dict[str, CasePollOutcome] = field(default_factory=dict)
    budget_stopped: bool = False


def _budget_has_room(budget: Budget) -> bool:
    remaining = budget.status()["remaining"]
    return min(remaining.values()) >= COST_PER_CASE


def run_poll_cycle(
    docket_ids: Optional[List[str]] = None,
    source: Optional[DocketSource] = None,
    budget: Optional[Budget] = None,
) -> PollCycleResult:
    """Poll `docket_ids` (default: every tracked case), summarize and notify.

    A case with no new entries is polled and marked (`mark_seen` with an
    empty id list, which still stamps `last_polled_at`) but never summarized
    or notified -- silence is not news. A case whose fetch fails is recorded
    in `outcomes[...].error` and the cycle moves on to the next case. A
    summary call that fails (`SummaryError`/`AIBackendError`, e.g. Ollama is
    down) does not drop the notification -- the new filings are still real
    news -- it just goes out without an AI opinion, recorded in
    `summary_error`. A spent rate budget instead stops the whole cycle and
    reports the untouched cases as `skipped`.
    """
    source = source or get_source()
    budget = budget or Budget()

    cases = [state_store.load_case(d) for d in docket_ids] if docket_ids else state_store.list_cases()

    result = PollCycleResult()
    for case in cases:
        if not _budget_has_room(budget):
            result.skipped.append(case.docket_id)
            result.budget_stopped = True
            logger.info("budget guard: skipping %s, rate budget nearly spent", case.docket_id)
            continue

        outcome = CasePollOutcome(docket_id=case.docket_id)
        try:
            docket = source.fetch_docket(case.docket_id)
            new_entries = state_store.diff(case.docket_id, docket.entries)
            state_store.save_entries(case.docket_id, docket.entries)
        except (DocketDataError, StateStoreError) as exc:
            outcome.error = str(exc)
            result.outcomes[case.docket_id] = outcome
            continue

        outcome.new_entries = new_entries
        if new_entries:
            try:
                outcome.summary = summarize_new_filings(case, new_entries)
            except (SummaryError, AIBackendError) as exc:
                outcome.summary_error = str(exc)
            notify.fire(case, new_entries, outcome.summary)

        state_store.mark_seen(case.docket_id, [e.id for e in new_entries])
        result.polled.append(case.docket_id)
        result.outcomes[case.docket_id] = outcome

    return result
