#!/usr/bin/env python3
"""
归档工具 v2.0 全链路验证脚本
================================
覆盖流程:
  1. 构建沙盒并伪造"旧版本"运行产物 (批次/失败队列/日志/收件箱样例)
  2. init 初始化 + detect-legacy 拦截旧版本
  3. migrate --apply 平滑迁移 (保留 batch_id / retry_count)
  4. precheck -> archive 归档新样例
  5. list-batches / list-failures / export-logs 验证链路
  6. rollback 回滚某个批次
  7. "重启"复查: 重新加载 Archiver, 验证状态持久化
  8. git status 确认源码目录干净

用法:
    python verify_full_flow.py

脚本所有操作在系统临时目录下的沙盒中执行，不影响真实项目目录。
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
        err = proc.stderr.decode("utf-8", errors="replace")
        if os.name == "nt":
            out_alt = proc.stdout.decode("mbcs", errors="replace")
            err_alt = proc.stderr.decode("mbcs", errors="replace")
            if "\ufffd" in out and "\ufffd" not in out_alt:
                out = out_alt
            if "\ufffd" in err and "\ufffd" not in err_alt:
                err = err_alt
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


def step1_build_sandbox(sandbox: Path):
    global MAIN
    _banner("Step 1: 构建沙盒，伪造旧版本 (v1.x) 运行产物")

    pkg_src = HERE / "invoice_archiver"
    pkg_dst = sandbox / "invoice_archiver"
    shutil.copytree(pkg_src, pkg_dst)
    shutil.copy(HERE / "main.py", sandbox / "main.py")
    MAIN = str(sandbox / "main.py")

    # 伪造旧版本收件箱样例
    temp_inbox = sandbox / "temp_inbox"
    temp_inbox.mkdir()
    samples = [
        "SUP001_invoice_202605.pdf",
        "SUP002_contract_2026Q2.pdf",
        "SUP003_receipt_A001.xlsx",
    ]
    for s in samples:
        (temp_inbox / s).write_bytes(b"%PDF-1.4 fake invoice " + s.encode())
    print(f"  [OK] 伪造收件箱样例 {len(samples)} 个: {list(samples)}")

    # 伪造旧版本归档目录 (含 1 个供应商 + 1 个批次)
    old_batch_id = "BATCH_20260601_091500_000001"
    archive = sandbox / "archive" / "SUP099" / old_batch_id
    archive.mkdir(parents=True)
    archived_file = archive / "SUP099_report_may.pdf"
    archived_file.write_bytes(b"%PDF-1.4 fake archive SUP099")
    print(f"  [OK] 伪造旧版本归档目录: archive/SUP099/{old_batch_id}/")

    # 伪造旧版本 state/batches.json
    state_dir = sandbox / "state"
    state_dir.mkdir()
    fake_batches = [
        {
            "batch_id": old_batch_id,
            "created_at": (datetime.now() - timedelta(days=2)).isoformat(),
            "committed_at": (datetime.now() - timedelta(days=2)).isoformat(),
            "rolled_back_at": None,
            "status": "committed",
            "files": [
                {
                    "source_path": str(temp_inbox / "_old_SUP099.pdf"),
                    "target_path": str(archived_file),
                    "filename": "SUP099_report_may.pdf",
                    "supplier_code": "SUP099",
                    "status": "moved",
                    "error": None,
                }
            ],
        }
    ]
    (state_dir / "batches.json").write_text(
        json.dumps(fake_batches, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  [OK] 伪造旧版本 batches.json: 包含 {old_batch_id}")

    # 伪造旧版本 state/failure_queue.json (保留 retry_count)
    fake_failures = [
        {
            "source_path": str(temp_inbox / "SUP999_bad_name.pdf"),
            "target_path": str(sandbox / "archive" / "SUP999" / "missing_dir" / "x.pdf"),
            "supplier_code": "SUP999",
            "error": "目标目录不存在，且无法创建 (权限不足模拟)",
            "retry_count": 3,
            "last_retry_at": (datetime.now() - timedelta(hours=5)).isoformat(),
            "batch_id": old_batch_id,
            "created_at": (datetime.now() - timedelta(days=1)).isoformat(),
        }
    ]
    (state_dir / "failure_queue.json").write_text(
        json.dumps(fake_failures, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  [OK] 伪造旧版本 failure_queue.json: 1 条记录, retry_count=3, batch_id={old_batch_id}")

    # 伪造旧版本 logs/archive_log.jsonl
    logs_dir = sandbox / "logs"
    logs_dir.mkdir()
    log_lines = [
        json.dumps({
            "timestamp": (datetime.now() - timedelta(days=2)).isoformat(),
            "source_path": str(temp_inbox / "_old_SUP099.pdf"),
            "target_path": str(archived_file),
            "action": "move",
            "batch_id": old_batch_id,
            "status": "success",
            "error_reason": None,
        }, ensure_ascii=False),
        json.dumps({
            "timestamp": (datetime.now() - timedelta(days=1)).isoformat(),
            "source_path": str(temp_inbox / "SUP999_bad_name.pdf"),
            "target_path": fake_failures[0]["target_path"],
            "action": "move",
            "batch_id": old_batch_id,
            "status": "fail",
            "error_reason": fake_failures[0]["error"],
        }, ensure_ascii=False),
    ]
    (logs_dir / "archive_log.jsonl").write_text(
        "\n".join(log_lines) + "\n", encoding="utf-8"
    )
    print(f"  [OK] 伪造旧版本 archive_log.jsonl: {len(log_lines)} 条")

    return old_batch_id, samples


def step2_init_and_detect(sandbox: Path):
    _banner("Step 2: init 初始化 + detect-legacy 拦截旧版本")

    # 运行 detect-legacy 前先确保不被 init 过
    rc, out, err = _run(
        [PY, MAIN, "detect-legacy"],
        cwd=str(sandbox), check=False,
    )
    assert rc == 2, f"detect-legacy 期望 rc=2 (有遗留)，实际 rc={rc}\nSTDOUT:{out}\nSTDERR:{err}"
    assert "源码目录检测到运行产物" in err, f"缺少拦截提示: {err[:500]}"
    print("  [OK] detect-legacy 正确拦截了旧版本运行产物 (rc=2)")

    # 运行 init (会覆盖 config.yaml，写入 .archive_version；但因为有 legacy
    # 没迁移，所以后续 precheck 仍应被拦截)
    rc, out, err = _run([PY, MAIN, "init"], cwd=str(sandbox))
    data = json.loads(out)
    assert data["status"] == "initialized"
    assert data["version"].startswith("2.")
    assert (sandbox / ".archive_version").exists()
    print(f"  [OK] init 成功，runtime_paths = {data['runtime_paths']}")

    # 读取新生成的 config.yaml 供后续步骤
    cfg_path = sandbox / "config.yaml"
    assert cfg_path.exists()
    print(f"  [OK] 新配置文件已生成: {cfg_path.name}")


def step3_migrate(sandbox: Path, expected_old_batch: str):
    _banner("Step 3: migrate --apply 平滑迁移 (保留 batch_id / retry_count)")

    # 先预览
    rc, out, err = _run([PY, MAIN, "migrate"], cwd=str(sandbox))
    preview = json.loads(out)
    assert preview["status"] == "plan_ready"
    assert preview["apply"] is False
    kinds = {item["kind"] for item in preview["items"]}
    assert {"batches.json", "failure_queue.json", "archive_log.jsonl",
            "temp_inbox", "archive"}.issubset(kinds), f"迁移项缺失: {kinds}"
    print(f"  [OK] 迁移预览包含: {sorted(kinds)}")

    # 执行迁移
    rc, out, err = _run([PY, MAIN, "migrate", "--apply"], cwd=str(sandbox))
    result = json.loads(out)
    assert result["success"] is True, f"迁移失败: {result}"
    assert result["migrated_items"] >= 6, f"迁移条目过少: {result['migrated_items']}"
    assert (sandbox / ".migrated_from_v1").exists()
    print(f"  [OK] migrate --apply 成功, 迁移 {result['migrated_items']} 项")

    # 验证 state_dir 中的 batches.json 保留了旧 batch_id
    from invoice_archiver.config import load_config
    cfg = load_config(str(sandbox / "config.yaml"))
    batches = _load_json(Path(cfg.state_dir) / "batches.json")
    ids = [b["batch_id"] for b in batches]
    assert expected_old_batch in ids, f"旧 batch_id 丢失! 期望 {expected_old_batch}, 实际 {ids}"
    print(f"  [OK] 迁移后 batches.json 仍含旧 batch_id = {expected_old_batch}")

    # 验证 failure_queue 保留了 retry_count 和 batch_id
    fq = _load_json(Path(cfg.state_dir) / "failure_queue.json")
    assert len(fq) >= 1
    matching = [f for f in fq if f.get("batch_id") == expected_old_batch]
    assert matching, f"旧 batch_id={expected_old_batch} 的失败记录未找到: {fq}"
    f0 = matching[0]
    assert f0["retry_count"] == 3, f"retry_count 丢失: {f0}"
    print(f"  [OK] 迁移后失败队列保留 retry_count={f0['retry_count']}, batch_id={f0['batch_id']}")

    # 验证 temp_inbox 样例文件已移动
    src_dir = Path(cfg.source_dir)
    moved = [p.name for p in src_dir.iterdir() if p.is_file()]
    assert len(moved) >= 3, f"收件箱样例没搬过去: {list(src_dir.glob('*'))}"
    print(f"  [OK] 迁移后收件箱样例已在用户目录: {sorted(moved)}")

    # 验证旧归档目录已搬走
    archive_dir = Path(cfg.archive_dir)
    assert (archive_dir / "SUP099" / expected_old_batch).is_dir(), \
        "旧归档目录未迁移到用户数据目录"
    print(f"  [OK] 旧归档目录已迁移到用户数据目录")

    # 验证源码目录下的 temp_inbox / archive / state / logs 都已清空
    for name in ("temp_inbox", "archive", "state", "logs"):
        p = sandbox / name
        if p.exists():
            contents = list(p.rglob("*"))
            assert not contents, f"{name}/ 仍有残留: {contents}"
    print("  [OK] 源码目录下的旧运行目录已清空")


def step4_precheck_archive(sandbox: Path):
    _banner("Step 4: precheck -> archive 归档新样例")

    from invoice_archiver.config import load_config
    cfg = load_config(str(sandbox / "config.yaml"))

    # 放入几个可归档的文件
    src_dir = Path(cfg.source_dir)
    new_samples = [
        "SUP100_invoice_202606.pdf",
        "SUP101_acceptance_form.png",
        "SUP102_contract_final.docx",
    ]
    for s in new_samples:
        (src_dir / s).write_bytes(b"fake " + s.encode())
    print(f"  [INFO] 放入收件箱 {len(new_samples)} 个样例: {new_samples}")

    # precheck
    rc, out, err = _run([PY, MAIN, "precheck"], cwd=str(sandbox))
    result = json.loads(out)
    assert result["would_archive"] == 3, f"precheck 数量不对: {result}"
    print(f"  [OK] precheck: would_archive={result['would_archive']}, would_skip={result['would_skip']}")

    # archive
    rc, out, err = _run([PY, MAIN, "archive"], cwd=str(sandbox))
    result = json.loads(out)
    assert result["archived"] == 3, f"archive 失败: {result}"
    assert result["failed"] == 0
    new_bid = result["batch_id"]
    assert new_bid and new_bid.startswith("BATCH_"), f"batch_id 无效: {new_bid}"
    print(f"  [OK] archive 成功: batch_id={new_bid}, archived={result['archived']}")

    # 验证归档目录
    for code in ("SUP100", "SUP101", "SUP102"):
        d = Path(cfg.archive_dir) / code / new_bid
        assert d.is_dir(), f"归档目录缺失: {d}"
        files = list(d.glob("*"))
        assert len(files) == 1, f"每供应商每批次应有 1 个文件: {d} -> {files}"
    print("  [OK] 归档目录结构正确 (供应商/批次两级)")

    return new_bid


def step5_list_and_export(sandbox: Path, expected_old_batch, expected_new_batch):
    _banner("Step 5: list-batches / list-failures / export-logs")

    # list-batches
    rc, out, err = _run([PY, MAIN, "list-batches"], cwd=str(sandbox))
    data = json.loads(out)
    ids = data["summary"]["batch_ids"]
    assert expected_old_batch in ids, f"旧批次丢失: {ids}"
    assert expected_new_batch in ids, f"新批次丢失: {ids}"
    assert data["summary"]["total"] >= 2
    print(f"  [OK] list-batches: total={data['summary']['total']}, 状态分布={data['summary']['by_status']}")

    # list-failures
    rc, out, err = _run([PY, MAIN, "list-failures"], cwd=str(sandbox))
    data = json.loads(out)
    assert data["total"] >= 1
    assert data["with_retry_count_gt0"] >= 1
    print(f"  [OK] list-failures: total={data['total']}, 已重试过={data['with_retry_count_gt0']}")

    # export-logs csv (应成功)
    export_csv = Path(tempfile.gettempdir()) / f"verify_export_{os.getpid()}.csv"
    if export_csv.exists():
        export_csv.unlink()
    rc, out, err = _run(
        [PY, MAIN, "export-logs", "--format", "csv", "--output", str(export_csv)],
        cwd=str(sandbox),
    )
    resp = json.loads(out)
    assert resp["status"] == "exported"
    assert resp["format"] == "csv"
    assert Path(resp["output"]).is_file()
    assert Path(resp["output"]).stat().st_size > 0
    print(f"  [OK] export-logs csv: size={resp['size_kb']} KB, 路径={resp['output']}")

    # 再次导出同一路径 (不带 --force 应失败 rc=4)
    rc, out, err = _run(
        [PY, MAIN, "export-logs", "--format", "csv", "--output", str(export_csv)],
        cwd=str(sandbox), check=False,
    )
    assert rc == 4, f"重复导出应 rc=4, 实际 rc={rc}\nSTDERR:{err}"
    assert "导出路径已被占用" in err
    print("  [OK] 导出路径占用拦截 (rc=4, 提示清晰)")

    # 带 --force 覆盖
    rc, out, err = _run(
        [PY, MAIN, "export-logs", "--format", "json",
         "--output", str(export_csv), "--force"],
        cwd=str(sandbox),
    )
    resp = json.loads(out)
    assert resp["status"] == "exported"
    assert resp["format"] == "json"
    print(f"  [OK] 加 --force 后强制覆盖导出成功")
    export_csv.unlink(missing_ok=True)


def step6_rollback_and_check_dupes(sandbox: Path, new_bid: str):
    _banner("Step 6: rollback 回滚 + 重复批次/回滚状态保护验证")

    # 不存在的批次
    rc, out, err = _run(
        [PY, MAIN, "rollback", "--batch-id", "BATCH_NOT_EXIST_123"],
        cwd=str(sandbox), check=False,
    )
    assert rc == 1
    assert "批次不存在" in err
    print("  [OK] 回滚不存在的批次 -> 清晰报错")

    # 正常回滚新批次
    rc, out, err = _run([PY, MAIN, "rollback", "--batch-id", new_bid], cwd=str(sandbox))
    resp = json.loads(out)
    assert resp["success"] is True
    assert resp["batch_id"] == new_bid
    assert resp["rolled_back"] >= 1
    print(f"  [OK] rollback 成功: {resp}")

    # 再次回滚同一批次 (应拒绝，状态已 rolled_back)
    rc, out, err = _run(
        [PY, MAIN, "rollback", "--batch-id", new_bid],
        cwd=str(sandbox), check=False,
    )
    assert rc == 1
    assert "不可重复回滚" in err
    print("  [OK] 对已回滚批次再次回滚 -> 清晰拒绝")


def step7_restart_and_verify_persistence(sandbox: Path, expected_old, expected_new):
    _banner('Step 7: "重启"复查 (重新加载 Archiver，验证状态持久化)')

    # 重新加载配置和 Archiver (模拟重启进程)
    from invoice_archiver.config import load_config
    from invoice_archiver.archiver import Archiver

    cfg = load_config(str(sandbox / "config.yaml"))
    fresh = Archiver(cfg)  # 完全新建实例，不复用任何之前的内存对象

    # list-batches 通过新实例
    batches = fresh.list_batches()
    ids = [b["batch_id"] for b in batches]
    assert expected_old in ids, f"重启后旧批次丢失: {ids}"
    assert expected_new in ids, f"重启后新批次丢失: {ids}"
    status_map = {b["batch_id"]: b["status"] for b in batches}
    assert status_map[expected_new] == "rolled_back", \
        f"重启后新批次状态不是 rolled_back: {status_map[expected_new]}"
    print(f"  [OK] 重启后 list-batches 仍可查到: {ids}")
    print(f"  [OK] 重启后状态正确: {expected_old}={status_map[expected_old]}, "
          f"{expected_new}={status_map[expected_new]}")

    # 失败队列持久化
    failures = fresh.list_failures()
    assert len(failures) >= 1
    assert failures[0]["retry_count"] == 3, f"重启后 retry_count 丢失: {failures[0]}"
    print(f"  [OK] 重启后 list-failures 仍可查到，retry_count={failures[0]['retry_count']}")

    # 日志持久化
    entries = fresh.logger.load_entries()
    assert len(entries) >= 4, f"日志条目不够: {len(entries)}"
    bids_in_logs = {e.batch_id for e in entries}
    assert expected_old in bids_in_logs, f"旧批次日志丢失"
    assert expected_new in bids_in_logs, f"新批次日志丢失"
    print(f"  [OK] 重启后日志可查询，共 {len(entries)} 条，涉及批次={bids_in_logs}")


def step8_git_status_clean(sandbox: Path):
    _banner("Step 8: git status 验证源码目录干净")

    # 先 init 一个 git 仓库以便测试
    _run(["git", "init", "-q"], cwd=str(sandbox))
    _run(["git", "add", "-A"], cwd=str(sandbox))
    rc, out, err = _run(["git", "status", "--porcelain"], cwd=str(sandbox))
    lines = [l for l in out.splitlines() if l.strip()]
    # 允许出现的: verify_full_flow.py 不在这里，但 main.py / config.yaml / invoice_archiver/
    # .gitignore 需要存在; 不允许出现 temp_inbox/ archive/ state/ logs/ 下的任何文件
    forbidden_tokens = ("temp_inbox/", "archive/", "state/", "logs/",
                        ".archive_version", ".migrated_from_v1",
                        "batches.json", "failure_queue.json",
                        "archive_log.jsonl")
    bad = [l for l in lines if any(tok in l for tok in forbidden_tokens)]
    assert not bad, f"git status 中出现运行产物 (应被 .gitignore 过滤):\n" + "\n".join(bad)

    print(f"  [OK] git status --porcelain 未发现运行产物残留")
    print(f"       跟踪到的文件数: {len(lines)} (含源码/配置/gitignore 本身，属正常)")


def main():
    sandbox_root = Path(tempfile.mkdtemp(prefix="ia_v2_verify_"))
    print(f"[INFO] 沙盒目录: {sandbox_root}")

    old_bid = None
    new_bid = None
    try:
        old_bid, _ = step1_build_sandbox(sandbox_root)
        step2_init_and_detect(sandbox_root)
        step3_migrate(sandbox_root, old_bid)
        new_bid = step4_precheck_archive(sandbox_root)
        step5_list_and_export(sandbox_root, old_bid, new_bid)
        step6_rollback_and_check_dupes(sandbox_root, new_bid)
        step7_restart_and_verify_persistence(sandbox_root, old_bid, new_bid)
        step8_git_status_clean(sandbox_root)

        _banner("ALL STEPS PASSED")
        print("全部 8 个步骤验证通过。退出码 0。")
        return 0
    finally:
        try:
            shutil.rmtree(sandbox_root, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
