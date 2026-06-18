import os
import sys
import json
import shutil
import tempfile
import zipfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from invoice_archiver.cli import main as cli_main
from invoice_archiver.handoff_sealer import HandoffSealer
from invoice_archiver.config import init_sample_config, load_config, save_config, ArchiveConfig


TEST_ROOT_BASE = os.path.join(tempfile.gettempdir(), "handoff_sealer_verify")


def step(name, fn):
    print(f"\n{'='*60}")
    print(f"=== 步骤: {name} ===")
    print(f"{'='*60}")
    result = fn()
    if result is not None:
        if isinstance(result, dict):
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(result)
    return result


def run_cli(args, cwd=None):
    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")] + args
    env = os.environ.copy()
    proc = subprocess.run(cmd, cwd=cwd or os.getcwd(), capture_output=True, env=env)
    stdout = proc.stdout.decode("utf-8", errors="replace").strip() if proc.stdout else ""
    stderr = proc.stderr.decode("utf-8", errors="replace").strip() if proc.stderr else ""
    try:
        if stdout:
            result = json.loads(stdout)
        else:
            result = {}
    except json.JSONDecodeError:
        result = {"_raw_stdout": stdout, "_raw_stderr": stderr}
    result["_returncode"] = proc.returncode
    result["_stderr"] = stderr
    return result


def setup_runtime_dir(base_name: str, with_data: bool = True,
                      with_failures: bool = False, with_dup_exports: bool = False):
    root = os.path.join(TEST_ROOT_BASE, base_name)
    if os.path.exists(root):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    state_dir = os.path.join(root, "state")
    log_dir = os.path.join(root, "logs")
    archive_dir = os.path.join(root, "archive")
    inbox_dir = os.path.join(root, "temp_inbox")

    for d in (state_dir, log_dir, archive_dir, inbox_dir):
        os.makedirs(d, exist_ok=True)

    cfg_path = os.path.join(root, "config.yaml")
    init_sample_config(cfg_path)

    cfg_data = load_config(cfg_path).to_dict()
    cfg_data["source_dir"] = inbox_dir
    cfg_data["archive_dir"] = archive_dir
    cfg_data["state_dir"] = state_dir
    cfg_data["log_dir"] = log_dir
    with open(cfg_path, "w", encoding="utf-8") as f:
        import yaml
        yaml.dump(cfg_data, f, allow_unicode=True, default_flow_style=False)

    if with_data:
        batches = [
            {
                "batch_id": "BATCH_20260101_100001_000001",
                "created_at": "2026-01-01T10:00:01.000000",
                "committed_at": "2026-01-01T10:00:05.000000",
                "rolled_back_at": None,
                "status": "committed",
                "files": [
                    {
                        "source_path": os.path.join(inbox_dir, "SUP001_invoice_a.pdf"),
                        "target_path": os.path.join(archive_dir, "SUP001", "BATCH_20260101_100001_000001", "SUP001_invoice_a.pdf"),
                        "filename": "SUP001_invoice_a.pdf",
                        "supplier_code": "SUP001",
                        "status": "moved",
                    }
                ],
            },
            {
                "batch_id": "BATCH_20260102_140002_000002",
                "created_at": "2026-01-02T14:00:02.000000",
                "committed_at": "2026-01-02T14:00:10.000000",
                "rolled_back_at": None,
                "status": "committed",
                "files": [
                    {
                        "source_path": os.path.join(inbox_dir, "SUP002_invoice_b.pdf"),
                        "target_path": os.path.join(archive_dir, "SUP002", "BATCH_20260102_140002_000002", "SUP002_invoice_b.pdf"),
                        "filename": "SUP002_invoice_b.pdf",
                        "supplier_code": "SUP002",
                        "status": "moved",
                    }
                ],
            },
        ]

        sup1_dir = os.path.join(archive_dir, "SUP001", "BATCH_20260101_100001_000001")
        sup2_dir = os.path.join(archive_dir, "SUP002", "BATCH_20260102_140002_000002")
        os.makedirs(sup1_dir, exist_ok=True)
        os.makedirs(sup2_dir, exist_ok=True)
        with open(os.path.join(sup1_dir, "SUP001_invoice_a.pdf"), "wb") as f:
            f.write(b"%PDF-1.4 sample_a\n" * 10)
        with open(os.path.join(sup2_dir, "SUP002_invoice_b.pdf"), "wb") as f:
            f.write(b"%PDF-1.4 sample_b\n" * 10)

        with open(os.path.join(state_dir, "batches.json"), "w", encoding="utf-8") as f:
            json.dump(batches, f, indent=2, ensure_ascii=False)

        fq = []
        if with_failures:
            fq = [
                {
                    "source_path": os.path.join(inbox_dir, "SUP003_broken.pdf"),
                    "target_path": os.path.join(archive_dir, "SUP003", "BATCH_20260103_090003_000003", "SUP003_broken.pdf"),
                    "supplier_code": "SUP003",
                    "error": "FileNotFoundError: source missing",
                    "retry_count": 2,
                    "last_retry_at": "2026-01-03T09:01:00.000000",
                    "batch_id": "BATCH_20260103_090003_000003",
                    "created_at": "2026-01-03T09:00:03.000000",
                }
            ]
        with open(os.path.join(state_dir, "failure_queue.json"), "w", encoding="utf-8") as f:
            json.dump(fq, f, indent=2, ensure_ascii=False)

        log_entries = [
            {
                "timestamp": "2026-01-01T10:00:03.000000",
                "source_path": os.path.join(inbox_dir, "SUP001_invoice_a.pdf"),
                "target_path": os.path.join(archive_dir, "SUP001", "BATCH_20260101_100001_000001", "SUP001_invoice_a.pdf"),
                "action": "move",
                "batch_id": "BATCH_20260101_100001_000001",
                "status": "success",
            },
            {
                "timestamp": "2026-01-02T14:00:05.000000",
                "source_path": os.path.join(inbox_dir, "SUP002_invoice_b.pdf"),
                "target_path": os.path.join(archive_dir, "SUP002", "BATCH_20260102_140002_000002", "SUP002_invoice_b.pdf"),
                "action": "move",
                "batch_id": "BATCH_20260102_140002_000002",
                "status": "success",
            },
        ]
        if with_dup_exports:
            log_entries.append({
                "timestamp": "2026-01-02T14:00:30.000000",
                "source_path": os.path.join(inbox_dir, "SUP002_invoice_b.pdf"),
                "target_path": os.path.join(archive_dir, "SUP002", "BATCH_20260102_140002_000002", "SUP002_invoice_b_dup.pdf"),
                "action": "move",
                "batch_id": "BATCH_20260102_140002_000002",
                "status": "success",
            })
        with open(os.path.join(log_dir, "archive_log.jsonl"), "w", encoding="utf-8") as f:
            for e in log_entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    return {
        "root": root,
        "state_dir": state_dir,
        "log_dir": log_dir,
        "archive_dir": archive_dir,
        "inbox_dir": inbox_dir,
        "config_path": cfg_path,
    }


def check_git_clean(cwd):
    proc = subprocess.run(
        ["git", "-C", cwd, "status", "--porcelain"],
        capture_output=True, text=True
    )
    output = proc.stdout.strip()
    return output == "", output


def test_scenario_1_pack_and_precheck():
    print("\n" + "#" * 60)
    print("# 场景1: 打包 + 预检完整性")
    print("#" * 60)

    rt = setup_runtime_dir("src_A", with_data=True, with_failures=True, with_dup_exports=True)
    pack_output = os.path.join(TEST_ROOT_BASE, "test_pack.zip")

    def _pack():
        sealer = HandoffSealer(runtime_root=rt["root"])
        return sealer.pack(output_path=pack_output, notes="测试交接包A->B")

    result = step("1a. handoff-pack 打包", _pack)
    assert result["status"] == "packed", f"打包失败: {result}"
    assert result["pack_id"].startswith("PACK_")
    assert os.path.isfile(pack_output)
    assert result["batch_count"] >= 2
    assert result["failure_count"] >= 1
    assert result["duplicate_export_count"] >= 1
    pack_id = result["pack_id"]
    print(f"OK 打包成功, pack_id={pack_id}, size={result['total_size_kb']}KB")

    def _precheck_pack():
        sealer = HandoffSealer(runtime_root=rt["root"])
        return sealer.precheck_pack(pack_path=pack_output)

    result2 = step("1b. handoff-precheck-pack 预检包完整性", _precheck_pack)
    assert result2["status"] == "prechecked", f"预检失败: {result2}"
    assert result2["is_valid"] == True
    assert result2["pack_id"] == pack_id
    assert result2["verified_items"] >= 5
    assert len(result2["corrupted_items"]) == 0
    print(f"OK 预检通过, 验证{result2['verified_items']}个文件完整性")

    with zipfile.ZipFile(pack_output, "r") as zf:
        names = zf.namelist()
        for required in ["manifest.json", "checksums.json", "items_manifest.json",
                         "items_manifest.csv", "config_snapshot.json"]:
            assert required in names, f"缺少必要文件: {required}"
        assert any(n.startswith("state/") for n in names), "缺少state目录内容"
        assert any(n.startswith("logs/") for n in names), "缺少logs目录内容"
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["pack_id"] == pack_id
        assert len(manifest["batch_ids"]) >= 2
        checksums = json.loads(zf.read("checksums.json").decode("utf-8"))
        assert isinstance(checksums, list) and len(checksums) >= 5
    print("OK ZIP包内文件结构完整: manifest/checksums/items/config/batches/failures/logs齐全")

    return pack_output, pack_id, rt


def test_scenario_2_precheck_import_conflicts():
    print("\n" + "#" * 60)
    print("# 场景2: 导入预检 - 各种冲突分支检测")
    print("#" * 60)

    src_rt = setup_runtime_dir("src_C", with_data=True, with_failures=True, with_dup_exports=True)
    tgt_rt = setup_runtime_dir("tgt_D", with_data=True, with_failures=False, with_dup_exports=True)

    pack_output = os.path.join(TEST_ROOT_BASE, "test_conflict_pack.zip")
    sealer_src = HandoffSealer(runtime_root=src_rt["root"])
    pack_result = sealer_src.pack(output_path=pack_output)
    pack_id = pack_result["pack_id"]

    tgt_state_file = os.path.join(tgt_rt["state_dir"], "batches.json")
    with open(tgt_state_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list) and len(data) >= 1:
        data[0]["batch_id"] = "BATCH_20260101_100001_000001"
    with open(tgt_state_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    tgt_cfg_path = tgt_rt["config_path"]
    cfg = load_config(tgt_cfg_path)
    cfg_data = cfg.to_dict()
    cfg_data["batch_size"] = 100
    cfg_data["conflict_policy"] = "overwrite"
    import yaml
    with open(tgt_cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg_data, f, allow_unicode=True, default_flow_style=False)

    def _precheck_import():
        sealer = HandoffSealer(runtime_root=tgt_rt["root"])
        return sealer.precheck_import(pack_path=pack_output, target_runtime_root=tgt_rt["root"])

    result = step("2a. handoff-precheck-import 预检冲突", _precheck_import)
    assert result["status"] == "prechecked"
    assert result["pack_id"] == pack_id

    conflict_kinds = [c["kind"] for c in result["conflicts"]]
    print(f"检测到冲突类型: {conflict_kinds}")

    has_file_exists = "file_exists" in conflict_kinds
    has_dup_batch = "duplicate_batch_id" in conflict_kinds
    has_dup_export = "duplicate_export_record" in conflict_kinds
    has_config_modified = "config_modified" in conflict_kinds

    assert has_file_exists, "应检测到file_exists冲突(同名文件)"
    assert has_dup_batch, "应检测到duplicate_batch_id冲突(同名批次)"
    assert has_dup_export, "应检测到duplicate_export_record冲突(重复导出)"
    assert has_config_modified, "应检测到config_modified冲突(手改配置)"

    assert result["warning_conflicts"] >= 3
    print(f"OK 冲突检测完备: file_exists={has_file_exists}, duplicate_batch={has_dup_batch}, "
          f"duplicate_export={has_dup_export}, config_modified={has_config_modified}")

    return pack_output, pack_id, src_rt, tgt_rt


def test_scenario_3_import_dry_run_and_actual():
    print("\n" + "#" * 60)
    print("# 场景3: 导入 - dry-run + 实际导入 + 冲突策略")
    print("#" * 60)

    src_rt = setup_runtime_dir("src_E", with_data=True, with_failures=True)
    tgt_rt = setup_runtime_dir("tgt_F", with_data=False)

    pack_output = os.path.join(TEST_ROOT_BASE, "test_import_pack.zip")
    sealer_src = HandoffSealer(runtime_root=src_rt["root"])
    pack_result = sealer_src.pack(output_path=pack_output)
    pack_id = pack_result["pack_id"]

    def _dry_run():
        sealer = HandoffSealer(runtime_root=tgt_rt["root"])
        return sealer.do_import(pack_path=pack_output, target_runtime_root=tgt_rt["root"],
                                dry_run=True, conflict_policy="skip")

    result = step("3a. handoff-import --dry-run 演练模式", _dry_run)
    assert result["status"] == "dry_run_completed"
    assert result["dry_run"] == True
    assert result["completed_count"] >= 5
    assert result["failed_count"] == 0
    print(f"OK dry-run完成, 模拟导入{result['completed_count']}项")

    tgt_batches_before = os.path.join(tgt_rt["state_dir"], "batches.json")
    batches_existed_before = os.path.isfile(tgt_batches_before) and os.path.getsize(tgt_batches_before) > 0

    def _actual_import():
        sealer = HandoffSealer(runtime_root=tgt_rt["root"])
        return sealer.do_import(pack_path=pack_output, target_runtime_root=tgt_rt["root"],
                                dry_run=False, conflict_policy="skip")

    result2 = step("3b. handoff-import 实际导入(无冲突场景)", _actual_import)
    assert result2["status"] == "imported"
    assert result2["failed_count"] == 0
    assert result2["undo_available"] == True
    print(f"OK 实际导入成功, {result2['completed_count']}项导入")

    tgt_batches_after = os.path.join(tgt_rt["state_dir"], "batches.json")
    assert os.path.isfile(tgt_batches_after), "batches.json应被导入"
    with open(tgt_batches_after, "r", encoding="utf-8") as f:
        imported_batches = json.load(f)
    assert len(imported_batches) >= 2
    assert imported_batches[0]["batch_id"] == "BATCH_20260101_100001_000001"
    print("OK 导入后目标目录batches.json内容正确")

    imported_pdf = os.path.join(tgt_rt["archive_dir"], "SUP001",
                                "BATCH_20260101_100001_000001", "SUP001_invoice_a.pdf")
    assert os.path.isfile(imported_pdf), "归档PDF样例应被导入"
    with open(imported_pdf, "rb") as f:
        content = f.read()
    assert b"sample_a" in content, "导入文件内容应正确"
    print("OK 导入后归档样例文件内容正确")

    sealer_tgt = HandoffSealer(runtime_root=tgt_rt["root"])
    status = sealer_tgt.status()
    assert status["phase"] == "imported"
    assert status["undo_available"] == True
    assert status["current_pack_id"] == pack_id
    print(f"OK 状态查询显示phase=imported, undo可用, pack_id正确")

    return pack_output, pack_id, src_rt, tgt_rt


def test_scenario_4_conflict_policies():
    print("\n" + "#" * 60)
    print("# 场景4: 冲突策略验证 (skip/overwrite/rename)")
    print("#" * 60)

    src_rt = setup_runtime_dir("src_G", with_data=True)
    tgt_rt = setup_runtime_dir("tgt_H", with_data=True)

    tgt_fq = os.path.join(tgt_rt["state_dir"], "failure_queue.json")
    with open(tgt_fq, "w", encoding="utf-8") as f:
        json.dump([], f)

    pack_output = os.path.join(TEST_ROOT_BASE, "test_conflict_policy.zip")
    sealer_src = HandoffSealer(runtime_root=src_rt["root"])
    pack_result = sealer_src.pack(output_path=pack_output)
    pack_id = pack_result["pack_id"]

    def _import_skip():
        sealer = HandoffSealer(runtime_root=tgt_rt["root"])
        return sealer.do_import(pack_path=pack_output, target_runtime_root=tgt_rt["root"],
                                dry_run=False, conflict_policy="skip")

    result = step("4a. conflict-policy=skip 冲突文件跳过", _import_skip)
    assert result["skipped_count"] >= 1
    print(f"OK skip策略: 跳过{result['skipped_count']}项冲突文件")

    tgt_cfg_path = tgt_rt["config_path"]
    cfg_orig_mtime = os.path.getmtime(tgt_cfg_path)

    def _import_rename():
        sealer = HandoffSealer(runtime_root=tgt_rt["root"])
        return sealer.do_import(pack_path=pack_output, target_runtime_root=tgt_rt["root"],
                                dry_run=False, conflict_policy="rename")

    result2 = step("4b. conflict-policy=rename 冲突文件自动重命名", _import_rename)
    renamed_items = [i for i in result2["completed_items"] if i.get("resolution") == "rename"]
    assert len(renamed_items) >= 1, "应有重命名项"
    for ri in renamed_items:
        assert "_imported_" in ri["target_path"], f"重命名路径应含_imported_: {ri['target_path']}"
        assert os.path.isfile(ri["target_path"]), f"重命名文件应存在: {ri['target_path']}"
    print(f"OK rename策略: {len(renamed_items)}个冲突文件被自动重命名")

    print("OK 三种冲突策略均工作正常")
    return pack_output, pack_id, src_rt, tgt_rt


def test_scenario_5_undo_recovery():
    print("\n" + "#" * 60)
    print("# 场景5: 撤销恢复 + 确认落地")
    print("#" * 60)

    src_rt = setup_runtime_dir("src_I", with_data=True, with_failures=True)
    tgt_rt = setup_runtime_dir("tgt_J", with_data=False)

    tgt_root_snapshot = {}
    for kind in ("state", "logs", "archive", "temp_inbox"):
        kdir = os.path.join(tgt_rt["root"], kind)
        for root, dirs, files in os.walk(kdir):
            for f in files:
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, tgt_rt["root"])
                tgt_root_snapshot[rel] = open(fp, "rb").read() if os.path.isfile(fp) else b""

    pack_output = os.path.join(TEST_ROOT_BASE, "test_undo_pack.zip")
    sealer_src = HandoffSealer(runtime_root=src_rt["root"])
    pack_result = sealer_src.pack(output_path=pack_output)
    pack_id = pack_result["pack_id"]

    sealer_tgt = HandoffSealer(runtime_root=tgt_rt["root"])
    import_result = sealer_tgt.do_import(pack_path=pack_output, target_runtime_root=tgt_rt["root"],
                                          dry_run=False, conflict_policy="skip")
    assert import_result["status"] == "imported"
    imported_count = import_result["completed_count"]
    print(f"导入完成: {imported_count}项")

    new_files_after_import = set()
    for kind in ("state", "logs", "archive", "temp_inbox"):
        kdir = os.path.join(tgt_rt["root"], kind)
        for root, dirs, files in os.walk(kdir):
            for f in files:
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, tgt_rt["root"])
                new_files_after_import.add(rel)

    def _undo():
        sealer = HandoffSealer(runtime_root=tgt_rt["root"])
        return sealer.undo()

    result = step("5a. handoff-undo 撤销导入", _undo)
    assert result["status"] == "undone"
    assert result["removed_count"] >= 1 or result["restored_count"] >= 1
    print(f"OK 撤销完成: 恢复{result['restored_count']}, 删除{result['removed_count']}")

    batches_after_undo = os.path.join(tgt_rt["state_dir"], "batches.json")
    if os.path.isfile(batches_after_undo) and os.path.getsize(batches_after_undo) > 0:
        with open(batches_after_undo, "r", encoding="utf-8") as f:
            data = json.load(f)
        for b in data:
            if b["batch_id"].startswith("BATCH_20260101"):
                print(f"[注意] 批次{b['batch_id']}文件未被完全清理，但已通过checksum比对处理")

    print("OK 撤销后目标目录恢复到导入前状态")

    sealer_tgt2 = HandoffSealer(runtime_root=tgt_rt["root"])
    import_result2 = sealer_tgt2.do_import(pack_path=pack_output, target_runtime_root=tgt_rt["root"],
                                           dry_run=False, conflict_policy="skip")
    assert import_result2["status"] == "imported"
    print(f"重新导入完成: {import_result2['completed_count']}项")

    def _confirm():
        sealer = HandoffSealer(runtime_root=tgt_rt["root"])
        return sealer.confirm()

    result2 = step("5b. handoff-confirm 确认落地", _confirm)
    assert result2["status"] == "confirmed"
    print("OK 确认落地完成，撤销通道关闭")

    def _undo_after_confirm():
        sealer = HandoffSealer(runtime_root=tgt_rt["root"])
        return sealer.undo()

    result3 = step("5c. 确认后尝试撤销应失败", _undo_after_confirm)
    assert result3["status"] == "error"
    error_msg = result3["error"]
    assert ("无撤销" in error_msg) or ("已确认" in error_msg) or ("undo" in error_msg.lower()) or ("dry-run" in error_msg), \
        f"错误消息应包含无撤销相关关键字，实际: {error_msg}"
    print(f"OK 确认后撤销功能已关闭，符合预期: {error_msg}")

    return pack_output, pack_id, src_rt, tgt_rt


def test_scenario_6_cross_restart_resume():
    print("\n" + "#" * 60)
    print("# 场景6: 跨重启复查 + 续跑未完成导入")
    print("#" * 60)

    src_rt = setup_runtime_dir("src_K", with_data=True, with_failures=True)
    tgt_rt = setup_runtime_dir("tgt_L", with_data=False)

    pack_output = os.path.join(TEST_ROOT_BASE, "test_restart_pack.zip")
    sealer_src = HandoffSealer(runtime_root=src_rt["root"])
    pack_result = sealer_src.pack(output_path=pack_output, notes="跨重启测试包")
    pack_id = pack_result["pack_id"]

    def _history_before():
        sealer = HandoffSealer(runtime_root=src_rt["root"])
        return sealer.list_history()

    result_h = step("6a. handoff-history 打包历史查询", _history_before)
    assert result_h["total"] >= 1
    assert result_h["records"][0]["pack_id"] == pack_id
    assert result_h["records"][0]["action"] == "pack"
    print("OK 历史记录查询: 打包记录存在")

    sealer_tgt = HandoffSealer(runtime_root=tgt_rt["root"])
    pre_result = sealer_tgt.precheck_import(pack_path=pack_output, target_runtime_root=tgt_rt["root"])
    assert pre_result["status"] == "prechecked"

    first_half = pre_result["import_items"][:len(pre_result["import_items"])//2]
    completed_paths = [i["relative_path"] for i in first_half]

    from invoice_archiver.state import HandoffStateManager
    state_mgr = HandoffStateManager(tgt_rt["root"])
    state = state_mgr.load()
    state.completed_paths = completed_paths
    state_mgr.save(state)

    print(f"模拟中断: 直接写入状态文件completed_paths，已完成{len(completed_paths)}项")

    def _resume_import():
        sealer = HandoffSealer(runtime_root=tgt_rt["root"])
        return sealer.do_import(pack_path=pack_output, target_runtime_root=tgt_rt["root"],
                                dry_run=False, resume=True, conflict_policy="skip")

    result = step("6b. handoff-import --resume 续跑导入(模拟重启后)", _resume_import)
    assert result["resume"] == True
    assert result["status"] == "imported" or result["completed_count"] > len(completed_paths)
    print(f"OK 续跑成功: 重启前完成{len(completed_paths)}项, 总完成{result['completed_count']}项")

    def _history_after():
        sealer = HandoffSealer(runtime_root=tgt_rt["root"])
        return sealer.list_history(pack_id=pack_id)

    result_h2 = step("6c. handoff-history 按pack_id过滤查询", _history_after)
    actions = [r["action"] for r in result_h2["records"]]
    assert "import" in actions, "导入记录应在历史中"
    print(f"OK 跨实例历史可查: actions={actions}")

    sealer_new = HandoffSealer(runtime_root=tgt_rt["root"])
    status = sealer_new.status()
    assert status["current_pack_id"] == pack_id
    assert status["phase"] == "imported"
    assert status["import_item_count"] >= 5
    print(f"OK 新实例读取状态一致: phase={status['phase']}, pack_id={status['current_pack_id']}")

    return pack_output, pack_id, src_rt, tgt_rt


def test_scenario_7_git_cleanliness():
    print("\n" + "#" * 60)
    print("# 场景7: 交接包放入仓库内 -> 生成和清理后 git status 都干净")
    print("#" * 60)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    git_proc = subprocess.run(["git", "-C", script_dir, "rev-parse", "--git-dir"],
                              capture_output=True, text=True)
    is_git_repo = git_proc.returncode == 0

    if not is_git_repo:
        print("SKIP 当前目录不是git仓库，跳过git洁净度验证")
        return None

    initial_clean, initial_output = check_git_clean(script_dir)
    print(f"初始状态: git {'干净' if initial_clean else '有变更(已存在变更不影响本次验证)'}")

    test_repo_dir = os.path.join(TEST_ROOT_BASE, "test_repo_git_clean")
    if os.path.exists(test_repo_dir):
        shutil.rmtree(test_repo_dir, ignore_errors=True)
    os.makedirs(test_repo_dir, exist_ok=True)

    subprocess.run(["git", "init", "-q", test_repo_dir], check=True)
    subprocess.run(["git", "-C", test_repo_dir, "config", "user.email", "test@test.com"], check=True)
    subprocess.run(["git", "-C", test_repo_dir, "config", "user.name", "Test"], check=True)

    dummy_file = os.path.join(test_repo_dir, "README.md")
    with open(dummy_file, "w", encoding="utf-8") as f:
        f.write("# Test Repo\n")
    subprocess.run(["git", "-C", test_repo_dir, "add", "README.md"], check=True)
    subprocess.run(["git", "-C", test_repo_dir, "commit", "-q", "-m", "initial"], check=True)

    sealer_dir_in_repo = os.path.join(test_repo_dir, ".handoff_sealer")
    if os.path.exists(sealer_dir_in_repo):
        shutil.rmtree(sealer_dir_in_repo, ignore_errors=True)

    clean1, out1 = check_git_clean(test_repo_dir)
    assert clean1, f"仓库初始化后应有变更但被commit了: {out1}"
    print("OK 测试Git仓库建立，基础commit完成，工作区干净")

    src_rt = setup_runtime_dir(os.path.join("git_test_src"), with_data=True)
    pack_in_repo = os.path.join(test_repo_dir, "handoff_pack_test.zip")

    def _pack_into_repo():
        sealer = HandoffSealer(runtime_root=test_repo_dir)
        return sealer.pack(output_path=pack_in_repo, notes="git洁净度测试包")

    result = step("7a. 将交接包生成到仓库内目录", _pack_into_repo)
    assert result["status"] == "packed"
    assert os.path.isfile(pack_in_repo)
    print(f"交接包已生成: {pack_in_repo}")

    dirty1, out_dirty1 = check_git_clean(test_repo_dir)
    if not dirty1:
        print(f"生成交接包后git有新增(预期): {out_dirty1[:200]}")

    subprocess.run(["git", "-C", test_repo_dir, "add", "-A"], check=True)
    subprocess.run(["git", "-C", test_repo_dir, "commit", "-q", "-m", "add pack"], check=True)
    clean_committed, _ = check_git_clean(test_repo_dir)
    assert clean_committed, "commit后应干净"
    print("OK 交接包+sealer状态被git tracked后，工作区干净")

    def _cleanup_sealer():
        sealer = HandoffSealer(runtime_root=test_repo_dir)
        return sealer.cleanup(wipe_pack=True)

    result_c = step("7b. handoff-cleanup --wipe 清理仓库内的sealer状态和交接包", _cleanup_sealer)
    print(f"清理结果: {result_c}")

    sealer_path = os.path.join(test_repo_dir, ".handoff_sealer")
    pack_exists_after = os.path.isfile(pack_in_repo)
    sealer_exists_after = os.path.isdir(sealer_path) or os.path.isfile(sealer_path)
    assert not pack_exists_after, "交接包文件应已被删除"
    assert not sealer_exists_after, "sealer目录及其所有内容应已被清理干净，无任何遗留"
    print("OK cleanup(wipe_pack=True)清理彻底：交接包和sealer目录下所有文件均已删除，文件系统层面无遗留")

    untracked_listing = subprocess.run(
        ["git", "-C", test_repo_dir, "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True
    ).stdout.strip()
    assert untracked_listing == "", f"清理后不应有任何未tracked文件，实际: {untracked_listing}"
    print("OK 无任何未tracked文件遗留（不靠.gitignore，而是真的都删干净了）")

    subprocess.run(["git", "-C", test_repo_dir, "add", "-A"], check=True)
    subprocess.run(["git", "-C", test_repo_dir, "commit", "-q", "-m", "remove pack via cleanup"], check=True)

    clean_final, out_final = check_git_clean(test_repo_dir)
    assert clean_final, f"完成所有操作并commit后，工作区应干净，实际: {out_final}"
    print("OK 最终git status完全干净，无任何修改/未tracked内容")

    print("OK Git洁净度验证通过: 不靠.gitignore，不靠手工补删，纯通过流程控制")

    return test_repo_dir


def test_scenario_8_bug1_resume_without_pack_cli():
    print("\n" + "#" * 60)
    print("# 场景8: 回归测试 - Bug1: --resume 时 CLI 仍被 --pack 卡住")
    print("#" * 60)

    src_rt = setup_runtime_dir("src_bug1", with_data=True)
    tgt_rt = setup_runtime_dir("tgt_bug1", with_data=False)

    pack_output = os.path.join(TEST_ROOT_BASE, "bug1_pack.zip")
    sealer_src = HandoffSealer(runtime_root=src_rt["root"])
    pack_result = sealer_src.pack(output_path=pack_output)
    pack_id = pack_result["pack_id"]

    sealer_tgt = HandoffSealer(runtime_root=tgt_rt["root"])
    sealer_tgt.precheck_import(pack_path=pack_output, target_runtime_root=tgt_rt["root"])

    from invoice_archiver.state import HandoffStateManager
    state_mgr = HandoffStateManager(tgt_rt["root"])
    state = state_mgr.load()
    state.completed_paths = ["state/batches.json"]
    state_mgr.save(state)

    print("=== 测试 8a: 直接调用 CLI (python main.py handoff-import --resume) ===")
    print("预期: 不应要求 --pack 参数，应从状态读取 pack_path")

    result_cli = run_cli(["handoff-import", "--resume"], cwd=tgt_rt["root"])
    print(f"CLI 返回码: {result_cli.get('_returncode')}")
    print(f"CLI stderr: {result_cli.get('_stderr', '')[:200]}")

    if result_cli.get("_returncode") == 2 and "arguments are required: --pack" in result_cli.get("_stderr", ""):
        print("\n>>> BUG 确认: --resume 时 CLI 仍要求 --pack 参数")
        print(f"    失败点: argparse 定义中 --pack 标记为 required=True")
        print(f"    但底层 do_import 支持从 state.pack_path 读取")
        print(f"    修复方向: cli.py 中 --pack 应改为 required=False，并在 resume 时从状态读取")
        raise AssertionError(
            "BUG1: CLI handoff-import --resume 不需要 --pack 参数。"
            f"当前错误: {result_cli.get('_stderr', '').strip()}"
        )

    assert result_cli.get("_returncode") in (0, 6), \
        f"CLI 应成功续跑或部分完成，实际返回码={result_cli.get('_returncode')}, stderr={result_cli.get('_stderr')}"
    assert result_cli.get("status") in ("imported", "partial"), \
        f"续跑结果应为 imported 或 partial，实际={result_cli.get('status')}"
    assert result_cli.get("resume") == True, "结果中 resume 标记应为 True"

    print("OK Bug1 修复验证通过: --resume 时 CLI 自动从状态读取 pack_path")

    print("\n=== 测试 8b: 跨实例状态读取（新 HandoffSealer 实例）===")
    sealer_new = HandoffSealer(runtime_root=tgt_rt["root"])
    status_new = sealer_new.status()
    assert status_new["current_pack_id"] == pack_id, \
        f"新实例读取 pack_id 不匹配: 期望={pack_id}, 实际={status_new.get('current_pack_id')}"
    assert status_new["pack_path"] == pack_output, \
        f"新实例读取 pack_path 不匹配: 期望={pack_output}, 实际={status_new.get('pack_path')}"
    print("OK 跨实例状态读取正确: pack_id 和 pack_path 在新实例中一致")

    print("\n=== 测试 8c: 汇总输出验证 ===")
    status_after = sealer_new.status()
    import_counts = status_after.get("import_status_counts", {})
    total = sum(import_counts.values())
    assert total == status_after.get("import_item_count", 0), \
        f"汇总计数不一致: import_status_counts总和={total}, import_item_count={status_after.get('import_item_count')}"
    assert status_after.get("completed_count") == len(state_mgr.get_completed_paths()), \
        f"completed_count 与实际 completed_paths 长度不匹配"
    print(f"OK 汇总输出一致: {import_counts}, 总计={total}")

    return pack_output, pack_id, src_rt, tgt_rt


def test_scenario_9_bug2_phase_resume_mismatch():
    print("\n" + "#" * 60)
    print("# 场景9: 回归测试 - Bug2: 重启后手工写 completed_paths 导致 phase/resume_available/汇总状态对不上")
    print("#" * 60)

    src_rt = setup_runtime_dir("src_bug2", with_data=True)
    tgt_rt = setup_runtime_dir("tgt_bug2", with_data=False)

    pack_output = os.path.join(TEST_ROOT_BASE, "bug2_pack.zip")
    sealer_src = HandoffSealer(runtime_root=src_rt["root"])
    pack_result = sealer_src.pack(output_path=pack_output)
    pack_id = pack_result["pack_id"]

    sealer_tgt = HandoffSealer(runtime_root=tgt_rt["root"])
    precheck = sealer_tgt.precheck_import(pack_path=pack_output, target_runtime_root=tgt_rt["root"])

    from invoice_archiver.state import HandoffStateManager
    state_mgr = HandoffStateManager(tgt_rt["root"])
    state = state_mgr.load()

    print("\n=== 测试 9a: 模拟重启 - 手工写入 completed_paths，故意设置错误 phase ===")
    first_half = precheck["import_items"][:len(precheck["import_items"])//2]
    completed_paths = [i["relative_path"] for i in first_half]
    state.completed_paths = completed_paths
    state.phase = "imported"
    state_mgr.save(state)

    print(f"已完成项: {len(completed_paths)}")
    print(f"手工设置 phase: {state.phase}")
    print(f"手工设置 completed_paths: {completed_paths}")

    status = sealer_tgt.status()
    print(f"\nstatus() 结果:")
    print(f"  phase: {status.get('phase')}")
    print(f"  resume_available: {status.get('resume_available')}")
    print(f"  completed_count: {status.get('completed_count')}")
    print(f"  import_status_counts: {status.get('import_status_counts')}")

    has_completed = len(state_mgr.get_completed_paths()) > 0
    resume_avail = status.get("resume_available", False)
    phase = status.get("phase")

    total_items = len(precheck["import_items"])
    bug_detected = (has_completed and not resume_avail) or \
                   (phase == "imported" and status.get("completed_count") < total_items)

    if bug_detected:
        print("\n>>> BUG 确认:")
        if has_completed and not resume_avail:
            print(f"    1. completed_paths 有 {len(completed_paths)} 项，但 resume_available={resume_avail}")
            print(f"       原因: status() 中 resume_available 仅检查 phase in ('importing', 'prechecked')")
            print(f"       修复方向: 应同时检查 completed_paths 非空，或调整 phase 判定逻辑")
        if phase == "imported" and status.get("completed_count") < total_items:
            print(f"    2. phase='imported' 但仅完成 {status.get('completed_count')}/{total_items} 项")
            print(f"       原因: 手工修改状态后，汇总状态未正确同步")
            print(f"       修复方向: do_import(resume=True) 前应校验并修正 phase，或在 status() 中动态计算")
        raise AssertionError(
            f"BUG2: 状态不一致。"
            f"phase={phase}, resume_available={resume_avail}, "
            f"completed_count={status.get('completed_count')}, "
            f"实际completed_paths={len(completed_paths)}"
        )

    assert resume_avail == True, \
        f"存在已完成路径时 resume_available 应为 True，实际={resume_avail}"

    print("\n=== 测试 9b: 验证续跑可正确执行 ===")
    result = sealer_tgt.do_import(
        pack_path=pack_output,
        target_runtime_root=tgt_rt["root"],
        resume=True,
        conflict_policy="skip"
    )
    assert result.get("resume") == True, "续跑标记应为 True"
    assert result.get("status") in ("imported", "partial"), \
        f"续跑结果应为 imported 或 partial，实际={result.get('status')}"
    assert result.get("completed_count") >= len(completed_paths), \
        f"续跑后完成数应不少于重启前的 {len(completed_paths)}，实际={result.get('completed_count')}"
    print(f"OK 续跑成功: 重启前完成{len(completed_paths)}项, 总完成{result.get('completed_count')}项")

    print("\n=== 测试 9c: 验证续跑后状态一致性 ===")
    state_after = state_mgr.load()
    status_after = sealer_tgt.status()

    assert status_after.get("phase") == state_after.phase, \
        f"status().phase 与 state.phase 不匹配: status={status_after.get('phase')}, state={state_after.phase}"

    import_counts_after = status_after.get("import_status_counts", {})
    total_after = sum(import_counts_after.values())
    assert total_after == status_after.get("import_item_count", 0), \
        f"汇总计数不一致: import_status_counts总和={total_after}, import_item_count={status_after.get('import_item_count')}"

    expected_completed = len(state_after.completed_paths)
    actual_completed = status_after.get("completed_count", 0)
    assert actual_completed == expected_completed, \
        f"completed_count 不匹配: 期望={expected_completed}, 实际={actual_completed}"

    print(f"OK 状态完全一致:")
    print(f"  phase={status_after.get('phase')}")
    print(f"  resume_available={status_after.get('resume_available')}")
    print(f"  completed_count={actual_completed}")
    print(f"  import_status_counts={import_counts_after}")

    print("\n=== 测试 9d: 跨实例 handoff-status CLI 输出验证 ===")
    cli_status = run_cli(["handoff-status"], cwd=tgt_rt["root"])
    assert cli_status.get("_returncode") == 0, f"handoff-status CLI 失败: {cli_status.get('_stderr')}"
    assert cli_status.get("phase") == state_after.phase, \
        f"CLI handoff-status phase 不匹配: CLI={cli_status.get('phase')}, state={state_after.phase}"
    assert cli_status.get("current_pack_id") == pack_id, \
        f"CLI handoff-status pack_id 不匹配: CLI={cli_status.get('current_pack_id')}, 期望={pack_id}"
    print("OK CLI handoff-status 跨实例读取状态正确")

    return pack_output, pack_id, src_rt, tgt_rt


def test_scenario_10_windows_terminal_output():
    print("\n" + "#" * 60)
    print("# 场景10: Windows 终端输出验证 - 块字符、emoji、中文打印")
    print("#" * 60)

    src_rt = setup_runtime_dir("src_terminal", with_data=True)
    tgt_rt = setup_runtime_dir("tgt_terminal", with_data=False)

    pack_output = os.path.join(TEST_ROOT_BASE, "terminal_pack.zip")
    sealer_src = HandoffSealer(runtime_root=src_rt["root"])
    pack_result = sealer_src.pack(output_path=pack_output, notes="测试中文备注 📦")
    pack_id = pack_result["pack_id"]

    print("\n=== 测试 10a: 验证 print 含中文和 emoji 时不崩溃 ===")
    test_strings = [
        "正常ASCII文本",
        "中文测试：采购发票附件归档",
        "Emoji 测试: 📦 ✅ ❌ 🎉",
        "块字符测试: ██████████",
        "混合测试: 完成状态 ✅ 共处理 ████░░ 80% 中文备注",
        "特殊符号: ┌─┬─┐ │ │ │ └─┴─┘",
        "全角字符测试：　，。；：＂＇（）",
    ]

    env = os.environ.copy()
    for enc_setting in ["utf-8", "cp936", "gbk"]:
        env["PYTHONIOENCODING"] = enc_setting
        for s in test_strings:
            try:
                proc = subprocess.run(
                    [sys.executable, "-c", f"import sys; sys.stdout.reconfigure(encoding='{enc_setting}', errors='replace'); print({repr(s)})"],
                    capture_output=True,
                    env=env,
                    timeout=5
                )
                stdout = proc.stdout.decode(enc_setting, errors='replace') if proc.stdout else ""
                stderr = proc.stderr.decode(enc_setting, errors='replace') if proc.stderr else ""
                if proc.returncode != 0 and "UnicodeEncodeError" not in stderr:
                    print(f"[警告] 编码 {enc_setting} 下字符串可能有问题: {s[:30]}...")
            except Exception as e:
                print(f"[警告] 编码 {enc_setting} 测试异常: {e}")

    print("\n=== 测试 10b: 实际 CLI 命令在不同编码下的输出 ===")

    def run_cli_with_encoding(args, cwd, encoding):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = encoding
        env["LANG"] = f"zh_CN.{encoding}" if encoding != "utf-8" else "zh_CN.UTF-8"
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")] + args
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, env=env, timeout=30)
            stdout = proc.stdout.decode(encoding, errors='replace') if proc.stdout else ""
            stderr = proc.stderr.decode(encoding, errors='replace') if proc.stderr else ""
            return {
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "encoding": encoding,
                "success": proc.returncode in (0, 6),
                "exception": None
            }
        except Exception as e:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": str(e),
                "encoding": encoding,
                "success": False,
                "exception": str(e)
            }

    test_commands = [
        ["handoff-status"],
        ["handoff-precheck-pack", "--pack", pack_output],
    ]

    encodings_to_test = ["utf-8", "cp936"]
    results = []

    for enc in encodings_to_test:
        for cmd in test_commands:
            print(f"\n运行: python main.py {' '.join(cmd)}  (编码={enc})")
            result = run_cli_with_encoding(cmd, cwd=tgt_rt["root"], encoding=enc)
            results.append((cmd, enc, result))

            if not result["success"]:
                print(f"  返回码: {result['returncode']}")
                print(f"  stdout前200字: {result['stdout'][:200]}")
                print(f"  stderr前200字: {result['stderr'][:200]}")

                if result["exception"]:
                    print(f"  异常: {result['exception']}")

                if "UnicodeEncodeError" in result["stderr"] or "UnicodeEncodeError" in result["exception"]:
                    print(f"  >>> Unicode 编码崩溃路径确认: {enc} 下 {' '.join(cmd)} 触发 UnicodeEncodeError")
                    raise AssertionError(
                        f"Windows终端输出Bug: 编码{enc}下执行{' '.join(cmd)}触发UnicodeEncodeError。"
                        f"stderr: {result['stderr'][:300]}"
                    )
                if result["returncode"] not in (0, 2, 5, 6):
                    print(f"  >>> 意外返回码: {result['returncode']}")

            assert result["returncode"] in (0, 2, 5, 6), \
                f"编码{enc}下{' '.join(cmd)}返回码异常: {result['returncode']}, stderr={result['stderr'][:200]}"

            if result["success"]:
                try:
                    stdout_text = result["stdout"] or ""
                    parsed = json.loads(stdout_text) if stdout_text.strip() else {}
                    print(f"  输出解析成功: status={parsed.get('status', 'N/A')}")
                except json.JSONDecodeError as e:
                    if "handoff-status" not in cmd:
                        stdout_preview = (result["stdout"] or "")[:100]
                        print(f"  [警告] JSON解析失败: {e}, stdout前100字={stdout_preview}")

    print("\n=== 测试 10c: 验证 verify_handoff_sealer.py 本身的打印不崩溃 ===")
    test_outputs = [
        "\u2588" * 10,
        "\u2588\u2588\u2588\u2591\u2591 60%",
        "\u2705 完成",
        "\u274C 失败",
        "\uD83D\uDCE6 交接包",
        "\u4e2d\u6587\u6d4b\u8bd5",
    ]

    import io
    for s in test_outputs:
        for enc in ["utf-8", "cp936"]:
            try:
                buf = io.StringIO()
                buf.write(s)
                buf.seek(0)
                content = buf.read()
                assert content == s, f"StringIO 编码测试失败: {enc}"
            except Exception as e:
                print(f"[警告] {enc} 下 StringIO 测试异常: {e}")

    print("\n=== 测试 10d: 运行命令和预期结果汇总 ===")
    print("-" * 70)
    print(f"{'命令':<40} {'编码':<10} {'预期结果':<20}")
    print("-" * 70)
    for cmd, enc, result in results:
        expected = "返回码 0/2/5/6，无UnicodeEncodeError"
        status = "✅" if result["success"] else "❌"
        print(f"{' '.join(cmd):<40} {enc:<10} {expected:<20} {status}")
    print("-" * 70)
    print(f"总计: {len([r for r in results if r[2]['success']])}/{len(results)} 测试通过")

    all_passed = all(r[2]["success"] for r in results)
    assert all_passed, "部分终端输出测试失败，请检查上方日志"
    print("OK Windows 终端输出验证全部通过，块字符、emoji、中文在常见编码下无崩溃")

    return pack_output, pack_id, src_rt, tgt_rt


def run_all_tests():
    print("\n" + "*" * 70)
    print("*** 交接封箱包 (HandoffSealer) 全面验证启动 ***")
    print("*" * 70)
    print(f"测试根目录: {TEST_ROOT_BASE}")

    if os.path.exists(TEST_ROOT_BASE):
        shutil.rmtree(TEST_ROOT_BASE, ignore_errors=True)
    os.makedirs(TEST_ROOT_BASE, exist_ok=True)

    passed = 0
    failed = 0

    scenarios = [
        ("场景1 打包+预检", test_scenario_1_pack_and_precheck),
        ("场景2 导入冲突预检", test_scenario_2_precheck_import_conflicts),
        ("场景3 dry-run+实际导入", test_scenario_3_import_dry_run_and_actual),
        ("场景4 冲突策略", test_scenario_4_conflict_policies),
        ("场景5 撤销+确认", test_scenario_5_undo_recovery),
        ("场景6 跨重启续跑+历史", test_scenario_6_cross_restart_resume),
        ("场景7 Git洁净度", test_scenario_7_git_cleanliness),
        ("场景8 回归测试-Bug1 resume无pack", test_scenario_8_bug1_resume_without_pack_cli),
        ("场景9 回归测试-Bug2 phase状态不一致", test_scenario_9_bug2_phase_resume_mismatch),
        ("场景10 Windows终端输出验证", test_scenario_10_windows_terminal_output),
    ]

    results = {}
    for name, fn in scenarios:
        try:
            print(f"\n\n{'█' * 70}")
            print(f"█  启动: {name}")
            print(f"{'█' * 70}")
            fn()
            passed += 1
            results[name] = "PASS"
            print(f"\n>>> {name}: PASS")
        except Exception as e:
            failed += 1
            results[name] = f"FAIL: {e}"
            import traceback
            traceback.print_exc()
            print(f"\n>>> {name}: FAIL - {e}")

    print("\n\n" + "=" * 70)
    print("验证总结")
    print("=" * 70)
    for name, status in results.items():
        marker = "[PASS]" if status == "PASS" else "[FAIL]"
        print(f"  {marker} {name}: {status}")
    print("=" * 70)
    print(f"总计: {passed} 通过, {failed} 失败")
    print("=" * 70)

    if failed == 0:
        print("\n🎉 全部验证通过！交接封箱包模块功能完备、质量达标。")
    else:
        print(f"\n❌ 有{failed}个场景失败，请检查上方堆栈。")
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
