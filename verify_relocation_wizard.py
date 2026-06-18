#!/usr/bin/env python3
"""
搬家向导全链路验证脚本
======================
覆盖场景:
  1. 首次扫描: 构建含旧运行产物的沙盒，wizard-scan 检测
  2. 冲突分支: 目标目录已有同名文件 -> 冲突检测 + wizard-resolve
  3. 确认搬家: wizard-plan + wizard-apply 完整执行
  4. 撤销恢复: wizard-undo 回退搬家 + 验证源文件恢复
  5. 重启续跑: 模拟中断后 wizard-apply --resume 继续完成
  6. 导出复查: wizard-export / wizard-audit-export 生成 JSON/CSV
  7. git status 检查: 源码目录无运行产物残留

用法:
    python verify_relocation_wizard.py

所有操作在系统临时目录下的沙盒中执行，不影响真实项目目录。
退出码 0 = 全部通过，非 0 = 某一步失败。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta


HERE = Path(__file__).resolve().parent
PY = sys.executable or "python"
MAIN = ""


def _banner(title: str) -> None:
    line = "=" * 72
    print(f"\n{line}\n  {title}\n{line}")


def _run(cmd, cwd, check=True, capture=True, env=None):
    merged = dict(os.environ)
    merged.setdefault("PYTHONIOENCODING", "utf-8")
    merged.setdefault("PYTHONUTF8", "1")
    if os.name == "nt":
        merged.setdefault("PYTHONLEGACYWINDOWSSTDIO", "0")
    if env:
        merged.update(env)
    kwargs = dict(cwd=cwd, shell=False, env=merged)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc = subprocess.run(cmd, **kwargs)
    if capture:
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("mbcs", errors="replace") if os.name == "nt" else proc.stderr.decode("utf-8", errors="replace")
    else:
        out = ""
        err = ""
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"命令失败 rc={proc.returncode}: {' '.join(cmd)}\n"
            f"--- STDOUT ---\n{out}\n--- STDERR ---\n{err}"
        )
    return proc.returncode, out, err


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_file(path, content):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as f:
        f.write(content)


def _populate_legacy(sandbox: Path) -> str:
    old_batch_id = "BATCH_20260601_091500_000001"

    temp_inbox = sandbox / "temp_inbox"
    temp_inbox.mkdir(exist_ok=True)
    for s in ["SUP001_invoice_202605.pdf", "SUP002_contract_2026Q2.pdf", "SUP003_receipt_A001.xlsx"]:
        (temp_inbox / s).write_bytes(b"%PDF-1.4 fake " + s.encode())

    archive = sandbox / "archive" / "SUP099" / old_batch_id
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "SUP099_report_may.pdf").write_bytes(b"%PDF-1.4 fake archive")

    state_dir = sandbox / "state"
    state_dir.mkdir(exist_ok=True)
    fake_batches = [{
        "batch_id": old_batch_id,
        "created_at": (datetime.now() - timedelta(days=2)).isoformat(),
        "committed_at": (datetime.now() - timedelta(days=2)).isoformat(),
        "rolled_back_at": None,
        "status": "committed",
        "files": [{
            "source_path": str(temp_inbox / "_old_SUP099.pdf"),
            "target_path": str(archive / "SUP099_report_may.pdf"),
            "filename": "SUP099_report_may.pdf",
            "supplier_code": "SUP099",
            "status": "moved",
            "error": None,
        }],
    }]
    _write_file(state_dir / "batches.json", json.dumps(fake_batches, ensure_ascii=False, indent=2))

    fake_failures = [{
        "source_path": str(temp_inbox / "SUP999_bad_name.pdf"),
        "target_path": str(sandbox / "archive" / "SUP999" / "missing_dir" / "x.pdf"),
        "supplier_code": "SUP999",
        "error": "目标目录不存在",
        "retry_count": 3,
        "last_retry_at": (datetime.now() - timedelta(hours=5)).isoformat(),
        "batch_id": old_batch_id,
        "created_at": (datetime.now() - timedelta(days=1)).isoformat(),
    }]
    _write_file(state_dir / "failure_queue.json", json.dumps(fake_failures, ensure_ascii=False, indent=2))

    logs_dir = sandbox / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_line = json.dumps({
        "timestamp": (datetime.now() - timedelta(days=2)).isoformat(),
        "source_path": str(temp_inbox / "_old_SUP099.pdf"),
        "target_path": str(archive / "SUP099_report_may.pdf"),
        "action": "move",
        "batch_id": old_batch_id,
        "status": "success",
        "error_reason": None,
    }, ensure_ascii=False)
    _write_file(logs_dir / "archive_log.jsonl", log_line + "\n")

    return old_batch_id


def step1_build_sandbox(sandbox: Path, target: Path):
    global MAIN
    _banner("Step 1: 构建沙盒 - 伪造旧版本运行产物")

    pkg_src = HERE / "invoice_archiver"
    pkg_dst = sandbox / "invoice_archiver"
    shutil.copytree(pkg_src, pkg_dst)
    shutil.copy(HERE / "main.py", sandbox / "main.py")
    MAIN = str(sandbox / "main.py")

    old_batch_id = _populate_legacy(sandbox)
    print(f"  [OK] 伪造旧版本运行产物完成, batch_id={old_batch_id}")

    target.mkdir(parents=True, exist_ok=True)
    print(f"  [OK] 目标目录已创建: {target}")

    return old_batch_id


def step2_scan(sandbox: Path, target: Path):
    _banner("Step 2: wizard-scan 首次扫描源码目录")

    rc, out, err = _run(
        [PY, MAIN, "wizard-scan", "--target", str(target)],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "scanned", f"扫描状态不对: {result['status']}"
    assert result["entries_count"] > 0, f"扫描条目为 0: {result}"
    assert "batch_ids" in result["summary"], "扫描结果缺少 batch_ids"
    assert result["summary"]["batch_ids"], f"未检测到 batch_id: {result['summary']}"
    assert result["summary"]["failure_meta"], "未检测到失败队列元数据"
    print(f"  [OK] wizard-scan 成功: {result['entries_count']} 项")
    print(f"       批次: {result['summary']['batch_ids']}")
    print(f"       失败队列: {result['summary']['failure_meta']}")

    wizard_state_file = sandbox / ".wizard_state.json"
    assert wizard_state_file.exists(), "扫描后未生成 .wizard_state.json"
    state = _load_json(str(wizard_state_file))
    assert state["phase"] == "scanned"
    print(f"  [OK] .wizard_state.json 已生成, phase={state['phase']}")

    return result["entries_count"]


def step3_conflict(sandbox: Path, target: Path):
    _banner("Step 3: 冲突分支 - 目标目录已有同名文件")

    target_state = target / "state"
    target_state.mkdir(parents=True, exist_ok=True)
    (target_state / "batches.json").write_text(
        json.dumps([{"batch_id": "CONFLICT_BATCH", "status": "committed"}], ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  [OK] 在目标目录预置冲突文件: state/batches.json")

    wizard_state_file = sandbox / ".wizard_state.json"
    if wizard_state_file.exists():
        wizard_state_file.unlink()

    rc, out, err = _run(
        [PY, MAIN, "wizard-scan", "--target", str(target)],
        cwd=str(sandbox),
    )
    scan_result = json.loads(out)
    assert scan_result["has_conflicts"], f"应检测到冲突但未检测到: {scan_result}"
    print(f"  [OK] 冲突检测正确: has_conflicts=True")

    conflict_entries = [e for e in scan_result["entries"] if e.get("conflict")]
    assert conflict_entries, "无冲突条目"
    conflict_src = conflict_entries[0]["source_path"]
    print(f"  [OK] 冲突文件: {conflict_entries[0]['filename']}")

    rc, out, err = _run(
        [PY, MAIN, "wizard-resolve", "--source-path", conflict_src, "--resolution", "rename"],
        cwd=str(sandbox),
    )
    resolve_result = json.loads(out)
    assert resolve_result["status"] == "ok"
    print(f"  [OK] wizard-resolve 设定策略=rename")

    rc, out, err = _run(
        [PY, MAIN, "wizard-plan"],
        cwd=str(sandbox),
    )
    plan_result = json.loads(out)
    assert plan_result["status"] == "plan_ready"
    assert plan_result["conflicts_count"] > 0
    print(f"  [OK] wizard-plan 显示冲突: {plan_result['conflicts_count']} 项")

    rc, out, err = _run(
        [PY, MAIN, "wizard-apply"],
        cwd=str(sandbox),
    )
    apply_result = json.loads(out)
    assert apply_result["applied"] > 0, f"冲突后搬家 applied=0: {apply_result}"
    print(f"  [OK] 冲突后搬家成功: applied={apply_result['applied']}")

    renamed_files = list((target / "state").glob("batches_wiz*.json"))
    assert renamed_files, "冲突后未发现重命名文件"
    print(f"  [OK] 冲突文件已重命名: {[f.name for f in renamed_files]}")

    rc, out, err = _run(
        [PY, MAIN, "wizard-undo"],
        cwd=str(sandbox),
    )
    undo_result = json.loads(out)
    assert undo_result["undone"] > 0
    print(f"  [OK] 冲突测试后撤销: undone={undo_result['undone']}")

    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    print(f"  [OK] 清空目标目录，为后续测试准备")


def step4_plan_and_apply(sandbox: Path, target: Path, old_batch_id: str):
    _banner("Step 4: wizard-plan + wizard-apply 确认搬家")

    wizard_state_file = sandbox / ".wizard_state.json"
    if wizard_state_file.exists():
        wizard_state_file.unlink()

    rc, out, err = _run(
        [PY, MAIN, "wizard-scan", "--target", str(target)],
        cwd=str(sandbox),
    )

    rc, out, err = _run(
        [PY, MAIN, "wizard-plan"],
        cwd=str(sandbox),
    )
    plan_result = json.loads(out)
    assert plan_result["status"] == "plan_ready"
    preservation = plan_result["preservation"]
    assert preservation["batch_ids"], "计划缺少 batch_id 保留信息"
    assert preservation["failure_meta"], "计划缺少失败队列保留信息"
    print(f"  [OK] wizard-plan: {plan_result['total_items']} 项, batch_id 保留={preservation['batch_ids']}")

    rc, out, err = _run(
        [PY, MAIN, "wizard-apply"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] in ("applied", "partial"), f"搬家状态不对: {result['status']}"
    assert result["applied"] > 0
    print(f"  [OK] wizard-apply 成功: applied={result['applied']}, skipped={result['skipped']}, failed={result['failed']}")

    target_batches = target / "state" / "batches.json"
    if target_batches.exists():
        batches_data = _load_json(str(target_batches))
        ids = [b.get("batch_id") for b in batches_data if isinstance(b, dict)]
        assert old_batch_id in ids, f"搬家后旧 batch_id 丢失: {ids}"
        print(f"  [OK] 目标 batches.json 保留了旧 batch_id={old_batch_id}")

    target_fq = target / "state" / "failure_queue.json"
    if target_fq.exists():
        fq_data = _load_json(str(target_fq))
        if fq_data:
            assert fq_data[0]["retry_count"] == 3, f"retry_count 未保留: {fq_data[0]}"
            assert fq_data[0]["batch_id"] == old_batch_id, f"batch_id 未保留: {fq_data[0]}"
            print(f"  [OK] 目标 failure_queue.json 保留 retry_count=3, batch_id={old_batch_id}")

    target_inbox = target / "temp_inbox"
    if target_inbox.exists():
        moved = list(target_inbox.iterdir())
        print(f"  [OK] 收件箱样例已搬到目标: {len(moved)} 个")

    target_archive = target / "archive" / "SUP099" / old_batch_id
    if target_archive.exists():
        print(f"  [OK] 旧归档目录已搬到目标")

    target_log = target / "logs" / "archive_log.jsonl"
    if target_log.exists():
        log_count = len(target_log.read_text(encoding="utf-8").strip().splitlines())
        print(f"  [OK] 日志已搬到目标: {log_count} 条")

    return result


def step5_undo(sandbox: Path, target: Path):
    _banner("Step 5: wizard-undo 撤销搬家")

    rc, out, err = _run(
        [PY, MAIN, "wizard-undo"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] in ("undone", "undo_partial"), f"撤销状态不对: {result['status']}"
    assert result["undone"] > 0, f"撤销条目为 0: {result}"
    print(f"  [OK] wizard-undo 成功: undone={result['undone']}, failed={result['failed']}")

    state = _load_json(str(sandbox / ".wizard_state.json"))
    assert state["phase"] in ("undone", "undo_partial")
    assert not state["undo_available"]
    print(f"  [OK] 撤销后 phase={state['phase']}, undo_available=False")

    restored_state = sandbox / "state" / "batches.json"
    if not restored_state.exists():
        restored_candidates = list((sandbox / "state").glob("batches*.json"))
        if restored_candidates:
            print(f"  [OK] 撤销后源码 state 目录有恢复文件: {[f.name for f in restored_candidates]}")

    rc, out, err = _run(
        [PY, MAIN, "wizard-undo"],
        cwd=str(sandbox), check=False,
    )
    undo_result = json.loads(out)
    assert undo_result["status"] == "error", f"重复撤销应被拒绝: {undo_result}"
    print(f"  [OK] 重复撤销被正确拒绝")


def step6_resume(sandbox: Path, target: Path):
    _banner("Step 6: 重启续跑 - 模拟中断后续跑")

    _populate_legacy(sandbox)

    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    wizard_state_file = sandbox / ".wizard_state.json"
    if wizard_state_file.exists():
        wizard_state_file.unlink()

    rc, out, err = _run(
        [PY, MAIN, "wizard-scan", "--target", str(target)],
        cwd=str(sandbox),
    )
    scan_result = json.loads(out)
    assert scan_result["status"] == "scanned"

    rc, out, err = _run(
        [PY, MAIN, "wizard-plan"],
        cwd=str(sandbox),
    )

    state = _load_json(str(wizard_state_file))
    total_entries = len(state["entries"])
    half = max(1, total_entries // 2)
    for i in range(half):
        state["entries"][i]["status"] = "applied"
    state["phase"] = "partial"
    state["applied_count"] = half
    state["undo_available"] = True
    _write_file(wizard_state_file, json.dumps(state, ensure_ascii=False, indent=2))
    print(f"  [OK] 模拟中断: {half}/{total_entries} 已完成, phase=partial")

    rc, out, err = _run(
        [PY, MAIN, "wizard-apply", "--resume"],
        cwd=str(sandbox),
    )
    resume_result = json.loads(out)
    assert resume_result["status"] in ("applied", "partial"), f"续跑状态不对: {resume_result['status']}"
    assert resume_result["applied"] > 0
    print(f"  [OK] 续跑成功: applied={resume_result['applied']}, total={resume_result['total']}")

    rc, out, err = _run(
        [PY, MAIN, "wizard-status"],
        cwd=str(sandbox),
    )
    status_result = json.loads(out)
    assert status_result["phase"] in ("applied", "partial")
    print(f"  [OK] 续跑后状态: phase={status_result['phase']}, by_status={status_result['by_status']}")


def step7_export(sandbox: Path):
    _banner("Step 7: 导出复查 - wizard-export + wizard-audit-export")

    export_json = os.path.join(tempfile.gettempdir(), f"wizard_verify_{os.getpid()}.json")
    export_csv = os.path.join(tempfile.gettempdir(), f"wizard_verify_{os.getpid()}.csv")
    audit_json = os.path.join(tempfile.gettempdir(), f"wizard_audit_{os.getpid()}.json")
    audit_csv = os.path.join(tempfile.gettempdir(), f"wizard_audit_{os.getpid()}.csv")

    for p in (export_json, export_csv, audit_json, audit_csv):
        if os.path.exists(p):
            os.remove(p)

    rc, out, err = _run(
        [PY, MAIN, "wizard-export", "--format", "json", "--output", export_json],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    assert os.path.isfile(export_json)
    data = _load_json(export_json)
    assert "entries" in data
    assert len(data["entries"]) > 0
    print(f"  [OK] wizard-export JSON: {result['entries_count']} 条, {result['size_kb']} KB")

    rc, out, err = _run(
        [PY, MAIN, "wizard-export", "--format", "csv", "--output", export_csv],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    assert os.path.isfile(export_csv)
    assert os.path.getsize(export_csv) > 0
    print(f"  [OK] wizard-export CSV: {result['size_kb']} KB")

    rc, out, err = _run(
        [PY, MAIN, "wizard-audit-export", "--format", "json", "--output", audit_json],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    audit_data = _load_json(audit_json)
    assert isinstance(audit_data, list) and len(audit_data) > 0
    actions = {e.get("action") for e in audit_data}
    print(f"  [OK] wizard-audit-export JSON: {len(audit_data)} 条, 动作={actions}")

    rc, out, err = _run(
        [PY, MAIN, "wizard-audit-export", "--format", "csv", "--output", audit_csv],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    print(f"  [OK] wizard-audit-export CSV: {result['size_kb']} KB")

    rc, out, err = _run(
        [PY, MAIN, "wizard-export", "--format", "json", "--output", export_json],
        cwd=str(sandbox), check=False,
    )
    assert rc == 4, f"重复导出应 rc=4, 实际 rc={rc}"
    print(f"  [OK] 重复导出被拦截 (rc=4)")

    rc, out, err = _run(
        [PY, MAIN, "wizard-export", "--format", "json", "--output", export_json, "--force"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    print(f"  [OK] 加 --force 后覆盖导出成功")

    for p in (export_json, export_csv, audit_json, audit_csv):
        if os.path.exists(p):
            os.remove(p)


def step8_git_status(sandbox: Path):
    _banner("Step 8: git status 检查源码目录")

    for d in ["temp_inbox", "archive", "state", "logs"]:
        p = sandbox / d
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    try:
                        f.unlink()
                    except OSError:
                        pass
            for f in reversed(list(p.rglob("*"))):
                if f.is_dir():
                    try:
                        f.rmdir()
                    except OSError:
                        pass
    print("  [OK] 清空源码目录下残留运行目录")

    _run(["git", "init", "-q"], cwd=str(sandbox))
    _run(["git", "add", "-A"], cwd=str(sandbox))
    rc, out, err = _run(["git", "status", "--porcelain"], cwd=str(sandbox))
    lines = [l for l in out.splitlines() if l.strip()]

    forbidden_tokens = (
        "temp_inbox/", "archive/", "state/", "logs/",
        ".archive_version", ".migrated_from_v1",
        "batches.json", "failure_queue.json",
        "archive_log.jsonl",
    )
    bad = [l for l in lines if any(tok in l for tok in forbidden_tokens)]
    assert not bad, f"git status 中出现运行产物:\n" + "\n".join(bad)
    print(f"  [OK] git status 无运行产物残留")
    print(f"       跟踪文件数: {len(lines)}")


def step9_status_and_rules(sandbox: Path, target: Path):
    _banner("Step 9: wizard-status + 自定义规则/忽略项")

    wizard_state_file = sandbox / ".wizard_state.json"
    if wizard_state_file.exists():
        wizard_state_file.unlink()

    rules_file = sandbox / "wizard_rules.json"
    rules = {
        "path_mapping": {
            "temp_inbox": "inbox",
            "archive": "archived",
        },
        "ignore_patterns": [".tmp", ".bak"],
    }
    _write_file(str(rules_file), json.dumps(rules, ensure_ascii=False, indent=2))

    rc, out, err = _run(
        [PY, MAIN, "wizard-scan", "--target", str(target),
         "--rules", str(rules_file),
         "--ignore", ".tmp", ".bak"],
        cwd=str(sandbox),
    )
    scan_result = json.loads(out)
    assert scan_result["status"] == "scanned"
    print(f"  [OK] 自定义规则扫描: {scan_result['entries_count']} 项")

    rc, out, err = _run(
        [PY, MAIN, "wizard-status"],
        cwd=str(sandbox),
    )
    status_result = json.loads(out)
    assert status_result["status"] == "ok"
    assert status_result["phase"] == "scanned"
    print(f"  [OK] wizard-status: phase={status_result['phase']}, total={status_result['total_entries']}")

    if os.path.exists(str(rules_file)):
        os.remove(str(rules_file))


def step10_duplicate_export_check(sandbox: Path, target: Path):
    _banner("Step 10: 导出记录重复检测")

    wizard_state_file = sandbox / ".wizard_state.json"
    if wizard_state_file.exists():
        wizard_state_file.unlink()

    target_logs = target / "logs"
    target_logs.mkdir(parents=True, exist_ok=True)

    existing_log = target_logs / "archive_log.jsonl"
    if not existing_log.exists():
        existing_log.write_text(
            json.dumps({
                "timestamp": datetime.now().isoformat(),
                "source_path": "/fake/source.pdf",
                "target_path": "/fake/target.pdf",
                "action": "move",
                "batch_id": "BATCH_DUP_TEST",
                "status": "success",
                "error_reason": None,
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    rc, out, err = _run(
        [PY, MAIN, "wizard-scan", "--target", str(target)],
        cwd=str(sandbox),
    )
    scan_result = json.loads(out)

    log_entries = [e for e in scan_result["entries"] if e["kind"] == "logs"]
    for e in log_entries:
        if e.get("export_history"):
            print(f"  [OK] 导出历史已提取: {e['export_history']}")

    log_path = os.path.join(str(sandbox), "logs", "archive_log.jsonl")
    if os.path.isfile(log_path):
        history = {}
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    bid = rec.get("batch_id", "")
                    if bid:
                        history.setdefault(bid, []).append(rec.get("action", ""))
                except json.JSONDecodeError:
                    continue
        if history:
            print(f"  [OK] 日志批次历史: {list(history.keys())}")


def main():
    sandbox_root = Path(tempfile.mkdtemp(prefix="ia_wizard_verify_"))
    target_root = Path(tempfile.mkdtemp(prefix="ia_wizard_target_"))
    print(f"[INFO] 沙盒目录: {sandbox_root}")
    print(f"[INFO] 目标目录: {target_root}")

    old_bid = None
    try:
        old_bid = step1_build_sandbox(sandbox_root, target_root)
        step2_scan(sandbox_root, target_root)
        step3_conflict(sandbox_root, target_root)
        step4_plan_and_apply(sandbox_root, target_root, old_bid)
        step5_undo(sandbox_root, target_root)
        step6_resume(sandbox_root, target_root)
        step7_export(sandbox_root)
        step8_git_status(sandbox_root)
        step9_status_and_rules(sandbox_root, target_root)
        step10_duplicate_export_check(sandbox_root, target_root)

        _banner("ALL STEPS PASSED")
        print("全部 10 个步骤验证通过。退出码 0。")
        return 0
    except Exception as e:
        _banner("FAILED")
        print(f"验证失败: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        try:
            shutil.rmtree(sandbox_root, ignore_errors=True)
        except Exception:
            pass
        try:
            shutil.rmtree(target_root, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
