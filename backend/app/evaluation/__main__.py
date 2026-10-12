"""从 backend 执行 uv run python -m app.evaluation --help。"""

import argparse
import asyncio
from pathlib import Path
import subprocess
import sys



def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # run 总是派生干净的 Python 进程；准备进程曾读取的答案字节不能留在 Reviewer 内存中。
    if argv and argv[0] == "run":
        completed = subprocess.run([sys.executable, "-m", "app.evaluation", "_review", *argv[1:]], check=False)
        raise SystemExit(completed.returncode)
    if argv and argv[0] == "_review":
        from app.evaluation.isolation import protect_reviewer_process
        protect_reviewer_process()
        argv[0] = "run"
    from app.evaluation.runner import run_dataset, write_json
    parser = argparse.ArgumentParser(description="RepoGuardian 固定快照评测")
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="冻结原始文件哈希和无答案样本清单")
    freeze.add_argument("--positive", type=Path, required=True)
    freeze.add_argument("--negative", type=Path, required=True)
    freeze.add_argument("--source-revision", required=True)
    freeze.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run", help="执行生产审查图；会调用真实模型")
    run.add_argument("--lock", type=Path, required=True)
    run.add_argument("--diff-lock", type=Path, help="v2 有效 diff 基准锁；缺省保持 v1 两点差分")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--sample-id", action="append")
    run.add_argument("--limit", type=int)
    run.add_argument("--timeout-seconds", type=float, default=1800)
    run.add_argument("--experiment-plan", type=Path)
    run.add_argument("--experiment-group", choices=["protocol_180", "full_180", "full_runtime"])
    run.add_argument("--experiment-stage", choices=["first", "remaining"])
    run.add_argument("--first-gate", type=Path)
    experiment = commands.add_parser("prepare-runtime-experiments", help="只读冻结 P5 组别、预算与 r9 基线")
    experiment.add_argument("--lock", type=Path, required=True)
    experiment.add_argument("--diff-lock", type=Path, required=True)
    experiment.add_argument("--selection", type=Path, required=True)
    experiment.add_argument("--baseline-run", type=Path, action="append", required=True)
    experiment.add_argument("--source-roots", type=Path, required=True)
    experiment.add_argument("--output", type=Path, required=True)
    comparison = commands.add_parser("compare-runtime-experiment", help="离线对照 wire 消费和固定 diff 覆盖")
    comparison.add_argument("--plan", type=Path, required=True)
    comparison.add_argument("--run", type=Path, required=True)
    comparison.add_argument("--group", choices=["protocol_180", "full_180", "full_runtime"], required=True)
    comparison.add_argument("--output", type=Path, required=True)
    prepare = commands.add_parser("prepare-diffs", help="只读核对已准备 Git 快照，生成 v2 diff 输入锁")
    prepare.add_argument("--lock", type=Path, required=True)
    prepare.add_argument("--run", type=Path, action="append", required=True)
    prepare.add_argument("--references", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "freeze":
        from app.evaluation.data import freeze_dataset
        lock = freeze_dataset(args.positive, args.negative, args.source_revision)
        if args.output.exists():
            parser.error("冻结文件已存在；使用新路径，不覆盖既有协议")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.output, lock.model_dump(mode="json"))
        print(f"已冻结 {len(lock.samples)} 个样本：{args.output}")
    elif args.command == "prepare-diffs":
        from app.evaluation.diff_inputs import prepare_diff_inputs
        audit_path = args.output.with_suffix(".audit.json")
        if args.output.exists() or audit_path.exists():
            parser.error("v2 输入锁或审计文件已存在，使用新路径")
        diff_lock, audit = prepare_diff_inputs(args.lock, args.run, args.references)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.output, diff_lock.model_dump(mode="json"))
        write_json(audit_path, audit)
        print(f"已核对 {len(diff_lock.samples)} 个样本：{args.output}")
        if any(s.alignment_status != "ready" for s in diff_lock.samples):
            raise SystemExit(1)
    elif args.command == "prepare-runtime-experiments":
        from app.evaluation.runtime_experiments import freeze_experiment_plan
        if args.output.exists():
            parser.error("实验清单已存在，使用新路径")
        plan = freeze_experiment_plan(args.lock, args.diff_lock, args.selection, args.baseline_run, args.source_roots)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.output, plan)
        print(f"已冻结 3 组、最多 15 个新样本：{args.output}")
    elif args.command == "compare-runtime-experiment":
        from app.evaluation.runtime_experiments import compare_group_run, read_json
        if args.output.exists():
            parser.error("对照文件已存在，使用新路径")
        result = compare_group_run(read_json(args.plan), args.run, args.group)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.output, result)
        print(f"已记录固定范围与真实用量对照，质量门仍未完成：{args.output}")
    else:
        from app.agents.providers import build_provider
        from app.core.config import settings

        plan, gate = None, None
        if args.experiment_plan:
            from app.evaluation.runtime_experiments import read_json, FIXED_SETTINGS, first_sample_gate
            plan = read_json(args.experiment_plan)
            gate = read_json(args.first_gate) if args.first_gate else None
            if gate and first_sample_gate(read_json(args.first_gate.parent / "0000.json"), plan,
                                           args.experiment_group) != gate:
                parser.error("首样本验收文件与真实结果不一致")
            for name, value in FIXED_SETTINGS.items():
                setattr(settings, name, value)
        elif args.experiment_group or args.experiment_stage or args.first_gate:
            parser.error("实验组和门必须绑定冻结清单")
        provider = build_provider(settings.repoguardian_provider, settings.openai_api_key,
                                  settings.openai_base_url, settings.repoguardian_model)
        summary = asyncio.run(run_dataset(args.lock, args.output, provider,
            sample_ids=args.sample_id, limit=args.limit, timeout_seconds=args.timeout_seconds,
            diff_lock_path=args.diff_lock, experiment_plan=plan, experiment_group=args.experiment_group,
            experiment_stage=args.experiment_stage, first_gate=gate,
            experiment_registry=Path(settings.repoguardian_workdir).resolve() / "evaluation-runs" /
                "runtime-experiment-dispatch" if plan else None))
        print(f"已记录 {len(summary['samples'])} 个样本：{args.output}")
        if any(x["status"] not in {"completed", "completed_with_warnings"} for x in summary["samples"]):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
