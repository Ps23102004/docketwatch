"""HTTP surface tests for `server.py`'s three new routes: `POST /api/cases`,
`DELETE /api/cases/{id}`, `GET /api/search`.

Same isolation pattern as `test_state_store.py` -- every test writes into a
tmp_path, never into the real `~/.docketwatch`. The live server thread reads
`docketwatch.state_store`'s module globals at call time, so monkeypatching
them before a request is enough even though the server started earlier.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest
import requests

from docketwatch import state_store
from docketwatch.server import Handler

ALPHA = "fixture-alpha-1"
BETA = "fixture-beta-2"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the store at a tmp dir, and force fixture mode."""
    monkeypatch.setattr(state_store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(state_store, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(state_store, "DIGESTS_DIR", tmp_path / "digests")
    monkeypatch.delenv("DOCKETWATCH_LIVE", raising=False)
    return tmp_path


@pytest.fixture(scope="module")
def base_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- POST /api/cases (track) ---------------------------------------------------


def test_post_cases_tracks_a_new_case(base_url):
    resp = requests.post(f"{base_url}/api/cases", json={"docket_id": BETA})
    assert resp.status_code == 201
    body = resp.json()
    assert body["docket_id"] == BETA
    assert body["case_name"]
    assert state_store.is_tracked(BETA)


def test_post_cases_missing_docket_id_is_400(base_url):
    resp = requests.post(f"{base_url}/api/cases", json={})
    assert resp.status_code == 400
    assert "docket_id" in resp.json()["error"]


def test_post_cases_unknown_docket_is_404(base_url):
    resp = requests.post(f"{base_url}/api/cases", json={"docket_id": "does-not-exist"})
    assert resp.status_code == 404
    assert "error" in resp.json()
    assert not state_store.is_tracked("does-not-exist")


def test_post_cases_already_tracked_is_409(base_url):
    first = requests.post(f"{base_url}/api/cases", json={"docket_id": ALPHA})
    assert first.status_code == 201
    second = requests.post(f"{base_url}/api/cases", json={"docket_id": ALPHA})
    assert second.status_code == 409
    assert "already tracked" in second.json()["error"]


def test_post_cases_bad_id_characters_is_400(base_url):
    resp = requests.post(f"{base_url}/api/cases", json={"docket_id": "../escape"})
    assert resp.status_code == 400


# -- DELETE /api/cases/{id} (untrack) ------------------------------------------


def test_delete_case_untracks_a_tracked_case(base_url):
    requests.post(f"{base_url}/api/cases", json={"docket_id": ALPHA})
    resp = requests.delete(f"{base_url}/api/cases/{ALPHA}")
    assert resp.status_code == 200
    assert resp.json() == {"docket_id": ALPHA, "untracked": True}
    assert not state_store.is_tracked(ALPHA)


def test_delete_case_not_tracked_is_404(base_url):
    resp = requests.delete(f"{base_url}/api/cases/never-tracked")
    assert resp.status_code == 404
    assert "error" in resp.json()


# -- GET /api/search ------------------------------------------------------------


def test_search_returns_matches_from_the_active_source(base_url):
    resp = requests.get(f"{base_url}/api/search", params={"q": "Redwood"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "Redwood"
    assert body["count"] == 1
    assert body["results"][0]["id"] == ALPHA


def test_search_missing_query_is_400(base_url):
    resp = requests.get(f"{base_url}/api/search")
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_search_no_matches_returns_an_empty_list_not_an_error(base_url):
    resp = requests.get(f"{base_url}/api/search", params={"q": "no-such-case-xyz"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["results"] == []


def test_search_respects_limit(base_url):
    resp = requests.get(f"{base_url}/api/search", params={"q": "v.", "limit": 1})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) <= 1
