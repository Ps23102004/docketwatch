"""Talks to a local LLM backend for DocketWatch's single-call AI features.

`AIBackend` is the seam -- one implementation today, `OllamaBackend`, talking
to a local Ollama server. Same "fails loudly, never invents data" philosophy
as `sources.py`'s `DocketDataError`: a connection failure raises a clear
`AIBackendError`, never a silent fallback to fake output.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Optional

import requests

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma4:e2b-mlx"
MODEL_ENV = "DOCKETWATCH_AI_MODEL"


class AIBackendError(Exception):
    """Raised when the AI backend cannot produce a completion."""


class AIBackend(ABC):
    """A place a system+user prompt can be sent for a completion."""

    model: str = "unknown"

    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """Return the model's response text, or raise AIBackendError."""


class OllamaBackend(AIBackend):
    """A local Ollama server's native `/api/chat` endpoint.

    No auth, no API key -- this only ever talks to 127.0.0.1. If Ollama isn't
    running or the model isn't pulled, `complete` raises `AIBackendError`
    with the actual cause rather than returning an invented answer. Model
    defaults to `DEFAULT_MODEL`, overridable via `DOCKETWATCH_AI_MODEL`.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: str = DEFAULT_OLLAMA_URL,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.model = model or os.environ.get(MODEL_ENV, DEFAULT_MODEL)
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
