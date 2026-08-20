"""Best-effort local notification for new docket filings.

`fire(case, new_entries, summary)` does two things:

1. Fires a native macOS notification via `osascript`, wrapped so ANY
   failure -- missing binary, no display, notifications disabled -- is
   swallowed. A broken notification must never break the poll pipeline (same
   "wrapped so it can't take anything else down" pattern as
   dynamo-task-watcher's `messaging.py`).
2. Appends a markdown entry to `~/.docketwatch/digests/<YYYY-MM-DD>.md`
   recording the case, how many new entries, and the AI summary (if any).
   This write is NOT best-effort -- an unwritable digests dir is a real
   problem and raises, same as every other write in this project.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import List, Optional

from docketwatch.models import DocketEntry, TrackedCase
from docketwatch.state_store import digest_path


def _applescript_quote(text: str) -> str:
    """Escape a string for embedding inside an AppleScript double-quoted literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _osascript_notify(title: str, body: str) -> None:
    """Best-effort. Any failure at all -- missing binary, no display, a
    timeout, notifications disabled -- is swallowed here and nowhere else."""
    try:
        script = (
            f'display notification "{_applescript_quote(body)}" '
            f'with title "{_applescript_quote(title)}"'
        )
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5, check=False)
    except Exception:
        pass


def _summary_text(summary: Optional[object]) -> str:
    """Render an `ai.summarize_new_filings` result (or None) as markdown."""
    if summary is None:
        return "(no AI summary -- see the filings above)"
    verdict = getattr(summary, "lens_verdict", None)
    if not verdict:
        note = getattr(summary, "note", None)
        return f"(no AI verdict -- {note})" if note else "(no AI summary)"
    lines = [f"VERDICT: {verdict}"]
    consensus = getattr(summary, "consensus", None) or []
    if consensus:
        lines.append("CONSENSUS:")
        lines += [f"- {c}" for c in consensus]
    disagreements = getattr(summary, "disagreements", None) or []
    if disagreements:
        lines.append("DISAGREEMENTS:")
        lines += [f"- {d}" for d in disagreements]
    return "\n".join(lines)


def fire(case: TrackedCase, new_entries: List[DocketEntry], summary: Optional[object] = None) -> None:
    """Announce `new_entries` on `case`: a native notification, then a digest line.

    Meant to be called only when `new_entries` is non-empty -- silence isn't
    news, so an empty list would fire an empty-handed notification.
    """
    label = case.case_name or case.docket_id
    count = len(new_entries)
    plural = "s" if count != 1 else ""
    _osascript_notify(f"DocketWatch: {label}", f"{count} new filing{plural}")

    path = digest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry_lines = "\n".join(
        f"- [{e.entry_number if e.entry_number is not None else '-'}] {e.date_filed} {e.description}"
        for e in new_entries
    )
    block = (
        f"\n## {label} -- {count} new filing{plural} ({stamp})\n\n"
        f"{entry_lines}\n\n"
        f"**AI summary:**\n{_summary_text(summary)}\n"
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)
