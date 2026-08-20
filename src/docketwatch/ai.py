"""Local-LLM helpers: single-call explain/ask, and a batch new-filings digest.

`explain_entry`/`answer_question` are single Ollama calls through
`ai_backend.OllamaBackend`. `summarize_new_filings` is different -- it runs
new docket entries through llm-ladder's shared lens engine (`chains.yaml`'s
"digest" chain), fanning the entries out to every locally available model and
reconciling their independent takes into a VERDICT/CONSENSUS/DISAGREEMENTS
result. Same pattern as grantradar's `match.py`, verbatim.

`digest.py` deliberately uses no model at all -- a plain daily digest is a
list of facts, and inventing prose over facts is how a tracker starts lying.
This module's job is different: it is an opt-in AI *opinion* on what new
filings mean, always kept separate from the plain digest and always labelled
as a model's take, never presented as fact.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from llm_ladder.config import ChainConfig, load_chains
from llm_ladder.digest import LensResult, LensTake, ProgressFn, _answered_takes, _run_lens_core
from llm_ladder.ledger import Ledger

from docketwatch.ai_backend import AIBackend, AIBackendError, OllamaBackend
from docketwatch.models import DocketEntry, TrackedCase

DIGEST_CHAIN_TAG = "digest"
# ai.py lives at src/docketwatch/ai.py -- three parents up is the repo root,
# where chains.yaml lives (same layout grantradar uses two parents up from
# its non-src package layout).
CHAINS_PATH = Path(__file__).resolve().parent.parent.parent / "chains.yaml"

UNAVAILABLE_MSG = (
    "AI unavailable: Ollama isn't running at localhost:11434. Start it with `ollama serve`."
)

_SYSTEM = (
    "You explain United States federal court docket filings to a non-lawyer. "
    "Use only the docket text you are given. If it does not contain the answer, "
    "say so plainly. Never guess at case outcomes, deadlines or filings that are "
    "not in the text. Two or three sentences."
)


class SummaryError(ValueError):
    """Raised when the new-filings summary chain cannot be used."""


def _case_text(case: TrackedCase, limit: int = 40) -> str:
    lines = [f"Case: {case.case_name} ({case.docket_number}, {case.court})"]
    for entry in case.entries[-limit:]:
        number = entry.entry_number if entry.entry_number is not None else "-"
        lines.append(f"[{number}] {entry.date_filed} ({entry.entry_type}) {entry.description}")
    return "\n".join(lines)


def explain_entry(entry: DocketEntry, case: Optional[TrackedCase] = None, backend: Optional[AIBackend] = None) -> str:
    """Plain-English explanation of one docket entry. Raises if the model is down."""
    backend = backend or OllamaBackend()
    context = f"Case: {case.case_name} ({case.docket_number}, {case.court})\n" if case else ""
    prompt = (
        f"{context}Docket entry {entry.entry_number} filed {entry.date_filed}, "
        f"classified locally as '{entry.entry_type}':\n\n{entry.description}\n\n"
        "What happened here, and what does it mean for the parties?"
    )
    return backend.complete(_SYSTEM, prompt)


def answer_question(case: TrackedCase, question: str, backend: Optional[AIBackend] = None) -> str:
    """Answer a question about a tracked case from its docket text only."""
    if not (question or "").strip():
        raise AIBackendError("Ask a question -- the question was empty.")
    backend = backend or OllamaBackend()
    prompt = f"{_case_text(case)}\n\nQuestion: {question}"
    return backend.complete(_SYSTEM, prompt)


# -- batch "new filings" digest, via llm-ladder's shared lens engine --------


def new_filings_lens_prompt(case: TrackedCase, new_entries: List[DocketEntry]) -> str:
    """Build the prompt given independently to every summarizing model."""
    serialized = [e.to_dict() if hasattr(e, "to_dict") else e for e in new_entries]
    return (
        "New docket entries just appeared on a federal court case. Summarize "
        "in plain English, for a non-lawyer, what happened and why it matters "
        "to the parties. Use only the facts given below -- do not guess at "
        "outcomes, deadlines, or anything not in the text.\n\n"
        f"CASE: {case.case_name} ({case.docket_number}, {case.court})\n\n"
        "NEW ENTRIES:\n"
        f"{json.dumps(serialized, indent=2, ensure_ascii=False, default=str)}"
    )


def new_filings_judge_prompt(takes: List[LensTake]) -> str:
    """Build the reconciliation prompt for the models' independent summaries."""
    answered = _answered_takes(takes)
    labeled = "\n\n".join(f"[{take.model}]: {take.take}" for take in answered)
    return (
        f"Here are {len(answered)} models' independent summaries of the same "
        "new docket filings. Compare them.\n\n"
        f"{labeled}\n\n"
        "Start with exactly one line: 'VERDICT: X of N models agree that "
        "<one-sentence summary of what happened>'. Then a 'CONSENSUS:' "
        "heading with '-' bullets for what the models broadly agree matters. "
        "Then a 'DISAGREEMENTS:' heading with '-' bullets naming which models "
        "are on each side of a substantive disagreement. Ignore mere wording "
        "differences."
    )


def summarize_new_filings(
    case: TrackedCase,
    new_entries: List[DocketEntry],
    models: Optional[List[str]] = None,
    chain: str = "digest",
    report_progress: ProgressFn = lambda p: None,
) -> LensResult:
    """Summarize `new_entries` on `case` using the lens engine (`chains.yaml`).

    Fans the entries out to every locally available Ollama model, then
    reconciles their independent takes into one VERDICT/CONSENSUS/
    DISAGREEMENTS result -- same shape and pattern as grantradar's
    `match.run_match`. Raises `SummaryError` for a bad chain or empty input.
    Fewer than two models answering is not an error -- `LensResult.note`
    explains it and `lens_verdict` stays None rather than inventing a verdict
    from one model's opinion.
    """
    if not new_entries:
        raise SummaryError("summarize_new_filings needs at least one new entry")
    try:
        chains = load_chains(str(CHAINS_PATH))
    except ValueError as exc:
        raise SummaryError(f"chains.yaml is invalid: {exc}") from exc
    try:
        chain_config: ChainConfig = chains[chain]
    except KeyError:
        raise SummaryError(f"chain '{chain}' not found") from None

    return _run_lens_core(
        models,
        new_filings_lens_prompt(case, new_entries),
        new_filings_judge_prompt,
        DIGEST_CHAIN_TAG,
        chain_config,
        Ledger(),
        report_progress,
    )
