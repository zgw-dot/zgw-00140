import json
import os
import shutil
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


LEGACY_DIR_NAMES = ("temp_inbox", "archive", "state", "logs")
LEGACY_STATE_FILES = ("batches.json", "failure_queue.json")
LEGACY_LOG_FILES = ("archive_log.jsonl",)
VERSION_MARKER = ".archive_version"
MIGRATION_MARKER = ".migrated_from_v1"


@dataclass
class LegacyDetection:
    source_root: str
    detected_dirs: Dict[str, str]
    detected_files: Dict[str, str]
    has_legacy_data: bool

    def as_report(self) -> Dict:
        return {
            "source_root": self.source_root,
            "has_legacy_data": self.has_legacy_data,
            "legacy_dirs": self.detected_dirs,
            "legacy_files": self.detected_files,
        }


@dataclass
class MigrationPlan:
    needs_migration: bool
    detection: LegacyDetection
    items_to_move: List[Dict] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)


@dataclass
class MigrationResult:
    success: bool
    migrated_items: int
    skipped_items: int
    errors: List[str] = field(default_factory=list)
    details: Dict = field(default_factory=dict)


def detect_legacy_layout(source_root: str) -> LegacyDetection:
    source_root = os.path.abspath(source_root)
    detected_dirs: Dict[str, str] = {}
    detected_files: Dict[str, str] = {}

    for name in LEGACY_DIR_NAMES:
        p = os.path.join(source_root, name)
        if os.path.isdir(p) and _has_contents(p):
            detected_dirs[name] = p

    state_dir = os.path.join(source_root, "state")
    for fname in LEGACY_STATE_FILES:
        fp = os.path.join(state_dir, fname)
        if os.path.isfile(fp) and os.path.getsize(fp) > 0:
            detected_files[fname] = fp

    log_dir = os.path.join(source_root, "logs")
    for fname in LEGACY_LOG_FILES:
        fp = os.path.join(log_dir, fname)
        if os.path.isfile(fp) and os.path.getsize(fp) > 0:
            detected_files[fname] = fp

    has_legacy = bool(detected_dirs) or bool(detected_files)
    return LegacyDetection(
        source_root=source_root,
        detected_dirs=detected_dirs,
        detected_files=detected_files,
        has_legacy_data=has_legacy,
    )


def _has_contents(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    for _, dirs, files in os.walk(path):
        if dirs or files:
            return True
    return False


def check_source_pollution(source_root: str) -> Tuple[bool, List[str], List[str]]:
    detection = detect_legacy_layout(source_root)
    if not detection.has_legacy_data:
        return False, [], []

    warnings: List[str] = []
    resolutions: List[str] = []

    source_root_abs = detection.source_root

    if "temp_inbox" in detection.detected_dirs:
        warnings.append(
            f"源码目录残留收件箱样例/待处理文件: {detection.detected_dirs['temp_inbox']}"
        )
        resolutions.append(
            "方案A: 执行 migrate 命令自动迁移到用户数据目录"
        )
        resolutions.append(
            "方案B: 手动将 temp_inbox/ 内的文件移动到配置的 source_dir，然后删除源码目录下的 temp_inbox/"
        )

    if "archive" in detection.detected_dirs:
        warnings.append(
            f"源码目录残留已归档文件: {detection.detected_dirs['archive']}"
        )
        resolutions.append(
            "方案A: 执行 migrate 命令自动迁移归档目录到用户数据目录（保持批次/文件结构不变）"
        )
        resolutions.append(
            "方案B: 手动将 archive/ 移动到安全位置后再运行，否则这些文件将被误判为源码内容"
        )

    if "state" in detection.detected_dirs or any(
        k in detection.detected_files for k in LEGACY_STATE_FILES
    ):
        warnings.append(
            f"源码目录残留状态数据: {detection.detected_dirs.get('state', os.path.join(source_root_abs, 'state'))}"
        )
        resolutions.append(
            "方案A: 执行 migrate 命令自动迁移 batches.json / failure_queue.json，保留所有 batch_id 和 retry_count"
        )
        resolutions.append(
            "方案B: 确认不再需要历史批次/失败队列后，手动删除 state/ 目录"
        )

    if "logs" in detection.detected_dirs or any(
        k in detection.detected_files for k in LEGACY_LOG_FILES
    ):
        warnings.append(
            f"源码目录残留操作日志: {detection.detected_dirs.get('logs', os.path.join(source_root_abs, 'logs'))}"
        )
        resolutions.append(
            "方案A: 执行 migrate 命令自动迁移 archive_log.jsonl，导出记录可继续查询"
        )
        resolutions.append(
            "方案B: 确认不再需要历史日志后，手动删除 logs/ 目录"
        )

    resolutions.append(
        "运行 `python main.py migrate --apply` 后，会在源码目录写入 .migrated_from_v1 标记，避免重复提示"
    )

    return True, warnings, resolutions


def _read_json_safe(path: str):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_json_atomic(data, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _append_jsonl(src: str, dst: str) -> int:
    if not os.path.isfile(src):
        return 0
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    count = 0
    with open(src, "r", encoding="utf-8") as fin, open(dst, "a", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if line:
                fout.write(line + "\n")
                count += 1
    return count


def _merge_batches(legacy_batches_path: str, new_batches_path: str) -> int:
    legacy = _read_json_safe(legacy_batches_path)
    if not legacy or not isinstance(legacy, list):
        return 0
    existing = _read_json_safe(new_batches_path) or []
    existing_ids = {b.get("batch_id") for b in existing if isinstance(b, dict)}
    merged = list(existing)
    added = 0
    for b in legacy:
        if not isinstance(b, dict):
            continue
        bid = b.get("batch_id")
        if bid and bid in existing_ids:
            continue
        if "files" in b and isinstance(b["files"], list):
            for f in b["files"]:
                if not isinstance(f, dict):
                    continue
                f.setdefault("status", "unknown")
        merged.append(b)
        existing_ids.add(bid)
        added += 1
    if added > 0:
        _write_json_atomic(merged, new_batches_path)
    return added


def _merge_failure_queue(legacy_fq_path: str, new_fq_path: str) -> int:
    legacy = _read_json_safe(legacy_fq_path)
    if not legacy or not isinstance(legacy, list):
        return 0
    existing = _read_json_safe(new_fq_path) or []
    existing_sources = {e.get("source_path") for e in existing if isinstance(e, dict)}
    merged = list(existing)
    added = 0
    for e in legacy:
        if not isinstance(e, dict):
            continue
        sp = e.get("source_path")
        if sp and sp in existing_sources:
            continue
        e.setdefault("retry_count", 0)
        e.setdefault("batch_id", None)
        e.setdefault("created_at", datetime.now().isoformat())
        merged.append(e)
        existing_sources.add(sp)
        added += 1
    if added > 0:
        _write_json_atomic(merged, new_fq_path)
    return added


def _migrate_dir_tree(src: str, dst: str) -> Tuple[int, List[str]]:
    if not os.path.isdir(src):
        return 0, []
    os.makedirs(dst, exist_ok=True)
    moved = 0
    errors: List[str] = []
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_root, exist_ok=True)
        for fname in files:
            s = os.path.join(root, fname)
            d = os.path.join(target_root, fname)
            try:
                if os.path.exists(d):
                    base, ext = os.path.splitext(fname)
                    counter = 1
                    while True:
                        alt = os.path.join(target_root, f"{base}_mig{counter}{ext}")
                        if not os.path.exists(alt):
                            d = alt
                            break
                        counter += 1
                shutil.move(s, d)
                moved += 1
            except OSError as e:
                errors.append(f"{s} -> {d}: {e}")
        for dname in dirs:
            sd = os.path.join(root, dname)
            if not _has_contents(sd):
                try:
                    os.rmdir(sd)
                except OSError:
                    pass
    return moved, errors


def _cleanup_empty_legacy_dirs(source_root: str, detection: LegacyDetection) -> None:
    for name in LEGACY_DIR_NAMES:
        p = os.path.join(source_root, name)
        if os.path.isdir(p):
            _remove_empty_tree(p)


def _remove_empty_tree(path: str) -> None:
    for root, dirs, files in os.walk(path, topdown=False):
        for d in dirs:
            dp = os.path.join(root, d)
            try:
                os.rmdir(dp)
            except OSError:
                pass
    try:
        if not _has_contents(path):
            shutil.rmtree(path)
    except OSError:
        try:
            os.rmdir(path)
        except OSError:
            pass


def _write_marker(source_root: str, result: MigrationResult) -> None:
    marker = os.path.join(source_root, MIGRATION_MARKER)
    with open(marker, "w", encoding="utf-8") as f:
        json.dump({
            "migrated_at": datetime.now().isoformat(),
            "migrated_items": result.migrated_items,
            "errors": result.errors,
        }, f, indent=2, ensure_ascii=False)


def _version_marker_path(source_root: str) -> str:
    return os.path.join(source_root, VERSION_MARKER)


def write_version_marker(source_root: str, version: str = "2.0.0") -> None:
    path = _version_marker_path(source_root)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "version": version,
            "layout": "user_data_separated",
            "updated_at": datetime.now().isoformat(),
        }, f, indent=2, ensure_ascii=False)


def read_version_marker(source_root: str) -> Optional[Dict]:
    path = _version_marker_path(source_root)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def already_migrated(source_root: str) -> bool:
    return os.path.isfile(os.path.join(source_root, MIGRATION_MARKER))


def build_migration_plan(
    source_root: str,
    target_state_dir: str,
    target_log_dir: str,
    target_archive_dir: str,
    target_source_dir: str,
) -> MigrationPlan:
    detection = detect_legacy_layout(source_root)
    if not detection.has_legacy_data or already_migrated(source_root):
        return MigrationPlan(needs_migration=False, detection=detection)

    items: List[Dict] = []
    blockers: List[str] = []

    if "batches.json" in detection.detected_files:
        new_path = os.path.join(target_state_dir, "batches.json")
        items.append({
            "type": "state",
            "kind": "batches.json",
            "from": detection.detected_files["batches.json"],
            "to": new_path,
            "note": "合并批次记录，保留原有 batch_id 不变",
        })

    if "failure_queue.json" in detection.detected_files:
        new_path = os.path.join(target_state_dir, "failure_queue.json")
        items.append({
            "type": "state",
            "kind": "failure_queue.json",
            "from": detection.detected_files["failure_queue.json"],
            "to": new_path,
            "note": "合并失败队列，保留 retry_count 和关联 batch_id",
        })

    if "archive_log.jsonl" in detection.detected_files:
        new_path = os.path.join(target_log_dir, "archive_log.jsonl")
        items.append({
            "type": "log",
            "kind": "archive_log.jsonl",
            "from": detection.detected_files["archive_log.jsonl"],
            "to": new_path,
            "note": "追加迁移日志条目，不覆盖已有记录",
        })

    if "temp_inbox" in detection.detected_dirs:
        items.append({
            "type": "dir",
            "kind": "temp_inbox",
            "from": detection.detected_dirs["temp_inbox"],
            "to": target_source_dir,
            "note": "移动收件箱样例/待处理文件，重名文件自动加 _migN 后缀",
        })

    if "archive" in detection.detected_dirs:
        items.append({
            "type": "dir",
            "kind": "archive",
            "from": detection.detected_dirs["archive"],
            "to": target_archive_dir,
            "note": "移动已归档文件，保持批次目录结构",
        })

    return MigrationPlan(
        needs_migration=bool(items),
        detection=detection,
        items_to_move=items,
        blockers=blockers,
    )


def apply_migration(plan: MigrationPlan) -> MigrationResult:
    if not plan.needs_migration:
        return MigrationResult(
            success=True,
            migrated_items=0,
            skipped_items=0,
            details={"reason": "no migration needed"},
        )

    errors: List[str] = []
    migrated = 0
    skipped = 0
    details: Dict = {"per_kind": {}}

    for item in plan.items_to_move:
        kind = item["kind"]
        src = item["from"]
        dst = item["to"]
        try:
            if kind == "batches.json":
                added = _merge_batches(src, dst)
                migrated += added
                details["per_kind"]["batches.json"] = {
                    "merged_records": added,
                    "note": "batch_id 保持原样",
                }
            elif kind == "failure_queue.json":
                added = _merge_failure_queue(src, dst)
                migrated += added
                details["per_kind"]["failure_queue.json"] = {
                    "merged_records": added,
                    "note": "retry_count / batch_id 保持原样",
                }
            elif kind == "archive_log.jsonl":
                appended = _append_jsonl(src, dst)
                migrated += appended
                details["per_kind"]["archive_log.jsonl"] = {
                    "appended_lines": appended,
                }
            elif kind in ("temp_inbox", "archive"):
                moved, move_errors = _migrate_dir_tree(src, dst)
                migrated += moved
                errors.extend(move_errors)
                details["per_kind"][kind] = {
                    "moved_files": moved,
                    "conflicts_handled": True,
                }
            else:
                skipped += 1
        except Exception as e:
            errors.append(f"{kind} 迁移失败: {src} -> {dst}: {e}")

    source_root = plan.detection.source_root
    _cleanup_empty_legacy_dirs(source_root, plan.detection)

    result = MigrationResult(
        success=not errors or all("已存在" in e for e in errors),
        migrated_items=migrated,
        skipped_items=skipped,
        errors=errors,
        details=details,
    )

    _write_marker(source_root, result)
    write_version_marker(source_root)
    return result


def describe_blockers(detection: LegacyDetection) -> List[str]:
    blocked, warnings, resolutions = check_source_pollution(detection.source_root)
    if not blocked:
        return []
    msgs = []
    msgs.append("=" * 60)
    msgs.append("源码目录检测到运行产物（旧版本遗留），已阻止继续运行")
    msgs.append("=" * 60)
    for w in warnings:
        msgs.append(f"  [警告] {w}")
    msgs.append("")
    msgs.append("处理建议:")
    for i, r in enumerate(resolutions, 1):
        msgs.append(f"  {i}. {r}")
    msgs.append("")
    msgs.append(
        "如已明确了解风险，可使用 --ignore-legacy 强制跳过检查（不推荐，可能污染 git 仓库）"
    )
    return msgs
