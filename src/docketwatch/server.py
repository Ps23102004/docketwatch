"""Local stdlib HTTP server for the DocketWatch web app.

`ThreadingHTTPServer` from the standard library. No framework, no bundler --
`web/` is vanilla HTML/CSS/JS served as static files, and everything under
`/api/` returns JSON in exactly the `to_dict()` shapes from `models.py`.

Routes:

    GET    /api/cases                     -> [TrackedCase, ...]
    POST   /api/cases   {"docket_id"}     -> TrackedCase (tracks it, same as `docketwatch track`)
    GET    /api/cases/{id}                -> TrackedCase
    DELETE /api/cases/{id}                -> {"docket_id","untracked":true} (same as `docketwatch untrack`)
    GET    /api/cases/{id}/timeline       -> {"docket_id","count","entries":[DocketEntry,...]}
    GET    /api/cases/{id}/digest         -> {"docket_id","markdown"}
    GET    /api/search?q=&limit=          -> {"query","count","results":[Docket,...]} (same as `docketwatch search`)
    POST   /api/ask   {"docket_id","question"} -> {"docket_id","question","answer"}

Errors are always `{"error": "..."}` with a real status code -- 404 for an
untracked case, 409 for a case already tracked, 503 when a dependency
(Ollama, or the active docket source) is down. Never a 200 carrying a
plausible-looking empty result.
"""

from __future__ import annotations

import json
import mimetypes
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from docketwatch import digest as digest_mod
from docketwatch import state_store
from docketwatch.ai import AIBackendError, answer_question
from docketwatch.sources import DocketDataError, get_source
from docketwatch.state_store import StateStoreError

DEFAULT_PORT = 8474
WEB_DIR = (Path(__file__).resolve().parent.parent.parent / "web").resolve()
_STATIC_ROUTES = {"/": "app.html", "/app.html": "app.html"}


class Handler(BaseHTTPRequestHandler):
    server_version = "docketwatch/0.1"

    # -- plumbing ---------------------------------------------------------

    def _send_json(self, status: HTTPStatus, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        relative = _STATIC_ROUTES.get(path, unquote(path).lstrip("/"))
        target = (WEB_DIR / relative).resolve()
        if WEB_DIR not in target.parents or not target.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        body = target.read_bytes()
        content_type, _ = mimetypes.guess_type(str(target))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            self._route_delete()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

    # -- routing ----------------------------------------------------------

    def _route_get(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/cases":
            self._handle_cases()
            return
        if path == "/api/search":
            self._handle_search()
            return
        if path.startswith("/api/cases/"):
            rest = [unquote(p) for p in path[len("/api/cases/") :].split("/") if p]
            if len(rest) == 1:
                self._handle_case(rest[0])
                return
            if len(rest) == 2 and rest[1] == "timeline":
                self._handle_timeline(rest[0])
                return
            if len(rest) == 2 and rest[1] == "digest":
                self._handle_digest(rest[0])
                return
        if path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._serve_static(path)

    def _route_post(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/ask":
            self._handle_ask()
        elif path == "/api/cases":
            self._handle_track()
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _route_delete(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/api/cases/"):
            rest = [unquote(p) for p in path[len("/api/cases/") :].split("/") if p]
            if len(rest) == 1:
                self._handle_untrack(rest[0])
                return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    # -- handlers ---------------------------------------------------------

    def _load(self, docket_id: str):
        """Load a tracked case, or send the error response and return None."""
        try:
            return state_store.load_case(docket_id)
        except StateStoreError as exc:
            status = HTTPStatus.NOT_FOUND if "not tracked" in str(exc) else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(status, {"error": str(exc)})
            return None

    def _handle_cases(self) -> None:
        try:
            cases = state_store.list_cases()
        except StateStoreError as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, [c.to_dict() for c in cases])

    def _handle_track(self) -> None:
        """POST /api/cases -- same `get_source().fetch_docket` + `state_store.track_case`
        call as the `docketwatch track` CLI command, just reached over HTTP."""
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid JSON body: {exc}"})
            return
        docket_id = str(body.get("docket_id") or "").strip()
        if not docket_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing required field: docket_id"})
            return
        try:
            already_tracked = state_store.is_tracked(docket_id)
        except StateStoreError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if already_tracked:
            self._send_json(HTTPStatus.CONFLICT, {"error": f"'{docket_id}' is already tracked."})
            return
        try:
            docket = get_source().fetch_docket(docket_id)
        except DocketDataError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        case = state_store.track_case(
            docket_id,
            {
                "case_name": docket.case_name,
                "docket_number": docket.docket_number,
                "court": docket.court,
                "entries": [e.to_dict() for e in docket.entries],
                "parties": docket.parties,
                "source": docket.source,
                "note": docket.note,
            },
        )
        self._send_json(HTTPStatus.CREATED, case.to_dict())

    def _handle_untrack(self, docket_id: str) -> None:
        """DELETE /api/cases/{id} -- same `state_store.untrack_case` as `docketwatch untrack`."""
        try:
            removed = state_store.untrack_case(docket_id)
        except StateStoreError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if not removed:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"'{docket_id}' is not tracked."})
            return
        self._send_json(HTTPStatus.OK, {"docket_id": docket_id, "untracked": True})

    def _handle_search(self) -> None:
        """GET /api/search?q=&limit= -- same `get_source().search` as `docketwatch search`."""
        params = parse_qs(urlsplit(self.path).query)
        query = (params.get("q", [""])[0] or "").strip()
        if not query:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing required query param: q"})
            return
        limit_raw = params.get("limit", ["10"])[0]
        try:
            limit = int(limit_raw)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"'limit' must be an integer, got {limit_raw!r}"})
            return
        try:
            hits = get_source().search(query, limit=limit)
        except DocketDataError as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"query": query, "count": len(hits), "results": [d.to_dict() for d in hits]})

    def _handle_case(self, docket_id: str) -> None:
        case = self._load(docket_id)
        if case is not None:
            self._send_json(HTTPStatus.OK, case.to_dict())

    def _handle_timeline(self, docket_id: str) -> None:
        case = self._load(docket_id)
        if case is None:
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "docket_id": case.docket_id,
                "count": len(case.entries),
                "entries": [e.to_dict() for e in case.entries],
            },
        )

    def _handle_digest(self, docket_id: str) -> None:
        case = self._load(docket_id)
        if case is None:
            return
        self._send_json(
            HTTPStatus.OK, {"docket_id": case.docket_id, "markdown": digest_mod.case_digest(case)}
        )

    def _handle_ask(self) -> None:
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid JSON body: {exc}"})
            return
        docket_id = (body.get("docket_id") or "").strip()
        question = (body.get("question") or "").strip()
        missing = [k for k, v in (("docket_id", docket_id), ("question", question)) if not v]
        if missing:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"missing required field(s): {', '.join(missing)}"})
            return
        case = self._load(docket_id)
        if case is None:
            return
        try:
            answer = answer_question(case, question)
        except AIBackendError as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"docket_id": docket_id, "question": question, "answer": answer})

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main(port: int = DEFAULT_PORT) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Serving on http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
