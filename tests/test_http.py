"""End-to-end HTTP smoke tests against a running server.

Spawns ``python -m app.server`` on an ephemeral port itself, so it also
runs outside Compose:  python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HttpSmokeTest(unittest.TestCase):
    proc = None
    base = ""
    tmpdir = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.mkdtemp(prefix="orbit-http-")
        port = _free_port()
        env = dict(os.environ)
        env.update({
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "DATA_DIR": cls.tmpdir,
            "STATIONS": "station-alpha,station-beta,station-gamma",
        })
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "app.server"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        cls.base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(cls.base + "/healthz", timeout=1) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.proc:
            cls.proc.terminate()
            cls.proc.wait(timeout=5)

    def _request(self, method: str, path: str, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_01_healthz(self):
        status, body = self._request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertIn("station-alpha", body["stations"])

    def test_02_empty_read(self):
        status, body = self._request("GET", "/rules/orbit-42")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "empty")

    def test_03_conflict_evidence_over_http(self):
        key = "/rules/orbit-conflict"
        status, _ = self._request("POST", key, {
            "site": "station-alpha", "count": 1,
            "content": "burn-window-08:00", "context": {}})
        self.assertEqual(status, 200)
        status, _ = self._request("POST", key, {
            "site": "station-beta", "count": 1,
            "content": "burn-window-09:30", "context": {}})
        self.assertEqual(status, 200)

        status, body = self._request("GET", key)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "conflict")
        self.assertEqual(len(body["candidates"]), 2)
        for cand in body["candidates"]:
            self.assertEqual(len(cand["dot"]), 2)
            self.assertIn("context", cand)

    def test_04_rejections_over_http(self):
        key = "/rules/orbit-reject"
        # A1 already exists on another key (test_03); dots are global, so use
        # A2 whose predecessor A:1 is present.
        status, _ = self._request("POST", key, {
            "site": "station-alpha", "count": 2, "content": "ok",
            "context": {"station-alpha": 1}})
        self.assertEqual(status, 200)
        # equivocating dot
        status, body = self._request("POST", key, {
            "site": "station-alpha", "count": 2, "content": "different",
            "context": {"station-alpha": 1}})
        self.assertEqual(status, 409)
        self.assertIn("equivocation", body["error"])
        # counter jump
        status, _ = self._request("POST", key, {
            "site": "station-beta", "count": 5, "content": "jump", "context": {}})
        self.assertEqual(status, 400)
        # unknown site
        status, _ = self._request("POST", key, {
            "site": "station-hacker", "count": 1, "content": "x", "context": {}})
        self.assertEqual(status, 400)
        # self-contradictory vector (dot G1 claiming it already observed G:1)
        status, _ = self._request("POST", key, {
            "site": "station-gamma", "count": 1, "content": "x",
            "context": {"station-gamma": 1}})
        self.assertEqual(status, 400)
        # read result unchanged after rejections
        status, body = self._request("GET", key)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["value"], "ok")

    def test_05_idempotent_retransmit(self):
        key = "/rules/orbit-retry"
        # G1 already became effective in test_03's key? no - use G2 waiting
        # for the not-yet-seen G1 dot.
        payload = {
            "site": "station-gamma", "count": 2,
            "content": "gamma-v2", "context": {"station-gamma": 1},
        }
        status, body = self._request("POST", key, payload)
        self.assertEqual(status, 202)
        self.assertEqual(body["state"], "pending")
        status, body2 = self._request("POST", key, payload)
        self.assertEqual(body2["duplicate"], True)
        self.assertEqual(body2["state"], "pending")

        self._request("POST", "/rules/other", {
            "site": "station-gamma", "count": 1,
            "content": "g1", "context": {}})
        status, body = self._request("POST", key, payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "accepted")


if __name__ == "__main__":
    unittest.main()
