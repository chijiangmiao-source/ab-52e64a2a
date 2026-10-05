"""点版本向量（DVV）规则存储：多地面站离线编辑的因果汇合。

语义概要：
- 每次写入携带 dot=(site, counter) 与因果上下文 context（site -> counter）。
- 同一规则键下，因果上下文互不覆盖的候选并存，读取时明确标记 conflict。
- 写入依赖的前驱（context 中的事件）尚未到达时，写入进入待定（pending），
  不进入有效投影；前驱补齐后自动激活生效。
- 写入/激活只淘汰其版本向量（context + 自身 dot）已覆盖的候选。
- 同一 dot 的相同内容重传复现首次结果；同一 dot 的不同内容、计数跳跃、
  未知站点、自相矛盾向量均被明确拒绝，且不影响已有状态。
- 状态每次变更后原子落盘，重启后已接受 dot、待定写入、冲突候选保持一致。
"""

import json
import os
import threading


class StoreError(Exception):
    """可映射为 HTTP 响应的存储层错误。"""

    http_status = 500
    error = "internal_error"

    def __init__(self, **extra):
        super().__init__(self.error)
        self.extra = extra

    def payload(self):
        body = {"error": self.error}
        body.update(self.extra)
        return body


class UnknownSite(StoreError):
    http_status = 403
    error = "unknown_site"


class UnknownContextSite(StoreError):
    http_status = 400
    error = "unknown_context_site"


class ContradictoryContext(StoreError):
    http_status = 400
    error = "contradictory_context"


class InvalidRequest(StoreError):
    http_status = 400
    error = "invalid_request"


class DotContentConflict(StoreError):
    http_status = 409
    error = "dot_content_conflict"


class CounterJump(StoreError):
    http_status = 409
    error = "counter_jump"


def _canon(value):
    """JSON 值的规范形式，用于重传内容一致性比较。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Store:
    def __init__(self, path, sites):
        self._path = path
        self._sites = set(sites)
        self._lock = threading.RLock()
        self._site_max = {}   # site -> 已接受的最高计数（含待定写入的 dot）
        self._writes = []     # 全部已接受写入（含待定/已淘汰），按接受顺序
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._site_max = {k: int(v) for k, v in data.get("site_max", {}).items()}
            self._writes = data.get("writes", [])

    # ------------------------------------------------------------------ 写入
    def accept(self, key, site, counter, value, context):
        """接受一次规则写入，返回 (HTTP 状态码, 响应体 dict)。

        抛出 StoreError 子类表示明确拒绝；拒绝不产生任何状态变更。
        """
        with self._lock:
            if site not in self._sites:
                raise UnknownSite(site=site)
            if not isinstance(counter, int) or isinstance(counter, bool) or counter < 1:
                raise InvalidRequest(detail="counter must be a positive integer")
            if context is None:
                context = {}
            if not isinstance(context, dict):
                raise InvalidRequest(detail="context must be an object of site->counter")
            for cs, cc in context.items():
                if not isinstance(cs, str):
                    raise InvalidRequest(detail="context keys must be site names")
                if cs not in self._sites:
                    raise UnknownContextSite(site=cs)
                if not isinstance(cc, int) or isinstance(cc, bool) or cc < 0:
                    raise InvalidRequest(detail="context counters must be non-negative integers")
            if context.get(site, 0) >= counter:
                # 上下文声称已包含自身 dot 或更晚的自身事件：自相矛盾
                raise ContradictoryContext(site=site, counter=counter)

            canon = _canon(value)
            max_seen = self._site_max.get(site, 0)
            if counter <= max_seen:
                old = self._find(site, counter)
                if old is None:  # 状态不一致，理论上不会发生
                    raise StoreError(detail=f"dot {site}/{counter} missing from log")
                if old["key"] == key and old["value_canon"] == canon and old["context"] == context:
                    # 相同 dot、相同内容的重传：复现首次结果
                    return old["status_code"], old["response"]
                # 相同 dot、不同内容：明确拒绝（并发同 dot 时最多接受一个）
                raise DotContentConflict(site=site, counter=counter)
            if counter != max_seen + 1:
                # 计数跳跃：前驱事件缺失，必须按序补齐
                raise CounterJump(site=site, expected=max_seen + 1, got=counter)

            write = {
                "site": site,
                "counter": counter,
                "key": key,
                "value": value,
                "value_canon": canon,
                "context": dict(context),
                "pending": False,
                "evicted": False,
                "status_code": None,
                "response": None,
            }
            self._writes.append(write)
            self._site_max[site] = counter

            # 前驱未齐 -> 待定，不进入有效投影
            write["pending"] = not self._deps_met(context)
            activated = []
            if not write["pending"]:
                self._apply(write)
            # 新接受的 dot 可能补齐其他待定写入的前驱，级联激活
            progressed = True
            while progressed:
                progressed = False
                for w in self._writes:
                    if w["pending"] and self._deps_met(w["context"]):
                        w["pending"] = False
                        self._apply(w)
                        activated.append({"site": w["site"], "counter": w["counter"]})
                        progressed = True

            status = "pending" if write["pending"] else "active"
            code = 202 if write["pending"] else 201
            response = {
                "key": key,
                "dot": {"site": site, "counter": counter},
                "status": status,
                "activated": activated,
                "projection": self._projection(key),
            }
            write["status_code"] = code
            write["response"] = response
            self._save()
            return code, response

    # ------------------------------------------------------------------ 读取
    def projection(self, key):
        """有效投影：单个候选为确定值，多个候选为冲突证据，无候选为 None。"""
        with self._lock:
            return self._projection(key)

    def debug_state(self):
        """完整因果状态（供调度中心/验收脚本观察与重启前后比对）。"""
        with self._lock:
            keys = sorted({w["key"] for w in self._writes})
            return {
                "site_max": dict(self._site_max),
                "writes": [
                    {
                        "site": w["site"],
                        "counter": w["counter"],
                        "key": w["key"],
                        "value": w["value"],
                        "context": w["context"],
                        "pending": w["pending"],
                        "evicted": w["evicted"],
                    }
                    for w in self._writes
                ],
                "keys": {k: self._projection(k) for k in keys},
            }

    # ------------------------------------------------------------------ 内部
    def _find(self, site, counter):
        for w in self._writes:
            if w["site"] == site and w["counter"] == counter:
                return w
        return None

    def _deps_met(self, context):
        # 前驱是否已全部到达（按各站点已接受的最高计数判定）
        return all(self._site_max.get(s, 0) >= c for s, c in context.items())

    def _apply(self, write):
        """将写入纳入其键的有效投影，并按版本向量覆盖关系淘汰候选。"""
        for o in self._writes:
            if o is write or o["key"] != write["key"] or o["pending"] or o["evicted"]:
                continue
            if o["context"].get(write["site"], 0) >= write["counter"]:
                # 现有候选的因果上下文已覆盖该 dot：该写入立即过期
                write["evicted"] = True
                return
        for o in self._writes:
            if o is write or o["key"] != write["key"] or o["pending"] or o["evicted"]:
                continue
            if write["context"].get(o["site"], 0) >= o["counter"]:
                # 只淘汰被本写入版本向量覆盖的候选
                o["evicted"] = True

    def _projection(self, key):
        cands = [
            {
                "dot": {"site": w["site"], "counter": w["counter"]},
                "value": w["value"],
                "context": w["context"],
            }
            for w in self._writes
            if w["key"] == key and not w["pending"] and not w["evicted"]
        ]
        cands.sort(key=lambda c: (c["dot"]["site"], c["dot"]["counter"]))
        if not cands:
            return None
        if len(cands) == 1:
            c = cands[0]
            return {
                "key": key,
                "status": "ok",
                "value": c["value"],
                "dot": c["dot"],
                "context": c["context"],
            }
        return {"key": key, "status": "conflict", "candidates": cands}

    def _save(self):
        data = {"site_max": self._site_max, "writes": self._writes}
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
