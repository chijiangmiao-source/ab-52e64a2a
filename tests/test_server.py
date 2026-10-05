"""HTTP 接口测试：健康检查、写入/读取、拒绝路径的状态码。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.server import make_handler
from app.store import Store

SITES = ["alpha", "beta", "gamma", "delta"]


class ServerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        store = Store(os.path.join(self._tmp.name, "state.json"), SITES)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def put(self, key, site, counter, value, ctx=None):
        return self.req("PUT", f"/rules/{key}", {
            "site": site, "counter": counter, "value": value, "context": ctx or {},
        })

    def test_health(self):
        code, body = self.req("GET", "/health")
        self.assertEqual((code, body["status"]), (200, "ok"))

    def test_write_and_read_single_value(self):
        code, body = self.put("r1", "alpha", 1, "v1")
        self.assertEqual(code, 201)
        self.assertEqual(body["status"], "active")
        code, proj = self.req("GET", "/rules/r1")
        self.assertEqual(code, 200)
        self.assertEqual(proj["status"], "ok")
        self.assertEqual(proj["value"], "v1")
        self.assertEqual(proj["dot"], {"site": "alpha", "counter": 1})

    def test_conflict_read_lists_candidates(self):
        self.put("r2", "alpha", 1, "a")
        self.put("r2", "beta", 1, "b")
        code, proj = self.req("GET", "/rules/r2")
        self.assertEqual(code, 200)
        self.assertEqual(proj["status"], "conflict")
        self.assertEqual(len(proj["candidates"]), 2)
        for c in proj["candidates"]:
            self.assertIn("dot", c)
            self.assertIn("context", c)
            self.assertIn("value", c)

    def test_pending_returns_202_and_not_readable(self):
        code, body = self.put("r3", "alpha", 1, "a", {"beta": 1})
        self.assertEqual(code, 202)
        self.assertEqual(body["status"], "pending")
        code, _ = self.req("GET", "/rules/r3")
        self.assertEqual(code, 404)

    def test_read_missing_key_404(self):
        code, body = self.req("GET", "/rules/nope")
        self.assertEqual(code, 404)
        self.assertEqual(body["error"], "not_found")

    def test_rejections(self):
        self.put("r4", "alpha", 1, "a")
        # 同 dot 不同内容
        code, body = self.put("r4", "alpha", 1, "tampered")
        self.assertEqual((code, body["error"]), (409, "dot_content_conflict"))
        # 计数跳跃
        code, body = self.put("r4", "alpha", 5, "x")
        self.assertEqual((code, body["error"]), (409, "counter_jump"))
        self.assertEqual(body["expected"], 2)
        # 未知站点
        code, body = self.put("r4", "ghost", 1, "x")
        self.assertEqual((code, body["error"]), (403, "unknown_site"))
        # 自相矛盾向量
        code, body = self.put("r4", "beta", 1, "x", {"beta": 1})
        self.assertEqual((code, body["error"]), (400, "contradictory_context"))
        # 非法请求
        code, body = self.put("r4", "beta", 0, "x")
        self.assertEqual((code, body["error"]), (400, "invalid_request"))
        # 读取结果保持不变
        code, proj = self.req("GET", "/rules/r4")
        self.assertEqual(proj["status"], "ok")
        self.assertEqual(proj["value"], "a")

    def test_retransmission_replays_identical_response(self):
        code1, body1 = self.put("r5", "alpha", 1, "a")
        code2, body2 = self.put("r5", "alpha", 1, "a")
        self.assertEqual((code1, body1), (code2, body2))

    def test_malformed_body_400(self):
        r = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/rules/r6",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                self.fail(f"expected 400, got {resp.status}")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_debug_state_endpoint(self):
        self.put("r7", "alpha", 1, "a")
        code, body = self.req("GET", "/debug/state")
        self.assertEqual(code, 200)
        self.assertEqual(body["site_max"]["alpha"], 1)
        self.assertEqual(len(body["writes"]), 1)
        self.assertEqual(body["keys"]["r7"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
