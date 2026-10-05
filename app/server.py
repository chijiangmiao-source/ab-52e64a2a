"""HTTP entry point for the orbit maintenance rule service.

Endpoints
---------
GET  /healthz                         liveness probe
GET  /rules/<key>                     read a rule: deterministic value or
                                      explicit conflict evidence
POST /rules/<key>                     submit a station write (dot + content
                                      + causal context)

All state is the causal :class:`~app.crdt.DotStore`; the HTTP layer only
does JSON (de)serialisation and translates :class:`Rejected` into an
explicit error status that leaves the read result untouched.

Configuration (environment)
---------------------------
HOST       bind address, default 0.0.0.0
PORT       bind port, default 8080
STATIONS   comma separated allow-list of ground stations,
           default "station-alpha,station-beta,station-gamma"
DATA_DIR   snapshot directory, default ./data
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .crdt import DotStore, Rejected

DEFAULT_STATIONS = "station-alpha,station-beta,station-gamma"
MAX_BODY_BYTES = 256 * 1024


def build_store() -> DotStore:
    stations = [
        s.strip() for s in os.environ.get("STATIONS", DEFAULT_STATIONS).split(",")
        if s.strip()
    ]
    data_dir = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
    return DotStore(stations, data_dir=data_dir)


class RuleHandler(BaseHTTPRequestHandler):
    server_version = "OrbitRuleCRDT/1.0"

    # injected by make_server
    store: DotStore

    def log_message(self, fmt: str, *args) -> None:  # quiet, structured logs
        if os.environ.get("HTTP_LOG"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _rule_key(self, path: str):
        # /rules/<key> where key is a path remainder (may contain '/', ':')
        prefix = "/rules/"
        p = urlsplit(path).path
        if p.startswith(prefix) and len(p) > len(prefix):
            return unquote(p[len(prefix):])
        return None

    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self._send_json(200, {
                "status": "ok",
                "stations": sorted(self.store.stations),
            })
            return
        key = self._rule_key(self.path)
        if key is None:
            self._send_json(404, {"error": "not found"})
            return
        try:
            self._send_json(200, self.store.read(key))
        except Rejected as exc:
            self._send_json(exc.status, {"error": exc.reason})

    def do_POST(self) -> None:  # noqa: N802
        key = self._rule_key(self.path)
        if key is None:
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "bad Content-Length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"error": "empty or oversized body"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
            return

        try:
            result = self.store.submit(
                key=key,
                site=payload.get("site"),
                count=payload.get("count"),
                content=payload.get("content"),
                context=payload.get("context"),
            )
        except Rejected as exc:
            self._send_json(exc.status, {"error": exc.reason})
            return

        status = 200 if result["state"] == "accepted" else 202
        self._send_json(status, result)


def make_server(host: str, port: int, store: DotStore | None = None) -> ThreadingHTTPServer:
    store = store or build_store()

    class _Bound(RuleHandler):
        pass

    _Bound.store = store
    httpd = ThreadingHTTPServer((host, port), _Bound)
    httpd.store = store  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    httpd = make_server(host, port)
    print(f"orbit-rule service listening on {host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
