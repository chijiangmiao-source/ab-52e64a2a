"""HTTP 入口：健康检查、规则写入/读取、因果状态观察。

端点：
- GET  /health            健康检查
- PUT  /rules/{key}       规则写入，body: {"site","counter","value","context"}
- GET  /rules/{key}       规则读取：确定值（ok）或冲突证据（conflict）
- GET  /debug/state       完整因果状态（已接受 dot、待定写入、候选与淘汰标记）
"""

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from .store import Store, StoreError

MISSING = object()
RULE_RE = re.compile(r"/rules/([^/]+)")


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "RuleSvc/1.0"

        def log_message(self, fmt, *args):
            print(f"[http] {self.address_string()} {fmt % args}", flush=True)

        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":
                return self._json(200, {"status": "ok"})
            if path == "/debug/state":
                return self._json(200, store.debug_state())
            m = RULE_RE.fullmatch(path)
            if m:
                key = unquote(m.group(1))
                proj = store.projection(key)
                if proj is None:
                    return self._json(404, {"error": "not_found", "key": key})
                return self._json(200, proj)
            return self._json(404, {"error": "not_found"})

        def do_PUT(self):
            path = urlparse(self.path).path
            m = RULE_RE.fullmatch(path)
            if not m:
                return self._json(404, {"error": "not_found"})
            key = unquote(m.group(1))
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                return self._json(
                    400, {"error": "invalid_request", "detail": "request body must be valid JSON"}
                )
            if not isinstance(body, dict):
                return self._json(
                    400, {"error": "invalid_request", "detail": "request body must be a JSON object"}
                )
            site = body.get("site")
            counter = body.get("counter")
            value = body.get("value", MISSING)
            context = body.get("context", {})
            if not isinstance(site, str) or not site:
                return self._json(
                    400, {"error": "invalid_request", "detail": "site must be a non-empty string"}
                )
            if not isinstance(counter, int) or isinstance(counter, bool):
                return self._json(
                    400, {"error": "invalid_request", "detail": "counter must be an integer"}
                )
            if value is MISSING:
                return self._json(
                    400, {"error": "invalid_request", "detail": "value is required"}
                )
            if not isinstance(context, dict):
                return self._json(
                    400,
                    {"error": "invalid_request", "detail": "context must be an object of site->counter"},
                )
            try:
                code, resp = store.accept(key, site, counter, value, context)
            except StoreError as e:
                return self._json(e.http_status, e.payload())
            return self._json(code, resp)

    return Handler


def main():
    port = int(os.environ.get("PORT", "8080"))
    sites = [s.strip() for s in os.environ.get("SITES", "alpha,beta,gamma,delta").split(",") if s.strip()]
    data_dir = os.environ.get("DATA_DIR", "/data")
    os.makedirs(data_dir, exist_ok=True)
    store = Store(os.path.join(data_dir, "state.json"), sites)
    httpd = ThreadingHTTPServer(("", port), make_handler(store))
    print(f"[server] listening on :{port}, sites={sorted(sites)}, data_dir={data_dir}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
