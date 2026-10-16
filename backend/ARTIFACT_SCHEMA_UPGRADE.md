# 产物约束升级与手动验收

## 修复范围

历史 SQLite 数据库可能仍有 `artifacts(sha256, kind)` 全局唯一约束，但当前产物按任务目录 URI 保存。不同任务产生相同初始 derivation 内容时，旧约束会拒绝检查点写入；该完整性错误之前又被转换为 `CoordinationLeaseLost`，表现为图节点 `CancelledError`。

- `20261016_0003` 移除旧命名约束，保留 URI 唯一、外键、索引和原数据；无旧约束的新库也可升级。
- 只有明确的 SQLite SideEffect 主键竞争映射为租约失效，其余完整性错误以脱敏 `IntegrityError` 传播。
- 降级到 `20260804_0002` 不恢复旧内容唯一约束，避免破坏升级后合法产生的同内容产物。完整恢复历史环境使用升级前备份。

## 本机升级记录

2026-10-16（Asia/Shanghai）完成：

- 升级前 8000、5173 端口关闭，未发现 uvicorn 进程；无 `leased` 作业，服务保持停止。
- 通过 SQLite backup 分别备份业务库与 checkpoint 库，复制全部产物并逐项验证 SHA-256。
- 备份目录：`backend/.repoguardian/backups/20261016-112620-artifact-schema/`。
- 备份共 415 个文件、114,397,006 字节；清单为目录内 `manifest.json`，数据库和产物不入 Git。
- 清单 SHA-256：`5917442bfede76604ad1f44c85ec8b73ed70acd99ef8c49d58a6e491f243c9be`。
- 执行 `uv run alembic upgrade head` 成功，当前版本为 `20261016_0003`，启动版本门禁通过。
- 除 `alembic_version` 外，所有表的行数与数据指纹均与升级前一致；400 条产物记录保留。
- `PRAGMA integrity_check` 返回 `ok`，`PRAGMA foreign_key_check` 无异常；产物唯一约束仅保留 URI。
- 原任务 `0353d1d01fd943fd8c83c2b491e146a8` 的失败状态、错误和作业记录保持原样。
- 原有 1 个 `retry` 作业未更改；启动 worker 后可能继续执行该作业。此次没有发起 Web 审查或模型请求。

## 已执行检查

工作目录为 `backend/`：

```bash
uv run pytest tests/test_artifact_schema_migration.py tests/test_runtime_integrity_errors.py tests/test_phase6a_persistence.py tests/test_task_runtime_p2.py tests/test_task_runtime_p4.py tests/test_task_cancel_boundary.py -q -p no:cacheprovider
uv run ruff check .
```

聚焦回归 88 项通过。随后增加降级和再次升级的数据保留断言，单独复验迁移测试 6 项通过。`git diff --check` 通过。未运行全量测试、付费模型或最终 Web 审查。

## 手动 Web 验收

在两个终端中分别启动，命令与仓库 README 一致：

```bash
cd backend
uv run uvicorn app.main:app --reload
```

```bash
cd frontend
npm run dev
```

1. 打开 `http://localhost:5173`，创建对 `https://github.com/dshakes/onpar/pull/95` 的新审查。
2. 观察任务能否进入 `task_review_runtime` 并产生运行报告；不应再次因该产物约束出现节点取消错误。
3. 对照实际覆盖、未决问题和用量判断结果；修复持久化故障不保证模型审查结论或完整覆盖。
4. 确认旧失败任务仍可查看且保持原失败记录。
5. 如仍失败，保留新任务 ID、时间和脱敏错误；不删除数据库或清理记录重试。

最终 Web 验收由用户执行，目前未验证。
