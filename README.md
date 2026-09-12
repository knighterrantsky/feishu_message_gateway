# 飞书消息网关

单实例 Python 3.12 + FastAPI + 飞书官方 `lark-oapi==1.7.3`，使用 SQLite WAL 保存消息队列、幂等键、投递状态。通过长连接接收单聊文本、群内 @ 本机器人的文本，异步转发到一个 Webhook；HTTP API 支持发文本、回复、查询状态和死信重放。

## 快速开始

```sh
cp .env.example .env
# 填入真实配置；生成两份独立密钥：python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
uv sync --frozen --python 3.12
uv run python -m gateway
```

- Swagger UI：`http://localhost:8080/docs`
- OpenAPI：`http://localhost:8080/openapi.json`；仓库静态文档：[docs/openapi.json](docs/openapi.json)
- [接口与签名示例](docs/api.md)
- [Zeabur、GitHub Actions 部署与回滚](docs/deployment.md)
- [可靠性语义与运行维护](docs/operations.md)
- [验收清单](docs/acceptance.md)

## 配置

所有配置均来自环境变量，本地可使用 `.env`。缺少必填字段或参数越界时启动退出，日志只列出无效字段名。

| 变量 | 默认值 / 说明 |
| --- | --- |
| `FEISHU_APP_ID` | 必填，自建应用 App ID |
| `FEISHU_APP_SECRET` | 必填，应用密钥 |
| `WEBHOOK_URL` | 必填，HTTP(S) 地址；生产使用 HTTPS |
| `WEBHOOK_SIGNING_SECRET` | 必填，至少 32 字符，与接收方约定 |
| `API_ACCESS_TOKEN` | 必填，至少 32 字符，独立于签名密钥 |
| `USER_ALLOWLIST` | 空，逗号分隔的用户 `open_id` |
| `CHAT_ALLOWLIST` | 空，逗号分隔的 `chat_id`，对单聊和群聊都生效 |
| `DATA_DIR` | 本地 `./data`；Docker `/data`，必须持久化 |
| `PORT` | `8080`，监听 `0.0.0.0` |
| `LOG_LEVEL` | `INFO`，支持 DEBUG / INFO / WARNING / ERROR |
| `RETRY_MAX_ATTEMPTS` | `8`，每轮最多尝试次数，包含首次 |
| `RETRY_BASE_SECONDS` | `2`，指数退避初始间隔 |
| `RETRY_MAX_SECONDS` | `300`，退避上限，附加 0.8–1.0 倍抖动 |
| `REQUEST_TIMEOUT_SECONDS` | `15`，上游/下游网络请求超时 |
| `SHUTDOWN_TIMEOUT_SECONDS` | `25`，须大于请求超时 |
| `CODE_VERSION` | 本地 `dev`；镜像构建为完整 Git SHA，Zeabur 不要覆盖 |

白名单是**入站消息过滤规则**。空列表表示该维度不限；两个列表均配置时必须同时满足。API Token 持有者可向应用有权限的会话发消息。群聊检查 mention 中的 `open_id` 是否匹配通过飞书接口自动获取的机器人身份，不会把 @ 普通用户误认为 @ 机器人。忽略非文本、其他群消息和机器人发送的消息。

## 本地验证

```sh
uv run ruff check gateway tests scripts
uv run ruff format --check gateway tests scripts
uv run mypy gateway scripts/deploy.py
uv run pytest -q
uv run python -m scripts.export_openapi
docker build --build-arg CODE_VERSION="$(git rev-parse HEAD)" -t feishu-gateway:local .
docker run --rm --env-file .env -e DATA_DIR=/data -p 8080:8080 \
  -v feishu-gateway-data:/data feishu-gateway:local
```

生产只能运行一个实例、一个 Uvicorn worker。接收进程和 HTTP/投递主进程属于同一服务，共享同一 SQLite 数据库。不要使用 `uvicorn --workers` 或水平扩容。
