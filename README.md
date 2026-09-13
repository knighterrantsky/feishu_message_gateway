# 飞书实时消息网关

一个独立的文本消息转发服务：飞书官方 SDK 负责长连接收发，客户端通过 WebSocket 订阅实时事件，通过 HTTP 或同一条 WebSocket 发送、回复。服务不要求工作流、AI Agent 或任何业务身份，也不需要 callback 地址。

网关不保存消息历史、持久队列或离线任务，允许消息丢失和重复。客户端负责持久化、业务处理和重连。连接、临时订阅、待写帧和短期幂等结果都有内存上限。首版单实例、单飞书应用，Python 3.12 + FastAPI。

## 快速开始

```sh
cp .env.example .env
# 填入飞书应用配置，为管理令牌和签名密钥分别生成一个独立随机值：
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
uv sync --frozen --python 3.12
uv run python -m gateway
```

1. 按 [部署说明](docs/deployment.md) 开通飞书长连接事件、接收和发送权限。
2. 管理端调用 `POST /v1/tokens`，签发指定操作和会话范围的客户端凭证。
3. 客户端连接 `/v1/ws`，首帧 `connect` 携带凭证，再发送 `subscribe`。
4. 接收事件后在客户端落盘，用真实 `message_id` 回复；`session_id` 只用于请求关联。

[完整 HTTP/WS 协议与调用示例](docs/api.md) · [设计与权限模型](docs/design/independent-backend.md) · [运行语义](docs/operations.md) · [部署与回滚](docs/deployment.md) · [验收清单](docs/acceptance.md)

Swagger UI：`http://localhost:8080/docs`；OpenAPI：`/openapi.json`，仓库副本为 [docs/openapi.json](docs/openapi.json)。WebSocket 协议独立记录在 [docs/api.md](docs/api.md)。

## 配置

环境变量只管理服务自身和密钥。客户端权限由管理 API 签发，订阅由在线连接动态提交，不绑定某一个下游。

| 变量 | 默认值 / 用途 |
| --- | --- |
| `FEISHU_APP_ID`、`FEISHU_APP_SECRET` | 必填，当前实例连接的飞书自建应用 |
| `GATEWAY_ADMIN_TOKEN` | 必填，至少 32 字符，只有签发客户端凭证的管理端持有 |
| `TOKEN_SIGNING_KEY` | 必填，至少 32 字符，与管理令牌不同；仅网关持有 |
| `TOKEN_ISSUER` / `TOKEN_AUDIENCE` | `feishu-message-gateway` / `feishu-message-gateway-clients` |
| `TOKEN_MAX_TTL_SECONDS` | `86400`，签发最长有效期；签发请求默认 `3600` 秒 |
| `USER_ALLOWLIST` / `CHAT_ALLOWLIST` | 空，逗号分隔 `open_id` / `chat_id`；仅过滤入站 |
| `PORT` / `LOG_LEVEL` | `8080` / `INFO` |
| `REQUEST_TIMEOUT_SECONDS` | `15`，SDK 单次 HTTP 网络超时；客户端等待还包含回复目标查询 |
| `SHUTDOWN_TIMEOUT_SECONDS` | `25`，大于请求超时；平台终止宽限建议至少 60 秒 |
| `MAX_CONNECTIONS` | `100`，包含等待首帧鉴权的连接 |
| `MAX_SUBSCRIPTIONS_PER_CONNECTION` | `16` |
| `MAX_MESSAGE_BYTES` | `65536`，HTTP 请求、WS 请求帧、规范化入站事件上限 |
| `CONNECTION_BUFFER_MESSAGES` / `CONNECTION_BUFFER_BYTES` | `100` / `1048576`，包含正在写出的帧 |
| `TOTAL_BUFFER_BYTES` | `33554432`，所有连接的应用层待写帧预算；不是进程 RSS 上限 |
| `WS_AUTH_TIMEOUT_SECONDS` / `WS_WRITE_TIMEOUT_SECONDS` | `10` / `5` |
| `MAX_OUTBOUND_REQUESTS` | `16`，飞书发送、回复与归属查询共享并发限制 |
| `IDEMPOTENCY_CACHE_ENTRIES` | `10000`，最多保留一小时的有界结果缓存 |
| `CODE_VERSION` | 本地 `dev`；镜像中为完整 commit SHA，Zeabur 不要覆盖 |

入站两类白名单同时配置时必须同时匹配。空白名单不限制该维度；客户端仍必须具有对应会话的 `events.receive` 权限。发送/回复由客户端令牌单独授权。群聊只转发 @ 本机器人的文本；非文本、机器人发言、其他群消息被忽略。

客户端令牌是签名、限时的 JWT，不是可自行修改的配置。默认一小时到期，重连不能延长它。管理端应通过受信渠道重新签发；网关不保存客户端账户或刷新令牌。首版没有单个令牌的立即撤销接口，详见 [凭证生命周期](docs/api.md#凭证生命周期)。

## 客户端示例

[examples/client.py](examples/client.py) 提供接收、重连、重订阅和客户端 SQLite 去重保存示例。此数据库属于客户端，网关镜像不包含示例代码。

```sh
uv run python examples/client.py --app-id cli_your_app \
  --url wss://your-domain/v1/ws --token-file ./client-token.txt \
  --chat-id oc_your_chat --db ./client-messages.sqlite3
```

示例持久化收到的消息，不执行业务或自动回复；生产客户端自行制定本地数据保留策略、发件箱及凭证续签机制。

## 验证与容器

```sh
uv run ruff check gateway tests scripts examples
uv run ruff format --check gateway tests scripts examples
uv run mypy gateway scripts examples
uv run pytest -q
uv run python -m scripts.export_openapi
docker build --build-arg CODE_VERSION="$(git rev-parse HEAD)" -t feishu-gateway:local .
docker run --rm --read-only --tmpfs /tmp:size=16m --env-file .env \
  -p 8080:8080 feishu-gateway:local
```

容器使用 UID/GID `10001:10001`，无需持久卷。仅运行一个实例和一个 Uvicorn worker。发布、重启、断线和慢连接关闭均可能丢消息；不会在恢复后补发。
