#!/usr/bin/env python3
"""验收脚本（Compose 内的 verify 服务）。

复现：并发冲突标记、待定写入激活、因果覆盖（只淘汰被覆盖候选）、
重传复现、各类明确拒绝、并发同 dot 最多接受一个、重启后状态一致；
穿插健康检查与业务接口 HTTP 冒烟。全部通过则退出码 0，否则非 0。

环境变量：
- APP_URL        服务地址（默认 http://127.0.0.1:8080）
- APP_CONTAINER  app 容器名（默认 rulesvc-app），用于 docker 重启
- DOCKER_SOCKET  docker socket 路径（默认 /var/run/docker.sock）
- RESTART_CMD    可选：自定义重启命令（本地调试用，优先于 docker socket）
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8080").rstrip("/")
APP_CONTAINER = os.environ.get("APP_CONTAINER", "rulesvc-app")
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
RESTART_CMD = os.environ.get("RESTART_CMD", "")

RUN = str(time.time_ns())


def key(name):
    """每次运行使用独立规则键，避免历史状态干扰。"""
    return f"{name}-{RUN}"


def step(msg):
    print(f"\n=== {msg} ===", flush=True)


def ok(msg):
    print(f"  [ok] {msg}", flush=True)


def fail(msg):
    print(f"  [FAIL] {msg}", flush=True)
    print("\nVERIFY FAILED", flush=True)
    sys.exit(1)


def check(cond, msg):
    if not cond:
        fail(msg)


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        APP_URL + path,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        fail(f"{method} {path} 请求异常: {e!r}")


def put(k, site, counter, value, ctx):
    return req("PUT", f"/rules/{k}", {
        "site": site, "counter": counter, "value": value, "context": ctx,
    })


def get_json(path):
    code, body = req("GET", path)
    return code, json.loads(body)


def state():
    code, s = get_json("/debug/state")
    check(code == 200, f"/debug/state 返回 {code}")
    return s


def nxt(site):
    return state()["site_max"].get(site, 0) + 1


def wait_healthy():
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(APP_URL + "/health", timeout=3) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.5)
    fail("健康检查超时：服务未就绪")


def restart_service():
    if RESTART_CMD:
        r = subprocess.run(RESTART_CMD, shell=True)
        check(r.returncode == 0, f"RESTART_CMD 退出码 {r.returncode}")
        ok("已通过 RESTART_CMD 重启服务")
        return
    if os.path.exists(DOCKER_SOCKET):
        class UnixConn(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(DOCKER_SOCKET)

        conn = UnixConn("docker")
        conn.request("POST", f"/containers/{APP_CONTAINER}/restart?t=10")
        resp = conn.getresponse()
        resp.read()
        check(resp.status == 204, f"docker 重启容器返回 {resp.status}")
        ok(f"已通过 docker socket 重启容器 {APP_CONTAINER}")
        return
    print("  [warn] 无 RESTART_CMD 且 docker socket 不可用，跳过真实重启", flush=True)


def candidates_of(proj):
    return {(c["dot"]["site"], c["dot"]["counter"]): c["value"] for c in proj.get("candidates", [])}


def main():
    step("0. 等待服务就绪（健康检查冒烟）")
    wait_healthy()
    ok("GET /health -> 200")

    step("1. 业务接口冒烟：读取不存在的规则键 -> 404")
    code, body = get_json(f"/rules/{key('nope')}")
    check(code == 404 and body["error"] == "not_found", f"期望 404 not_found，实际 {code} {body}")
    ok("GET /rules/{missing} -> 404 not_found")

    # ------------------------------------------------------------------
    step("2. 并发写入同一规则键 -> 读取明确标记冲突并列出候选")
    k1 = key("conflict")
    a1 = nxt("alpha")
    code, body = put(k1, "alpha", a1, "alpha-ruling", {"alpha": a1 - 1})
    check(code == 201 and json.loads(body)["status"] == "active", f"alpha 写入失败: {code} {body}")
    b1 = nxt("beta")
    code, body = put(k1, "beta", b1, "beta-ruling", {"beta": b1 - 1})
    check(code == 201, f"beta 写入失败: {code} {body}")
    code, proj = get_json(f"/rules/{k1}")
    check(code == 200 and proj["status"] == "conflict", f"期望 conflict，实际 {code} {proj}")
    got = candidates_of(proj)
    check(got == {("alpha", a1): "alpha-ruling", ("beta", b1): "beta-ruling"},
          f"冲突候选不符: {got}")
    for c in proj["candidates"]:
        check("dot" in c and "context" in c and "value" in c, f"候选缺少 dot/context/value: {c}")
    ok(f"冲突候选 = {sorted(got)}，各含 dot 与因果上下文")

    # ------------------------------------------------------------------
    step("3. 前驱未到的写入待定，不进入投影；前驱补齐后自动激活")
    k2, k3 = key("pending"), key("predecessor")
    g1 = nxt("gamma")
    a2 = nxt("alpha")
    code, body = put(k2, "alpha", a2, "alpha-awaiting-gamma", {"alpha": a1, "gamma": g1})
    check(code == 202 and json.loads(body)["status"] == "pending",
          f"期望 202 pending，实际 {code} {body}")
    code, _ = get_json(f"/rules/{k2}")
    check(code == 404, "待定写入不得进入有效投影")
    ok("alpha 的写入因前驱 gamma 事件未到而待定（202，读取 404）")
    code, body = put(k3, "gamma", g1, "gamma-event", {"gamma": g1 - 1})
    act = json.loads(body).get("activated", [])
    check(code == 201, f"gamma 写入失败: {code} {body}")
    check({"site": "alpha", "counter": a2} in act, f"前驱到达后未激活待定写入: {act}")
    code, proj = get_json(f"/rules/{k2}")
    check(code == 200 and proj["status"] == "ok" and proj["value"] == "alpha-awaiting-gamma",
          f"激活后投影不符: {code} {proj}")
    check(proj["dot"] == {"site": "alpha", "counter": a2}, f"激活后 dot 不符: {proj}")
    ok("前驱补齐后待定写入自动生效")

    # ------------------------------------------------------------------
    step("4. 因果覆盖：只淘汰版本向量已覆盖的候选")
    k4 = key("overwrite")
    a3 = nxt("alpha")
    put(k4, "alpha", a3, "red", {"alpha": a2})
    b2 = nxt("beta")
    put(k4, "beta", b2, "blue", {"beta": b1})
    g2 = nxt("gamma")
    put(k4, "gamma", g2, "green", {"gamma": g1})
    code, proj = get_json(f"/rules/{k4}")
    check(proj["status"] == "conflict" and len(proj["candidates"]) == 3,
          f"期望三方冲突，实际 {proj}")
    ok("三方并发冲突成立（alpha/beta/gamma）")
    a4 = nxt("alpha")
    code, body = put(k4, "alpha", a4, "red+blue", {"alpha": a3, "beta": b2})
    check(code == 201, f"alpha 裁决写入失败: {code} {body}")
    first_a4_response = body
    code, proj = get_json(f"/rules/{k4}")
    got = candidates_of(proj)
    check(got == {("alpha", a4): "red+blue", ("gamma", g2): "green"},
          f"部分覆盖后候选不符（gamma 未被覆盖，应保留）: {got}")
    ok("alpha 的裁决只淘汰其上下文覆盖的 alpha/beta 候选，gamma 候选保留")
    g3 = nxt("gamma")
    code, body = put(k4, "gamma", g3, "final", {"gamma": g2, "alpha": a4})
    check(code == 201, f"gamma 裁决写入失败: {code} {body}")
    code, proj = get_json(f"/rules/{k4}")
    check(proj["status"] == "ok" and proj["value"] == "final" and
          proj["dot"] == {"site": "gamma", "counter": g3},
          f"因果覆盖后应收敛为确定值: {proj}")
    ok("覆盖全部候选后收敛为确定值 final")

    # ------------------------------------------------------------------
    step("5. 重传复现首次结果；各类非法写入被明确拒绝且读取不变")
    code, body = put(k4, "alpha", a4, "red+blue", {"alpha": a3, "beta": b2})
    check(code == 201, f"重传应复现首次状态码 201，实际 {code}")
    check(body == first_a4_response, "重传响应应与首次响应逐字节一致（复现首次结果）")
    ok("相同 dot 相同内容重传 -> 与首次响应完全一致（201，幂等）")

    code, body = put(k4, "alpha", a4, "tampered", {"alpha": a3, "beta": b2})
    check(code == 409 and json.loads(body)["error"] == "dot_content_conflict",
          f"同 dot 不同内容应 409，实际 {code} {body}")
    ok("相同 dot 不同内容 -> 409 dot_content_conflict")

    jump = nxt("alpha") + 2
    code, body = put(key("jump"), "alpha", jump, "skip", {"alpha": a4})
    b = json.loads(body)
    check(code == 409 and b["error"] == "counter_jump" and b["expected"] == nxt("alpha")
          and b["got"] == jump, f"计数跳跃应 409，实际 {code} {b}")
    ok(f"计数跳跃 -> 409 counter_jump (expected={b['expected']}, got={b['got']})")

    code, body = put(key("ghost"), "ghost", 1, "x", {})
    check(code == 403 and json.loads(body)["error"] == "unknown_site",
          f"未知站点应 403，实际 {code} {body}")
    ok("未知站点 -> 403 unknown_site")

    nb = nxt("beta")
    code, body = put(key("contra"), "beta", nb, "x", {"beta": nb})
    check(code == 400 and json.loads(body)["error"] == "contradictory_context",
          f"自相矛盾向量应 400，实际 {code} {body}")
    ok("自相矛盾向量（context 含自身 dot）-> 400 contradictory_context")

    code, body = put(key("neg"), "beta", nb, "x", {"beta": -1})
    check(code == 400 and json.loads(body)["error"] == "invalid_request",
          f"负计数上下文应 400，实际 {code} {body}")
    code, body = put(key("zero"), "beta", 0, "x", {})
    check(code == 400 and json.loads(body)["error"] == "invalid_request",
          f"计数 0 应 400，实际 {code} {body}")
    ok("非法计数/上下文 -> 400 invalid_request")

    code, proj = get_json(f"/rules/{k4}")
    check(proj["status"] == "ok" and proj["value"] == "final",
          f"拒绝后读取结果应保持不变: {proj}")
    check(nxt("alpha") == a4 + 1 and nxt("beta") == nb, "拒绝后站点计数不应前进")
    ok("所有拒绝均未改变读取结果与站点计数")

    # ------------------------------------------------------------------
    step("6. 并发提交同一 dot 的不同内容 -> 最多接受一个")
    k6 = key("race")
    d1 = nxt("delta")
    codes = []
    lock = threading.Lock()

    def worker(i):
        code, _ = put(k6, "delta", d1, f"contender-{i}", {"delta": d1 - 1})
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = codes.count(201)
    check(wins == 1 and codes.count(409) == 7,
          f"并发同 dot 应恰好接受一个，实际状态码分布: {sorted(codes)}")
    code, proj = get_json(f"/rules/{k6}")
    check(proj["status"] == "ok", f"并发后投影应为单候选确定值: {proj}")
    ok(f"8 路并发同 dot：201 x{wins}，409 x{codes.count(409)}")

    # ------------------------------------------------------------------
    step("7. 重启后已接受 dot、待定写入、冲突候选与重启前一致")
    k7 = key("long-pending")
    nb = nxt("beta")
    far = nxt("gamma") + 2  # 依赖更晚的 gamma 事件，保持待定
    code, body = put(k7, "beta", nb, "beta-long-pending", {"beta": nb - 1, "gamma": far})
    check(code == 202, f"期望 202 pending，实际 {code} {body}")
    code, _ = get_json(f"/rules/{k7}")
    check(code == 404, "待定写入不应进入投影")
    pre = state()
    ok(f"重启前快照：site_max={pre['site_max']}，待定写入 beta/{nb}")

    restart_service()
    wait_healthy()
    ok("服务已重启并通过健康检查")

    post = state()
    check(pre == post, "重启后因果状态与重启前不一致:\npre=%s\npost=%s" %
          (json.dumps(pre, sort_keys=True), json.dumps(post, sort_keys=True)))
    ok("已接受 dot、待定写入、冲突候选与重启前完全一致")
    code, _ = get_json(f"/rules/{k7}")
    check(code == 404, "重启后待定写入仍不应进入投影")
    code, proj = get_json(f"/rules/{k1}")
    check(proj["status"] == "conflict" and len(proj["candidates"]) == 2,
          f"重启后冲突候选应保持: {proj}")
    ok("重启后：待定写入仍待定，冲突候选完整保留")

    k8 = key("catchup")
    last = None
    while state()["site_max"].get("gamma", 0) < far:
        g = nxt("gamma")
        code, body = put(k8, "gamma", g, f"gamma-catchup-{g}", {"gamma": g - 1})
        check(code == 201, f"gamma 补齐写入失败: {code} {body}")
        last = json.loads(body)
    check({"site": "beta", "counter": nb} in last.get("activated", []),
          f"重启后前驱补齐应激活跨重启的待定写入: {last}")
    code, proj = get_json(f"/rules/{k7}")
    check(proj["status"] == "ok" and proj["value"] == "beta-long-pending",
          f"跨重启待定写入激活后投影不符: {proj}")
    ok("重启后补齐前驱，跨重启的待定写入自动生效")

    # ------------------------------------------------------------------
    step("8. 收尾健康检查冒烟")
    code, body = get_json("/health")
    check(code == 200 and body["status"] == "ok", f"健康检查异常: {code} {body}")
    ok("GET /health -> 200")

    print("\nVERIFY PASS", flush=True)


if __name__ == "__main__":
    main()
