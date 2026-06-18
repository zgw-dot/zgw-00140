import argparse
import json
import os
import sys
import tempfile

from .config import load_config, init_sample_config, ArchiveConfig
from .archiver import Archiver
from . import migration as mig
from .relocation_wizard import RelocationWizard


_COMMANDS_THAT_ALLOW_LEGACY = {"init", "migrate", "detect-legacy"}


def _default_export_path(fmt: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"archive_logs.{fmt}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invoice-archiver",
        description="采购发票附件归档守护工具 v2.0+  (源码/运行目录分离)",
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument(
        "--ignore-legacy",
        action="store_true",
        help="强制跳过源码目录遗留运行产物检查（不推荐，可能污染 git 仓库）",
    )
    parser.add_argument(
        "--allow-source-in-repo",
        action="store_true",
        help="允许配置将运行目录指向源码仓库内部（不推荐）",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="生成示例配置文件和目录结构，并写入版本标记")
    sub.add_parser("detect-legacy", help="仅检测源码目录是否有旧版本遗留的运行产物")

    p_mig = sub.add_parser("migrate", help="将旧版本源码目录下的运行产物平滑迁移到用户数据目录")
    p_mig.add_argument("--apply", action="store_true", help="执行实际迁移，不加则仅预览")

    sub.add_parser("precheck", help="预检：扫描并报告，不移动文件")
    sub.add_parser("archive", help="执行归档（创建新批次）")
    sub.add_parser("retry", help="重试失败队列")

    p_rollback = sub.add_parser("rollback", help="回滚指定批次")
    p_rollback.add_argument("--batch-id", required=True, help="要回滚的批次ID")

    p_query = sub.add_parser("query-batch", help="查询批次详情")
    p_query.add_argument("--batch-id", required=True, help="批次ID")

    sub.add_parser("list-batches", help="列出所有批次（重启后仍可查到）")
    sub.add_parser("list-failures", help="列出失败队列（重启后仍可查到）")

    p_export = sub.add_parser("export-logs", help="导出日志（csv/json）")
    p_export.add_argument(
        "--format", choices=["csv", "json"], default="csv", dest="fmt",
        help="导出格式",
    )
    p_export.add_argument(
        "--output", default=None, help="输出文件路径，默认到系统临时目录",
    )
    p_export.add_argument(
        "--force", action="store_true", help="如输出文件已存在，强制覆盖",
    )

    p_wiz_scan = sub.add_parser("wizard-scan", help="搬家向导: 扫描源码目录运行产物")
    p_wiz_scan.add_argument("--target", default=None, help="搬家目标根目录 (默认: 用户数据目录)")
    p_wiz_scan.add_argument("--rules", default=None, help="规则文件路径 (JSON, 含 path_mapping/ignore_patterns)")
    p_wiz_scan.add_argument("--ignore", nargs="*", default=[], help="忽略的文件名模式 (子串匹配)")

    sub.add_parser("wizard-plan", help="搬家向导: 生成搬家计划")

    p_wiz_apply = sub.add_parser("wizard-apply", help="搬家向导: 执行搬家")
    p_wiz_apply.add_argument("--resume", action="store_true", help="续跑上次未完成的搬家")

    sub.add_parser("wizard-undo", help="搬家向导: 撤销上次搬家")
    sub.add_parser("wizard-status", help="搬家向导: 查看搬家状态")

    p_wiz_export = sub.add_parser("wizard-export", help="搬家向导: 导出搬家结果 (JSON/CSV)")
    p_wiz_export.add_argument("--format", choices=["csv", "json"], default="json", dest="fmt")
    p_wiz_export.add_argument("--output", default=None, help="输出文件路径")
    p_wiz_export.add_argument("--force", action="store_true", help="如输出文件已存在，强制覆盖")

    p_wiz_conflict = sub.add_parser("wizard-resolve", help="搬家向导: 设置冲突处理策略")
    p_wiz_conflict.add_argument("--source-path", required=True, help="冲突文件源路径")
    p_wiz_conflict.add_argument("--resolution", required=True, choices=["skip", "overwrite", "rename", "ask"],
                                help="冲突处理策略")

    p_wiz_audit = sub.add_parser("wizard-audit-export", help="搬家向导: 导出审计日志 (JSON/CSV)")
    p_wiz_audit.add_argument("--format", choices=["csv", "json"], default="json", dest="fmt")
    p_wiz_audit.add_argument("--output", default=None, help="输出文件路径")
    p_wiz_audit.add_argument("--force", action="store_true", help="如输出文件已存在，强制覆盖")

    return parser


def _cmd_detect_legacy(source_root: str) -> int:
    detection = mig.detect_legacy_layout(source_root)
    report = detection.as_report()
    marker = mig.read_version_marker(source_root)
    report["version_marker"] = marker
    report["already_migrated"] = mig.already_migrated(source_root)

    if detection.has_legacy_data and not mig.already_migrated(source_root):
        blockers = mig.describe_blockers(detection)
        report["blocker_messages"] = blockers
        print(json.dumps(report, indent=2, ensure_ascii=False))
        print("\n" + "\n".join(blockers), file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if marker:
        print(f"\n当前目录已标记为 v{marker.get('version')} 布局: {marker.get('layout')}")
    return 0


def _cmd_migrate(
    args: argparse.Namespace, source_root: str, cfg: ArchiveConfig
) -> int:
    plan = mig.build_migration_plan(
        source_root=source_root,
        target_state_dir=cfg.state_dir,
        target_log_dir=cfg.log_dir,
        target_archive_dir=cfg.archive_dir,
        target_source_dir=cfg.source_dir,
    )

    if not plan.needs_migration:
        print(json.dumps({
            "status": "nothing_to_migrate",
            "source_root": source_root,
            "already_migrated": mig.already_migrated(source_root),
            "note": "未检测到旧版本遗留数据，或已执行过迁移",
        }, indent=2, ensure_ascii=False))
        return 0

    if not args.apply:
        print(json.dumps({
            "status": "plan_ready",
            "apply": False,
            "items_count": len(plan.items_to_move),
            "items": plan.items_to_move,
        }, indent=2, ensure_ascii=False))
        print("\n[提示] 以上为迁移预览。确认无误后，加 --apply 执行实际迁移。", file=sys.stderr)
        return 0

    result = mig.apply_migration(plan)
    output = {
        "success": result.success,
        "migrated_items": result.migrated_items,
        "skipped_items": result.skipped_items,
        "errors": result.errors,
        "details": result.details,
        "markers_written": {
            ".migrated_from_v1": os.path.join(source_root, mig.MIGRATION_MARKER),
            ".archive_version": os.path.join(source_root, mig.VERSION_MARKER),
        },
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0 if result.success else 3


def _cmd_init(args: argparse.Namespace, source_root: str) -> int:
    cfg_path = init_sample_config(args.config)
    cfg = load_config(cfg_path)
    os.makedirs(cfg.state_dir, exist_ok=True)
    os.makedirs(cfg.log_dir, exist_ok=True)
    os.makedirs(cfg.archive_dir, exist_ok=True)
    os.makedirs(cfg.source_dir, exist_ok=True)
    mig.write_version_marker(source_root, version="2.0.0")
    print(json.dumps({
        "status": "initialized",
        "version": "2.0.0+",
        "config": cfg_path,
        "project_source_root": source_root,
        "runtime_paths": {
            "source_dir": os.path.abspath(cfg.source_dir),
            "archive_dir": os.path.abspath(cfg.archive_dir),
            "state_dir": os.path.abspath(cfg.state_dir),
            "log_dir": os.path.abspath(cfg.log_dir),
        },
        "separation_check": "ok",
        "notice": "运行时产物已默认放到系统用户目录，不会污染源码仓库",
    }, indent=2, ensure_ascii=False))
    return 0


def _preflight_check(
    args: argparse.Namespace, cfg: ArchiveConfig, source_root: str
) -> int:
    detection = mig.detect_legacy_layout(source_root)
    if detection.has_legacy_data and not mig.already_migrated(source_root):
        if args.ignore_legacy or args.command in _COMMANDS_THAT_ALLOW_LEGACY:
            pass
        else:
            blockers = mig.describe_blockers(detection)
            print("\n".join(blockers), file=sys.stderr)
            return 2

    ok, issues = cfg.paths_separated_from_source()
    if not ok and not args.allow_source_in_repo:
        print("=" * 60, file=sys.stderr)
        print("配置路径未与源码目录分离，已阻止继续运行：", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        for issue in issues:
            print(f"  [路径冲突] {issue}", file=sys.stderr)
        print("", file=sys.stderr)
        print("可选择：", file=sys.stderr)
        print("  1. 修改 config.yaml 中 source/archive/state/log_dir 指向源码仓库外的路径（推荐）", file=sys.stderr)
        print("  2. 加 --allow-source-in-repo 强制继续（可能污染 git status）", file=sys.stderr)
        return 2

    return 0


def _check_export_output(output: str, force: bool) -> int:
    out_abs = os.path.abspath(output)
    if os.path.exists(out_abs) and not force:
        print(f"[错误] 导出路径已被占用: {out_abs}", file=sys.stderr)
        print("  处理方式:", file=sys.stderr)
        print("    - 更换 --output 指定新路径", file=sys.stderr)
        print("    - 或加 --force 强制覆盖（将丢失现有文件）", file=sys.stderr)
        return 4
    if os.path.isdir(out_abs):
        print(f"[错误] --output 指向了目录而非文件: {out_abs}", file=sys.stderr)
        return 4
    return 0


def _cmd_wizard_scan(args: argparse.Namespace, source_root: str) -> int:
    target = args.target
    if not target:
        if os.path.exists(args.config):
            cfg = load_config(args.config)
            from .config import _app_data_dir
            target = _app_data_dir()
        else:
            from .config import _app_data_dir
            target = _app_data_dir()

    wizard = RelocationWizard(
        source_root=source_root,
        target_root=target,
        rules_file=args.rules,
        ignore_patterns=args.ignore,
    )
    result = wizard.scan()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("has_conflicts"):
        print("\n[提示] 检测到目标路径同名文件冲突，可用 wizard-resolve 设置处理策略，再执行 wizard-apply", file=sys.stderr)
    return 0


def _cmd_wizard_plan(args: argparse.Namespace, source_root: str) -> int:
    wizard = RelocationWizard(source_root=source_root, target_root=source_root)
    result = wizard.plan()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_wizard_apply(args: argparse.Namespace, source_root: str) -> int:
    wizard = RelocationWizard(source_root=source_root, target_root=source_root)
    if args.resume:
        result = wizard.resume()
    else:
        result = wizard.apply()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "partial":
        print("\n[提示] 搬家部分失败，可修复问题后用 wizard-apply --resume 续跑", file=sys.stderr)
        return 6
    return 0


def _cmd_wizard_undo(args: argparse.Namespace, source_root: str) -> int:
    wizard = RelocationWizard(source_root=source_root, target_root=source_root)
    result = wizard.undo()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "undo_partial":
        return 6
    return 0


def _cmd_wizard_status(args: argparse.Namespace, source_root: str) -> int:
    wizard = RelocationWizard(source_root=source_root, target_root=source_root)
    result = wizard.status()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_wizard_export(args: argparse.Namespace, source_root: str) -> int:
    output = args.output or os.path.join(tempfile.gettempdir(), f"wizard_result.{args.fmt}")
    output_abs = os.path.abspath(output)
    if os.path.exists(output_abs) and not args.force:
        print(f"[错误] 导出路径已被占用: {output_abs}", file=sys.stderr)
        print("  处理方式:", file=sys.stderr)
        print("    - 更换 --output 指定新路径", file=sys.stderr)
        print("    - 或加 --force 强制覆盖", file=sys.stderr)
        return 4
    wizard = RelocationWizard(source_root=source_root, target_root=source_root)
    result = wizard.export_result(args.fmt, output_abs)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_wizard_resolve(args: argparse.Namespace, source_root: str) -> int:
    wizard = RelocationWizard(source_root=source_root, target_root=source_root)
    result = wizard.set_conflict_resolution(args.source_path, args.resolution)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_wizard_audit_export(args: argparse.Namespace, source_root: str) -> int:
    output = args.output or os.path.join(tempfile.gettempdir(), f"wizard_audit.{args.fmt}")
    output_abs = os.path.abspath(output)
    if os.path.exists(output_abs) and not args.force:
        print(f"[错误] 导出路径已被占用: {output_abs}", file=sys.stderr)
        return 4
    wizard = RelocationWizard(source_root=source_root, target_root=source_root)
    result = wizard.export_audit(args.fmt, output_abs)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    source_root = os.path.abspath(os.getcwd())

    if args.command == "init":
        sys.exit(_cmd_init(args, source_root))

    if args.command == "detect-legacy":
        sys.exit(_cmd_detect_legacy(source_root))

    _WIZARD_COMMANDS = {
        "wizard-scan", "wizard-plan", "wizard-apply",
        "wizard-undo", "wizard-status", "wizard-export",
        "wizard-resolve", "wizard-audit-export",
    }

    if args.command in _WIZARD_COMMANDS:
        if args.command == "wizard-scan":
            sys.exit(_cmd_wizard_scan(args, source_root))
        elif args.command == "wizard-plan":
            sys.exit(_cmd_wizard_plan(args, source_root))
        elif args.command == "wizard-apply":
            sys.exit(_cmd_wizard_apply(args, source_root))
        elif args.command == "wizard-undo":
            sys.exit(_cmd_wizard_undo(args, source_root))
        elif args.command == "wizard-status":
            sys.exit(_cmd_wizard_status(args, source_root))
        elif args.command == "wizard-export":
            sys.exit(_cmd_wizard_export(args, source_root))
        elif args.command == "wizard-resolve":
            sys.exit(_cmd_wizard_resolve(args, source_root))
        elif args.command == "wizard-audit-export":
            sys.exit(_cmd_wizard_audit_export(args, source_root))

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"配置文件未找到: {args.config}", file=sys.stderr)
        print("请先运行 invoice-archiver init 生成示例配置", file=sys.stderr)
        sys.exit(1)

    source_root = config.project_source_root

    if args.command == "migrate":
        sys.exit(_cmd_migrate(args, source_root, config))

    rc = _preflight_check(args, config, source_root)
    if rc != 0:
        sys.exit(rc)

    archiver = Archiver(config)

    if args.command == "precheck":
        result = archiver.precheck()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "archive":
        result = archiver.archive()
        if result.get("batch_id") is None:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            existing = [b["batch_id"] for b in archiver.list_batches()]
            dupes = [b for b in existing if b == result["batch_id"]]
            if len(dupes) > 1:
                result["warning"] = (
                    f"出现重复批次ID: {result['batch_id']} (出现 {len(dupes)} 次)，"
                    "这通常是毫秒级并发导致，请使用 query-batch 核对"
                )
            print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "retry":
        result = archiver.retry_failures()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "rollback":
        bid = args.batch_id
        batch = archiver.batch_mgr.get_batch(bid)
        if batch is None:
            print(json.dumps({
                "success": False,
                "error": f"批次不存在: {bid}",
                "hint": "使用 list-batches 查看所有批次ID",
            }, indent=2, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
        if batch.status == "rolled_back":
            print(json.dumps({
                "success": False,
                "error": f"批次已处于回滚状态，不可重复回滚: {bid}",
                "rolled_back_at": batch.rolled_back_at,
            }, indent=2, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
        result = archiver.rollback(bid)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "query-batch":
        result = archiver.query_batch(args.batch_id)
        if result is None:
            print(json.dumps({
                "error": f"批次 {args.batch_id} 未找到",
                "hint": "使用 list-batches 查看所有批次ID",
            }, indent=2, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "list-batches":
        batches = archiver.list_batches()
        summary = {
            "total": len(batches),
            "by_status": {},
            "batch_ids": [b["batch_id"] for b in batches],
        }
        seen = set()
        dupes = []
        for b in batches:
            st = b.get("status", "unknown")
            summary["by_status"][st] = summary["by_status"].get(st, 0) + 1
            bid = b.get("batch_id")
            if bid in seen:
                dupes.append(bid)
            seen.add(bid)
        if dupes:
            summary["duplicate_batch_ids"] = list(set(dupes))
        print(json.dumps({"summary": summary, "batches": batches}, indent=2, ensure_ascii=False))

    elif args.command == "list-failures":
        entries = archiver.list_failures()
        dup_src = set()
        seen_src = set()
        for e in entries:
            sp = e.get("source_path")
            if sp in seen_src:
                dup_src.add(sp)
            seen_src.add(sp)
        out = {
            "total": len(entries),
            "with_retry_count_gt0": sum(1 for e in entries if e.get("retry_count", 0) > 0),
            "entries": entries,
        }
        if dup_src:
            out["duplicate_source_paths"] = sorted(dup_src)
            out["warning"] = "失败队列存在重复 source_path 条目，retry 时会同时影响"
        print(json.dumps(out, indent=2, ensure_ascii=False))

    elif args.command == "export-logs":
        output = args.output or _default_export_path(args.fmt)
        rc = _check_export_output(output, args.force)
        if rc != 0:
            sys.exit(rc)
        if args.force and os.path.exists(output):
            try:
                os.remove(output)
            except OSError as e:
                print(f"[错误] 无法删除旧导出文件: {output}: {e}", file=sys.stderr)
                sys.exit(5)
        final = archiver.export_logs(args.fmt, output)
        size_kb = round(os.path.getsize(final) / 1024, 2) if os.path.isfile(final) else 0
        print(json.dumps({
            "status": "exported",
            "format": args.fmt,
            "output": os.path.abspath(final),
            "size_kb": size_kb,
            "note": "如需再次导出到同一路径，请加 --force",
        }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
