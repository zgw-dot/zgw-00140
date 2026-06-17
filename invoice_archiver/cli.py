import argparse
import json
import sys
import os

from .config import load_config, init_sample_config, ArchiveConfig
from .archiver import Archiver


def main():
    parser = argparse.ArgumentParser(
        prog="invoice-archiver",
        description="采购发票附件归档守护工具",
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="生成示例配置文件和目录结构")
    sub.add_parser("precheck", help="预检：扫描并报告，不移动文件")
    sub.add_parser("archive", help="执行归档")
    sub.add_parser("retry", help="重试失败队列")

    p_rollback = sub.add_parser("rollback", help="回滚指定批次")
    p_rollback.add_argument("--batch-id", required=True, help="要回滚的批次ID")

    p_query = sub.add_parser("query-batch", help="查询批次详情")
    p_query.add_argument("--batch-id", required=True, help="批次ID")

    sub.add_parser("list-batches", help="列出所有批次")
    sub.add_parser("list-failures", help="列出失败队列")

    p_export = sub.add_parser("export-logs", help="导出日志")
    p_export.add_argument(
        "--format", choices=["csv", "json"], default="csv", dest="fmt",
        help="导出格式",
    )
    p_export.add_argument("--output", default="archive_logs.csv", help="输出文件路径")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "init":
        cfg_path = init_sample_config(args.config)
        cfg = ArchiveConfig()
        os.makedirs(cfg.source_dir, exist_ok=True)
        os.makedirs(cfg.archive_dir, exist_ok=True)
        print(json.dumps({
            "status": "initialized",
            "config": cfg_path,
            "source_dir": os.path.abspath(cfg.source_dir),
            "archive_dir": os.path.abspath(cfg.archive_dir),
        }, indent=2, ensure_ascii=False))
        return

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"配置文件未找到: {args.config}", file=sys.stderr)
        print("请先运行 invoice-archiver init 生成示例配置", file=sys.stderr)
        sys.exit(1)

    archiver = Archiver(config)

    if args.command == "precheck":
        result = archiver.precheck()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "archive":
        result = archiver.archive()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "retry":
        result = archiver.retry_failures()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "rollback":
        result = archiver.rollback(args.batch_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "query-batch":
        result = archiver.query_batch(args.batch_id)
        if result is None:
            print(f"批次 {args.batch_id} 未找到", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "list-batches":
        result = archiver.list_batches()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "list-failures":
        result = archiver.list_failures()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "export-logs":
        output = archiver.export_logs(args.fmt, args.output)
        print(f"日志已导出: {os.path.abspath(output)}")


if __name__ == "__main__":
    main()
