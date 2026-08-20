"""Local-LLM helpers for `explain` and `ask`.

Same single-call `OllamaBackend` shape as permitpulse's `ai_backend.py`, and
the same rule: if the model isn't reachable, raise `AIBackendError`. Never
answer from imagination and never dress a canned string up as a model's
opinion.

Every prompt here is given the docket text verbatim and told to say so when
the text does not contain the answer. `digest.py` deliberately uses no model
at all -- a daily digest is a list of facts, and inventing prose over facts is
how a tracker starts lying.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import requests

from docketwatch.models import DocketEntry, TrackedCase

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma4:e2b-mlx"

UNAVAILABLE_MSG = (
    "AI unavailable: Ollama isn't running at localhost:11434. Start it with `ollama serve`."
)

_SYSTEM = (
    "You explain United States federal court docket filings to a non-lawyer. "
    "Use only the docket text you are given. If it does not contain the answer, "
    "say so plainly. Never guess at case outcomes, deadlines or filings that are "
    "not in the text. Two or three sentences."
)


class AIBackendError(Exception):
    """Raised when the AI backend cannot produce a completion."""


class AIBackend(ABC):
    model: str = "unknown"

    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """Return the model's response text, or raise AIBackendError."""


class OllamaBackend(AIBackend):
    """A local Ollama server's `/api/chat`. Talks only to 127.0.0.1, no auth."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_OLLAMA_URL,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._session = session or requests.Session()

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }
        try:
            resp = self._session.post(f"{self.base_url}/api/chat", json=payload, timeout=120)
        except requests.RequestException as exc:
            raise AIBackendError(
                f"local Ollama isn't reachable at {self.base_url} -- is it running? ({exc})"
            ) from exc
        try:
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise AIBackendError(f"Ollama response was unusable (HTTP {resp.status_code}): {exc}") from exc
        content = (body.get("message") or {}).get("content")
        if not content:
            raise AIBackendError(f"Ollama returned no usable content: {body!r}")
        return content.strip()


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
