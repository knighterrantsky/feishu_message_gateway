# GitHub Actions → GHCR → Zeabur

## 一次性准备

1. 在飞书开放平台创建企业自建应用，启用机器人能力，发布应用版本并授权给需要的用户/群。配置“使用长连接接收事件”，订阅 `im.message.receive_v1`，授予接收单聊、接收群内 @ 机器人、发送消息及查询消息所需的权限（例如 `im:message.p2p_msg:readonly`、`im:message.group_at_msg:readonly`、`im:message:send_as_bot`；查询历史消息按控制台要求授权）。群内先添加机器人。API 名称/权限以当前飞书控制台为准。
2. 首次主分支检查通过后，Actions 构建并推送 `ghcr.io/knighterrantsky/feishu_message_gateway:<完整commit SHA>`。仓库源码公开不代表 GHCR 包自动公开；将包可见性设为 Public，或为 Zeabur 配置只读 GHCR 凭据。
3. 在 Zeabur 创建 **Docker / Prebuilt 服务**，镜像 repository 为 `ghcr.io/knighterrantsky/feishu_message_gateway`，tag 填上一步 SHA。使用单实例，避免旧新实例同时连接同一数据卷。采用先停旧实例、再启动新实例的替换方式；若平台调度发生短暂重叠，新进程会因实例锁失败退出，须确认平台会重试启动。
4. 配置 `.env.example` 中的应用、Webhook、API Token 等真实环境变量。设置 `DATA_DIR=/data`；`PORT` 使用 Zeabur 提供的监听端口。**不要设置 `CODE_VERSION`**，它已经写入镜像。
5. 添加持久卷，挂载到 `/data`，确保 UID/GID `10001:10001` 可写。空卷默认若归 root，需要通过卷初始化/管理终端一次性 `chown -R 10001:10001 /data`；应用容器始终非 root，不在启动时自动提权。不要把数据库放临时文件系统，不要将卷共享给其他实例。
6. 为 HTTP 服务配置 HTTPS 域名。存活探针 `/healthz`，就绪探针 `/readyz`（若平台支持独立就绪探针）；重启策略开启，终止宽限建议至少 60 秒。Docker HEALTHCHECK 使用存活接口，避免飞书暂时断线导致重启风暴。
7. 在 GitHub 创建 `production` Environment。仓库 **Settings → Secrets and variables → Actions** 设置：

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| Secret（仓库或 production 环境） | `ZEABUR_API_TOKEN` | Zeabur API Key |
| **Repository Variable** | `ZEABUR_SERVICE_ID` | Docker 服务 ID；存在时开启自动部署 |
| Repository Variable | `ZEABUR_ENVIRONMENT_ID` | Zeabur 环境 ID |
| Repository Variable | `GATEWAY_BASE_URL` | HTTPS 服务地址，不含结尾接口路径 |

`ZEABUR_SERVICE_ID` 必须是仓库 Variable，因为 job 的 `if` 在进入 Environment 前求值。Project ID 用于控制台定位，镜像更新 API 只需 Service ID 和 Environment ID。飞书密钥只放 Zeabur，不需要复制到 GitHub。`GITHUB_TOKEN` 由 Actions 自动提供（工作流请求 `packages: write`），不必手工创建。

未配置 Service ID 时，CI 和镜像发布正常运行，部署 job 明确跳过；这仅表示完成构建，**不代表上线验收通过**。配置后，每次合并主分支自动更新指定服务。初次配置完成后可手动运行工作流进行首次部署。

## 流水线行为

- PR：锁定依赖安装、Ruff lint/format、Mypy、测试、OpenAPI 同步检查、Docker 多阶段构建、非 root/PORT/健康/重启持久化 smoke test。
- 主分支：以上检查通过后发布 Git SHA 标签镜像，再调用 Zeabur 官方 GraphQL `updateServiceImage`。
- 部署：`production` 并发组保证同一环境不并行更新，不取消正在进行的部署。自动部署检查主分支当前 SHA，跳过已经被更新提交取代的构建。
- 验证：每 5 秒检查 `/healthz` 和 `/readyz`，两者均返回 200 且版本等于目标 SHA，连续三次才成功。最多 60 轮（网络异常时耗时更长），整个部署 job 上限 20 分钟。
- Actions 的并发组会合并等待中的运行，不能保证每个中间提交都部署。主分支应保持最新可部署状态。
- Actions 固定到核实过的 commit，Python 依赖由 `uv.lock` 锁定；修改依赖时同步更新锁文件。

## 手动部署与回滚

GitHub → Actions → **CI and release** → Run workflow，选择 `main`：

- `image_tag` 留空：检查并构建当前 main，发布后部署。
- `image_tag` 填历史 **40 位小写 commit SHA**：验证该 GHCR 镜像存在，直接部署该历史镜像，不重新构建历史源码。

也可通过 CLI：

```sh
gh workflow run ci.yml --ref main -f image_tag=0123456789abcdef0123456789abcdef01234567 \
  --repo knighterrantsky/feishu_message_gateway
```

回滚保持同一持久卷。当前数据库 schema version 为 1；未来不兼容迁移前必须通过 SQLite backup API 做快照，并验证旧镜像能读取目标数据库。不要直接用旧文件覆盖运行中的 WAL 数据库。保留历史 GHCR SHA 标签，否则无法回滚。镜像构建失败时不部署；部署校验失败时工作流标红，检查日志并手工选择已知健康 SHA 回滚，避免自动回滚覆盖并发的新发布。

如果密钥或环境变量改坏，仅回滚镜像不会恢复这些配置，需要同时恢复 Zeabur 环境变量。

## 官方参考

- [飞书官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)
- [Zeabur Public API](https://zeabur.com/docs/en-US/developer/public-api)
- [Zeabur 镜像更新说明](https://zeabur.com/docs/en-US/deploy/manage/update-image-reference)
- [Zeabur 官方 CLI 镜像更新实现](https://github.com/zeabur/cli/blob/main/pkg/api/service.go)
