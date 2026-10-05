"""DVV 存储核心语义测试。"""

import os
import tempfile
import threading
import unittest

from app.store import (
    ContradictoryContext,
    CounterJump,
    DotContentConflict,
    InvalidRequest,
    Store,
    UnknownContextSite,
    UnknownSite,
)

SITES = ["alpha", "beta", "gamma", "delta"]


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "state.json")
        self.st = Store(self.path, SITES)

    def tearDown(self):
        self._tmp.cleanup()

    def accept(self, key, site, counter, value, ctx=None):
        return self.st.accept(key, site, counter, value, ctx or {})

    # ---------------------------------------------------------------- 冲突
    def test_concurrent_writes_produce_conflict_with_dots_and_contexts(self):
        code, _ = self.accept("k", "alpha", 1, "alpha-ruling")
        self.assertEqual(code, 201)
        code, _ = self.accept("k", "beta", 1, "beta-ruling")
        self.assertEqual(code, 201)

        proj = self.st.projection("k")
        self.assertEqual(proj["status"], "conflict")
        self.assertEqual(len(proj["candidates"]), 2)
        by_site = {c["dot"]["site"]: c for c in proj["candidates"]}
        self.assertEqual(by_site["alpha"]["value"], "alpha-ruling")
        self.assertEqual(by_site["alpha"]["dot"], {"site": "alpha", "counter": 1})
        self.assertEqual(by_site["beta"]["value"], "beta-ruling")
        self.assertIn("context", by_site["alpha"])
        self.assertIn("context", by_site["beta"])

    def test_causal_overwrite_evicts_only_covered_candidates(self):
        self.accept("k", "alpha", 1, "red")
        self.accept("k", "beta", 1, "blue")
        self.accept("k", "gamma", 1, "green")
        self.assertEqual(len(self.st.projection("k")["candidates"]), 3)

        # alpha 的裁决只覆盖 alpha/1 与 beta/1，不覆盖 gamma/1
        code, _ = self.accept("k", "alpha", 2, "red+blue", {"alpha": 1, "beta": 1})
        self.assertEqual(code, 201)
        proj = self.st.projection("k")
        self.assertEqual(proj["status"], "conflict")
        got = {(c["dot"]["site"], c["dot"]["counter"]): c["value"] for c in proj["candidates"]}
        self.assertEqual(got, {("alpha", 2): "red+blue", ("gamma", 1): "green"})

        # gamma 的后续裁决覆盖全部候选，收敛为确定值
        self.accept("k", "gamma", 2, "final", {"gamma": 1, "alpha": 2})
        proj = self.st.projection("k")
        self.assertEqual(proj["status"], "ok")
        self.assertEqual(proj["value"], "final")
        self.assertEqual(proj["dot"], {"site": "gamma", "counter": 2})

    # ---------------------------------------------------------------- 待定
    def test_pending_write_activates_when_predecessor_arrives(self):
        # alpha/1 依赖尚未到达的 beta/1 -> 待定，不进入投影
        code, body = self.accept("k1", "alpha", 1, "awaiting-beta", {"beta": 1})
        self.assertEqual(code, 202)
        self.assertEqual(body["status"], "pending")
        self.assertIsNone(self.st.projection("k1"))

        # 前驱 beta/1 到达（写在另一个键上）-> alpha/1 自动激活
        code, body = self.accept("k2", "beta", 1, "beta-event")
        self.assertEqual(code, 201)
        self.assertIn({"site": "alpha", "counter": 1}, body["activated"])
        proj = self.st.projection("k1")
        self.assertEqual(proj["status"], "ok")
        self.assertEqual(proj["value"], "awaiting-beta")

    def test_pending_dot_counts_as_knowledge_for_dependents(self):
        # alpha/1 依赖 beta/1（未到）-> 待定
        self.accept("k", "alpha", 1, "a1", {"beta": 1})
        # beta/1 自身依赖 gamma/1（未到）-> 待定，但其 dot 已计入知识，激活 alpha/1
        code, body = self.accept("k", "beta", 1, "b1", {"gamma": 1})
        self.assertEqual(code, 202)
        self.assertIn({"site": "alpha", "counter": 1}, body["activated"])
        # gamma/1 到达 -> 激活 beta/1；但 beta/1 已被 alpha/1 的上下文 {beta:1} 覆盖，
        # 激活后立即过期，不进入投影
        code, body = self.accept("k", "gamma", 1, "g1")
        self.assertIn({"site": "beta", "counter": 1}, body["activated"])
        proj = self.st.projection("k")
        self.assertEqual(proj["status"], "conflict")
        got = {(c["dot"]["site"], c["dot"]["counter"]) for c in proj["candidates"]}
        self.assertEqual(got, {("alpha", 1), ("gamma", 1)})
        writes = self.st.debug_state()["writes"]
        beta1 = [w for w in writes if w["site"] == "beta" and w["counter"] == 1][0]
        self.assertFalse(beta1["pending"])
        self.assertTrue(beta1["evicted"])

    def test_activated_write_covered_by_existing_candidate_is_obsolete(self):
        # alpha/1 待定（等 beta/1）；alpha/2 已覆盖 alpha/1 且生效
        self.accept("k", "alpha", 1, "old", {"beta": 1})
        self.accept("k", "alpha", 2, "new", {"alpha": 1})
        self.assertEqual(self.st.projection("k")["value"], "new")
        # beta/1 到达后 alpha/1 激活，但已被 alpha/2 的上下文覆盖 -> 立即过期
        self.accept("other", "beta", 1, "b")
        proj = self.st.projection("k")
        self.assertEqual(proj["status"], "ok")
        self.assertEqual(proj["value"], "new")

    # ---------------------------------------------------------------- 重传与拒绝
    def test_retransmission_replays_first_result(self):
        code1, body1 = self.accept("k", "alpha", 1, "v1")
        code2, body2 = self.accept("k", "alpha", 1, "v1")
        self.assertEqual(code1, code2)
        self.assertEqual(body1, body2)
        self.assertEqual(len(self.st.debug_state()["writes"]), 1)

    def test_same_dot_different_content_rejected_and_state_unchanged(self):
        self.accept("k", "alpha", 1, "v1")
        with self.assertRaises(DotContentConflict):
            self.accept("k", "alpha", 1, "tampered")
        proj = self.st.projection("k")
        self.assertEqual(proj["status"], "ok")
        self.assertEqual(proj["value"], "v1")
        self.assertEqual(self.st.debug_state()["site_max"]["alpha"], 1)

    def test_counter_jump_rejected(self):
        with self.assertRaises(CounterJump) as cm:
            self.accept("k", "alpha", 3, "v")
        self.assertEqual(cm.exception.extra["expected"], 1)
        self.assertEqual(cm.exception.extra["got"], 3)
        self.accept("k", "alpha", 1, "v")
        with self.assertRaises(CounterJump):
            self.accept("k", "alpha", 3, "v")

    def test_unknown_site_rejected(self):
        with self.assertRaises(UnknownSite):
            self.accept("k", "ghost", 1, "v")

    def test_unknown_context_site_rejected(self):
        with self.assertRaises(UnknownContextSite):
            self.accept("k", "alpha", 1, "v", {"ghost": 1})

    def test_contradictory_context_rejected(self):
        with self.assertRaises(ContradictoryContext):
            self.accept("k", "alpha", 1, "v", {"alpha": 1})
        with self.assertRaises(ContradictoryContext):
            self.accept("k", "alpha", 2, "v", {"alpha": 5})

    def test_invalid_counter_and_context_rejected(self):
        with self.assertRaises(InvalidRequest):
            self.accept("k", "alpha", 0, "v")
        with self.assertRaises(InvalidRequest):
            self.accept("k", "alpha", 1, "v", {"beta": -1})

    def test_concurrent_same_dot_accepts_at_most_one(self):
        results = []
        lock = threading.Lock()

        def worker(i):
            try:
                code, _ = self.st.accept("k", "alpha", 1, f"contender-{i}", {})
                with lock:
                    results.append(("ok", code))
            except DotContentConflict:
                with lock:
                    results.append(("rejected", 409))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        oks = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(oks), 1, "同一 dot 的并发不同内容最多接受一个")
        self.assertEqual(len(results), 16)
        proj = self.st.projection("k")
        self.assertEqual(proj["status"], "ok")

    # ---------------------------------------------------------------- 持久化
    def test_persistence_roundtrip_preserves_dots_pending_and_conflicts(self):
        self.accept("k1", "alpha", 1, "alpha-ruling")
        self.accept("k1", "beta", 1, "beta-ruling")          # 冲突候选
        self.accept("k2", "alpha", 2, "pending-one", {"alpha": 1, "gamma": 2})  # 待定
        code1, body1 = self.accept("k3", "delta", 1, "d1")

        before = self.st.debug_state()
        st2 = Store(self.path, SITES)  # 模拟重启：从磁盘恢复
        after = st2.debug_state()

        self.assertEqual(before["site_max"], after["site_max"])
        self.assertEqual(before["writes"], after["writes"])
        self.assertEqual(before["keys"], after["keys"])
        # 待定写入仍是待定，不在投影中
        self.assertIsNone(st2.projection("k2"))
        # 冲突候选保持一致
        self.assertEqual(st2.projection("k1")["status"], "conflict")
        # 重传在重启后仍复现首次结果
        code2, body2 = st2.accept("k3", "delta", 1, "d1", {})
        self.assertEqual((code1, body1), (code2, body2))
        # 前驱补齐后，跨重启的待定写入自动激活
        st2.accept("k4", "gamma", 1, "g1", {})
        _, body = st2.accept("k4", "gamma", 2, "g2", {"gamma": 1})
        self.assertIn({"site": "alpha", "counter": 2}, body["activated"])
        self.assertEqual(st2.projection("k2")["value"], "pending-one")


if __name__ == "__main__":
    unittest.main()
