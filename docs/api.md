# HTTP 接口与 Webhook

所有 `/v1/*` 接口要求 `Authorization: Bearer <API_ACCESS_TOKEN>`。发送/回复要求 `Idempotency-Key`（1–128 个可打印 ASCII 字符，不含空格），两接口共享键空间，调用方应为每个业务动作生成全局唯一键。键长期保存在数据库；相同键、相同内容返回同一记录，相同键、不同内容返回 409。

## 发送与回复

```sh
export GATEWAY_URL=http://localhost:8080
# 预先在本地环境中设置 API_ACCESS_TOKEN，避免把真实令牌写入代码。
curl -sS "$GATEWAY_URL/v1/messages" \
  -H "Authorization: Bearer $API_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-20260912-created' \
  -d '{"chat_id":"oc_replace","text":"订单已创建"}'

curl -sS "$GATEWAY_URL/v1/messages/om_replace/replies" \
  -H "Authorization: Bearer $API_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-20260912-reply' \
  -d '{"text":"收到，正在处理"}'
```

返回 HTTP 202，代表已持久化入队。返回字段包括 `delivery_id`、`kind`、`status`、`attempts`、`total_attempts`、`created_at`、`updated_at`、`next_attempt_at`、`last_error`、`result_message_id`。时间为 Unix 秒。

发送使用飞书 `chat_id`（单聊、群聊均可）；回复的会话优先由本地消息映射获得，未见过的消息调用飞书查询接口确定会话后再入队。文本为 1–10000 字符，不接受其他消息类型或未知字段。发送后可能因飞书权限、会话不可用等进入死信，以投递记录为准。

```sh
curl -sS "$GATEWAY_URL/v1/deliveries/DELIVERY_ID" \
  -H "Authorization: Bearer $API_ACCESS_TOKEN"
curl -sS "$GATEWAY_URL/v1/status" -H "Authorization: Bearer $API_ACCESS_TOKEN"
curl -sS -X POST "$GATEWAY_URL/v1/deliveries/DELIVERY_ID/replay" \
  -H "Authorization: Bearer $API_ACCESS_TOKEN"
```

状态：`pending` → `processing` → `succeeded`；暂时失败回到 `pending`；不可重试或次数耗尽为 `dead`。仅 `dead` 可重放，重置本轮 `attempts`，保留 `total_attempts`、原始队列位置和 delivery ID。状态查询不返回消息正文、密钥或原始上游错误。

统一错误格式：

```json
{"error":{"code":"idempotency_conflict","message":"idempotency_conflict"}}
```

| HTTP | code 示例 | 含义 |
| --- | --- | --- |
| 401 | unauthorized | Token 缺失或不匹配 |
| 404 | delivery_not_found | 投递记录不存在 |
| 409 | idempotency_conflict / delivery_not_dead | 键冲突 / 不能重放 |
| 422 | invalid_request / invalid_message_id | 请求校验失败 |
| 503 | storage_unavailable / upstream_unavailable / not_ready | 存储、上游或就绪状态异常 |
| 500 | internal_error | 未分类内部错误，响应不包含敏感详情 |

`GET /healthz` 不鉴权，返回进程存活与代码版本。`GET /readyz` 不鉴权，要求数据库可读、长连接心跳新鲜且进程存活、投递线程运行、未退出，否则 503。下游 Webhook 暂时故障不使服务失去就绪；应监控队列和死信。`GET /v1/status` 返回长连接、worker、队列/死信数量、最多前 100 个死信 ID、最近投递结果和版本。

## 统一入站事件

```json
{
  "schema_version": "1.0",
  "type": "message.received",
  "app_id": "cli_example",
  "event_id": "event_example",
  "message_id": "om_example",
  "chat_id": "oc_example",
  "chat_type": "p2p",
  "sender": {"open_id": "ou_example", "user_id": "user_example", "union_id": "on_example"},
  "text": "你好",
  "mentions": [],
  "create_time": "1700000000000"
}
```

`sender` 保留 SDK 提供的 ID 字段；具体字段取决于飞书权限。`create_time` 是飞书的 Unix 毫秒字符串。`text` 保留原文，包括 `@_user_1` 占位符；`mentions` 保留用于替换占位符的结构。

## Webhook 签名校验

网关发送请求头：

- `X-Gateway-Delivery-Id`：本地稳定投递 ID，重试和重放保持不变。
- `X-Gateway-Timestamp`：本次请求 Unix 秒，每次投递重新生成。
- `X-Gateway-Signature`：`v1=` + HMAC-SHA256 十六进制字符串。

签名内容为 `timestamp + "." + delivery_id + "." + 原始 HTTP body 字节`，密钥是 `WEBHOOK_SIGNING_SECRET`。必须先校验签名和时间窗口，再解析 JSON；不要把 JSON 重新序列化后验签。

```python
from gateway.signing import verify

valid = verify(
    secret=shared_secret,
    timestamp=headers["X-Gateway-Timestamp"],
    delivery_id=headers["X-Gateway-Delivery-Id"],
    body=raw_request_body,
    signature=headers["X-Gateway-Signature"],
    tolerance=300,
)
if not valid:
    # 返回 401，不执行副作用
    ...
# 在接收方数据库事务中按 delivery_id 去重，并持久化业务操作/任务。
# 重复 delivery_id 应返回成功；签名校验本身不提供业务去重。
```

只有 HTTP 2xx 视为成功。408/425/429/5xx 及网络错误自动重试；其他非 2xx 直接进入死信，不跟随重定向。接收方应在持久化后尽快返回 2xx。
