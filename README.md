# 星载指令包修订服务

地面站与多版本维护终端之间往返编辑星载指令包的协作服务。核心目标：

- 旧终端不认识的新字段（扩展字段）**绝不静默抹去**——原始扩展子树与可编辑核心字段**分离持久化**，旧终端提交时其未知子树**逐字节保留**；
- 并发修改**不伪装成安全合并**——两个陈旧修订仅在各自实际改动的规范路径不重叠时才合并；同一路径不同值、删除未知/未声明字段、同一请求标识替换载荷一律拒绝且不改写既有版本；
- 页面重开或服务重启后，相同请求**回放同一修订与规范摘要**，新终端读取仍得到完整扩展内容。

## 组成

```
app/packager/        服务源码（FastAPI + SQLite）
  rawjson.py         字节级 JSON 成员切分（不重序列化扩展子树）
  core.py            规范路径代数、diff、合并裁决（纯函数，可单测）
  envelope.py        请求包络解析与校验（EnvelopeError→422, RejectError→409）
  store.py           SQLite：核心字段与原始扩展子树分离存储、请求回放、裁决日志
  server.py          HTTP API + 审查员网页
  static/index.html  审查台页面（创建/提交局部修改/查看修订·摘要·扩展字段·裁决）
verify/
  tests/test_envelope_contract.py   包络契约测试（18 项，直接测核心模块）
  tests/smoke.py                    HTTP 冒烟（字段保留/冲突/健康，10 项）
  run_verify.sh                     契约测试 → 构建检查 → HTTP 冒烟，退出码即验收结果
docker-compose.yml
```

## 运行

```bash
# 默认宿主端口 8080；可用 APP_HOST_PORT 覆盖
APP_HOST_PORT=9090 docker compose up --build --exit-code-from verify --abort-on-container-exit
echo $?   # verify 容器的退出码即验收结果：0=全部通过，非 0=失败
```

- `app`：指令包服务，健康检查 `GET /healthz`，数据存于命名卷 `app-data`（重启后回放一致）。
- `verify`：等 `app` 健康后依次执行 ①包络契约测试 ②构建检查（compileall + 服务模块导入）③针对字段保留、冲突裁决与健康接口的 HTTP 冒烟，全部通过以退出码 0 结束。

打开 `http://localhost:8080/` 使用审查台：创建含核心字段与扩展字段的指令包、以终端声明的已知字段集合提交基于指定修订的局部修改，并查看当前修订、规范摘要（sha256）、被保留的扩展字段原始字节与每次裁决记录。

## API 摘要

| 方法/路径 | 说明 |
|---|---|
| `GET /healthz` | 健康检查 `{"status":"ok"}` |
| `POST /api/packages` | 创建指令包。包络：`{request_id, terminal:{id,known_fields}, document:{...}}`；核心字段入结构化存储，其余字段原始字节入扩展存储 |
| `POST /api/packages/{id}/revisions` | 提交局部修改。包络：`{request_id, base_revision, terminal, patch:{set:{路径:值}, delete:[路径]}}` |
| `GET /api/packages/{id}` | 完整文档（核心 + 逐字节拼接的扩展子树），响应头带 `X-Package-Revision` / `X-Package-Summary` |
| `GET /api/packages/{id}/meta` | 当前修订、规范摘要、核心/扩展字段清单、扩展子树原始字节 |
| `GET /api/packages/{id}/adjudications` | 历次裁决（created/applied/merged/replayed/noop/rejected 及理由） |

## 裁决规则

1. **幂等回放**：`request_id` 一经裁决（创建/应用/合并/空操作/拒绝）即被消耗：相同载荷 → 回放原裁决与原状态码（重启后仍成立）；不同载荷 → `409 request_id_reuse`，不改写任何版本。拒绝裁决同样消耗该标识，拒绝不会让它在后续请求中重新可用。
2. **字段边界**：补丁路径的顶层字段必须属于核心 schema（否则 `409 unknown_field`）且在终端声明的 `known_fields` 内（否则 `409 undeclared_field`）——删除未知字段即在此被拒绝。
3. **合并**：`base_revision` 落后于当前修订时，仅当补丁**实际改动**的规范路径（对基修订求 diff，支持前缀重叠判定）与中间修订的改动路径不重叠才合并（`merged`）；同一路径不同值 → `409 conflicting_paths`；同值或删除已不存在路径属无害重叠，允许合并。
4. **空补丁**：不产生新修订，返回 `noop` 与当前修订。

规范摘要 = 完整文档（核心 + 扩展解析值）的 canonical JSON（键排序、紧凑分隔）之 SHA-256，与字段顺序、扩展子树的原始空白/转义无关。
