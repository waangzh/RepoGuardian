# Project CI 验证协议

`ProjectCIBackend` 使用 GitHub Actions `workflow_dispatch` 验证候选补丁。它不会创建、
推送或删除 Git ref，因此 GitHub App 不需要 `Contents: write`。

## 权限

GitHub App 最小权限：

- Metadata、Contents、Checks、Pull requests：read；
- Actions：read/write（dispatch、查询、取消及读取结果 artifact）；
- 不授予 Contents write。

目标仓库必须显式安装 `.github/workflows/repoguardian-validation.yml`，workflow 名称必须与
`REPOGUARDIAN_PROJECT_CI_WORKFLOW_NAME` 一致。还必须使用下面的 `run-name`，以便 GitHub
返回 204、没有 run ID 时仍能把 run 唯一绑定到 validation request。

```yaml
name: RepoGuardian Validation
run-name: RepoGuardian Validation ${{ inputs.validation_request_id }}

on:
  workflow_dispatch:
    inputs:
      validation_request_id: { required: true, type: string }
      head_sha: { required: true, type: string }
      patch_sha: { required: true, type: string }
      patch_artifact: { required: true, type: string }
      profile: { required: true, type: string }

permissions: {}

jobs:
  prepare:
    # 该 job 只检出、校验和打包，不执行仓库代码。
    permissions:
      contents: read
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ inputs.head_sha }}
          persist-credentials: false
      - name: Decode and verify patch
        env:
          PATCH_ARTIFACT: ${{ inputs.patch_artifact }}
          PATCH_SHA: ${{ inputs.patch_sha }}
        run: |
          python - <<'PY'
          import base64, hashlib, os, pathlib
          value = os.environ["PATCH_ARTIFACT"]
          if not value.startswith("inline-base64:"):
              raise SystemExit("unsupported patch artifact")
          patch = base64.b64decode(value.removeprefix("inline-base64:"), validate=True)
          normalized = patch.replace(b"\r\n", b"\n").rstrip(b"\n") + b"\n"
          if hashlib.sha256(normalized).hexdigest() != os.environ["PATCH_SHA"]:
              raise SystemExit("patch sha mismatch")
          pathlib.Path("/tmp/candidate.patch").write_bytes(normalized)
          PY
          git apply --check /tmp/candidate.patch
          git apply /tmp/candidate.patch
          tar --exclude=.git -czf /tmp/candidate.tgz .
      - uses: actions/upload-artifact@v4
        with:
          name: repoguardian-candidate-${{ inputs.validation_request_id }}
          path: /tmp/candidate.tgz
          retention-days: 1

  execute:
    needs: prepare
    # 不可信目标代码只在无 GitHub 权限、无 RepoGuardian secret 的 job 中执行。
    permissions: {}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: repoguardian-candidate-${{ inputs.validation_request_id }}
      - run: mkdir source && tar -xzf candidate.tgz -C source
      - name: Run registered profile
        id: tests
        working-directory: source
        continue-on-error: true
        env:
          PROFILE: ${{ inputs.profile }}
        run: |
          case "$PROFILE" in
            unit) ./ci/repoguardian-unit.sh ;;
            *) exit 2 ;;
          esac
      - name: Build structured result
        if: always()
        env:
          REQUEST_ID: ${{ inputs.validation_request_id }}
          HEAD_SHA: ${{ inputs.head_sha }}
          PATCH_SHA: ${{ inputs.patch_sha }}
          PROFILE: ${{ inputs.profile }}
          TEST_OUTCOME: ${{ steps.tests.outcome }}
          REPOSITORY: ${{ github.repository }}
          WORKFLOW_NAME: RepoGuardian Validation
          WORKFLOW_REF: main
          RUN_ID: ${{ github.run_id }}
        run: |
          python - <<'PY'
          import json, os
          passed = os.environ["TEST_OUTCOME"] == "success"
          result = {
              "validation_request_id": os.environ["REQUEST_ID"],
              "repository": os.environ["REPOSITORY"],
              "workflow_name": os.environ["WORKFLOW_NAME"],
              "ref": os.environ["WORKFLOW_REF"],
              "run_id": int(os.environ["RUN_ID"]),
              "head_sha": os.environ["HEAD_SHA"],
              "patch_sha": os.environ["PATCH_SHA"],
              "profile": os.environ["PROFILE"],
              "checks": [{
                  "name": os.environ["PROFILE"],
                  "status": "passed" if passed else "failed",
              }],
              "failure_kind": None if passed else "test",
          }
          open("result.json", "w", encoding="utf-8").write(json.dumps(result))
          PY
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: repoguardian-validation-${{ inputs.validation_request_id }}
          path: result.json
          retention-days: 1
      - if: steps.tests.outcome != 'success'
        run: exit 1
```

生产仓库应把 `unit` 等 profile 映射到仓库维护者审查过的固定脚本，不能把 input 拼接为 shell
命令。`prepare` job 不执行目标代码；`execute` job 不获取长期 RepoGuardian 凭据，也没有
GitHub 仓库权限。这是 fork PR 安全边界的一部分。RepoGuardian 默认拒绝把 fork PR dispatch
到 Project CI；此类请求应人工批准或改用 UserRunner。

## 结果绑定与映射

服务同时校验 repository、workflow ID/名称、ref、run ID、validation request ID、head SHA、
patch SHA 和 profile。workflow `success` 只有在配置 profile 对应的检查明确为 `passed` 时才
得到 `passed`；`skipped`、`neutral` 和缺少指定检查均为 `inconclusive`。测试失败映射为
`failed`，`failure_kind: infrastructure` 映射为 `infrastructure_error`。

GitHub `workflow_run` webhook 是主同步路径，定时查询是兜底。重复 delivery 幂等；验证超时
只把补丁标为未决并尝试取消 workflow，不会使已经完成的 Review 失败。Actions artifact 使用
短 TTL。本策略不创建临时 ref，因此不存在误删用户分支的清理路径；未来临时分支策略应作为
独立实现，并显式申请 Contents write。
