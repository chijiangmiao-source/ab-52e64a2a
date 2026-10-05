# 轨道维护规则汇合服务（rulesvc）

多个地面站离线编辑同一轨道维护规则后向调度中心汇合。中心以**点版本向量
（Dotted Version Vectors, DVV）**语义保留真正并发的不同裁决，不因提交先后
静默覆盖；调度中心可通过 HTTP 观察确定值或冲突证据。

## 语义模型

- 每次写入携带 **dot** `(site, counter)` 与**因果上下文** `context`
  （`{site: counter}`，即该写入所见的各站点事件）。
- 同一规则键下，因果上下文**互不覆盖**的候选并存：读取返回
  `status=conflict`，列出全部候选及各自的 dot 与因果上下文。
- 写入依赖的前驱（context 中的事件）尚未到达时，写入进入**待定**
  （`202 pending`），不进入有效投影；前驱补齐后**自动激活**生效。
- 写入/激活只淘汰其版本向量（context + 自身 dot）**已覆盖**的候选；
  未被覆盖的并发候选保留。
- 幂等与拒绝（拒绝不产生任何状态变更，读取结果保持不变）：
  - 相同 dot、相同内容的重传：**复现首次结果**（状态码与响应体逐字节一致）；
  - 相同 dot、不同内容：`409 dot_content_conflict`（并发同 dot 时最多接受一个）；
  - 计数跳跃（counter 非该站点下一个）：`409 counter_jump`；
  - 未知站点：`403 unknown_site`（context 引用未知站点为 `400 unknown_context_site`）；
  - 自相矛盾向量（context 包含自身 dot 或更晚的自身事件）：`400 contradictory_context`；
  - 非法计数/上下文：`400 invalid_request`。
- 每次状态变更原子落盘（tmp + fsync + rename）。**服务重启后，已接受 dot、
  待定写入、冲突候选与重启前完全一致**；跨重启的待定写入在前驱补齐后照常激活。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查，`200 {"status":"ok"}` |
| PUT | `/rules/{key}` | 规则写入：`{"site","counter","value","context"}`；`201` 生效 / `202` 待定 |
| GET | `/rules/{key}` | 规则读取：`ok`（确定值）/ `conflict`（候选列表）/ `404` |
| GET | `/debug/state` | 完整因果状态（site_max、全部写入含待定/淘汰标记、各键投影） |

示例：

```sh
# 站点 alpha 的第一个写入
curl -X PUT localhost:8080/rules/orbit-1 \
  -H 'Content-Type: application/json' \
  -d '{"site":"alpha","counter":1,"value":"raise-apogee","context":{}}'

# 站点 beta 的并发写入（上下文不含 alpha/1）-> 冲突
curl -X PUT localhost:8080/rules/orbit-1 \
  -H 'Content-Type: application/json' \
  -d '{"site":"beta","counter":1,"value":"lower-perigee","context":{}}'

# 读取：status=conflict，列出两个候选及各自 dot 与因果上下文
curl localhost:8080/rules/orbit-1

# alpha 的裁决覆盖两者 -> 收敛为确定值
curl -X PUT localhost:8080/rules/orbit-1 \
  -H 'Content-Type: application/json' \
  -d '{"site":"alpha","counter":2,"value":"merged","context":{"alpha":1,"beta":1}}'
```

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HOST_PORT` | `8080` | **宿主机端口**（compose 端口映射，可配置） |
| `PORT` | `8080` | 容器内监听端口 |
| `SITES` | `alpha,beta,gamma,delta` | 已知站点注册表（逗号分隔），之外站点的写入被拒绝 |
| `DATA_DIR` | `/data` | 状态文件目录（挂卷以跨重启持久化） |

## 运行验收（Docker Compose）

```sh
# 构建镜像（构建期自动执行代码测试）-> 启动 app -> verify 执行验收 -> 以退出码报告结果
docker compose up --build --exit-code-from verify --abort-on-container-exit
echo $?   # 0 = 验收通过，非 0 = 失败

docker compose down -v   # 清理（含持久卷，重置状态）
```

verify 服务依次执行：健康检查与业务接口 HTTP 冒烟 → 并发冲突标记 →
待定写入激活 → 因果覆盖（含“只淘汰被覆盖候选”）→ 重传复现与各类拒绝 →
并发同 dot 最多接受一个 → **通过 docker socket 重启 app 容器**并比对重启前后
的已接受 dot、待定写入与冲突候选 → 跨重启待定写入激活。全部通过退出码 0。

## 本地运行（无 Docker）

```sh
python3 -m unittest discover -s tests -t . -v     # 代码测试

PORT=18080 DATA_DIR=/tmp/rulesvc-data nohup python3 -m app.server &  # 启动服务
APP_URL=http://127.0.0.1:18080 RESTART_CMD=/workspace/restart_local.sh \
  python3 verify/verify.py                        # 完整验收（含重启一致性）
```

## 结构

```
app/store.py        DVV 核心：接受/拒绝、待定与激活、覆盖淘汰、原子持久化
app/server.py       HTTP 入口（health、rules 读写、debug/state）
app/healthcheck.py  容器健康检查
tests/              单元测试（镜像构建期执行）
verify/verify.py    Compose 验收程序（退出码报告验收结果）
Dockerfile          多阶段：test（代码测试）-> app / verify
docker-compose.yml  app（可配置宿主机端口）+ verify
```
