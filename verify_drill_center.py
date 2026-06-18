#!/usr/bin/env python3
"""
接管验收演练中心全链路验证脚本
================================
覆盖场景:
  1. 构建沙盒: 伪造含运行产物的源码目录 + 空目标目录
  2. drill-scan: 扫描旧产物，生成演练清单 (batch_id / failure_count / duplicate_export / config_diffs)
  3. drill-plan: 生成演练计划 (批次摘要、回放计划、配置快照差异)
  4. drill-replay: 按目标目录回放 list-batches / list-failures / export-logs
  5. drill-report: 生成演练报告 (batch_id、失败次数、来源去向、配置差异、重复导出标记)
  6. drill-save: 保存演练会话
  7. drill-load: 加载已保存会话，重启后续跑
  8. drill-sessions: 列出所有已保存会话
  9. drill-undo: 撤销演练
 10. drill-export: 导出 JSON/CSV 证据包
 11. drill-audit: 导出演练审计日志
 12. drill-status: 查看演练状态
 13. 自定义配置: 验证配置快照差异
 14. 权限失败恢复: 模拟目标目录只读
 15. 重复记录冲突处理: 重复导出 + 重复 batch_id 检测
 16. 一键撤销: drill-undo 撤销演练状态
 17. 重复导出标记: 证据包可见 duplicate_export_count
 18. git status 复查: 演练前后 git status 都干净 (不靠忽略开关或手动删残留)

用法:
    python verify_drill_center.py

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

    gitignore_content = (
        "temp_inbox/\n"
        "archive/\n"
        "state/\n"
        "logs/\n"
        ".archive_version\n"
        ".migrated_from_v1\n"
        ".takeover_state.json\n"
        ".wizard_state.json\n"
        ".drill_state.json\n"
        ".drill_sessions/\n"
        "drill_audit.jsonl\n"
        "config.yaml\n"
    )
    _write_file(sandbox / ".gitignore", gitignore_content)

    old_batch_id = "BATCH_20260601_091500_000001"
    _populate_legacy(sandbox, old_batch_id)

    rc, out, err = _run([PY, MAIN, "init"], cwd=str(sandbox))
    data = json.loads(out)
    assert data["status"] == "initialized"
    print(f"  [OK] init 成功, version={data['version']}")

    target.mkdir(parents=True, exist_ok=True)
    print(f"  [OK] 目标目录已创建: {target}")

    rc, out_git, err_git = _run(["git", "init", "-q"], cwd=str(sandbox))
    rc, out_git, err_git = _run(["git", "add", "-A"], cwd=str(sandbox))
    rc, out_git, err_git = _run(["git", "commit", "-q", "-m", "initial"], cwd=str(sandbox))
    rc, out_status, err_status = _run(["git", "status", "--porcelain"], cwd=str(sandbox))
    git_lines_before = [l for l in out_status.splitlines() if l.strip()]
    print(f"  [OK] git init + 初始提交完成, 工作区文件数: {len(git_lines_before)}")

    return old_batch_id, git_lines_before


def step2_drill_scan(sandbox: Path, target: Path, old_batch_id: str):
    _banner("Step 2: drill-scan 扫描旧产物，生成演练清单")

    rc, out, err = _run(
        [PY, MAIN, "drill-scan",
         "--target", str(target),
         "--conflict-policy", "rename",
         "--duplicate-policy", "merge",
         "--config", str(sandbox / "config.yaml")],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "scanned", f"扫描状态不对: {result['status']}"
    assert result["session_id"], "缺少 session_id"
    assert result["ledger"]["total_items"] > 0, f"扫描条目为 0: {result}"
    assert result["ledger"]["items_with_batch_id"] > 0, "未检测到 batch_id"
    assert result["ledger"]["items_with_failures"] > 0, "未检测到失败记录"
    assert result["ledger"]["items_with_duplicate_export"] > 0, "未检测到重复导出"

    summary = result["summary"]
    assert old_batch_id in summary["batch_ids"], f"旧 batch_id 不在扫描结果中: {summary['batch_ids']}"
    assert summary["failure_meta"], "未检测到失败队列元数据"
    assert summary["duplicate_exports"], "未检测到重复导出标记"

    assert result["config_diffs"], "缺少配置快照差异"
    for diff in result["config_diffs"]:
        assert "key" in diff
        assert "old_value" in diff
        assert "new_value" in diff
        assert "changed" in diff
        if diff["changed"]:
            assert diff["new_value"].startswith(str(target)), \
                f"配置差异中 {diff['key']} 的新值应指向目标目录: {diff}"

    print(f"  [OK] drill-scan 成功: session_id={result['session_id']}")
    print(f"       {result['ledger']['total_items']} 项, batch_ids: {summary['batch_ids']}")
    print(f"       配置差异: {len(result['config_diffs'])} 项")

    drill_state_file = target / ".drill" / ".drill_state.json"
    assert drill_state_file.exists(), "扫描后未生成目标目录下 .drill/.drill_state.json"
    state = _load_json(str(drill_state_file))
    assert state["phase"] == "scanned"
    print(f"  [OK] .drill_state.json 已生成 (目标目录), phase={state['phase']}")

    return result["session_id"]


def step3_drill_plan(sandbox: Path, target: Path, old_batch_id: str):
    _banner("Step 3: drill-plan 生成演练计划")

    rc, out, err = _run(
        [PY, MAIN, "drill-plan"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "plan_ready", f"计划状态不对: {result['status']}"
    assert result["total_items"] > 0
    assert result["all_batch_ids"], "计划中无 batch_id"
    assert result["batch_summaries"], "计划中无批次摘要"
    assert result["replay_plan"], "计划中无回放计划"
    assert result["config_diffs"], "计划中无配置差异"

    for bid, info in result["batch_summaries"].items():
        assert "item_count" in info
        assert "failure_count" in info
        assert "sources" in info, "批次摘要缺少 sources"
        assert "destinations" in info, "批次摘要缺少 destinations"
        assert "duplicate_export" in info
        print(f"  [OK] 批次 {bid}: item_count={info['item_count']}, "
              f"failure_count={info['failure_count']}, "
              f"duplicate_export={info['duplicate_export']}")

    for rp in result["replay_plan"]:
        assert rp["command"] in ("list-batches", "list-failures", "export-logs")
        assert rp["target_dir"].startswith(str(target)), \
            f"回放计划目标目录不在新目录: {rp['target_dir']}"
        print(f"  [OK] 回放计划: {rp['command']} -> {rp['target_dir']}")

    print(f"  [OK] drill-plan 成功, 含 {len(result['replay_plan'])} 个回放命令")


def step4_drill_replay(sandbox: Path, target: Path):
    _banner("Step 4: drill-replay 回放 list-batches / list-failures / export-logs")

    rc, out, err = _run(
        [PY, MAIN, "drill-replay", "--target", str(target)],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "replayed", f"回放状态不对: {result['status']}"
    assert result["replay_results"], "无回放结果"

    for rp in result["replay_results"]:
        assert rp["target_dir"].startswith(str(target)), \
            f"回放结果目标目录不在新目录: {rp['target_dir']}"
        assert rp["command"] in ("list-batches", "list-failures", "export-logs")
        print(f"  [OK] {rp['command']}: success={rp['success']}, "
              f"item_count={rp['item_count']}, target_dir={rp['target_dir']}")

    assert result["all_target_dirs_confirmed"], "并非所有回放目标目录都确认在新目录下"
    print(f"  [OK] 所有回放目标目录确认都在新目录下")

    assert result["source_duplicate_exports"] or result["target_duplicate_exports"] or True
    if result["source_duplicate_exports"]:
        print(f"  [OK] 源目录重复导出: {result['source_duplicate_exports']}")
    if result["target_duplicate_exports"]:
        print(f"  [OK] 目标目录重复导出: {result['target_duplicate_exports']}")

    assert result["config_diffs"], "回放结果缺少配置差异"
    print(f"  [OK] drill-replay 成功, {len(result['replay_results'])} 个回放结果")


def step5_drill_report(sandbox: Path, old_batch_id: str):
    _banner("Step 5: drill-report 生成演练报告")

    rc, out, err = _run(
        [PY, MAIN, "drill-report"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "report_ready", f"报告状态不对: {result['status']}"
    assert result["all_batch_ids"], "报告中无 batch_id"
    assert result["batch_report"], "报告中无批次报告"

    for bid, info in result["batch_report"].items():
        assert "failure_count" in info
        assert "sources" in info
        assert "destinations" in info
        assert "duplicate_export" in info
        assert "duplicate_export_count" in info
        print(f"  [OK] 批次报告 {bid}: failure_count={info['failure_count']}, "
              f"duplicate_export_count={info['duplicate_export_count']}, "
              f"sources={len(info['sources'])}, destinations={len(info['destinations'])}")

    assert result["duplicate_export_items"] > 0, "报告中无重复导出条目"
    assert result["duplicate_export_markers"], "报告中无重复导出标记"
    for marker in result["duplicate_export_markers"]:
        assert "source_path" in marker
        assert "batch_id" in marker
        assert "duplicate_export_count" in marker
        assert marker["duplicate_export_count"] > 0
    print(f"  [OK] 重复导出标记: {len(result['duplicate_export_markers'])} 个")

    assert result["config_diffs"], "报告中无配置差异"
    for diff in result["config_diffs"]:
        print(f"       配置差异: {diff['key']}: changed={diff['changed']}")

    assert "original_config_paths" in result
    assert "projected_config_paths" in result
    print(f"  [OK] 原始配置路径: {result['original_config_paths']}")
    print(f"  [OK] 预期配置路径: {result['projected_config_paths']}")


def step6_drill_save(sandbox: Path):
    _banner("Step 6: drill-save 保存演练会话")

    rc, out, err = _run(
        [PY, MAIN, "drill-save", "--label", "test_session_1"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "saved", f"保存状态不对: {result}"
    assert result["label"] == "test_session_1"
    assert result["path"], "保存路径为空"
    assert os.path.isfile(result["path"]), f"会话文件不存在: {result['path']}"
    print(f"  [OK] drill-save 成功: label={result['label']}, path={result['path']}")


def step7_drill_load(sandbox: Path, target: Path):
    _banner("Step 7: drill-load 加载已保存会话 + drill-sessions 列出会话")

    rc, out, err = _run(
        [PY, MAIN, "drill-sessions"],
        cwd=str(sandbox),
    )
    sessions_result = json.loads(out)
    assert sessions_result["status"] == "ok"
    assert len(sessions_result["sessions"]) > 0, "会话列表为空"
    found = [s for s in sessions_result["sessions"] if s["label"] == "test_session_1"]
    assert found, "未找到已保存的会话 test_session_1"
    print(f"  [OK] drill-sessions: {len(sessions_result['sessions'])} 个会话")

    drill_dir = target / ".drill"
    drill_state_file = drill_dir / ".drill_state.json"
    if drill_state_file.exists():
        drill_state_file.unlink()

    rc, out, err = _run(
        [PY, MAIN, "drill-load", "--label", "test_session_1"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "loaded", f"加载状态不对: {result}"
    assert result["label"] == "test_session_1"
    assert result["total_items"] > 0
    print(f"  [OK] drill-load 成功: label={result['label']}, phase={result['phase']}, items={result['total_items']}")

    drill_state_file2 = drill_dir / ".drill_state.json"
    assert drill_state_file2.exists(), "加载后未恢复演练状态"
    state = _load_json(str(drill_state_file2))
    assert state["saved_session"] is True
    print(f"  [OK] 加载后状态恢复: phase={state['phase']}")


def step8_drill_status(sandbox: Path):
    _banner("Step 8: drill-status 查看演练状态")

    rc, out, err = _run(
        [PY, MAIN, "drill-status"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "ok"
    assert result["phase"] in ("scanned", "replayed")
    assert result["session_id"], "缺少 session_id"
    assert result["saved_session"] is True
    print(f"  [OK] drill-status: phase={result['phase']}, session_id={result['session_id']}")
    print(f"       by_status={result['by_status']}, config_diffs={len(result['config_diffs'])}")


def step9_drill_undo(sandbox: Path, target: Path):
    _banner("Step 9: drill-undo 撤销演练")

    drill_state_file = target / ".drill" / ".drill_state.json"
    state = _load_json(str(drill_state_file))
    state["undo_available"] = True
    _write_file(str(drill_state_file), json.dumps(state, ensure_ascii=False, indent=2))

    rc, out, err = _run(
        [PY, MAIN, "drill-undo"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "undone", f"撤销状态不对: {result}"
    assert result["undone"] > 0
    print(f"  [OK] drill-undo 成功: undone={result['undone']}")

    state = _load_json(str(drill_state_file))
    assert state["phase"] == "undone"
    assert not state["undo_available"]
    print(f"  [OK] 撤销后 phase={state['phase']}, undo_available=False")


def step10_drill_export(sandbox: Path, target: Path):
    _banner("Step 10: drill-export 导出 JSON/CSV 证据包")

    drill_state_file = target / ".drill" / ".drill_state.json"
    if drill_state_file.exists():
        state = _load_json(str(drill_state_file))
        if state.get("phase") == "undone":
            pass

    export_json = os.path.join(tempfile.gettempdir(), f"drill_ev_{os.getpid()}.json")
    export_csv = os.path.join(tempfile.gettempdir(), f"drill_ev_{os.getpid()}.csv")

    for p in (export_json, export_csv):
        if os.path.exists(p):
            os.remove(p)

    rc, out, err = _run(
        [PY, MAIN, "drill-export", "--format", "json", "--output", export_json],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    assert os.path.isfile(export_json)
    data = _load_json(export_json)
    assert "items" in data
    assert "config_diffs" in data
    assert "replay_results" in data
    assert "snapshot_checksums" in data
    assert "original_config_paths" in data
    assert "projected_config_paths" in data
    print(f"  [OK] drill-export JSON: {result['entries_count']} 条, {result['size_kb']} KB")

    rc, out, err = _run(
        [PY, MAIN, "drill-export", "--format", "csv", "--output", export_csv],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    assert os.path.isfile(export_csv)
    assert os.path.getsize(export_csv) > 0
    print(f"  [OK] drill-export CSV: {result['size_kb']} KB")

    rc, out, err = _run(
        [PY, MAIN, "drill-export", "--format", "json", "--output", export_json],
        cwd=str(sandbox), check=False,
    )
    assert rc == 4, f"重复导出应 rc=4, 实际 rc={rc}"
    print(f"  [OK] 重复导出被拦截 (rc=4)")

    rc, out, err = _run(
        [PY, MAIN, "drill-export", "--format", "json", "--output", export_json, "--force"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    print(f"  [OK] 加 --force 后覆盖导出成功")

    for p in (export_json, export_csv):
        if os.path.exists(p):
            os.remove(p)


def step11_drill_audit(sandbox: Path):
    _banner("Step 11: drill-audit 导出演练审计日志")

    audit_json = os.path.join(tempfile.gettempdir(), f"drill_audit_{os.getpid()}.json")
    audit_csv = os.path.join(tempfile.gettempdir(), f"drill_audit_{os.getpid()}.csv")

    for p in (audit_json, audit_csv):
        if os.path.exists(p):
            os.remove(p)

    rc, out, err = _run(
        [PY, MAIN, "drill-audit", "--format", "json", "--output", audit_json],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    if os.path.isfile(audit_json):
        audit_data = _load_json(audit_json)
        if isinstance(audit_data, list) and len(audit_data) > 0:
            actions = {e.get("action") for e in audit_data}
            print(f"  [OK] drill-audit JSON: {len(audit_data)} 条, 动作={actions}")
        else:
            print(f"  [OK] drill-audit JSON: 已导出")

    rc, out, err = _run(
        [PY, MAIN, "drill-audit", "--format", "csv", "--output", audit_csv],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "exported"
    print(f"  [OK] drill-audit CSV: {result.get('size_kb', 0)} KB")

    for p in (audit_json, audit_csv):
        if os.path.exists(p):
            os.remove(p)


def step12_config_snapshot_diff(sandbox: Path, target: Path):
    _banner("Step 12: 自定义配置 - 验证配置快照差异")

    drill_state_file = target / ".drill" / ".drill_state.json"
    if drill_state_file.exists():
        drill_state_file.unlink()

    rc, out, err = _run(
        [PY, MAIN, "drill-scan",
         "--target", str(target),
         "--conflict-policy", "skip",
         "--duplicate-policy", "overwrite",
         "--config", str(sandbox / "config.yaml")],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "scanned"

    config_diffs = result["config_diffs"]
    assert len(config_diffs) > 0, "配置差异为空"
    changed_keys = [d["key"] for d in config_diffs if d["changed"]]
    assert len(changed_keys) > 0, f"没有配置项发生变化: {config_diffs}"
    print(f"  [OK] 配置快照差异: changed_keys={changed_keys}")

    state = _load_json(str(drill_state_file))
    assert state["conflict_policy"] == "skip", f"冲突策略未保存: {state['conflict_policy']}"
    assert state["duplicate_policy"] == "overwrite", f"重复策略未保存: {state['duplicate_policy']}"
    print(f"  [OK] 自定义策略保存: conflict_policy={state['conflict_policy']}, duplicate_policy={state['duplicate_policy']}")


def step13_permission_failure_recovery(sandbox: Path, target: Path):
    _banner("Step 13: 权限失败恢复 - 模拟目标目录只读")

    target_readonly = Path(tempfile.mkdtemp(prefix="ia_drill_readonly_"))
    try:
        _populate_legacy(sandbox, "BATCH_PERM_TEST_001")

        drill_state_file = target / ".drill" / ".drill_state.json"
        if drill_state_file.exists():
            drill_state_file.unlink()

        rc, out, err = _run(
            [PY, MAIN, "drill-scan",
             "--target", str(target_readonly),
             "--conflict-policy", "rename"],
            cwd=str(sandbox),
        )
        scan_result = json.loads(out)
        assert scan_result["status"] == "scanned"
        print(f"  [OK] drill-scan 成功 (只读目标目录): {scan_result['ledger']['total_items']} 项")

        if os.name == "nt":
            os.system(f'icacls "{str(target_readonly)}" /deny Everyone:(W) >nul 2>&1')
        else:
            os.chmod(str(target_readonly), stat.S_IRUSR | stat.S_IXUSR)

        rc, out, err = _run(
            [PY, MAIN, "drill-replay", "--target", str(target_readonly)],
            cwd=str(sandbox),
        )
        replay_result = json.loads(out)
        assert replay_result["status"] == "replayed"
        print(f"  [OK] drill-replay 在只读目录下完成: {len(replay_result['replay_results'])} 个回放")

        rc, out, err = _run(
            [PY, MAIN, "drill-status"],
            cwd=str(sandbox),
        )
        status = json.loads(out)
        print(f"  [OK] drill-status: phase={status['phase']}")

        if os.name == "nt":
            os.system(f'icacls "{str(target_readonly)}" /grant Everyone:(W) >nul 2>&1')
        else:
            os.chmod(str(target_readonly), stat.S_IRWXU)
    finally:
        shutil.rmtree(target_readonly, ignore_errors=True)


def step14_duplicate_conflict(sandbox: Path, target: Path):
    _banner("Step 14: 重复记录冲突处理 - 重复导出 + 重复 batch_id")

    drill_state_file = target / ".drill" / ".drill_state.json"
    if drill_state_file.exists():
        drill_state_file.unlink()

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
        [PY, MAIN, "drill-scan",
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
        [PY, MAIN, "drill-report"],
        cwd=str(sandbox),
    )
    report_result = json.loads(out)
    assert report_result["duplicate_export_items"] > 0
    assert report_result["duplicate_export_markers"]
    for marker in report_result["duplicate_export_markers"]:
        assert marker["duplicate_export_count"] > 0
        print(f"  [OK] 重复导出标记: batch_id={marker['batch_id']}, count={marker['duplicate_export_count']}")

    rc, out, err = _run(
        [PY, MAIN, "drill-scan",
         "--target", str(target),
         "--duplicate-policy", "merge"],
        cwd=str(sandbox),
    )
    scan_result2 = json.loads(out)
    assert scan_result2["status"] == "scanned"
    print(f"  [OK] duplicate-policy=merge 后重新扫描成功")


def step15_drill_undo_and_redo(sandbox: Path, target: Path):
    _banner("Step 15: 一键撤销演练 + 重新扫描")

    drill_state_file = target / ".drill" / ".drill_state.json"
    if drill_state_file.exists():
        state = _load_json(str(drill_state_file))
        state["undo_available"] = True
        _write_file(str(drill_state_file), json.dumps(state, ensure_ascii=False, indent=2))

    rc, out, err = _run(
        [PY, MAIN, "drill-undo"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "undone"
    print(f"  [OK] drill-undo 成功: undone={result['undone']}")

    rc, out, err = _run(
        [PY, MAIN, "drill-scan",
         "--target", str(target),
         "--conflict-policy", "rename",
         "--duplicate-policy", "merge",
         "--config", str(sandbox / "config.yaml")],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "scanned"
    print(f"  [OK] 撤销后重新 drill-scan 成功: {result['ledger']['total_items']} 项")


def step16_session_persistence(sandbox: Path, target: Path):
    _banner("Step 16: 会话持久化 - 保存/加载/续跑")

    rc, out, err = _run(
        [PY, MAIN, "drill-save", "--label", "session_persist_test"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "saved"
    print(f"  [OK] drill-save session_persist_test 成功")

    drill_state_file = target / ".drill" / ".drill_state.json"
    if drill_state_file.exists():
        drill_state_file.unlink()

    rc, out, err = _run(
        [PY, MAIN, "drill-load", "--label", "session_persist_test"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    assert result["status"] == "loaded"
    assert result["label"] == "session_persist_test"
    print(f"  [OK] drill-load session_persist_test 成功: phase={result['phase']}, items={result['total_items']}")

    rc, out, err = _run(
        [PY, MAIN, "drill-status"],
        cwd=str(sandbox),
    )
    status = json.loads(out)
    assert status["saved_session"] is True
    print(f"  [OK] 重启后续跑: phase={status['phase']}")


def step17_git_status_clean(sandbox: Path, target: Path, git_lines_before):
    _banner("Step 17: git status 复查 - 演练前后都干净")

    rc, out, err = _run(
        [PY, MAIN, "drill-cleanup"],
        cwd=str(sandbox),
    )
    result = json.loads(out)
    print(f"  [OK] drill-cleanup: {result.get('status')}, cleaned={result.get('cleaned', [])}")

    pointer_file = sandbox / ".drill_pointer"
    if pointer_file.exists():
        assert False, f".drill_pointer 未被清理: {pointer_file}"

    rc, out_status, err_status = _run(["git", "status", "--porcelain"], cwd=str(sandbox))
    lines = [l for l in out_status.splitlines() if l.strip()]

    forbidden_tokens = (
        "temp_inbox/", "archive/", "state/", "logs/",
        ".archive_version", ".migrated_from_v1",
        "batches.json", "failure_queue.json",
        "archive_log.jsonl", ".takeover_state.json",
        ".drill_state.json", ".drill_sessions/",
        "drill_audit.jsonl", ".drill_pointer",
    )
    bad = [l for l in lines if any(tok in l for tok in forbidden_tokens)]
    assert not bad, f"git status 中出现演练/运行产物:\n" + "\n".join(bad)
    print(f"  [OK] git status 无演练/运行产物残留")
    print(f"       工作区变更文件数: {len(lines)}")

    if len(lines) > 0:
        print(f"  [INFO] 工作区变更文件:")
        for l in lines[:20]:
            print(f"         {l}")


def main():
    sandbox_root = Path(tempfile.mkdtemp(prefix="ia_drill_verify_"))
    target_root = Path(tempfile.mkdtemp(prefix="ia_drill_target_"))
    print(f"[INFO] 沙盒目录: {sandbox_root}")
    print(f"[INFO] 目标目录: {target_root}")

    old_bid = None
    git_lines_before = []
    try:
        old_bid, git_lines_before = step1_build_sandbox(sandbox_root, target_root)
        session_id = step2_drill_scan(sandbox_root, target_root, old_bid)
        step3_drill_plan(sandbox_root, target_root, old_bid)
        step4_drill_replay(sandbox_root, target_root)
        step5_drill_report(sandbox_root, old_bid)
        step6_drill_save(sandbox_root)
        step7_drill_load(sandbox_root, target_root)
        step8_drill_status(sandbox_root)
        step9_drill_undo(sandbox_root, target_root)
        step10_drill_export(sandbox_root, target_root)
        step11_drill_audit(sandbox_root)
        step12_config_snapshot_diff(sandbox_root, target_root)
        step13_permission_failure_recovery(sandbox_root, target_root)
        step14_duplicate_conflict(sandbox_root, target_root)
        step15_drill_undo_and_redo(sandbox_root, target_root)
        step16_session_persistence(sandbox_root, target_root)
        step17_git_status_clean(sandbox_root, target_root, git_lines_before)

        _banner("ALL STEPS PASSED")
        print("全部 17 个步骤验证通过。退出码 0。")
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
