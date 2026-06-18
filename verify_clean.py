import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from invoice_archiver.config import load_config
from invoice_archiver.archiver import Archiver

TEST_ROOT = r"C:\Users\13623\AppData\Local\Temp\invoice_archiver_test_1052469661"
CONFIG_PATH = os.path.join(TEST_ROOT, "test_config.yaml")
SOURCE_DIR = os.path.join(TEST_ROOT, "source")

def step(name, fn):
    print(f"\n{'='*60}")
    print(f"=== 步骤: {name} ===")
    print(f"{'='*60}")
    result = fn()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result

def main():
    config = load_config(CONFIG_PATH)
    archiver = Archiver(config)

    source_files_before = set(os.listdir(SOURCE_DIR))
    print(f"源目录文件数: {len(source_files_before)}")

    r1 = step("precheck (预检不搬文件)", archiver.precheck)
    assert r1["would_archive"] == 7, f"期望归档7个，实际{r1['would_archive']}"
    assert r1["would_skip"] == 2, f"期望跳过2个，实际{r1['would_skip']}"
    source_files_after = set(os.listdir(SOURCE_DIR))
    assert source_files_before == source_files_after, "预检不应移动文件！"
    print("OK 预检通过，文件未移动")

    r2 = step("archive (执行归档)", archiver.archive)
    assert r2["archived"] == 7, f"期望归档7个，实际{r2['archived']}"
    assert r2["failed"] == 0, f"期望失败0个，实际{r2['failed']}"
    assert r2["skipped"] == 2, f"期望跳过2个，实际{r2['skipped']}"
    batch_id = r2["batch_id"]
    print(f"OK 归档完成，批次ID: {batch_id}")

    r3 = step("list-batches (列批次)", archiver.list_batches)
    assert len(r3) >= 1, "至少应有1个批次"
    print(f"OK 批次列表正常，共{len(r3)}个批次")

    r4 = step("list-failures (列失败队列)", archiver.list_failures)
    assert len(r4) == 0, "失败队列应为空"
    print("OK 失败队列为空")

    r5 = step("export-logs (导出CSV)", lambda: {
        "output": archiver.export_logs("csv", os.path.join(TEST_ROOT, "export_logs.csv")),
        "format": "csv"
    })
    assert os.path.exists(r5["output"]), "导出文件应存在"
    assert r5["output"].startswith(tempfile.gettempdir()) or r5["output"].startswith(TEST_ROOT), "导出文件不在源码目录"
    print(f"OK CSV日志已导出到: {r5['output']}")

    r6 = step("export-logs (导出JSON)", lambda: {
        "output": archiver.export_logs("json", os.path.join(TEST_ROOT, "export_logs.json")),
        "format": "json"
    })
    assert os.path.exists(r6["output"]), "导出文件应存在"
    print(f"OK JSON日志已导出到: {r6['output']}")

    r7 = step("query-batch (查询批次详情)", lambda: archiver.query_batch(batch_id))
    assert r7 is not None, "批次应存在"
    assert r7["batch"]["batch_id"] == batch_id
    assert len(r7["logs"]) >= 7, "至少应有7条移动日志"
    print("OK 批次详情查询正常")

    r8 = step("rollback (回滚批次)", lambda: archiver.rollback(batch_id))
    assert r8["success"] == True
    assert r8["rolled_back"] == 7
    print("OK 批次已回滚，7个文件返回源目录")

    r9 = step("retry (重试失败队列)", archiver.retry_failures)
    assert r9["retried"] == 0
    print("OK 重试完成（无失败项）")

    source_files_final = set(os.listdir(SOURCE_DIR))
    assert source_files_before == source_files_final, f"回滚后源目录应恢复所有文件"
    print(f"OK 回滚验证：源目录文件全部恢复，共{len(source_files_final)}个")

    print("\n" + "="*60)
    print("=== 跨进程验证 ===")
    print("="*60)
    print("新建 Archiver 实例（模拟重启）...")
    archiver2 = Archiver(config)
    r10 = step("重启后 list-batches", archiver2.list_batches)
    assert len(r10) >= 1, "重启后批次记录应仍存在"
    assert any(b["batch_id"] == batch_id for b in r10), "重启后目标批次应存在"
    batch_data = next(b for b in r10 if b["batch_id"] == batch_id)
    assert batch_data["status"] == "rolled_back", "重启后批次状态应为rolled_back"
    print("OK 重启后批次状态正确")

    print("\n" + "="*60)
    print("=== 源码目录检查 ===")
    print("="*60)
    repo_root = os.path.dirname(os.path.abspath(__file__))
    forbidden_dirs = ["temp_inbox", "archive", "state", "logs"]
    found = []
    for d in forbidden_dirs:
        if os.path.isdir(os.path.join(repo_root, d)):
            found.append(d)
    if found:
        print(f"FAIL 源码目录仍有运行产物目录: {found}")
        sys.exit(1)
    else:
        print("OK 源码目录无运行产物目录")

    forbidden_files = ["archive_logs.csv", "full_logs.json", "*.jsonl"]
    for f in os.listdir(repo_root):
        if f.endswith(".csv") or f.endswith(".jsonl") or f in ["archive_logs.csv", "full_logs.json"]:
            fp = os.path.join(repo_root, f)
            if os.path.isfile(fp):
                found.append(f)
    if found:
        print(f"FAIL 源码目录仍有运行产物文件: {found}")
        sys.exit(1)
    else:
        print("OK 源码目录无运行产物文件")

    print("\n" + "="*60)
    print("PASS 所有验证通过！")
    print("="*60)

if __name__ == "__main__":
    main()
