# HTTP 与 WebSocket 协议 v1

生产使用 HTTPS/WSS。HTTP 使用 `Authorization: Bearer <token>`；WS 在 10 秒内发送 `connect` 首帧，令牌不得放进 URL。管理令牌只调用签发接口，普通客户端只持有自己的限时权限令牌。

## 签发权限凭证

`POST /v1/tokens` 使用 `GATEWAY_ADMIN_TOKEN` 鉴权。签发请求由受信管理端构造，不把未经审核的客户端自报权限直接转交本接口。

```sh
umask 077
curl --fail-with-body -sS "$GATEWAY_URL/v1/tokens" \
  -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"principal_id":"client-a","ttl_seconds":3600,"grants":[
    {"app_id":"cli_your_app","operations":["events.receive","messages.reply"],
     "chat_ids":["oc_chat_a","oc_chat_b"]}
  ]}' | jq -er '.access_token' > client-token.txt
```

成功响应 200：`{"access_token":"…","token_type":"Bearer","expires_at":1789260000}`，带 `Cache-Control: no-store`。不要将管理令牌分发给客户端；签发响应、令牌文件和签名密钥不提交 Git。`principal_id` 是管理端分配的稳定技术标识，不要求账户注册；不同独立调用方应分配不同标识。

| 操作 | 作用 | 所需资源 |
| --- | --- | --- |
| `events.receive` | 建立临时订阅、接收事件 | `app_id` + `chat_ids` |
| `messages.send` | 向会话发送新文本 | `app_id` + `chat_ids` |
| `messages.reply` | 回复已有消息 | `app_id` + 消息真实所属 `chat_ids` |
| `status.read` | 查看实例运行统计 | `app_id`，可不填 `chat_ids` |

每条 grant 中的操作与会话范围成组关联，多个 grant 取并集。除 `status.read` 外必须显式指定会话列表；`["*"]` 是管理员明确授予当前应用所有可访问会话的权限，包含未来会话，不能由订阅自行获得。当前实例只签发已配置 App ID 的权限。凭证授权不扩大飞书应用本身的权限。

### 凭证生命周期

- HS256 验签固定算法，并验证签发者、受众、签发时间、到期时间；令牌内容可解码，但不能篡改权限。
- 请求默认有效期 3600 秒，受 `TOKEN_MAX_TTL_SECONDS` 限制，默认最大 86400 秒。
- WS 在空闲时也会按到期时间关闭，原因 `token_expired`。每次事件转发和每次命令还会检查到期状态。
- 续期由受信管理端重新签发并安全交付。客户端重新连接、重新订阅；业务方无法自行续期或升级权限。
- 新发一个权限更窄的令牌，不会使旧令牌失效。旧令牌到期前仍保有原授权。首版不提供逐个撤销、持久凭证表或刷新令牌。
- 如需立即撤销全部客户端，轮换 `TOKEN_SIGNING_KEY` 并重启实例，所有旧连接及令牌失效；只轮换管理令牌不会撤销已签发令牌。
- 同一逻辑客户端续签保持 `principal_id` 不变，以保留稳定的上游幂等 UUID。不要把签名密钥交给外部签发者或普通客户端。

## HTTP 接口

### 发送文本

`POST /v1/messages`，需要 `messages.send`；必填 `Idempotency-Key`（1–128 个可见 ASCII 字符）。目标只接受飞书 `chat_id`，单聊/群聊都使用此格式。

```sh
curl --fail-with-body "$GATEWAY_URL/v1/messages" \
  -H "Authorization: Bearer $CLIENT_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: order-123-send-1' \
  -d '{"chat_id":"oc_chat_a","text":"你好","session_id":"local-conversation-123"}'
```

成功 200：

```json
{"status":"sent","message_id":"om_sent","session_id":"local-conversation-123"}
```

`text` 为 1–10000 字符，完整请求默认最多 64 KiB。`session_id` 可省略，为客户端关联标签；不会创建服务器会话、赋予权限或改变目的地。

### 回复指定消息

`POST /v1/messages/{message_id}/replies`，需要 `messages.reply`，同样必填幂等键。

```sh
curl --fail-with-body "$GATEWAY_URL/v1/messages/om_original/replies" \
  -H "Authorization: Bearer $CLIENT_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: original-reply-1' \
  -d '{"text":"收到","session_id":"local-conversation-123"}'
```

网关通过飞书查询消息实际 `chat_id` 再授权，客户端不提交用于授权的 `chat_id`。查询失败或无访问权不会继续发送；缓存命中时也重新验证归属和当前凭证范围。此接口需要飞书查询消息权限。成功结构与发送相同。

### 状态

- `GET /healthz`：公开，进程存活及代码版本。
- `GET /readyz`：公开，SDK 心跳健康、接收循环工作中返回 200，否则 503；不要求在线订阅者。
- `GET /v1/status`：需 `status.read`；返回 `version`、`boot_id`、`long_connection`、`receiver_restarts`、`connections`、`subscriptions`、`buffered_bytes`、`outbound_active`、`outbound_completed`、`outbound_failed`、`last_outbound_result`、`received`、`unrouted`、`dropped`、`slow_consumers`。统计不包含消息正文。

## WebSocket

路径 `/v1/ws`，仅接受 JSON 文本帧。协议版本 1。一个连接可以处理多段业务对话；重连产生新 `connection_id`，所有临时订阅从空开始。客户端需处理响应与事件交错，使用请求 `id` 关联响应，不把下一帧一律当成某个请求的结果。

请求：

```json
{"type":"req","id":"r1","method":"connect","params":{"token":"<client-token>"}}
```

首帧成功响应：

```json
{"type":"res","id":"r1","ok":true,"payload":{"connection_id":"…","boot_id":"…","protocol_version":1,"expires_at":1789260000}}
```

认证失败、首帧超时或畸形协议帧会关闭连接；此时不保证存在错误响应。连接总数用尽时拒绝升级。通过握手后，合法 RPC 的业务错误返回 `ok:false`。

### 订阅与取消

```json
{"type":"req","id":"r2","method":"subscribe","params":{"subscription_id":"incoming","filter":{"app_id":"cli_your_app","event_types":["message.received"],"chat_ids":["oc_chat_a"],"sender_ids":["ou_user_a"]}}}
```

过滤规则：

- `app_id` 必填，首版 `event_types` 只支持 `message.received`，可省略使用默认值。
- `chat_ids`、`sender_ids` 可省略。省略表示该维度不进一步过滤，仍受凭证授权限制；空数组和字面量 `*` 无效。
- `sender_ids` 使用飞书 `open_id`，只作为进一步过滤条件，不能提升权限。
- 显式提交任意一个未授权会话时，整个订阅失败 `forbidden`，不会静默建立部分订阅。
- 同一连接内，相同订阅 ID + 相同规范化过滤条件可重复提交；同 ID + 不同条件返回 `subscription_conflict`。修改时先取消后重建。
- 一条事件匹配本连接多个订阅时只发一份，`subscription_ids` 列出全部匹配 ID；不同获授权连接分别收到一份。

成功响应包含实际 `subscription_id` 和规范化 `filter`。取消只作用于当前连接：

```json
{"type":"req","id":"r3","method":"unsubscribe","params":{"subscription_id":"incoming"}}
```

取消后不再为该订阅新增事件；此前已排队的帧可能先于取消响应到达。另一个连接的同名订阅不受影响。

### 推送事件

```json
{
  "type":"event","event":"message.received","seq":1,
  "subscription_ids":["incoming"],
  "payload":{
    "schema_version":"1.0","type":"message.received",
    "app_id":"cli_your_app","event_id":"ev_123","message_id":"om_123",
    "chat_id":"oc_chat_a","conversation_id":"feishu:cli_your_app:oc_chat_a",
    "chat_type":"p2p","sender":{"open_id":"ou_user_a"},
    "text":"你好","mentions":[],"create_time":"1700000000000"
  }
}
```

`create_time` 保留飞书毫秒 Unix 时间字符串；`sender` 和 `mentions` 保留 SDK 可用的标识字段。`conversation_id` 按应用和会话区分，不建立 Agent 上下文；首版不提供独立主题会话路由。

`seq` 仅为当前连接已匹配事件的递增序号，重连重置，不能用作恢复游标或检测所有上游遗漏。客户端建议按 `(app_id, message_id)` 持久去重，保存 `event_id` 用于追踪。重复事件仍可能被转发。

### 发送、回复和状态 RPC

```json
{"type":"req","id":"r4","method":"messages.send","params":{"chat_id":"oc_chat_a","text":"你好","idempotency_key":"send-123","session_id":"local-123"}}
```

```json
{"type":"req","id":"r5","method":"messages.reply","params":{"message_id":"om_123","text":"收到","idempotency_key":"reply-123","session_id":"local-123"}}
```

```json
{"type":"req","id":"r6","method":"status.get","params":{}}
```

权限、返回值和幂等行为与 HTTP 共用。一个连接串行执行 RPC，慢发送会延迟该连接后续 RPC；事件写出独立进行。需要独立命令并发时可调用 HTTP，仍受全实例出站并发限制。不要重复使用未完成请求的 `id`。

## 幂等与结果不确定

客户端先保存幂等键，再提交请求。网关将 `(app_id, principal_id, send/reply, idempotency_key)` 确定性映射为 36 字符 UUID，传给官方发送/回复接口。HTTP 与 WS、同一客户端续签、网关重启均得到相同 UUID。

内存缓存默认最多 10000 条、一小时，保存请求指纹和结果，不保存正文历史。同键在缓存有效期内改变目标或正文返回 409；`session_id` 不参与指纹。成功返回缓存结果，并发重复请求合并；失败没有后台重试，新的一次显式同键请求可以重新调用飞书。网关重启后不再具有原内容指纹或结果。

飞书官方文档规定相同 `uuid` 一小时内最多成功发送一次，长度上限 50 字符，但未承诺每次重复请求都返回最初的 `message_id`，也不提供本服务可用的按 UUID 查询结果接口。该窗口不等于网关承诺跨任意时间去重。客户端必须保存原目标、正文和键；超过窗口不能把同键重试当成必然无重复。[发送文档](https://open.feishu.cn/document/server-docs/im-v1/message/create)、[回复文档](https://open.feishu.cn/document/server-docs/im-v1/message/reply)

发送发起后超时、网络异常或成功响应缺少消息 ID：`outcome_unknown`。连接在响应前断开时，客户端同样必须认为结果可能已经成功；断开不会撤回正在执行的飞书调用。网关不自动重试。收到明确 API 拒绝时返回安全的错误代码；客户端决定是否在上游幂等窗口内用原键重试。

## 错误格式

HTTP：

```json
{"error":{"code":"forbidden","message":"forbidden","request_id":"server-generated-id"}}
```

WS：

```json
{"type":"res","id":"r5","ok":false,"error":{"code":"forbidden","message":"forbidden","request_id":"r5"}}
```

| HTTP 状态 | 常见代码 / 含义 |
| --- | --- |
| 401 | `unauthorized`、`token_expired` |
| 403 | `forbidden` |
| 404 | `reply_target_unavailable`、`subscription_not_found`、`unknown_method` |
| 409 | `idempotency_conflict`、`subscription_conflict` |
| 413 | `request_too_large` |
| 422 | `invalid_request`、`invalid_message_id`、`unknown_app_id`、`token_ttl_exceeded` |
| 429 | `subscription_limit`、`connection_limit` |
| 502 | `feishu_<code>`，保留官方数字错误码，省略原始响应正文 |
| 503 | `outbound_capacity`、`idempotency_capacity`、`upstream_unavailable`、`not_ready` |
| 504 | `outcome_unknown` |

WS 不携 HTTP 状态码；错误代码含义相同。慢消费者以 `slow_consumer` 关闭，全局缓冲不足以 `buffer_capacity` 关闭；关闭原因仅尽力送达。没有历史投递查询、死信、ACK/NACK 或离线补投接口，首版也没有 Webhook 注册接口。
