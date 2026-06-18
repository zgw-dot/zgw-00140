import argparse
import json
import os
import sys
import tempfile

from .config import load_config, init_sample_config, ArchiveConfig
from .archiver import Archiver
from . import migration as mig
from .relocation_wizard import RelocationWizard
from .takeover_ledger import TakeoverLedger
from .drill_center import DrillCenter
from .handoff_sealer import HandoffSealer


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

    p_tk_scan = sub.add_parser("takeover-scan", help="接管台账: 扫描运行目录产物，生成接管清单")
    p_tk_scan.add_argument("--target", required=True, help="接管目标运行目录")
    p_tk_scan.add_argument("--config", dest="tk_config", default=None, help="配置文件路径 (用于记录原始路径)")
    p_tk_scan.add_argument("--conflict-policy", choices=["skip", "overwrite", "rename"], default="rename",
                           help="冲突处理策略 (默认: rename)")
    p_tk_scan.add_argument("--duplicate-policy", choices=["skip", "merge", "overwrite", "ask"], default="merge",
                           help="重复导出记录处理策略 (默认: merge)")

    sub.add_parser("takeover-confirm", help="接管台账: 确认接管清单")
    sub.add_parser("takeover-apply", help="接管台账: 执行接管 (切换到新运行目录)")

    p_tk_resume = sub.add_parser("takeover-resume", help="接管台账: 续跑上次未完成的接管")
    p_tk_resume.add_argument("--resume", action="store_true", help="续跑上次未完成的接管")

    sub.add_parser("takeover-undo", help="接管台账: 一键撤销接管 (恢复原始路径)")
    sub.add_parser("takeover-status", help="接管台账: 查看接管状态和台账详情")

    p_tk_export = sub.add_parser("takeover-export", help="接管台账: 导出台账 (JSON/CSV)")
    p_tk_export.add_argument("--format", choices=["csv", "json"], default="json", dest="fmt")
    p_tk_export.add_argument("--output", default=None, help="输出文件路径")
    p_tk_export.add_argument("--force", action="store_true", help="如输出文件已存在，强制覆盖")

    p_tk_audit = sub.add_parser("takeover-audit", help="接管台账: 导出审计日志 (JSON/CSV)")
    p_tk_audit.add_argument("--format", choices=["csv", "json"], default="json", dest="fmt")
    p_tk_audit.add_argument("--output", default=None, help="输出文件路径")
    p_tk_audit.add_argument("--force", action="store_true", help="如输出文件已存在，强制覆盖")

    p_tk_resolve = sub.add_parser("takeover-resolve", help="接管台账: 设置冲突/重复策略")
    p_tk_resolve.add_argument("--source-path", default=None, help="冲突文件源路径")
    p_tk_resolve.add_argument("--resolution", choices=["skip", "overwrite", "rename", "ask"], default=None,
                              help="冲突处理策略")
    p_tk_resolve.add_argument("--duplicate-policy", choices=["skip", "merge", "overwrite", "ask"], default=None,
                              help="重复导出记录处理策略")

    p_drill_scan = sub.add_parser("drill-scan", help="演练中心: 扫描旧产物，生成演练清单")
    p_drill_scan.add_argument("--target", required=True, help="演练目标运行目录")
    p_drill_scan.add_argument("--config", dest="drill_config", default=None, help="配置文件路径")
    p_drill_scan.add_argument("--conflict-policy", choices=["skip", "overwrite", "rename"], default="rename",
                              help="冲突处理策略 (默认: rename)")
    p_drill_scan.add_argument("--duplicate-policy", choices=["skip", "merge", "overwrite", "ask"], default="merge",
                              help="重复导出记录处理策略 (默认: merge)")
    p_drill_scan.add_argument("--session-id", default=None, help="自定义会话 ID")

    sub.add_parser("drill-plan", help="演练中心: 生成演练计划 (含批次摘要、回放计划、配置差异)")

    p_drill_replay = sub.add_parser("drill-replay", help="演练中心: 按目标目录回放 list-batches / list-failures / export-logs")
    p_drill_replay.add_argument("--target", default=None, help="回放目标目录 (覆盖已有)")

    sub.add_parser("drill-report", help="演练中心: 生成演练报告 (batch_id、失败次数、来源去向、配置快照差异、重复导出标记)")

    p_drill_save = sub.add_parser("drill-save", help="演练中心: 保存当前演练会话")
    p_drill_save.add_argument("--label", default=None, help="会话标签 (默认使用 session_id)")

    p_drill_load = sub.add_parser("drill-load", help="演练中心: 加载已保存的演练会话")
    p_drill_load.add_argument("--label", required=True, help="要加载的会话标签")

    sub.add_parser("drill-sessions", help="演练中心: 列出所有已保存的演练会话")

    sub.add_parser("drill-undo", help="演练中心: 撤销演练 (回退演练状态)")

    sub.add_parser("drill-status", help="演练中心: 查看演练状态")

    p_drill_export = sub.add_parser("drill-export", help="演练中心: 导出演练证据包 (JSON/CSV)")
    p_drill_export.add_argument("--format", choices=["csv", "json"], default="json", dest="fmt")
    p_drill_export.add_argument("--output", default=None, help="输出文件路径")
    p_drill_export.add_argument("--force", action="store_true", help="如输出文件已存在，强制覆盖")

    p_drill_audit = sub.add_parser("drill-audit", help="演练中心: 导出演练审计日志 (JSON/CSV)")
    p_drill_audit.add_argument("--format", choices=["csv", "json"], default="json", dest="fmt")
    p_drill_audit.add_argument("--output", default=None, help="输出文件路径")
    p_drill_audit.add_argument("--force", action="store_true", help="如输出文件已存在，强制覆盖")

    p_drill_cleanup = sub.add_parser("drill-cleanup", help="演练中心: 清理源目录中的演练残留文件")

    p_hs_pack = sub.add_parser("handoff-pack", help="交接封箱包: 打包当前运行目录配置、批次、失败队列、导出记录为可回放交接包")
    p_hs_pack.add_argument("--output", default=None, help="输出包路径 (.zip)，默认到系统临时目录")
    p_hs_pack.add_argument("--notes", default="", help="交接包备注说明")
    p_hs_pack.add_argument("--no-samples", action="store_true", help="不打包 archive/temp_inbox 中的样例文件（仅元数据）")

    p_hs_precheck_pack = sub.add_parser("handoff-precheck-pack", help="交接封箱包: 预检交接包完整性和校验摘要")
    p_hs_precheck_pack.add_argument("--pack", required=True, help="交接包路径 (.zip)")

    p_hs_precheck_import = sub.add_parser("handoff-precheck-import", help="交接封箱包: 预检导入冲突（目录已有数据、同名批次、重复导出、手改配置等）")
    p_hs_precheck_import.add_argument("--pack", required=True, help="交接包路径 (.zip)")
    p_hs_precheck_import.add_argument("--target", default=None, help="目标运行目录（默认当前目录）")
    p_hs_precheck_import.add_argument("--dry-run", action="store_true", help="预检模式，不实际写入")

    p_hs_import = sub.add_parser("handoff-import", help="交接封箱包: 导入交接包到目标运行目录")
    p_hs_import.add_argument("--pack", required=True, help="交接包路径 (.zip)")
    p_hs_import.add_argument("--target", default=None, help="目标运行目录（默认当前目录）")
    p_hs_import.add_argument("--dry-run", action="store_true", help="演练模式，不实际写入文件")
    p_hs_import.add_argument("--resume", action="store_true", help="续跑上次未完成的导入（重启后继续）")
    p_hs_import.add_argument("--conflict-policy", choices=["skip", "overwrite", "rename"], default="skip",
                             help="冲突处理策略: skip跳过(默认), overwrite覆盖, rename自动重命名")

    sub.add_parser("handoff-confirm", help="交接封箱包: 确认导入落地，关闭撤销通道")

    sub.add_parser("handoff-undo", help="交接封箱包: 撤销导入，恢复目标目录到导入前状态")

    p_hs_history = sub.add_parser("handoff-history", help="交接封箱包: 查看打包/导入历史和审计日志")
    p_hs_history.add_argument("--pack-id", default=None, help="按包ID过滤历史记录")
    p_hs_history.add_argument("--limit", type=int, default=100, help="最多显示记录数（默认100）")

    sub.add_parser("handoff-status", help="交接封箱包: 查看当前操作状态")
    sub.add_parser("handoff-cleanup", help="交接封箱包: 清理sealer状态文件和审计日志")

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


def _cmd_takeover_scan(args: argparse.Namespace, source_root: str) -> int:
    config_path = getattr(args, "tk_config", None) or args.config
    ledger = TakeoverLedger(
        source_root=source_root,
        target_root=args.target,
        config_path=config_path,
        conflict_policy=args.conflict_policy,
        duplicate_policy=args.duplicate_policy,
    )
    result = ledger.scan()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("has_conflicts"):
        print("\n[提示] 检测到目标路径同名文件冲突，可用 takeover-resolve 设置处理策略", file=sys.stderr)
    if result.get("ledger", {}).get("items_with_duplicate_export", 0) > 0:
        print("[提示] 检测到重复导出记录，可用 takeover-resolve --duplicate-policy 设置策略", file=sys.stderr)
    return 0


def _cmd_takeover_confirm(source_root: str) -> int:
    ledger = TakeoverLedger(source_root=source_root, target_root=source_root)
    result = ledger.confirm()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    return 0


def _cmd_takeover_apply(source_root: str) -> int:
    ledger = TakeoverLedger(source_root=source_root, target_root=source_root)
    result = ledger.apply()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "partial":
        print("\n[提示] 接管部分失败，可修复问题后用 takeover-resume 续跑", file=sys.stderr)
        return 6
    return 0


def _cmd_takeover_resume(source_root: str) -> int:
    ledger = TakeoverLedger(source_root=source_root, target_root=source_root)
    result = ledger.resume()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "partial":
        return 6
    return 0


def _cmd_takeover_undo(source_root: str) -> int:
    ledger = TakeoverLedger(source_root=source_root, target_root=source_root)
    result = ledger.undo()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "undo_partial":
        return 6
    return 0


def _cmd_takeover_status(source_root: str) -> int:
    ledger = TakeoverLedger(source_root=source_root, target_root=source_root)
    result = ledger.status()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_takeover_export(args: argparse.Namespace, source_root: str) -> int:
    output = args.output or os.path.join(tempfile.gettempdir(), f"takeover_ledger.{args.fmt}")
    output_abs = os.path.abspath(output)
    if os.path.exists(output_abs) and not args.force:
        print(f"[错误] 导出路径已被占用: {output_abs}", file=sys.stderr)
        print("  处理方式:", file=sys.stderr)
        print("    - 更换 --output 指定新路径", file=sys.stderr)
        print("    - 或加 --force 强制覆盖", file=sys.stderr)
        return 4
    ledger = TakeoverLedger(source_root=source_root, target_root=source_root)
    result = ledger.export_ledger(args.fmt, output_abs)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_takeover_audit(args: argparse.Namespace, source_root: str) -> int:
    output = args.output or os.path.join(tempfile.gettempdir(), f"takeover_audit.{args.fmt}")
    output_abs = os.path.abspath(output)
    if os.path.exists(output_abs) and not args.force:
        print(f"[错误] 导出路径已被占用: {output_abs}", file=sys.stderr)
        return 4
    ledger = TakeoverLedger(source_root=source_root, target_root=source_root)
    result = ledger.export_audit(args.fmt, output_abs)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_takeover_resolve(args: argparse.Namespace, source_root: str) -> int:
    ledger = TakeoverLedger(source_root=source_root, target_root=source_root)
    results = []
    if args.source_path and args.resolution:
        result = ledger.set_conflict_resolution(args.source_path, args.resolution)
        results.append(result)
    if args.duplicate_policy:
        result = ledger.set_duplicate_policy(args.duplicate_policy)
        results.append(result)
    if not results:
        print("[错误] 请指定 --source-path + --resolution 或 --duplicate-policy", file=sys.stderr)
        return 5
    for r in results:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0


def _cmd_drill_scan(args: argparse.Namespace, source_root: str) -> int:
    config_path = getattr(args, "drill_config", None) or args.config
    drill = DrillCenter(
        source_root=source_root,
        target_root=args.target,
        config_path=config_path,
        conflict_policy=args.conflict_policy,
        duplicate_policy=args.duplicate_policy,
        session_id=getattr(args, "session_id", None),
    )
    result = drill.scan()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("has_conflicts"):
        print("\n[提示] 检测到目标路径同名文件冲突", file=sys.stderr)
    if result.get("ledger", {}).get("items_with_duplicate_export", 0) > 0:
        print("[提示] 检测到重复导出记录", file=sys.stderr)
    return 0


def _resolve_drill_target(source_root: str) -> str:
    from .drill_center import _safe_read_json, DRILL_STATE_FILE, DRILL_POINTER_FILE
    abs_root = os.path.abspath(source_root)
    state_path = os.path.join(abs_root, ".drill_state.json")
    data = _safe_read_json(state_path)
    if data and data.get("target_root"):
        return data["target_root"]
    pointer_path = os.path.join(abs_root, DRILL_POINTER_FILE)
    pdata = _safe_read_json(pointer_path)
    if pdata and pdata.get("target_root"):
        return pdata["target_root"]
    for entry in os.listdir(abs_root):
        candidate = os.path.join(abs_root, entry, ".drill", DRILL_STATE_FILE)
        if os.path.isfile(candidate):
            d = _safe_read_json(candidate)
            if d and d.get("target_root"):
                return d["target_root"]
    return source_root


def _cmd_drill_plan(source_root: str) -> int:
    target = _resolve_drill_target(source_root)
    drill = DrillCenter(source_root=source_root, target_root=target)
    result = drill.plan()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    return 0


def _cmd_drill_replay(args: argparse.Namespace, source_root: str) -> int:
    target = getattr(args, "target", None)
    if not target:
        target = _resolve_drill_target(source_root)
    drill = DrillCenter(source_root=source_root, target_root=target)
    result = drill.replay()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    return 0


def _cmd_drill_report(source_root: str) -> int:
    target = _resolve_drill_target(source_root)
    drill = DrillCenter(source_root=source_root, target_root=target)
    result = drill.report()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    return 0


def _cmd_drill_save(args: argparse.Namespace, source_root: str) -> int:
    target = _resolve_drill_target(source_root)
    drill = DrillCenter(source_root=source_root, target_root=target)
    result = drill.save_session(label=getattr(args, "label", None))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    return 0


def _cmd_drill_load(args: argparse.Namespace, source_root: str) -> int:
    target = _resolve_drill_target(source_root)
    drill = DrillCenter(source_root=source_root, target_root=target)
    result = drill.load_session(label=args.label)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    return 0


def _cmd_drill_sessions(source_root: str) -> int:
    target = _resolve_drill_target(source_root)
    drill = DrillCenter(source_root=source_root, target_root=target)
    result = drill.list_sessions()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_drill_undo(source_root: str) -> int:
    target = _resolve_drill_target(source_root)
    drill = DrillCenter(source_root=source_root, target_root=target)
    result = drill.undo()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    return 0


def _cmd_drill_status(source_root: str) -> int:
    target = _resolve_drill_target(source_root)
    drill = DrillCenter(source_root=source_root, target_root=target)
    result = drill.status()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_drill_export(args: argparse.Namespace, source_root: str) -> int:
    output = args.output or os.path.join(tempfile.gettempdir(), f"drill_evidence.{args.fmt}")
    output_abs = os.path.abspath(output)
    if os.path.exists(output_abs) and not args.force:
        print(f"[错误] 导出路径已被占用: {output_abs}", file=sys.stderr)
        print("  处理方式:", file=sys.stderr)
        print("    - 更换 --output 指定新路径", file=sys.stderr)
        print("    - 或加 --force 强制覆盖", file=sys.stderr)
        return 4
    target = _resolve_drill_target(source_root)
    drill = DrillCenter(source_root=source_root, target_root=target)
    result = drill.export_evidence(args.fmt, output_abs)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    return 0


def _cmd_drill_audit(args: argparse.Namespace, source_root: str) -> int:
    output = args.output or os.path.join(tempfile.gettempdir(), f"drill_audit.{args.fmt}")
    output_abs = os.path.abspath(output)
    if os.path.exists(output_abs) and not args.force:
        print(f"[错误] 导出路径已被占用: {output_abs}", file=sys.stderr)
        return 4
    target = _resolve_drill_target(source_root)
    drill = DrillCenter(source_root=source_root, target_root=target)
    result = drill.export_audit(args.fmt, output_abs)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    return 0


def _cmd_drill_cleanup(source_root: str) -> int:
    target = _resolve_drill_target(source_root)
    drill = DrillCenter(source_root=source_root, target_root=target)
    result = drill.cleanup()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_handoff_pack(args: argparse.Namespace, source_root: str) -> int:
    sealer = HandoffSealer(runtime_root=source_root)
    result = sealer.pack(
        output_path=args.output,
        notes=args.notes,
        include_samples=not args.no_samples,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    return 0


def _cmd_handoff_precheck_pack(args: argparse.Namespace, source_root: str) -> int:
    sealer = HandoffSealer(runtime_root=source_root)
    result = sealer.precheck_pack(pack_path=args.pack)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    if not result.get("is_valid", False):
        print("\n[错误] 交接包存在问题，请检查 issues 列表", file=sys.stderr)
        return 5
    return 0


def _cmd_handoff_precheck_import(args: argparse.Namespace, source_root: str) -> int:
    sealer = HandoffSealer(runtime_root=source_root)
    target = args.target or source_root
    result = sealer.precheck_import(
        pack_path=args.pack,
        target_runtime_root=target,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    if result.get("error_conflicts", 0) > 0:
        print("\n[错误] 存在阻断性冲突（权限不足等），请先修复", file=sys.stderr)
        return 6
    if result.get("warning_conflicts", 0) > 0:
        print("\n[提示] 存在冲突警告（同名文件、重复批次等），导入时可用 --conflict-policy 指定处理策略", file=sys.stderr)
    return 0


def _cmd_handoff_import(args: argparse.Namespace, source_root: str) -> int:
    sealer = HandoffSealer(runtime_root=source_root)
    target = args.target or source_root
    result = sealer.do_import(
        pack_path=args.pack,
        target_runtime_root=target,
        dry_run=args.dry_run,
        resume=args.resume,
        conflict_policy=args.conflict_policy,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    if result.get("status") == "partial":
        print("\n[提示] 导入部分失败，可修复问题后用 handoff-import --resume 续跑，或 handoff-undo 撤销", file=sys.stderr)
        return 6
    if result.get("status") == "imported":
        print("\n[提示] 导入完成，执行 handoff-confirm 确认落地或 handoff-undo 撤销", file=sys.stderr)
    return 0


def _cmd_handoff_confirm(source_root: str) -> int:
    sealer = HandoffSealer(runtime_root=source_root)
    result = sealer.confirm()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    return 0


def _cmd_handoff_undo(source_root: str) -> int:
    sealer = HandoffSealer(runtime_root=source_root)
    result = sealer.undo()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        return 5
    if result.get("status") == "undo_partial":
        return 6
    return 0


def _cmd_handoff_history(args: argparse.Namespace, source_root: str) -> int:
    sealer = HandoffSealer(runtime_root=source_root)
    result = sealer.list_history(pack_id=args.pack_id, limit=args.limit)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_handoff_status(source_root: str) -> int:
    sealer = HandoffSealer(runtime_root=source_root)
    result = sealer.status()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_handoff_cleanup(source_root: str) -> int:
    sealer = HandoffSealer(runtime_root=source_root)
    result = sealer.cleanup()
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

    _TAKEOVER_COMMANDS = {
        "takeover-scan", "takeover-confirm", "takeover-apply",
        "takeover-resume", "takeover-undo", "takeover-status",
        "takeover-export", "takeover-audit", "takeover-resolve",
    }

    _DRILL_COMMANDS = {
        "drill-scan", "drill-plan", "drill-replay", "drill-report",
        "drill-save", "drill-load", "drill-sessions", "drill-undo",
        "drill-status", "drill-export", "drill-audit", "drill-cleanup",
    }

    _HANDOFF_COMMANDS = {
        "handoff-pack", "handoff-precheck-pack", "handoff-precheck-import",
        "handoff-import", "handoff-confirm", "handoff-undo",
        "handoff-history", "handoff-status", "handoff-cleanup",
    }

    if args.command in _TAKEOVER_COMMANDS:
        if args.command == "takeover-scan":
            sys.exit(_cmd_takeover_scan(args, source_root))
        elif args.command == "takeover-confirm":
            sys.exit(_cmd_takeover_confirm(source_root))
        elif args.command == "takeover-apply":
            sys.exit(_cmd_takeover_apply(source_root))
        elif args.command == "takeover-resume":
            sys.exit(_cmd_takeover_resume(source_root))
        elif args.command == "takeover-undo":
            sys.exit(_cmd_takeover_undo(source_root))
        elif args.command == "takeover-status":
            sys.exit(_cmd_takeover_status(source_root))
        elif args.command == "takeover-export":
            sys.exit(_cmd_takeover_export(args, source_root))
        elif args.command == "takeover-audit":
            sys.exit(_cmd_takeover_audit(args, source_root))
        elif args.command == "takeover-resolve":
            sys.exit(_cmd_takeover_resolve(args, source_root))

    if args.command in _DRILL_COMMANDS:
        if args.command == "drill-scan":
            sys.exit(_cmd_drill_scan(args, source_root))
        elif args.command == "drill-plan":
            sys.exit(_cmd_drill_plan(source_root))
        elif args.command == "drill-replay":
            sys.exit(_cmd_drill_replay(args, source_root))
        elif args.command == "drill-report":
            sys.exit(_cmd_drill_report(source_root))
        elif args.command == "drill-save":
            sys.exit(_cmd_drill_save(args, source_root))
        elif args.command == "drill-load":
            sys.exit(_cmd_drill_load(args, source_root))
        elif args.command == "drill-sessions":
            sys.exit(_cmd_drill_sessions(source_root))
        elif args.command == "drill-undo":
            sys.exit(_cmd_drill_undo(source_root))
        elif args.command == "drill-status":
            sys.exit(_cmd_drill_status(source_root))
        elif args.command == "drill-export":
            sys.exit(_cmd_drill_export(args, source_root))
        elif args.command == "drill-audit":
            sys.exit(_cmd_drill_audit(args, source_root))
        elif args.command == "drill-cleanup":
            sys.exit(_cmd_drill_cleanup(source_root))

    if args.command in _HANDOFF_COMMANDS:
        if args.command == "handoff-pack":
            sys.exit(_cmd_handoff_pack(args, source_root))
        elif args.command == "handoff-precheck-pack":
            sys.exit(_cmd_handoff_precheck_pack(args, source_root))
        elif args.command == "handoff-precheck-import":
            sys.exit(_cmd_handoff_precheck_import(args, source_root))
        elif args.command == "handoff-import":
            sys.exit(_cmd_handoff_import(args, source_root))
        elif args.command == "handoff-confirm":
            sys.exit(_cmd_handoff_confirm(source_root))
        elif args.command == "handoff-undo":
            sys.exit(_cmd_handoff_undo(source_root))
        elif args.command == "handoff-history":
            sys.exit(_cmd_handoff_history(args, source_root))
        elif args.command == "handoff-status":
            sys.exit(_cmd_handoff_status(source_root))
        elif args.command == "handoff-cleanup":
            sys.exit(_cmd_handoff_cleanup(source_root))

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
