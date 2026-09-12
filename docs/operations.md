# 可靠性与运行维护

## 投递语义

接收回调仅过滤、转换和提交 SQLite 事务，提交成功才返回给官方 SDK；SDK 随后确认事件。存储失败抛出异常，由 SDK 返回失败确认，不能在未持久化时吞掉异常。接收测试直接运行已安装 SDK 的 dispatcher 和 WebSocket ACK 路径验证这一点。

同一 App ID + 飞书 message ID 去重，重复 envelope 即使 event ID 改变也只入队一次；原始 event ID 保留在统一事件中。发送与回复使用全局 API 幂等键；请求内容哈希不同则冲突。SQLite 使用 WAL、`synchronous=FULL` 和短事务；网络请求在事务外执行。

Webhook 为**至少一次投递**：接收方成功但网关尚未记录成功就崩溃时会重发，接收方必须按 delivery ID 做持久化去重。发送和回复将稳定 delivery ID 作为飞书 `uuid`，用于飞书提供的有限时间幂等能力；不能声称跨任意重试/长时间死信重放具有 exactly-once 语义。跨越飞书去重窗口或结果不确定的历史发件重放前，先确认目标会话，可能产生重复消息。

同一会话按 SQLite 入队序号投递，按**网关接收顺序**，不是消息创建时间。入站 Webhook 与出站飞书请求分为两条有序流，避免 Webhook 故障阻塞对飞书的回复。出站 send/reply 共享一条流。一个后台线程串行执行网络投递；重试等待和死信仅阻塞同会话同方向的后续任务，其他会话可以继续。单次慢请求会暂时占用 worker，第一版以简单可靠为优先。

永久失败或重试用尽进入死信；后续同流任务保持 pending，不越过死信。修复原因后调用 replay，保留原队列序号和 ID。没有跳过死信接口；如确需丢弃需做明确的运维变更，避免隐式破坏顺序。

主进程启动获得文件排他锁后，将上次 `processing` 恢复为 `pending`。只要持久卷保留，幂等键和记录不会随容器更新丢失。所有记录第一版长期保留，不自动清理，需监控磁盘增长；删除已完成记录会改变长期去重语义，不应自行定时清表。

## SDK 生命周期

飞书官方 SDK 的长连接自带重连。由于当前 SDK 的 `start()` 拥有事件循环且没有公共 stop 接口，适配层在单独进程中运行，固定 SDK 版本，并把其 loop 绑定到子进程事件循环。监控使用 `_conn`，退出使用 `_disconnect` 和 `_auto_reconnect`；升级 SDK 必须复核这些私有接口和契约测试。

主进程每 5 秒检查接收子进程是否退出并恢复。长连接心跳超过 10 秒不更新时 readiness 失败。身份获取失败会每 5 秒重试；SDK 建连中的同步请求不会阻塞父进程 HTTP。子进程正常退出先停连接，超过宽限则终止；没有提交的事务自动回滚，已经提交的消息可安全恢复。

SIGTERM：HTTP 停止接收新请求，停止接收子进程，等待当前投递，未完成的任务留在数据库。若投递线程超过退出等待时间，实例锁保留到进程退出，避免第二实例抢跑。平台终止宽限应允许 HTTP 请求结束、接收进程关闭及 worker 收尾。

## 状态与告警

定期携 Token 请求 `/v1/status`。关注 `long_connection=false`、`worker_alive=false`、`queue_count` 持续增长、`dead_count>0`。最近结果只保留状态、错误类别和时间，不包含下游响应正文。`/readyz` 不把下游故障当作进程故障，队列告警不可省略。

日志不记录消息正文、完整配置或原始上游错误；关闭 SDK/HTTP 依赖可能泄露正文和连接 token 的日志，应用日志对已配置密钥和 Webhook URL 脱敏。调试级别也不启用 SDK 原始日志。SQLite 中保留消息正文，需要按业务数据要求保护持久卷和备份。

## 数据备份

使用 SQLite backup API，运行时不要只复制主数据库文件（WAL 中可能还有已提交数据）：

```python
import sqlite3
with sqlite3.connect('/data/gateway.sqlite3') as source:
    with sqlite3.connect('/backup/gateway.sqlite3') as target:
        source.backup(target)
```

备份存到独立持久化位置并限制访问权限。恢复时先停止服务，再整体恢复数据库；勿混用旧库和新 WAL/SHM。重启后检查队列数量、历史幂等键及 `/readyz`。
