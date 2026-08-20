"""Builds the daily digest markdown. No model, on purpose.

A digest is a list of what got filed. Every line in it is a fact copied from a
docket entry, so there is nothing here for an LLM to add that wouldn't be an
invention. `ai.py` handles the parts where a model genuinely helps.

Shared by `cli.digest` and `GET /api/cases/{id}/digest`, so both render the
identical text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List, Optional

from docketwatch.models import DocketEntry, TrackedCase


def _group(entries: Iterable[DocketEntry]) -> List[str]:
    lines = []
    for entry in entries:
        number = f"#{entry.entry_number}" if entry.entry_number is not None else "#-"
        docs = len(entry.recap_documents)
        suffix = f" ({docs} document{'s' if docs != 1 else ''})" if docs else ""
        lines.append(f"- **{entry.date_filed}** {number} _{entry.entry_type}_ — {entry.description}{suffix}")
    return lines


def case_digest(case: TrackedCase, entries: Optional[List[DocketEntry]] = None) -> str:
    """Markdown for one case. `entries` defaults to the whole cached timeline."""
    entries = case.entries if entries is None else entries
    header = [
        f"## {case.case_name or case.docket_id}",
        "",
        f"`{case.docket_number}` · {case.court} · docket id `{case.docket_id}`",
        f"Source: **{case.source}** — {case.note}" if case.note else f"Source: **{case.source}**",
        "",
    ]
    if not entries:
        return "\n".join(header + ["_No docket entries cached yet. Run `docketwatch fetch`._", ""])
    return "\n".join(header + _group(entries) + [""])


def daily_digest(cases: List[TrackedCase], new_by_case: Optional[dict] = None) -> str:
    """Markdown covering every tracked case.

    `new_by_case` maps docket_id -> list of new entries; when given, only
    those entries are listed and a case with nothing new says so, which is
    what `docketwatch poll --digest` wants.
    """
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = [f"# DocketWatch digest — {day}", ""]
    if not cases:
        return "\n".join(out + ["_No cases are being tracked. Run `docketwatch track <docket_id>`._", ""])

    total_new = 0
    for case in cases:
        if new_by_case is None:
            out.append(case_digest(case))
            continue
        new = new_by_case.get(case.docket_id, [])
        total_new += len(new)
        if new:
            out.append(case_digest(case, new))
        else:
            out.extend([f"## {case.case_name or case.docket_id}", "", "_Nothing new._", ""])
    if new_by_case is not None:
        out.insert(1, f"{total_new} new entr{'y' if total_new == 1 else 'ies'} across {len(cases)} case(s).")
        out.insert(2, "")
    return "\n".join(out)
