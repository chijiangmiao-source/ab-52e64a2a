"""Unit tests for the causal dot store.

Run with: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from app.crdt import DotStore, Rejected

A = "station-alpha"
B = "station-beta"
G = "station-gamma"
STATIONS = [A, B, G]


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DotStore(STATIONS)

    # ------------------------------------------------------------------
    def test_concurrent_writes_to_same_key_are_both_retained_as_conflict(self):
        r1 = self.store.submit("rule/x", A, 1, "v-from-alpha", {})
        r2 = self.store.submit("rule/x", B, 1, "v-from-beta", {})
        self.assertEqual(r1["state"], "accepted")
        self.assertEqual(r2["state"], "accepted")

        read = self.store.read("rule/x")
        self.assertEqual(read["status"], "conflict")
        self.assertEqual(len(read["candidates"]), 2)
        dots = {tuple(c["dot"]) for c in read["candidates"]}
        self.assertEqual(dots, {(A, 1), (B, 1)})
        for cand in read["candidates"]:
            self.assertIn("content", cand)
            self.assertIn("context", cand)
            self.assertEqual(cand["context"], {})
        contents = {c["content"] for c in read["candidates"]}
        self.assertEqual(contents, {"v-from-alpha", "v-from-beta"})

    def test_different_keys_are_independent(self):
        self.store.submit("rule/x", A, 1, "x", {})
        self.store.submit("rule/y", B, 1, "y", {})
        self.assertEqual(self.store.read("rule/x")["status"], "ok")
        self.assertEqual(self.store.read("rule/y")["value"], "y")
        self.assertEqual(self.store.read("rule/z")["status"], "empty")

    def test_single_writer_sequence_reads_latest(self):
        self.store.submit("k", A, 1, "one", {})
        self.store.submit("k", A, 2, "two", {A: 1})
        read = self.store.read("k")
        self.assertEqual(read["status"], "ok")
        self.assertEqual(read["value"], "two")
        self.assertEqual(read["winner"]["dot"], [A, 2])

    # ------------------------------------------------------------------
    # pending predecessors
    # ------------------------------------------------------------------
    def test_write_with_missing_predecessor_is_pending_and_invisible(self):
        r = self.store.submit("k", A, 1, "future", {B: 1})
        self.assertEqual(r["state"], "pending")
        self.assertEqual(r["waiting_for"], [[B, 1]])

        read = self.store.read("k")
        self.assertEqual(read["status"], "empty")
        self.assertEqual(len(read["pending"]), 1)
        self.assertEqual(read["pending"][0]["dot"], [A, 1])

    def test_pending_write_activates_automatically_when_predecessor_arrives(self):
        self.store.submit("k", A, 1, "depends-on-b1", {B: 1})
        self.assertEqual(self.store.read("k")["status"], "empty")

        self.store.submit("other", B, 1, "the-predecessor", {})
        read = self.store.read("k")
        self.assertEqual(read["status"], "ok")
        self.assertEqual(read["value"], "depends-on-b1")
        self.assertEqual(read["pending"], [])

    def test_pending_chain_activates_cascade(self):
        # G1 waits for A1; A1 waits for B1 -> everything activates once B1 lands
        self.store.submit("k", G, 1, "g1", {A: 1})
        self.store.submit("k", A, 1, "a1", {B: 1})
        self.assertEqual(self.store.read("k")["status"], "empty")
        self.store.submit("k", B, 1, "b1", {})
        read = self.store.read("k")
        # B1 and G1 are concurrent; A1 causally covers B1; G1 covers A1 -> G1 wins
        self.assertEqual(read["status"], "ok")
        self.assertEqual(read["winner"]["dot"], [G, 1])

    # ------------------------------------------------------------------
    # causal coverage
    # ------------------------------------------------------------------
    def test_version_vector_successor_prunes_only_covered_candidates(self):
        self.store.submit("k", A, 1, "a1", {})
        self.store.submit("k", B, 1, "b1", {})
        # conflict first
        self.assertEqual(self.store.read("k")["status"], "conflict")

        # B's next dot causally descends from both A1 and B1
        self.store.submit("k", B, 2, "merged", {A: 1, B: 1})
        read = self.store.read("k")
        self.assertEqual(read["status"], "ok")
        self.assertEqual(read["value"], "merged")
        self.assertEqual(read["winner"]["context"], {A: 1, B: 1})

    def test_unrelated_concurrent_write_keeps_older_conflict_candidate(self):
        self.store.submit("k", A, 1, "a1", {})
        self.store.submit("k", B, 1, "b1", {})
        # B2 builds only on B1 -> A1 and B2 remain concurrent
        self.store.submit("k", B, 2, "b2", {B: 1})
        read = self.store.read("k")
        self.assertEqual(read["status"], "conflict")
        self.assertEqual(
            {tuple(c["dot"]) for c in read["candidates"]},
            {(A, 1), (B, 2)},
        )

    # ------------------------------------------------------------------
    # dot immutability / retransmission
    # ------------------------------------------------------------------
    def test_same_dot_same_content_retransmission_is_idempotent(self):
        first = self.store.submit("k", A, 1, "same", {B: 1})
        self.assertTrue(first["state"] == "pending")
        again = self.store.submit("k", A, 1, "same", {B: 1})
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["state"], "pending")
        self.assertEqual(again["waiting_for"], [[B, 1]])

        self.store.submit("k", B, 1, "b", {})
        third = self.store.submit("k", A, 1, "same", {B: 1})
        self.assertTrue(third["duplicate"])
        self.assertEqual(third["state"], "accepted")
        self.assertEqual(self.store.read("k")["status"], "ok")

    def test_same_dot_different_content_is_rejected_and_read_unchanged(self):
        self.store.submit("k", A, 1, "original", {})
        with self.assertRaises(Rejected) as ctx:
            self.store.submit("k", A, 1, "tampered", {})
        self.assertEqual(ctx.exception.status, 409)
        read = self.store.read("k")
        self.assertEqual(read["status"], "ok")
        self.assertEqual(read["value"], "original")

    def test_same_dot_different_context_is_rejected(self):
        self.store.submit("k", A, 1, "x", {})
        with self.assertRaises(Rejected):
            self.store.submit("k", A, 1, "x", {B: 1})

    def test_concurrent_same_dot_different_content_at_most_one_accepted(self):
        ok, rejected = 0, 0
        for content in ("one", "two"):
            try:
                self.store.submit("k", A, 1, content, {})
                ok += 1
            except Rejected:
                rejected += 1
        self.assertEqual((ok, rejected), (1, 1))
        read = self.store.read("k")
        self.assertEqual(read["status"], "ok")
        self.assertIn(read["value"], {"one", "two"})

    # ------------------------------------------------------------------
    # malformed writes
    # ------------------------------------------------------------------
    def test_counter_jump_is_rejected(self):
        with self.assertRaises(Rejected):
            self.store.submit("k", A, 3, "jump", {})
        self.store.submit("k", A, 1, "a1", {})
        with self.assertRaises(Rejected):
            self.store.submit("k", A, 2, "a2-wrong", {})  # missing A:1 in context
        with self.assertRaises(Rejected):
            self.store.submit("k", A, 3, "a3-skip", {A: 1})
        self.assertEqual(self.store.read("k")["value"], "a1")

    def test_unknown_site_is_rejected(self):
        with self.assertRaises(Rejected):
            self.store.submit("k", "station-mystery", 1, "x", {})
        with self.assertRaises(Rejected):
            self.store.submit("k", A, 1, "x", {"station-mystery": 1})

    def test_self_contradictory_vector_is_rejected(self):
        # dot A1 claiming it already saw A:1
        with self.assertRaises(Rejected):
            self.store.submit("k", A, 1, "x", {A: 1})
        # negative / non-int counters
        with self.assertRaises(Rejected):
            self.store.submit("k", A, 1, "x", {B: -1})
        with self.assertRaises(Rejected):
            self.store.submit("k", A, 1, "x", "not-a-vector")

    def test_bad_key_count_content_rejected(self):
        with self.assertRaises(Rejected):
            self.store.submit("bad key!", A, 1, "x", {})
        with self.assertRaises(Rejected):
            self.store.submit("k", A, 0, "x", {})
        with self.assertRaises(Rejected):
            self.store.submit("k", A, 1, "", {})
        with self.assertRaises(Rejected):
            self.store.submit("k", A, True, "x", {})

    def test_parallel_same_dot_submissions_accept_exactly_one(self):
        import threading

        outcomes: list[bool] = []
        lock = threading.Lock()

        def worker(content: str) -> None:
            try:
                self.store.submit("k", A, 1, content, {})
                ok = True
            except Rejected:
                ok = False
            with lock:
                outcomes.append(ok)

        threads = [threading.Thread(target=worker, args=(f"v{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(len(outcomes), 8)
        self.assertIn(self.store.read("k")["value"], {f"v{i}" for i in range(8)})

    # ------------------------------------------------------------------
    # restart persistence
    # ------------------------------------------------------------------
    def test_restart_preserves_accepted_pending_and_conflicts(self):
        data_dir = tempfile.mkdtemp(prefix="dotstore-")
        try:
            store = DotStore(STATIONS, data_dir=data_dir)
            store.submit("rule/x", A, 1, "alpha", {})
            store.submit("rule/x", B, 1, "beta", {})           # conflict
            store.submit("rule/x", G, 1, "waiting", {A: 2})   # pending
            before = store.read("rule/x")
            self.assertEqual(before["status"], "conflict")
            self.assertEqual(len(before["pending"]), 1)

            revived = DotStore(STATIONS, data_dir=data_dir)
            after = revived.read("rule/x")
            self.assertEqual(after["status"], "conflict")
            self.assertEqual(
                {tuple(c["dot"]) for c in after["candidates"]},
                {tuple(c["dot"]) for c in before["candidates"]},
            )
            self.assertEqual(
                {c["content"] for c in after["candidates"]},
                {"alpha", "beta"},
            )
            self.assertEqual(len(after["pending"]), 1)
            self.assertEqual(after["pending"][0]["dot"], [G, 1])

            # equivocation still rejected after restart
            with self.assertRaises(Rejected):
                revived.submit("rule/x", A, 1, "tampered", {})

            # completing the predecessor after restart activates the pending dot
            revived.submit("rule/x", A, 2, "alpha2", {A: 1, B: 1})
            final = revived.read("rule/x")
            # A2 covers A1,B1; G1 waits for A2 and then covers it -> G1 wins,
            # unless G1 context only had A:2 -> G1 covers A2 transitively
            self.assertEqual(final["status"], "ok")
            self.assertEqual(final["value"], "waiting")
            self.assertEqual(final["pending"], [])
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
