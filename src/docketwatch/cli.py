"""Command-line interface for DocketWatch.

Every data command takes `--json` and prints exactly the `to_dict()` shape
the web API serves, so a script and the frontend see identical bytes.

Fixture mode is the default. `DOCKETWATCH_LIVE=1` plus a
`COURTLISTENER_API_TOKEN` switches every fetch to the real API; without the
token, live mode raises rather than quietly serving sample data.
"""

from __future__ import annotations

import json as jsonlib
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from docketwatch import digest as digest_mod
from docketwatch import state_store
from docketwatch.ai import AIBackendError, answer_question, explain_entry
from docketwatch.models import Docket, DocketEntry, TrackedCase
from docketwatch.sources import Budget, DocketDataError, get_source, live_enabled
from docketwatch.state_store import StateStoreError

app = typer.Typer(help="Tracks federal court dockets and tells you what's new since you last looked.")
console = Console()
error_console = Console(stderr=True)


def _fail(exc: Exception) -> None:
    error_console.print(f"[bold red]Error:[/bold red] {exc}")
    raise typer.Exit(1)


def _emit(payload, json_out: bool) -> bool:
    """Print `payload` as JSON when `--json` was passed. True means we're done."""
    if json_out:
        print(jsonlib.dumps(payload, indent=2))
    return json_out


def _mode_note() -> str:
    return "live CourtListener" if live_enabled() else "sample fixtures (fictional cases)"


def _entries_table(entries: List[DocketEntry], title: str) -> Table:
    table = Table(title=title)
    table.add_column("#", justify="right", style="bold")
    table.add_column("Filed")
    table.add_column("Type")
    table.add_column("Description", max_width=72)
    table.add_column("Docs", justify="right")
    for e in entries:
        table.add_row(
            str(e.entry_number) if e.entry_number is not None else "—",
            e.date_filed or "—",
            e.entry_type,
            e.description,
            str(len(e.recap_documents)) or "0",
        )
    return table


def _fetch_docket(docket_id: str) -> Docket:
    return get_source().fetch_docket(docket_id)


@app.command()
def track(
    docket_id: str = typer.Argument(..., help="CourtListener docket id, or a fixture id"),
    json_out: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Start tracking a case, seeding it with the docket as it stands today."""
    try:
        docket = _fetch_docket(docket_id)
        case = state_store.track_case(
            docket_id,
            {
                "case_name": docket.case_name,
                "docket_number": docket.docket_number,
                "court": docket.court,
                "entries": [e.to_dict() for e in docket.entries],
                "source": docket.source,
                "note": docket.note,
            },
        )
    except (DocketDataError, StateStoreError) as exc:
        _fail(exc)
        return
    if _emit(case.to_dict(), json_out):
        return
    console.print(
        f"Tracking [bold]{case.case_name or case.docket_id}[/bold] "
        f"({case.docket_number}, {case.court}) — {len(case.entries)} entries cached, "
        f"0 marked seen. Run `docketwatch poll` to see what's new."
    )


@app.command()
def untrack(
    docket_id: str = typer.Argument(..., help="Docket id to stop tracking"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Stop tracking a case and delete its dedup state."""
    try:
        removed = state_store.untrack_case(docket_id)
    except StateStoreError as exc:
        _fail(exc)
        return
    if _emit({"docket_id": docket_id, "untracked": removed}, json_out):
        return
    console.print(f"Untracked [bold]{docket_id}[/bold]." if removed else f"'{docket_id}' was not tracked.")


@app.command()
def cases(json_out: bool = typer.Option(False, "--json")) -> None:
    """List every tracked case."""
    try:
        tracked = state_store.list_cases()
    except StateStoreError as exc:
        _fail(exc)
        return
    if _emit([c.to_dict() for c in tracked], json_out):
        return
    if not tracked:
        console.print("No cases tracked yet. Run `docketwatch track <docket_id>`.")
        return
    table = Table(title="Tracked cases")
    table.add_column("Docket id", style="bold")
    table.add_column("Case")
    table.add_column("Number")
    table.add_column("Court")
    table.add_column("Entries", justify="right")
    table.add_column("Seen", justify="right")
    table.add_column("Last polled")
    table.add_column("Source")
    for c in tracked:
        table.add_row(
            c.docket_id,
            c.case_name,
            c.docket_number,
            c.court,
            str(len(c.entries)),
            str(len(c.seen_entry_ids)),
            c.last_polled_at or "never",
            c.source,
        )
    console.print(table)


@app.command()
def fetch(
    docket_id: str = typer.Argument(..., help="Docket id to fetch"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Fetch a docket from the active source and cache it against the tracked case."""
    try:
        docket = _fetch_docket(docket_id)
        if state_store.is_tracked(docket_id):
            state_store.save_entries(docket_id, docket.entries)
    except (DocketDataError, StateStoreError) as exc:
        _fail(exc)
        return
    if _emit(docket.to_dict(), json_out):
        return
    console.print(Panel(f"[bold]{docket.case_name}[/bold]\n{docket.docket_number} · {docket.court}\n{docket.note}"))
    console.print(_entries_table(docket.entries, f"{len(docket.entries)} entries from {_mode_note()}"))


@app.command()
def timeline(
    docket_id: str = typer.Argument(..., help="Tracked docket id"),
    entry_type: Optional[str] = typer.Option(None, "--type", help="Filter: motion|order|notice|opinion|stipulation|other"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show a tracked case's cached docket entries, oldest first."""
    try:
        case = state_store.load_case(docket_id)
    except StateStoreError as exc:
        _fail(exc)
        return
    entries = [e for e in case.entries if not entry_type or e.entry_type == entry_type]
    if _emit([e.to_dict() for e in entries], json_out):
        return
    if not entries:
        console.print(f"No cached entries for '{docket_id}'. Run `docketwatch fetch {docket_id}`.")
        return
    console.print(_entries_table(entries, f"{case.case_name or docket_id} — {len(entries)} entries"))


@app.command()
def search(
    query: str = typer.Argument(..., help="Case name, docket number or court"),
    limit: int = typer.Option(10, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Search the active source for cases to track."""
    try:
        hits = get_source().search(query, limit=limit)
    except DocketDataError as exc:
        _fail(exc)
        return
    if _emit([d.to_dict() for d in hits], json_out):
        return
    if not hits:
        console.print(f"Nothing matched '{query}' in {_mode_note()}.")
        return
    table = Table(title=f"{len(hits)} match(es) in {_mode_note()}")
    table.add_column("Docket id", style="bold")
    table.add_column("Case")
    table.add_column("Number")
    table.add_column("Court")
    table.add_column("Filed")
    for d in hits:
        table.add_row(d.id, d.case_name, d.docket_number, d.court, d.date_filed)
    console.print(table)


@app.command()
def explain(
    docket_id: str = typer.Argument(..., help="Tracked docket id"),
    entry_id: str = typer.Argument(..., help="Docket entry id to explain"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Explain one docket entry in plain English (local Ollama)."""
    try:
        case = state_store.load_case(docket_id)
    except StateStoreError as exc:
        _fail(exc)
        return
    match = next((e for e in case.entries if e.id == entry_id), None)
    if match is None:
        _fail(StateStoreError(f"No entry '{entry_id}' cached on case '{docket_id}'."))
        return
    try:
        text = explain_entry(match, case)
    except AIBackendError as exc:
        _fail(exc)
        return
    if _emit({"entry": match.to_dict(), "explanation": text}, json_out):
        return
    console.print(Panel(text, title=f"#{match.entry_number} {match.entry_type} · {match.date_filed}"))


@app.command()
def ask(
    docket_id: str = typer.Argument(..., help="Tracked docket id"),
    question: str = typer.Argument(..., help="Question about the case"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Ask a question about a tracked case (local Ollama, docket text only)."""
    try:
        case = state_store.load_case(docket_id)
        answer = answer_question(case, question)
    except (StateStoreError, AIBackendError) as exc:
        _fail(exc)
        return
    if _emit({"docket_id": docket_id, "question": question, "answer": answer}, json_out):
        return
    console.print(Panel(answer, title=question))


@app.command()
def digest(
    docket_id: Optional[str] = typer.Argument(None, help="One case, or omit for all tracked cases"),
    write: bool = typer.Option(False, "--write", help="Also save to ~/.docketwatch/digests/<date>.md"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Render a markdown digest of tracked cases."""
    try:
        if docket_id:
            text = digest_mod.case_digest(state_store.load_case(docket_id))
        else:
            text = digest_mod.daily_digest(state_store.list_cases())
    except StateStoreError as exc:
        _fail(exc)
        return
    path = None
    if write:
        path = state_store.digest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if _emit({"markdown": text, "written_to": str(path) if path else None}, json_out):
        return
    console.print(text)
    if path:
        console.print(f"[dim]Written to {path}[/dim]")


@app.command()
def poll(
    docket_id: Optional[str] = typer.Argument(None, help="One case, or omit to poll every tracked case"),
    write: bool = typer.Option(False, "--write", help="Save the digest to ~/.docketwatch/digests/<date>.md"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Fetch each tracked case and report only entries not seen before.

    Live mode costs 2 API requests per case; with a 5/minute free-tier budget
    that is 2 cases a minute, and `sources.Budget` refuses the third rather
    than letting CourtListener throttle it.
    """
    try:
        targets = [state_store.load_case(docket_id)] if docket_id else state_store.list_cases()
    except StateStoreError as exc:
        _fail(exc)
        return
    if not targets:
        console.print("No cases tracked yet. Run `docketwatch track <docket_id>`.")
        return

    source = get_source()
    new_by_case, errors = {}, {}
    for case in targets:
        try:
            docket = source.fetch_docket(case.docket_id)
            new = state_store.diff(case.docket_id, docket.entries)
            state_store.save_entries(case.docket_id, docket.entries)
            state_store.mark_seen(case.docket_id, [e.id for e in new])
            new_by_case[case.docket_id] = new
        except (DocketDataError, StateStoreError) as exc:
            errors[case.docket_id] = str(exc)

    refreshed = [state_store.load_case(c.docket_id) for c in targets if c.docket_id not in errors]
    text = digest_mod.daily_digest(refreshed, {k: v for k, v in new_by_case.items()})
    path = None
    if write:
        path = state_store.digest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    if json_out:
        print(
            jsonlib.dumps(
                {
                    "polled": [c.docket_id for c in targets],
                    "new_entries": {k: [e.to_dict() for e in v] for k, v in new_by_case.items()},
                    "errors": errors,
                    "markdown": text,
                    "written_to": str(path) if path else None,
                },
                indent=2,
            )
        )
        return

    for case in targets:
        if case.docket_id in errors:
            error_console.print(f"[bold red]{case.docket_id}:[/bold red] {errors[case.docket_id]}")
            continue
        new = new_by_case[case.docket_id]
        label = case.case_name or case.docket_id
        if not new:
            console.print(f"[dim]{label}: nothing new.[/dim]")
        else:
            console.print(_entries_table(new, f"{label} — {len(new)} NEW"))
    if path:
        console.print(f"[dim]Digest written to {path}[/dim]")
    if errors:
        raise typer.Exit(1)


@app.command()
def budget(json_out: bool = typer.Option(False, "--json")) -> None:
    """How much of the CourtListener free-tier rate limit today has spent."""
    state = Budget().status()
    if _emit(state, json_out):
        return
    table = Table(title="CourtListener free-tier budget")
    table.add_column("Window")
    table.add_column("Used", justify="right")
    table.add_column("Limit", justify="right")
    table.add_column("Remaining", justify="right")
    for window in ("minute", "hour", "day"):
        table.add_row(
            window,
            str(state["used"][window]),
            str(state["limits"][window]),
            str(state["remaining"][window]),
        )
    console.print(table)
    console.print(f"[dim]{state['budget_file']}[/dim]")


@app.command()
def serve(port: int = typer.Option(8474, "--port", help="Port to listen on")) -> None:
    """Serve the local web app plus the /api/ JSON routes."""
    from docketwatch.server import main as serve_main

    serve_main(port=port)


if __name__ == "__main__":
    app()
