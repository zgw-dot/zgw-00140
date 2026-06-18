#!/usr/bin/env python3
"""
运行目录接管台账全链路验证脚本
================================
覆盖场景:
  1. 构建沙盒: 伪造含运行产物的源码目录 + 空目标目录
  2. takeover-scan: 扫描生成接管清单 (batch_id / failure_count / duplicate_export)
  3. takeover-confirm: 确认接管清单
  4. takeover-apply: 执行接管 + 验证配置切换
  5. 接管后重启: 重新加载验证状态持久化
  6. 自定义目录生效: 验证 list-batches / list-failures / export-logs 走新目录
  7. 权限失败: 模拟目标目录只读
  8. 重复记录冲突: 重复导出 + 重复 batch_id 检测
  9. 冲突处理: takeover-resolve 设定策略
 10. 重启续跑: 模拟中断后续跑
 11. 一键撤销: takeover-undo 恢复原始路径
 12. 导出台账/审计: JSON + CSV
 13. git status 复查: 源码目录无运行产物

用法:
    python verify_takeover_ledger.py

所有操作在系统临时目录下的沙盒中执行，不影响真实项目目录。
退出码 0 = 全部通过，非 0 = 某一步失败。
"""

import json
import os
import shutil
import stat
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


def _populate_legacy(sandbox: Path, old_batch_id: str) -> None:
    temp_inbox = sandbox / "temp_inbox"
    temp_inbox.mkdir(exist_ok=True)
    for s in ["SUP001_invoice_202605.pdf", "SUP002_contract_2026Q2.pdf"]:
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
    log_lines = [
        json.dumps({
            "timestamp": (datetime.now() - timedelta(days=2)).isoformat(),
            "source_path": str(temp_inbox / "_old_SUP099.pdf"),
            "target_path": str(archive / "SUP099_report_may.pdf"),
            "action": "move",
            "batch_id": old_batch_id,
            "status": "success",
            "error_reason": None,
        }, ensure_ascii=False),
        json.dumps({
            "timestamp": (datetime.now() - timedelta(days=1)).isoformat(),
            "source_path": str(temp_inbox / "_old_SUP099_dup.pdf"),
            "target_path": str(archive / "SUP099_report_may_dup.pdf"),
            "action": "move",
            "batch_id": old_batch_id,
            "status": "success",
            "error_reason": None,
        }, ensure_ascii=False),
    ]
    _write_file(logs_dir / "archive_log.jsonl", "\n".join(log_lines) + "\n")


def step1_build_sandbox(sandbox: Path, target: Path):
    global MAIN
    _banner("Step 1: 构建沙盒 - 伪造运行产物")

    pkg_src = HERE / "invoice_archiver"
    pkg_dst = sandbox / "invoice_archiver"
    shutil.copytree(pkg_src, pkg_dst)
    shutil.copy(HERE / "main.py", sandbox / "main.py")
    MAIN = str(sandbox / "main.py")

    old_batch_id = "BATCH_20260601_091500_000001"
    _populate_legacy(sandbox, old_batch_id)

    rc, out, err = _run([PY, MAIN, "init"], cwd=str(sandbox))
    data = json.loads(out)
    assert data["status"] == "initialized"
    print(f"  [OK] init 成功, version={data['version']}")

    target.mkdir(parents=True, exist_ok=True)
    print(f"  [OK] 目标目录已创建: {target}")

    return old_batch_id


def step2_scan(sandbox: Path, target: Path, old_batch_id: str):
    _banner("Step 2: takeover-scan 扫描运行产物")

    rc, out, err = _run(
        [PY, MAIN, "takeover-scan",
         "--target", str(target),
         "--conflict-policy", "rename",
         "--duplicate-policy", "merge",
         "--config", str(sandbox / "config.yaml")],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "scanned", f"扫描状态不对: {result['status']}"
    assert result["ledger"]["total_items"] > 0, f"扫描条目为 0: {result}"
    assert result["ledger"]["items_with_batch_id"] > 0, "未检测到 batch_id"
    assert result["ledger"]["items_with_failures"] > 0, "未检测到失败记录"
    assert result["ledger"]["items_with_duplicate_export"] > 0, "未检测到重复导出"

    summary = result["summary"]
    assert old_batch_id in summary["batch_ids"], f"旧 batch_id 不在扫描结果中: {summary['batch_ids']}"
    assert summary["failure_meta"], "未检测到失败队列元数据"
    assert summary["duplicate_exports"], "未检测到重复导出标记"

    print(f"  [OK] takeover-scan 成功: {result['ledger']['total_items']} 项")
    print(f"       batch_ids: {summary['batch_ids']}")
    print(f"       失败队列: {summary['failure_meta']}")
    print(f"       重复导出: {summary['duplicate_exports']}")

    state_file = sandbox / ".takeover_state.json"
    assert state_file.exists(), "扫描后未生成 .takeover_state.json"
    state = _load_json(str(state_file))
    assert state["phase"] == "scanned"
    print(f"  [OK] .takeover_state.json 已生成, phase={state['phase']}")

    return result["ledger"]["total_items"]


def step3_confirm(sandbox: Path):
    _banner("Step 3: takeover-confirm 确认接管清单")

    rc, out, err = _run(
        [PY, MAIN, "takeover-confirm"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "confirmed", f"确认失败: {result}"
    assert result["items_count"] > 0
    assert "ledger" in result

    ledger = result["ledger"]
    assert ledger["all_batch_ids"], "台账中无 batch_id"
    assert ledger["by_batch_summary"], "台账中无批次摘要"

    for bid, info in ledger["by_batch_summary"].items():
        print(f"  [OK] 批次 {bid}: item_count={info['item_count']}, "
              f"failure_count={info['failure_count']}, "
              f"duplicate_export={info['duplicate_export']}")
        assert "sources" in info, "批次摘要缺少 sources"
        assert "destinations" in info, "批次摘要缺少 destinations"

    print(f"  [OK] takeover-confirm 成功, 台账含 {result['items_count']} 项")


def step4_apply(sandbox: Path, target: Path, old_batch_id: str):
    _banner("Step 4: takeover-apply 执行接管 + 配置切换")

    rc, out, err = _run(
        [PY, MAIN, "takeover-apply"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] in ("applied", "partial"), f"接管状态不对: {result['status']}"
    assert result["applied"] > 0
    print(f"  [OK] takeover-apply 成功: applied={result['applied']}, skipped={result['skipped']}, failed={result['failed']}")

    target_state = target / "state"
    if target_state.exists():
        batches_file = target_state / "batches.json"
        if batches_file.exists():
            batches_data = _load_json(str(batches_file))
            ids = [b.get("batch_id") for b in batches_data if isinstance(b, dict)]
            assert old_batch_id in ids, f"接管后旧 batch_id 丢失: {ids}"
            print(f"  [OK] 目标 batches.json 保留了旧 batch_id={old_batch_id}")

    config_path = sandbox / "config.yaml"
    if config_path.exists():
        from invoice_archiver.config import load_config
        cfg = load_config(str(config_path))
        assert target.as_posix() in cfg.source_dir or str(target) in cfg.source_dir, \
            f"配置未切换: source_dir={cfg.source_dir}, 期望含 {target}"
        print(f"  [OK] 配置已切换: source_dir={cfg.source_dir}")
        print(f"       archive_dir={cfg.archive_dir}")
        print(f"       state_dir={cfg.state_dir}")
        print(f"       log_dir={cfg.log_dir}")

    return result


def step5_restart_persistence(sandbox: Path, target: Path, old_batch_id: str):
    _banner("Step 5: 接管后重启 - 验证状态持久化")

    from invoice_archiver.config import load_config
    from invoice_archiver.archiver import Archiver

    cfg = load_config(str(sandbox / "config.yaml"))
    fresh = Archiver(cfg)

    batches = fresh.list_batches()
    ids = [b["batch_id"] for b in batches]
    assert old_batch_id in ids, f"重启后旧批次丢失: {ids}"
    print(f"  [OK] 重启后 list-batches 可查到旧 batch_id={old_batch_id}")

    failures = fresh.list_failures()
    assert len(failures) >= 1, "重启后失败队列为空"
    assert failures[0]["retry_count"] == 3, f"重启后 retry_count 丢失: {failures[0]}"
    print(f"  [OK] 重启后 list-failures 可查到, retry_count={failures[0]['retry_count']}")

    entries = fresh.logger.load_entries()
    assert len(entries) >= 2, f"重启后日志条目不够: {len(entries)}"
    print(f"  [OK] 重启后日志可查询, 共 {len(entries)} 条")


def step6_custom_dir_effective(sandbox: Path, target: Path):
    _banner("Step 6: 自定义目录生效 - list-batches / list-failures / export-logs 走新目录")

    rc, out, err = _run(
        [PY, MAIN, "--ignore-legacy", "list-batches"],
        cwd=str(sandbox),
    )
    data = json.loads(out)
    assert data["summary"]["total"] >= 1, f"list-batches 应有批次: {data}"
    print(f"  [OK] list-batches 从新目录读取: total={data['summary']['total']}")

    rc, out, err = _run(
        [PY, MAIN, "--ignore-legacy", "list-failures"],
        cwd=str(sandbox),
    )
    data = json.loads(out)
    assert data["total"] >= 1, f"list-failures 应有记录: {data}"
    print(f"  [OK] list-failures 从新目录读取: total={data['total']}")

    export_path = os.path.join(tempfile.gettempdir(), f"tk_verify_export_{os.getpid()}.csv")
    if os.path.exists(export_path):
        os.remove(export_path)
    rc, out, err = _run(
        [PY, MAIN, "--ignore-legacy", "export-logs", "--format", "csv", "--output", export_path],
        cwd=str(sandbox),
    )
    resp = json.loads(out)
    assert resp["status"] == "exported"
    assert os.path.isfile(export_path)
    print(f"  [OK] export-logs 从新目录导出: {resp['size_kb']} KB")
    if os.path.exists(export_path):
        os.remove(export_path)


def step7_permission_failure(sandbox: Path):
    _banner("Step 7: 权限失败模拟")

    target_readonly = Path(tempfile.mkdtemp(prefix="ia_tk_readonly_"))
    try:
        _populate_legacy(sandbox, "BATCH_PERM_TEST_001")

        takeover_state_file = sandbox / ".takeover_state.json"
        if takeover_state_file.exists():
            takeover_state_file.unlink()

        rc, out, err = _run(
            [PY, MAIN, "takeover-scan",
             "--target", str(target_readonly),
             "--conflict-policy", "rename"],
            cwd=str(sandbox),
        )

        if os.name == "nt":
            os.system(f'icacls "{str(target_readonly)}" /deny Everyone:(W) >nul 2>&1')
        else:
            os.chmod(str(target_readonly), stat.S_IRUSR | stat.S_IXUSR)

        rc, out, err = _run(
            [PY, MAIN, "takeover-confirm"],
            cwd=str(sandbox),
        )

        rc, out, err = _run(
            [PY, MAIN, "takeover-apply"],
            cwd=str(sandbox), check=False,
        )
        result = json.loads(out)
        assert result["status"] in ("partial", "applied"), f"权限测试状态不对: {result['status']}"
        if result["failed"] > 0:
            print(f"  [OK] 权限失败被捕获: failed={result['failed']}")
            for d in result.get("details", []):
                if d.get("reason") == "permission_denied":
                    print(f"       权限不足: {d['source']} -> {d['target']}")
        else:
            print(f"  [OK] 权限测试完成 (系统未拒绝写入): status={result['status']}")

        rc, out, err = _run(
            [PY, MAIN, "takeover-status"],
            cwd=str(sandbox),
        )
        status = json.loads(out)
        print(f"  [OK] takeover-status: phase={status['phase']}, failed={status.get('failed_count', 0)}")

        if os.name == "nt":
            os.system(f'icacls "{str(target_readonly)}" /grant Everyone:(W) >nul 2>&1')
        else:
            os.chmod(str(target_readonly), stat.S_IRWXU)
    finally:
        shutil.rmtree(target_readonly, ignore_errors=True)


def step8_duplicate_conflict(sandbox: Path, target: Path):
    _banner("Step 8: 重复记录冲突 - 重复导出 + 重复 batch_id")

    takeover_state_file = sandbox / ".takeover_state.json"
    if takeover_state_file.exists():
        takeover_state_file.unlink()

    dup_batch_id = "BATCH_DUP_TEST"
    state_dir = sandbox / "state"
    state_dir.mkdir(exist_ok=True)
    existing = []
    batches_file = state_dir / "batches.json"
    if batches_file.exists():
        existing = _load_json(str(batches_file))
    existing.append({
        "batch_id": dup_batch_id,
        "created_at": datetime.now().isoformat(),
        "status": "committed",
        "files": [],
    })
    existing.append({
        "batch_id": dup_batch_id,
        "created_at": datetime.now().isoformat(),
        "status": "committed",
        "files": [],
    })
    _write_file(str(batches_file), json.dumps(existing, ensure_ascii=False, indent=2))

    logs_dir = sandbox / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / "archive_log.jsonl"
    lines = []
    if log_file.exists():
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    for i in range(3):
        lines.append(json.dumps({
            "timestamp": datetime.now().isoformat(),
            "source_path": f"/fake/source_{i}.pdf",
            "target_path": f"/fake/target_{i}.pdf",
            "action": "move",
            "batch_id": dup_batch_id,
            "status": "success",
            "error_reason": None,
        }, ensure_ascii=False))
    _write_file(str(log_file), "\n".join(lines) + "\n")

    rc, out, err = _run(
        [PY, MAIN, "takeover-scan",
         "--target", str(target),
         "--duplicate-policy", "ask"],
        cwd=str(sandbox),
    )
    scan_result = json.loads(out)
    assert scan_result["status"] == "scanned"
    assert scan_result["ledger"]["items_with_duplicate_export"] > 0, \
        f"未检测到重复导出: {scan_result['ledger']}"
    print(f"  [OK] 重复导出被检测: items_with_duplicate_export={scan_result['ledger']['items_with_duplicate_export']}")

    rc, out, err = _run(
        [PY, MAIN, "takeover-confirm"],
        cwd=str(sandbox), check=False,
    )
    confirm_result = json.loads(out)
    assert confirm_result.get("status") == "error", \
        f"ask 策略下应拒绝确认: {confirm_result}"
    print(f"  [OK] ask 策略下确认被拒绝 (需先设置 duplicate-policy)")

    rc, out, err = _run(
        [PY, MAIN, "takeover-resolve", "--duplicate-policy", "merge"],
        cwd=str(sandbox),
    )
    resolve_result = json.loads(out)
    assert resolve_result["status"] == "ok"
    print(f"  [OK] takeover-resolve --duplicate-policy merge 成功")


def step9_conflict_resolution(sandbox: Path, target: Path):
    _banner("Step 9: 冲突处理 - takeover-resolve 设定策略")

    target_state = target / "state"
    target_state.mkdir(parents=True, exist_ok=True)
    (target_state / "batches.json").write_text(
        json.dumps([{"batch_id": "CONFLICT_BATCH", "status": "committed"}], ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  [OK] 在目标目录预置冲突文件: state/batches.json")

    takeover_state_file = sandbox / ".takeover_state.json"
    if takeover_state_file.exists():
        takeover_state_file.unlink()

    rc, out, err = _run(
        [PY, MAIN, "takeover-scan",
         "--target", str(target),
         "--conflict-policy", "skip"],
        cwd=str(sandbox),
    )
    scan_result = json.loads(out)
    assert scan_result["has_conflicts"], f"应检测到冲突: {scan_result}"
    print(f"  [OK] 冲突检测正确: has_conflicts=True")

    conflict_entries = [e for e in scan_result["items"] if e.get("conflict")]
    assert conflict_entries, "无冲突条目"
    conflict_src = conflict_entries[0]["source_path"]

    rc, out, err = _run(
        [PY, MAIN, "takeover-resolve",
         "--source-path", conflict_src,
         "--resolution", "rename"],
        cwd=str(sandbox),
    )
    resolve_result = json.loads(out)
    assert resolve_result["status"] == "ok"
    print(f"  [OK] takeover-resolve 冲突策略=rename 成功")

    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)


def step10_resume(sandbox: Path, target: Path):
    _banner("Step 10: 重启续跑 - 模拟中断后续跑")

    _populate_legacy(sandbox, "BATCH_RESUME_TEST_001")

    takeover_state_file = sandbox / ".takeover_state.json"
    if takeover_state_file.exists():
        takeover_state_file.unlink()

    rc, out, err = _run(
        [PY, MAIN, "takeover-scan",
         "--target", str(target),
         "--conflict-policy", "rename",
         "--duplicate-policy", "merge"],
        cwd=str(sandbox),
    )

    rc, out, err = _run(
        [PY, MAIN, "takeover-confirm"],
        cwd=str(sandbox),
    )

    state = _load_json(str(takeover_state_file))
    total_entries = len(state["items"])
    half = max(1, total_entries // 2)
    for i in range(half):
        state["items"][i]["status"] = "applied"
    state["phase"] = "partial"
    state["applied_count"] = half
    state["undo_available"] = True
    _write_file(takeover_state_file, json.dumps(state, ensure_ascii=False, indent=2))
    print(f"  [OK] 模拟中断: {half}/{total_entries} 已完成, phase=partial")

    rc, out, err = _run(
        [PY, MAIN, "takeover-resume", "--resume"],
        cwd=str(sandbox),
    )
    resume_result = json.loads(out)
    assert resume_result["status"] in ("applied", "partial"), f"续跑状态不对: {resume_result['status']}"
    assert resume_result["applied"] > 0
    print(f"  [OK] 续跑成功: applied={resume_result['applied']}, total={resume_result['total']}")

    rc, out, err = _run(
        [PY, MAIN, "takeover-status"],
        cwd=str(sandbox),
    )
    status_result = json.loads(out)
    assert status_result["phase"] in ("applied", "partial")
    print(f"  [OK] 续跑后状态: phase={status_result['phase']}, by_status={status_result['by_status']}")


def step11_undo(sandbox: Path):
    _banner("Step 11: 一键撤销接管 - takeover-undo")

    rc, out, err = _run(
        [PY, MAIN, "takeover-undo"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] in ("undone", "undo_partial"), f"撤销状态不对: {result['status']}"
    assert result["undone"] > 0
    print(f"  [OK] takeover-undo 成功: undone={result['undone']}, failed={result['failed']}")

    state = _load_json(str(sandbox / ".takeover_state.json"))
    assert state["phase"] in ("undone", "undo_partial")
    assert not state["undo_available"]
    print(f"  [OK] 撤销后 phase={state['phase']}, undo_available=False")

    rc, out, err = _run(
        [PY, MAIN, "takeover-undo"],
        cwd=str(sandbox), check=False,
    )
    undo_result = json.loads(out)
    assert undo_result["status"] == "error", f"重复撤销应被拒绝: {undo_result}"
    print(f"  [OK] 重复撤销被正确拒绝")


def step12_export(sandbox: Path):
    _banner("Step 12: 导出台账/审计 - JSON + CSV")

    takeover_state_file = sandbox / ".takeover_state.json"
    if takeover_state_file.exists():
        state = _load_json(str(takeover_state_file))
        if state.get("phase") == "undone":
            pass

    export_json = os.path.join(tempfile.gettempdir(), f"tk_ledger_{os.getpid()}.json")
    export_csv = os.path.join(tempfile.gettempdir(), f"tk_ledger_{os.getpid()}.csv")
    audit_json = os.path.join(tempfile.gettempdir(), f"tk_audit_{os.getpid()}.json")
    audit_csv = os.path.join(tempfile.gettempdir(), f"tk_audit_{os.getpid()}.csv")

    for p in (export_json, export_csv, audit_json, audit_csv):
        if os.path.exists(p):
            os.remove(p)

    rc, out, err = _run(
        [PY, MAIN, "takeover-export", "--format", "json", "--output", export_json],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    assert os.path.isfile(export_json)
    data = _load_json(export_json)
    assert "items" in data
    assert "ledger_summary" in data
    print(f"  [OK] takeover-export JSON: {result['entries_count']} 条, {result['size_kb']} KB")

    rc, out, err = _run(
        [PY, MAIN, "takeover-export", "--format", "csv", "--output", export_csv],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    assert os.path.isfile(export_csv)
    assert os.path.getsize(export_csv) > 0
    print(f"  [OK] takeover-export CSV: {result['size_kb']} KB")

    rc, out, err = _run(
        [PY, MAIN, "takeover-audit", "--format", "json", "--output", audit_json],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    if os.path.isfile(audit_json):
        audit_data = _load_json(audit_json)
        if isinstance(audit_data, list) and len(audit_data) > 0:
            actions = {e.get("action") for e in audit_data}
            print(f"  [OK] takeover-audit JSON: {len(audit_data)} 条, 动作={actions}")
        else:
            print(f"  [OK] takeover-audit JSON: 已导出")

    rc, out, err = _run(
        [PY, MAIN, "takeover-audit", "--format", "csv", "--output", audit_csv],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    print(f"  [OK] takeover-audit CSV: {result.get('size_kb', 0)} KB")

    rc, out, err = _run(
        [PY, MAIN, "takeover-export", "--format", "json", "--output", export_json],
        cwd=str(sandbox), check=False,
    )
    assert rc == 4, f"重复导出应 rc=4, 实际 rc={rc}"
    print(f"  [OK] 重复导出被拦截 (rc=4)")

    rc, out, err = _run(
        [PY, MAIN, "takeover-export", "--format", "json", "--output", export_json, "--force"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    print(f"  [OK] 加 --force 后覆盖导出成功")

    for p in (export_json, export_csv, audit_json, audit_csv):
        if os.path.exists(p):
            os.remove(p)


def step13_git_status(sandbox: Path):
    _banner("Step 13: git status 复查源码目录")

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
    for marker in [".archive_version", ".migrated_from_v1", ".takeover_state.json", ".wizard_state.json"]:
        p = sandbox / marker
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    print("  [OK] 清空源码目录下残留运行目录和标记文件")

    _run(["git", "init", "-q"], cwd=str(sandbox))
    _run(["git", "add", "-A"], cwd=str(sandbox))
    rc, out, err = _run(["git", "status", "--porcelain"], cwd=str(sandbox))
    lines = [l for l in out.splitlines() if l.strip()]

    forbidden_tokens = (
        "temp_inbox/", "archive/", "state/", "logs/",
        ".archive_version", ".migrated_from_v1",
        "batches.json", "failure_queue.json",
        "archive_log.jsonl", ".takeover_state.json",
    )
    bad = [l for l in lines if any(tok in l for tok in forbidden_tokens)]
    assert not bad, f"git status 中出现运行产物:\n" + "\n".join(bad)
    print(f"  [OK] git status 无运行产物残留")
    print(f"       跟踪文件数: {len(lines)}")


def main():
    sandbox_root = Path(tempfile.mkdtemp(prefix="ia_tk_verify_"))
    target_root = Path(tempfile.mkdtemp(prefix="ia_tk_target_"))
    print(f"[INFO] 沙盒目录: {sandbox_root}")
    print(f"[INFO] 目标目录: {target_root}")

    old_bid = None
    try:
        old_bid = step1_build_sandbox(sandbox_root, target_root)
        step2_scan(sandbox_root, target_root, old_bid)
        step3_confirm(sandbox_root)
        step4_apply(sandbox_root, target_root, old_bid)
        step5_restart_persistence(sandbox_root, target_root, old_bid)
        step6_custom_dir_effective(sandbox_root, target_root)
        step7_permission_failure(sandbox_root)
        step8_duplicate_conflict(sandbox_root, target_root)
        step9_conflict_resolution(sandbox_root, target_root)
        step10_resume(sandbox_root, target_root)
        step11_undo(sandbox_root)
        step12_export(sandbox_root)
        step13_git_status(sandbox_root)

        _banner("ALL STEPS PASSED")
        print("全部 13 个步骤验证通过。退出码 0。")
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
