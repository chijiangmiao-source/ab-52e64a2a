# Orbit Maintenance Rule Service

地面站离线编辑、汇合（rendezvous）场景下的轨道维护规则服务。基于
**dot + 版本向量（version vector）因果模型**，保证调度中心：

- 不会因提交先后而静默覆盖真正并发的不同裁决——读取明确返回 `conflict`，
  并列出每个候选的 `dot`、内容与因果 `context`；
- 前驱缺失的写入保持 `pending`，不进入有效投影；前驱补齐后自动级联生效；
- 只淘汰版本向量已被覆盖的候选，并发候选继续作为冲突证据保留；
- 相同 dot 的相同内容重传幂等复现首次结果；相同 dot 的不同内容（equivocation）、
  计数跳跃、未知站点、自相矛盾向量一律明确拒绝，读取结果不变；
- 并发提交同一 dot 的不同内容最多接受一个；
- accepted dot、pending 写入、冲突候选均原子快照持久化，重启后完全一致。

纯 Python 3.11 标准库实现，无第三方运行时依赖。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查，返回站点白名单 |
| GET | `/rules/<key>` | 读取规则：`ok`（确定值+winner）/ `conflict`（候选及 dot、context）/ `empty`（含 pending 清单） |
| POST | `/rules/<key>` | 提交站点写入 |

写入请求体：

```json
{
  "site": "station-alpha",
  "count": 2,
  "content": "burn-window 08:00-08:30",
  "context": {"station-alpha": 1, "station-beta": 1}
}
```

- `dot = (site, count)`；站点自己的计数必须连续，即 `context[site] == count-1`。
- `context` 中声明的每个 `(site, n)` 都是该写入因果依赖的前驱 dot。
- 前驱未到达时返回 `202 {"state":"pending","waiting_for":[...]}`。
- dot 冲突内容返回 `409`，其余非法写入返回 `400`，均不改变既有读取结果。

## 运行

宿主机端口可通过 `HOST_PORT` 配置：

```bash
HOST_PORT=9090 docker compose up -d --build orbit
curl -s localhost:9090/healthz
```

## 验收（verify）

```bash
docker compose run --build --rm verify ; echo "exit=$?"
```

verify 服务在全新的临时实例（tmpfs）上穿插执行三段，全部通过退出码 0：

1. **代码测试** —— `python -m unittest discover -s tests`（因果语义、拒绝规则、
   并发同 dot、重启持久化、HTTP 冒烟）；
2. **镜像构建** —— `--build` 构建 `orbit-rule-crdt:latest`；
3. **HTTP 冒烟** —— 健康检查 + `scripts/verify_scenario.py` 复现
   pending 写入自动激活、因果覆盖仅淘汰被覆盖候选、冲突证据保留与拒绝语义。

无 Docker 时的本地等价运行：

```bash
python -m unittest discover -s tests -v          # 1+3 中的代码测试
HOST=127.0.0.1 PORT=8080 DATA_DIR=./data python -m app.server &
BASE_URL=http://127.0.0.1:8080 python scripts/verify_scenario.py
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | 容器内监听端口 |
| `HOST_PORT` | `8080` | Compose 映射的宿主机端口 |
| `STATIONS` | `station-alpha,station-beta,station-gamma` | 合法站点白名单 |
| `DATA_DIR` | `/data`（容器）/ `./data`（本地） | 快照目录 |
