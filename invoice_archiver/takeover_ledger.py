import csv
import json
import os
import shutil
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


TAKEOVER_STATE_FILE = ".takeover_state.json"
TAKEOVER_AUDIT_LOG = "takeover_audit.jsonl"

DIR_KINDS = ("temp_inbox", "archive", "state", "logs")
STATE_FILES = ("batches.json", "failure_queue.json")
LOG_FILES = ("archive_log.jsonl",)
EXPORT_FILES = ("export_logs.csv", "export_logs.json")


def _now() -> str:
    return datetime.now().isoformat()


def _file_checksum(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return ""


def _safe_read_json(path: str):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_json_atomic(data, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _extract_batch_ids(batches_path: str) -> List[str]:
    data = _safe_read_json(batches_path)
    if not data or not isinstance(data, list):
        return []
    return [b.get("batch_id", "") for b in data if isinstance(b, dict) and b.get("batch_id")]


def _extract_failure_meta(fq_path: str) -> List[Dict]:
    data = _safe_read_json(fq_path)
    if not data or not isinstance(data, list):
        return []
    result = []
    for e in data:
        if not isinstance(e, dict):
            continue
        result.append({
            "source_path": e.get("source_path", ""),
            "batch_id": e.get("batch_id"),
            "retry_count": e.get("retry_count", 0),
        })
    return result


def _extract_export_history(log_path: str) -> Dict[str, List[str]]:
    if not os.path.isfile(log_path):
        return {}
    history: Dict[str, List[str]] = {}
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            bid = rec.get("batch_id", "")
            if not bid:
                continue
            history.setdefault(bid, []).append(rec.get("action", ""))
    return history


def _detect_duplicate_exports(log_path: str) -> Dict[str, int]:
    history = _extract_export_history(log_path)
    dupes = {}
    for bid, actions in history.items():
        export_count = sum(1 for a in actions if a in ("move", "retry"))
        if export_count > 1:
            dupes[bid] = export_count
    return dupes


def _collect_export_files(log_dir: str) -> List[str]:
    found = []
    if not os.path.isdir(log_dir):
        return found
    for fname in os.listdir(log_dir):
        if fname in EXPORT_FILES or fname.endswith(".csv") or fname.endswith(".jsonl"):
            found.append(os.path.join(log_dir, fname))
    return found


@dataclass
class TakeoverItem:
    batch_id: Optional[str]
    failure_count: int
    source_path: str
    target_path: str
    kind: str
    filename: str
    size: int
    duplicate_export: bool = False
    duplicate_export_count: int = 0
    checksum: str = ""
    status: str = "pending"
    conflict: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TakeoverAuditEntry:
    timestamp: str
    action: str
    detail: str
    file_path: Optional[str] = None
    batch_id: Optional[str] = None
    success: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TakeoverLedgerState:
    source_root: str
    target_root: str
    config_path: Optional[str] = None
    conflict_policy: str = "rename"
    duplicate_policy: str = "merge"
    items: List[Dict] = field(default_factory=list)
    phase: str = "scanned"
    created_at: str = ""
    updated_at: str = ""
    applied_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    undo_available: bool = False
    conflict_resolutions: Dict[str, str] = field(default_factory=dict)
    original_config_paths: Dict[str, str] = field(default_factory=dict)
    snapshot_checksums: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class TakeoverAuditLogger:
    def __init__(self, store_dir: str):
        self.audit_file = os.path.join(store_dir, TAKEOVER_AUDIT_LOG)
        os.makedirs(store_dir, exist_ok=True)

    def log(self, action: str, detail: str, file_path: str = None,
            batch_id: str = None, success: bool = True) -> None:
        entry = TakeoverAuditEntry(
            timestamp=_now(),
            action=action,
            detail=detail,
            file_path=file_path,
            batch_id=batch_id,
            success=success,
        )
        with open(self.audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def load_entries(self) -> List[Dict]:
        if not os.path.isfile(self.audit_file):
            return []
        entries = []
        with open(self.audit_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries

    def export_csv(self, output_path: str) -> str:
        entries = self.load_entries()
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        fields = ["timestamp", "action", "detail", "file_path", "batch_id", "success"]
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for e in entries:
                writer.writerow(e)
        return output_path

    def export_json(self, output_path: str) -> str:
        entries = self.load_entries()
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
        return output_path


class TakeoverLedger:
    def __init__(self, source_root: str, target_root: str,
                 config_path: Optional[str] = None,
                 conflict_policy: str = "rename",
                 duplicate_policy: str = "merge"):
        self.source_root = os.path.abspath(source_root)
        self.target_root = os.path.abspath(target_root)
        self.config_path = config_path
        self.conflict_policy = conflict_policy
        self.duplicate_policy = duplicate_policy
        self._state_path = os.path.join(self.source_root, TAKEOVER_STATE_FILE)
        self._audit: Optional[TakeoverAuditLogger] = None

    def _get_audit(self) -> TakeoverAuditLogger:
        if self._audit is None:
            store_dir = os.path.join(self.source_root, "state") if os.path.isdir(
                os.path.join(self.source_root, "state")) else self.source_root
            self._audit = TakeoverAuditLogger(store_dir)
        return self._audit

    def _target_path_for(self, kind: str) -> str:
        mapping = {
            "temp_inbox": "temp_inbox",
            "archive": "archive",
            "state": "state",
            "logs": "logs",
        }
        return os.path.join(self.target_root, mapping.get(kind, kind))

    def scan(self) -> Dict:
        items: List[Dict] = []
        scan_summary = {
            "source_root": self.source_root,
            "target_root": self.target_root,
            "dirs_found": {},
            "files_found": {},
            "batch_ids": [],
            "failure_meta": [],
            "export_history": {},
            "export_files": [],
            "duplicate_exports": {},
        }

        for kind in DIR_KINDS:
            src_dir = os.path.join(self.source_root, kind)
            if not os.path.isdir(src_dir):
                continue
            has_content = any(
                files or dirs for _, dirs, files in os.walk(src_dir)
            )
            if has_content:
                scan_summary["dirs_found"][kind] = src_dir

        state_dir = os.path.join(self.source_root, "state")
        for fname in STATE_FILES:
            fp = os.path.join(state_dir, fname)
            if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                scan_summary["files_found"][fname] = fp

        log_dir = os.path.join(self.source_root, "logs")
        for fname in LOG_FILES:
            fp = os.path.join(log_dir, fname)
            if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                scan_summary["files_found"][fname] = fp

        export_files = _collect_export_files(log_dir)
        if export_files:
            scan_summary["export_files"] = export_files

        batches_path = os.path.join(state_dir, "batches.json")
        if os.path.isfile(batches_path):
            scan_summary["batch_ids"] = _extract_batch_ids(batches_path)

        fq_path = os.path.join(state_dir, "failure_queue.json")
        failure_meta = []
        if os.path.isfile(fq_path):
            failure_meta = _extract_failure_meta(fq_path)
            scan_summary["failure_meta"] = failure_meta

        log_path = os.path.join(log_dir, "archive_log.jsonl")
        if os.path.isfile(log_path):
            scan_summary["export_history"] = _extract_export_history(log_path)
            scan_summary["duplicate_exports"] = _detect_duplicate_exports(log_path)

        batch_failure_counts: Dict[str, int] = {}
        for fm in failure_meta:
            bid = fm.get("batch_id")
            if bid:
                batch_failure_counts[bid] = batch_failure_counts.get(bid, 0) + 1

        duplicate_exports = scan_summary["duplicate_exports"]

        for kind in DIR_KINDS:
            src_dir = os.path.join(self.source_root, kind)
            if not os.path.isdir(src_dir):
                continue
            tgt_dir = self._target_path_for(kind)
            for root, dirs, files in os.walk(src_dir):
                for fname in files:
                    src_file = os.path.join(root, fname)
                    rel = os.path.relpath(src_file, src_dir)
                    tgt_file = os.path.join(tgt_dir, rel)

                    batch_id = None
                    failure_count = 0
                    is_dup_export = False
                    dup_export_count = 0

                    if kind == "state" and fname in STATE_FILES:
                        if fname == "batches.json":
                            bid_list = _extract_batch_ids(src_file)
                            if bid_list:
                                batch_id = ", ".join(bid_list)
                        elif fname == "failure_queue.json":
                            meta = _extract_failure_meta(src_file)
                            if meta:
                                failure_count = sum(1 for m in meta if m.get("retry_count", 0) > 0)
                                bids = [m.get("batch_id") for m in meta if m.get("batch_id")]
                                if bids:
                                    batch_id = ", ".join(set(bids))

                    if kind == "logs" and fname == "archive_log.jsonl":
                        history = _extract_export_history(src_file)
                        if history:
                            export_bids = list(history.keys())
                            batch_id = ", ".join(export_bids)
                            for bid, count in duplicate_exports.items():
                                is_dup_export = True
                                dup_export_count = max(dup_export_count, count)

                    size = 0
                    try:
                        size = os.path.getsize(src_file)
                    except OSError:
                        pass

                    checksum = _file_checksum(src_file)

                    conflict = None
                    if os.path.exists(tgt_file):
                        conflict = "same_name"

                    items.append({
                        "batch_id": batch_id,
                        "failure_count": failure_count,
                        "source_path": src_file,
                        "target_path": tgt_file,
                        "kind": kind,
                        "filename": fname,
                        "size": size,
                        "duplicate_export": is_dup_export,
                        "duplicate_export_count": dup_export_count,
                        "checksum": checksum,
                        "status": "pending",
                        "conflict": conflict,
                        "error": None,
                    })

        original_config_paths = {}
        if self.config_path and os.path.isfile(self.config_path):
            try:
                from .config import load_config
                cfg = load_config(self.config_path)
                original_config_paths = {
                    "source_dir": cfg.source_dir,
                    "archive_dir": cfg.archive_dir,
                    "state_dir": cfg.state_dir,
                    "log_dir": cfg.log_dir,
                }
            except Exception:
                pass

        snapshot_checksums = {}
        for item in items:
            if item["checksum"]:
                snapshot_checksums[item["source_path"]] = item["checksum"]

        state = TakeoverLedgerState(
            source_root=self.source_root,
            target_root=self.target_root,
            config_path=self.config_path,
            conflict_policy=self.conflict_policy,
            duplicate_policy=self.duplicate_policy,
            items=items,
            phase="scanned",
            created_at=_now(),
            updated_at=_now(),
            original_config_paths=original_config_paths,
            snapshot_checksums=snapshot_checksums,
        )
        self._save_state(state)

        self._get_audit().log("scan", f"接管扫描完成: {len(items)} 项, "
                              f"目录={list(scan_summary['dirs_found'].keys())}, "
                              f"文件={list(scan_summary['files_found'].keys())}")

        return {
            "status": "scanned",
            "summary": scan_summary,
            "ledger": {
                "total_items": len(items),
                "items_with_batch_id": sum(1 for i in items if i.get("batch_id")),
                "items_with_failures": sum(1 for i in items if i.get("failure_count", 0) > 0),
                "items_with_duplicate_export": sum(1 for i in items if i.get("duplicate_export")),
                "items_with_conflict": sum(1 for i in items if i.get("conflict")),
            },
            "items": items,
            "has_conflicts": any(i.get("conflict") for i in items),
        }

    def confirm(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到扫描状态，请先执行 takeover-scan"}

        if state.phase != "scanned":
            return {"status": "error", "message": f"当前阶段={state.phase}，无法确认，需先扫描"}

        conflicts = [i for i in state.items if i.get("conflict")]
        unresolved = [
            i for i in conflicts
            if i["source_path"] not in state.conflict_resolutions
        ]
        if unresolved:
            return {
                "status": "error",
                "message": f"存在 {len(unresolved)} 个未解决的冲突，请先用 takeover-resolve 设置策略",
                "unresolved_conflicts": [i["source_path"] for i in unresolved],
            }

        duplicate_items = [i for i in state.items if i.get("duplicate_export")]
        if duplicate_items and state.duplicate_policy == "ask":
            return {
                "status": "error",
                "message": f"存在 {len(duplicate_items)} 个重复导出记录，请先用 takeover-resolve --duplicate-policy 设置策略",
            }

        ledger = self._build_ledger_report(state)

        self._get_audit().log("confirm",
                              f"接管清单确认: {len(state.items)} 项, "
                              f"冲突={len(conflicts)}, 重复导出={len(duplicate_items)}")

        return {
            "status": "confirmed",
            "ledger": ledger,
            "items_count": len(state.items),
            "message": "确认接管清单。执行 takeover-apply 开始接管。",
        }

    def _build_ledger_report(self, state: TakeoverLedgerState) -> Dict:
        by_kind = {}
        by_batch = {}
        all_batch_ids = set()

        for i in state.items:
            k = i.get("kind", "unknown")
            by_kind.setdefault(k, []).append(i)
            bid = i.get("batch_id")
            if bid:
                for b in bid.split(", "):
                    b = b.strip()
                    if b:
                        all_batch_ids.add(b)
                        by_batch.setdefault(b, []).append(i)

        ledger = {
            "source_root": state.source_root,
            "target_root": state.target_root,
            "total_items": len(state.items),
            "all_batch_ids": sorted(all_batch_ids),
            "by_kind_summary": {},
            "by_batch_summary": {},
            "conflict_policy": state.conflict_policy,
            "duplicate_policy": state.duplicate_policy,
        }

        for k, items in by_kind.items():
            ledger["by_kind_summary"][k] = {
                "count": len(items),
                "with_batch_id": sum(1 for i in items if i.get("batch_id")),
                "with_failure_count": sum(1 for i in items if i.get("failure_count", 0) > 0),
                "total_failure_count": sum(i.get("failure_count", 0) for i in items),
                "with_duplicate_export": sum(1 for i in items if i.get("duplicate_export")),
                "with_conflict": sum(1 for i in items if i.get("conflict")),
            }

        for bid, items in by_batch.items():
            failure_count = sum(i.get("failure_count", 0) for i in items)
            source_paths = [i["source_path"] for i in items]
            target_paths = [i["target_path"] for i in items]
            ledger["by_batch_summary"][bid] = {
                "item_count": len(items),
                "failure_count": failure_count,
                "sources": source_paths,
                "destinations": target_paths,
                "duplicate_export": any(i.get("duplicate_export") for i in items),
            }

        return ledger

    def apply(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到扫描状态，请先执行 takeover-scan"}

        if state.phase == "applied":
            return {"status": "already_applied", "message": "接管已完成，可用 takeover-undo 撤销"}
        if state.phase == "undone":
            return {"status": "already_undone", "message": "接管已撤销，请重新 takeover-scan"}

        if state.phase not in ("scanned", "partial"):
            return {"status": "error", "message": f"当前阶段={state.phase}，无法执行接管"}

        applied = 0
        skipped = 0
        failed = 0
        details = []

        for i in state.items:
            if i.get("status") == "applied":
                applied += 1
                continue
            if i.get("status") == "skipped":
                skipped += 1
                continue

            src = i["source_path"]
            dst = i["target_path"]

            if not os.path.exists(src):
                i["status"] = "skipped"
                i["error"] = "源文件不存在"
                skipped += 1
                details.append({"source": src, "target": dst, "status": "skipped",
                                "reason": "source_missing"})
                continue

            if os.path.exists(dst):
                resolution = state.conflict_resolutions.get(src, state.conflict_policy)
                if resolution == "skip":
                    i["status"] = "skipped"
                    i["error"] = "目标已存在，策略=跳过"
                    skipped += 1
                    details.append({"source": src, "target": dst, "status": "skipped",
                                    "reason": "conflict_skip"})
                    continue
                elif resolution == "rename":
                    base, ext = os.path.splitext(dst)
                    counter = 1
                    while True:
                        alt = f"{base}_tkov{counter}{ext}"
                        if not os.path.exists(alt):
                            dst = alt
                            i["target_path"] = dst
                            break
                        counter += 1
                elif resolution == "overwrite":
                    try:
                        os.remove(dst)
                    except OSError as ex:
                        i["status"] = "failed"
                        i["error"] = f"无法覆盖目标文件: {ex}"
                        failed += 1
                        details.append({"source": src, "target": dst, "status": "failed",
                                        "reason": str(ex)})
                        self._get_audit().log("apply_failed",
                                              f"无法覆盖: {dst} -> {ex}",
                                              file_path=dst, success=False)
                        continue

            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                try:
                    os.remove(src)
                except OSError:
                    pass
                i["status"] = "applied"
                applied += 1
                details.append({"source": src, "target": dst, "status": "applied"})
                self._get_audit().log("apply", f"接管: {src} -> {dst}",
                                      file_path=src,
                                      batch_id=i.get("batch_id"))
            except PermissionError as ex:
                i["status"] = "failed"
                i["error"] = f"权限不足: {ex}"
                failed += 1
                details.append({"source": src, "target": dst, "status": "failed",
                                "reason": "permission_denied"})
                self._get_audit().log("apply_failed", f"权限不足: {src} -> {ex}",
                                      file_path=src, success=False)
            except OSError as ex:
                i["status"] = "failed"
                i["error"] = str(ex)
                failed += 1
                details.append({"source": src, "target": dst, "status": "failed",
                                "reason": str(ex)})
                self._get_audit().log("apply_failed", f"接管失败: {src} -> {ex}",
                                      file_path=src, success=False)

        all_done = all(i.get("status") in ("applied", "skipped") for i in state.items)
        any_failed = any(i.get("status") == "failed" for i in state.items)

        if all_done and not any_failed:
            state.phase = "applied"
            self._switch_config_to_target(state)
        elif any_failed:
            state.phase = "partial"
        else:
            state.phase = "partial"

        state.applied_count = applied
        state.skipped_count = skipped
        state.failed_count = failed
        state.updated_at = _now()
        state.undo_available = applied > 0
        self._save_state(state)

        return {
            "status": state.phase,
            "applied": applied,
            "skipped": skipped,
            "failed": failed,
            "total": len(state.items),
            "details": details,
        }

    def _switch_config_to_target(self, state: TakeoverLedgerState) -> None:
        if not state.config_path or not os.path.isfile(state.config_path):
            return

        new_paths = {
            "source_dir": os.path.join(state.target_root, "temp_inbox"),
            "archive_dir": os.path.join(state.target_root, "archive"),
            "state_dir": os.path.join(state.target_root, "state"),
            "log_dir": os.path.join(state.target_root, "logs"),
        }

        try:
            from .config import load_config, save_config
            cfg = load_config(state.config_path)
            for key, val in new_paths.items():
                setattr(cfg, key, val)
            save_config(cfg, state.config_path)
            self._get_audit().log("config_switch",
                                  f"配置已切换到目标目录: {new_paths}")
        except Exception as ex:
            self._get_audit().log("config_switch_failed",
                                  f"配置切换失败: {ex}", success=False)

    def undo(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到接管状态"}

        if not state.undo_available:
            return {"status": "error", "message": "无可撤销的接管操作"}

        undone = 0
        failed_undo = 0
        details = []

        for i in reversed(state.items):
            if i.get("status") != "applied":
                continue

            dst = i["target_path"]
            if not os.path.exists(dst):
                i["status"] = "undone"
                undone += 1
                details.append({"target": dst, "status": "undone", "reason": "target_missing"})
                continue

            src = i["source_path"]
            try:
                if os.path.exists(src):
                    if os.path.isfile(dst):
                        chk_src = _file_checksum(src)
                        chk_dst = _file_checksum(dst)
                        if chk_src == chk_dst and chk_src:
                            os.remove(dst)
                            i["status"] = "undone"
                            undone += 1
                            details.append({"source": src, "target": dst,
                                            "status": "undone", "reason": "identical_removed"})
                            continue

                    base, ext = os.path.splitext(src)
                    counter = 1
                    alt = src
                    while os.path.exists(alt):
                        alt = f"{base}_undo{counter}{ext}"
                        counter += 1
                    shutil.move(dst, alt)
                    i["status"] = "undone"
                    i["source_path"] = alt
                    undone += 1
                    details.append({"target": dst, "restored_to": alt,
                                    "status": "undone"})
                else:
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    shutil.move(dst, src)
                    i["status"] = "undone"
                    undone += 1
                    details.append({"target": dst, "restored_to": src,
                                    "status": "undone"})

                self._get_audit().log("undo", f"撤销接管: {dst} -> {src}",
                                      file_path=dst,
                                      batch_id=i.get("batch_id"))
            except OSError as ex:
                i["error"] = f"撤销失败: {ex}"
                failed_undo += 1
                details.append({"target": dst, "status": "undo_failed", "reason": str(ex)})
                self._get_audit().log("undo_failed", f"撤销失败: {dst} -> {ex}",
                                      file_path=dst, success=False)

        self._restore_config(state)

        state.phase = "undone" if failed_undo == 0 else "undo_partial"
        state.applied_count = 0
        state.undo_available = False
        state.updated_at = _now()
        self._save_state(state)

        return {
            "status": state.phase,
            "undone": undone,
            "failed": failed_undo,
            "details": details,
        }

    def _restore_config(self, state: TakeoverLedgerState) -> None:
        if not state.config_path or not os.path.isfile(state.config_path):
            return
        if not state.original_config_paths:
            return

        try:
            from .config import load_config, save_config
            cfg = load_config(state.config_path)
            for key, val in state.original_config_paths.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, val)
            save_config(cfg, state.config_path)
            self._get_audit().log("config_restore",
                                  f"配置已恢复到原始路径: {state.original_config_paths}")
        except Exception as ex:
            self._get_audit().log("config_restore_failed",
                                  f"配置恢复失败: {ex}", success=False)

    def status(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {
                "status": "no_state",
                "message": "未找到接管状态，请先执行 takeover-scan",
            }

        items = state.items
        by_status = {}
        by_kind = {}
        for i in items:
            st = i.get("status", "pending")
            by_status[st] = by_status.get(st, 0) + 1
            k = i.get("kind", "unknown")
            by_kind[k] = by_kind.get(k, 0) + 1

        ledger = self._build_ledger_report(state) if state.phase in ("applied", "scanned") else {}

        return {
            "status": "ok",
            "phase": state.phase,
            "source_root": state.source_root,
            "target_root": state.target_root,
            "total_entries": len(items),
            "applied_count": state.applied_count,
            "skipped_count": state.skipped_count,
            "failed_count": state.failed_count,
            "undo_available": state.undo_available,
            "conflict_policy": state.conflict_policy,
            "duplicate_policy": state.duplicate_policy,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "by_status": by_status,
            "by_kind": by_kind,
            "ledger": ledger,
            "conflicts_unresolved": [
                i["source_path"] for i in items
                if i.get("conflict") and i["source_path"] not in state.conflict_resolutions
            ],
            "original_config_paths": state.original_config_paths,
        }

    def set_conflict_resolution(self, source_path: str, resolution: str) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到扫描状态"}

        valid = {"skip", "overwrite", "rename", "ask"}
        if resolution not in valid:
            return {"status": "error", "message": f"无效策略: {resolution}, 可选: {valid}"}

        state.conflict_resolutions[source_path] = resolution
        state.updated_at = _now()
        self._save_state(state)

        self._get_audit().log("conflict_resolution",
                              f"冲突策略设定: {source_path} -> {resolution}",
                              file_path=source_path)

        return {"status": "ok", "source_path": source_path, "resolution": resolution}

    def set_duplicate_policy(self, policy: str) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到扫描状态"}

        valid = {"skip", "merge", "overwrite", "ask"}
        if policy not in valid:
            return {"status": "error", "message": f"无效策略: {policy}, 可选: {valid}"}

        state.duplicate_policy = policy
        state.updated_at = _now()
        self._save_state(state)

        self._get_audit().log("duplicate_policy",
                              f"重复导出策略设定: {policy}")

        return {"status": "ok", "duplicate_policy": policy}

    def export_ledger(self, fmt: str, output_path: str) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到接管状态"}

        items = state.items
        ledger = self._build_ledger_report(state)
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        if fmt == "csv":
            fields = [
                "batch_id", "failure_count", "source_path", "target_path",
                "kind", "filename", "size", "duplicate_export",
                "duplicate_export_count", "checksum", "status",
                "conflict", "error",
            ]
            with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for i in items:
                    writer.writerow(i)
        else:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump({
                    "ledger_summary": ledger,
                    "state": state.to_dict(),
                    "items": items,
                }, f, indent=2, ensure_ascii=False)

        size_kb = round(os.path.getsize(output_path) / 1024, 2) if os.path.isfile(output_path) else 0
        self._get_audit().log("export", f"导出接管台账: {output_path} ({fmt})")

        return {
            "status": "exported",
            "format": fmt,
            "output": os.path.abspath(output_path),
            "size_kb": size_kb,
            "entries_count": len(items),
        }

    def export_audit(self, fmt: str, output_path: str) -> Dict:
        audit = self._get_audit()
        if fmt == "csv":
            audit.export_csv(output_path)
        else:
            audit.export_json(output_path)

        size_kb = round(os.path.getsize(output_path) / 1024, 2) if os.path.isfile(output_path) else 0
        return {
            "status": "exported",
            "format": fmt,
            "output": os.path.abspath(output_path),
            "size_kb": size_kb,
        }

    def resume(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "no_state", "message": "无可续跑的接管状态"}

        if state.phase == "applied":
            return {"status": "already_applied", "message": "接管已全部完成"}
        if state.phase == "undone":
            return {"status": "undone", "message": "接管已撤销"}

        return self.apply()

    def _save_state(self, state: TakeoverLedgerState) -> None:
        _write_json_atomic(state.to_dict(), self._state_path)

    def _load_state(self) -> Optional[TakeoverLedgerState]:
        data = _safe_read_json(self._state_path)
        if data is None:
            return None
        return TakeoverLedgerState(**data)

    def has_previous_state(self) -> bool:
        return os.path.isfile(self._state_path)
