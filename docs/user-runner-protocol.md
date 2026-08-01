# User Runner 验证协议（第一版）

User Runner 在用户控制的开发或企业环境中执行验证。RepoGuardian 不安装项目依赖，
也不把该环境描述为安全沙箱；`trusted=true` 只表示结果来自已注册 Runner、签名有效且
与有效 claim 绑定，不表示 Runner 主机本身可信。

## 安全边界

- 服务端只接受 `REPOGUARDIAN_RUNNER_PROFILES` 中预注册的 profile ID。映射值是逻辑
  `command_id`，不是服务端执行的 shell 字符串。
- Runner 从自己的本地配置把 profile ID 映射到 argv，并且只在用户环境中执行它。
- claim 响应只包含仓库 clone/fetch 信息、base/head SHA、patch、profile、request ID 和
  过期时间；不会包含 LLM Key、GitHub App 私钥、数据库凭据或其他仓库凭据。
- 私有仓库的凭据由 Runner 所在环境独立获取，必须是最小权限、短期凭据。clone URL
  不嵌入凭据，RepoGuardian 的 `GITHUB_TOKEN` 永远不会转发给 Runner。
- API Token 用于 Bearer 身份认证；独立 HMAC 密钥只用于结果完整性签名。注册响应不回显
  二者，管理 UI 只能看到 `RunnerRegistration` 公有元数据。

## 注册

`POST /api/runners/register`，请求头：

```text
X-RepoGuardian-Admin-Token: <REPOGUARDIAN_RUNNER_REGISTRATION_TOKEN>
```

请求体包含 `runner_id`、`display_name`、`allowed_repositories`、`allowed_profiles`、
`enabled`、`api_token` 和 `hmac_secret`。两个秘密至少 32 个字符，并应由 Runner 本地使用
密码学安全随机源生成。响应只返回：

```python
class RunnerRegistration(BaseModel):
    runner_id: str
    display_name: str
    public_key: str  # 第一版为 hmac-sha256:<密钥指纹>
    allowed_repositories: list[str]
    allowed_profiles: list[str]
    enabled: bool
```

建议最小 CLI 行为：

```text
repoguardian-runner register  # 本地生成并保存 token/secret，再调用注册 API
```

## Claim

```text
POST /api/validation-requests/{request_id}/claim
Authorization: Bearer <runner_api_token>
```

相同 Runner 在有效租约内重复 claim 会得到同一响应；其他 Runner 会收到 `409`。租约过期
后任务可重新领取。未授权仓库或 profile 返回 `403`。成功响应示例：

```json
{
  "request_id": "...",
  "repository": {
    "repository_id": "owner/repo",
    "clone_url": "https://github.com/owner/repo.git",
    "fetch_ref": "feature"
  },
  "base_sha": "...",
  "head_sha": "...",
  "patch_content": "diff --git ...",
  "validation_profile_id": "unit",
  "expires_at": "2026-08-01T12:00:00Z"
}
```

对应 CLI：

```text
repoguardian-runner claim <request-id>
```

## 本地验证

Runner 必须按以下顺序校验并执行：clone/fetch、checkout 精确 `head_sha`、计算规范化 patch
SHA-256、应用 patch、查找本地 profile argv、使用参数数组且 `shell=false` 启动子进程。
服务端返回的任何字段都不能被解释成命令。

```text
repoguardian-runner validate <request-id>
```

自定义命令只存在于 Runner 本地配置中并只在用户环境执行。上传结果应标注环境指纹和
Runner 身份；RepoGuardian 不会执行该命令。

## 签名与结果提交

```text
POST /api/validation-requests/{request_id}/result
Authorization: Bearer <runner_api_token>
```

请求体字段为：`request_id`、`runner_id`、`head_sha`、`patch_sha`、`profile`、`checks`、
`exit_status`、`duration_ms`、`environment_fingerprint`、`submitted_at`、
`idempotency_key`、`log_summary`、`artifact_references`、`signature`。

签名算法：从请求体移除 `signature`，把剩余对象按 UTF-8、JSON key 排序、无多余空格
（Python `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`）
序列化，然后计算小写十六进制 `HMAC-SHA256`。服务端使用
`hmac.compare_digest` 验签。

```text
repoguardian-runner submit <request-id>
```

同一 Runner 重试相同 `idempotency_key` 和相同载荷时回放首次结果；复用该 key 提交不同
载荷会返回 `409`。签名、claim、SHA、profile 或时效任一校验失败都不能把 Patch 标记为
`verified`。成功且 exit status 为 0、至少一个 check 且所有 check 均为 `passed` 时，服务端
记录：

```text
trusted = true
trust_source = user_runner
runner_id = <已认证 Runner>
```

取消请求使用受管理令牌保护的
`POST /api/validation-requests/{request_id}/cancel`；取消后的结果不能覆盖 `cancelled`。
