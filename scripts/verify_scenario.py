#!/usr/bin/env python3
"""Compose verify scenario.

Exercised against a running server (BASE_URL env, default
http://localhost:8080).  Reproduces the two rendezvous outcomes the
scheduling centre cares about:

1. a write whose predecessor has not arrived stays pending and invisible,
   then activates automatically once the predecessor is delivered;
2. a causally later version vector prunes exactly the candidates it
   covers while genuinely concurrent candidates survive as conflict
   evidence.

Also verifies idempotent retransmission and restart-independent dot
immutability (the latter from the caller's view: an equivocating redelivery
is refused).  Exits non-zero on the first failed expectation.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:8080")
A, B, G = "station-alpha", "station-beta", "station-gamma"

failures: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def call(method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def main() -> int:
    status, health = call("GET", "/healthz")
    check("healthz returns 200 ok", status == 200 and health.get("status") == "ok",
          f"{status} {health}")

    # ---- scenario 1: pending activation --------------------------------
    key = "/rules/rendezvous-1"

    # G1 causally depends on B1 which has not reached the centre yet.
    status, body = call("POST", key, {
        "site": G, "count": 1, "content": "gamma-final",
        "context": {B: 1}})
    check("dependent write is held pending (202)",
          status == 202 and body["state"] == "pending"
          and body["waiting_for"] == [[B, 1]], f"{status} {body}")

    status, read = call("GET", key)
    check("pending write absent from effective projection",
          read["status"] == "empty" and len(read["pending"]) == 1, str(read))

    # Same dot, identical payload retransmitted while still pending.
    status, body = call("POST", key, {
        "site": G, "count": 1, "content": "gamma-final",
        "context": {B: 1}})
    check("identical dot retransmission reproduces pending result",
          body.get("duplicate") is True and body["state"] == "pending", str(body))

    # Predecessor arrives (to a different key: dependency is causal, not per-key).
    status, _ = call("POST", "/rules/telemetry-b", {
        "site": B, "count": 1, "content": "b1-heartbeat", "context": {}})
    check("predecessor accepted", status == 200)

    status, read = call("GET", key)
    check("pending write activates automatically after predecessor arrives",
          read["status"] == "ok" and read["value"] == "gamma-final"
          and read["pending"] == [], str(read))

    # ---- scenario 2: causal coverage vs true concurrency ----------------
    # Note dots are global per station, not per key: B1 and G1 were already
    # used in scenario 1, so this scenario continues their sequences.
    key = "/rules/rendezvous-2"
    call("POST", key, {"site": A, "count": 1, "content": "a1", "context": {}})
    status, body = call("POST", key, {"site": B, "count": 2, "content": "b2",
                                      "context": {B: 1}})
    check("concurrent branch write accepted", status == 200, f"{status} {body}")
    _, read = call("GET", key)
    check("two concurrent writes surface as conflict with both candidates",
          read["status"] == "conflict"
          and {tuple(c["dot"]) for c in read["candidates"]} == {(A, 1), (B, 2)}
          and all("content" in c and "context" in c for c in read["candidates"]),
          str(read))

    # B3 descends from both A1 and B2 -> prunes exactly those two.
    status, body = call("POST", key, {
        "site": B, "count": 3, "content": "merged-b3",
        "context": {A: 1, B: 2}})
    check("merge write accepted", status == 200, str(body))
    _, read = call("GET", key)
    check("version-vector successor prunes covered candidates only",
          read["status"] == "ok" and read["value"] == "merged-b3"
          and read["winner"]["dot"] == [B, 3], str(read))

    # G2 descends from G1 (scenario 1) but not from B3: the two branches
    # stay genuinely concurrent.
    call("POST", key, {"site": G, "count": 2, "content": "g2-concurrent",
                       "context": {G: 1}})
    _, read = call("GET", key)
    check("unrelated concurrent write resurrects conflict, covered dot stays pruned",
          read["status"] == "conflict"
          and {tuple(c["dot"]) for c in read["candidates"]} == {(B, 3), (G, 2)},
          str(read))

    # ---- scenario 3: security rejections leave reads untouched ----------
    status, body = call("POST", key, {
        "site": A, "count": 1, "content": "tampered-a1", "context": {}})
    check("same dot different content rejected with 409",
          status == 409 and "equivocation" in body["error"], f"{status} {body}")
    status, _ = call("POST", key, {
        "site": A, "count": 9, "content": "jump", "context": {}})
    check("counter jump rejected with 400", status == 400)
    status, _ = call("POST", key, {
        "site": "station-rogue", "count": 1, "content": "x", "context": {}})
    check("unknown site rejected with 400", status == 400)
    status, _ = call("POST", key, {
        "site": A, "count": 2, "content": "x", "context": {A: 2}})
    check("self-contradictory vector rejected with 400", status == 400)
    _, read_after = call("GET", key)
    check("read result unchanged after rejected writes",
          read_after == read, f"before={read} after={read_after}")

    print()
    if failures:
        print(f"VERIFY FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("VERIFY OK: all rendezvous guarantees observed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
