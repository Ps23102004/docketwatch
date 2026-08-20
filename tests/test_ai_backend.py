"""AI backend tests. Ollama's HTTP call is mocked -- CI never needs a live Ollama."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock

import pytest
import requests

from docketwatch.ai_backend import DEFAULT_MODEL, MODEL_ENV, AIBackendError, OllamaBackend


def _fake_session(status_code=200, json_body=None, raise_exc=None):
    session = MagicMock(spec=requests.Session)
    if raise_exc:
        session.post.side_effect = raise_exc
        return session
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_body
    session.post.return_value = resp
    return session


def test_complete_returns_stripped_content_on_success():
    session = _fake_session(json_body={"message": {"content": "  Order granting the motion.  "}})
    backend = OllamaBackend(session=session)
    assert backend.complete("system", "user") == "Order granting the motion."
    _, kwargs = session.post.call_args
    assert kwargs["json"]["messages"][0] == {"role": "system", "content": "system"}
    assert kwargs["json"]["messages"][1] == {"role": "user", "content": "user"}
    assert kwargs["json"]["stream"] is False


def test_complete_raises_on_connection_failure():
    session = _fake_session(raise_exc=requests.ConnectionError("connection refused"))
    backend = OllamaBackend(session=session)
    with pytest.raises(AIBackendError, match="isn't reachable"):
        backend.complete("system", "user")


def test_complete_raises_on_timeout():
    session = _fake_session(raise_exc=requests.Timeout("timed out"))
    backend = OllamaBackend(session=session)
    with pytest.raises(AIBackendError):
        backend.complete("system", "user")


def test_complete_raises_on_bad_status():
    session = _fake_session(raise_exc=None)
    session.post.return_value.raise_for_status.side_effect = requests.HTTPError("500")
    backend = OllamaBackend(session=session)
    with pytest.raises(AIBackendError, match="unusable"):
        backend.complete("system", "user")


def test_complete_raises_on_empty_content():
    session = _fake_session(json_body={"message": {}})
    backend = OllamaBackend(session=session)
    with pytest.raises(AIBackendError, match="no usable content"):
        backend.complete("system", "user")


def test_model_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv(MODEL_ENV, raising=False)
    assert OllamaBackend().model == DEFAULT_MODEL == "gemma4:e2b-mlx"
    monkeypatch.setenv(MODEL_ENV, "gemma4:e4b-mlx")
    assert OllamaBackend().model == "gemma4:e4b-mlx"
    # An explicit constructor arg wins over the env var.
    assert OllamaBackend(model="qwen3-vl").model == "qwen3-vl"


def _ollama_reachable() -> bool:
    try:
        with socket.create_connection(("localhost", 11434), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.network
@pytest.mark.skipif(not _ollama_reachable(), reason="Ollama isn't running at localhost:11434")
def test_live_ollama_completes():
    """Optional real-Ollama integration test -- skips cleanly if Ollama's down."""
    text = OllamaBackend().complete("Reply with one word.", "Say hello.")
    assert isinstance(text, str)
    assert len(text) > 0
