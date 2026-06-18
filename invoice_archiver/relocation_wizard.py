import csv
import json
import os
import shutil
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


WIZARD_STATE_FILE = ".wizard_state.json"
WIZARD_AUDIT_LOG = "wizard_audit.jsonl"

DIR_KINDS = ("temp_inbox", "archive", "state", "logs")
STATE_FILES = ("batches.json", "failure_queue.json")
LOG_FILES = ("archive_log.jsonl",)
EXPORT_FILES = ("export_logs.csv", "export_logs.json")


@dataclass
class WizardFileEntry:
    source_path: str
    target_path: str
    kind: str
    filename: str
    size: int
    checksum: str = ""
    batch_id: Optional[str] = None
    retry_count: Optional[int] = None
    export_history: Optional[List[str]] = None
    status: str = "pending"
    conflict: Optional[str] = None
    error: Optional[str] = None


@dataclass
class WizardAuditEntry:
    timestamp: str
    action: str
    detail: str
    file_path: Optional[str] = None
    batch_id: Optional[str] = None
    success: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WizardState:
    source_root: str
    target_root: str
    rules_file: Optional[str] = None
    ignore_patterns: List[str] = field(default_factory=list)
    entries: List[Dict] = field(default_factory=list)
    phase: str = "scanned"
    created_at: str = ""
    updated_at: str = ""
    applied_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    undo_available: bool = False
    conflict_resolutions: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


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


def _matches_ignore(filename: str, patterns: List[str]) -> bool:
    for pat in patterns:
        if pat and pat in filename:
            return True
    return False


def _extract_batch_ids_from_batches(batches_path: str) -> List[str]:
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


def _extract_export_history_from_log(log_path: str) -> Dict[str, List[str]]:
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


def _collect_export_files(log_dir: str) -> List[str]:
    found = []
    if not os.path.isdir(log_dir):
        return found
    for fname in os.listdir(log_dir):
        if fname in EXPORT_FILES or fname.endswith(".csv") or fname.endswith(".jsonl"):
            found.append(os.path.join(log_dir, fname))
    return found


class WizardAuditLogger:
    def __init__(self, state_dir: str):
        self.audit_file = os.path.join(state_dir, WIZARD_AUDIT_LOG)
        os.makedirs(state_dir, exist_ok=True)

    def log(self, action: str, detail: str, file_path: str = None,
            batch_id: str = None, success: bool = True) -> None:
        entry = WizardAuditEntry(
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


class RelocationWizard:
    def __init__(self, source_root: str, target_root: str,
                 rules_file: Optional[str] = None,
                 ignore_patterns: Optional[List[str]] = None):
        self.source_root = os.path.abspath(source_root)
        self.target_root = os.path.abspath(target_root)
        self.rules_file = rules_file
        self.ignore_patterns = ignore_patterns or []
        self._rules = self._load_rules(rules_file)
        self._state_path = os.path.join(self.source_root, WIZARD_STATE_FILE)
        self._audit: Optional[WizardAuditLogger] = None

    def _load_rules(self, rules_file: Optional[str]) -> Dict:
        if not rules_file or not os.path.isfile(rules_file):
            return {}
        data = _safe_read_json(rules_file)
        if isinstance(data, dict):
            self.ignore_patterns = data.get("ignore_patterns", self.ignore_patterns)
            return data
        return {}

    def _get_audit(self) -> WizardAuditLogger:
        if self._audit is None:
            audit_dir = os.path.join(self.source_root, "state") if os.path.isdir(
                os.path.join(self.source_root, "state")) else self.source_root
            self._audit = WizardAuditLogger(audit_dir)
        return self._audit

    def _target_subdir(self, kind: str) -> str:
        defaults = {
            "temp_inbox": "temp_inbox",
            "archive": "archive",
            "state": "state",
            "logs": "logs",
        }
        override = self._rules.get("path_mapping", {}) if self._rules else {}
        return override.get(kind, defaults.get(kind, kind))

    def _target_path_for(self, kind: str) -> str:
        return os.path.join(self.target_root, self._target_subdir(kind))

    def scan(self) -> Dict:
        entries: List[Dict] = []
        scan_summary = {
            "source_root": self.source_root,
            "target_root": self.target_root,
            "dirs_found": {},
            "files_found": {},
            "batch_ids": [],
            "failure_meta": [],
            "export_history": {},
            "export_files": [],
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
            scan_summary["batch_ids"] = _extract_batch_ids_from_batches(batches_path)

        fq_path = os.path.join(state_dir, "failure_queue.json")
        if os.path.isfile(fq_path):
            scan_summary["failure_meta"] = _extract_failure_meta(fq_path)

        log_path = os.path.join(log_dir, "archive_log.jsonl")
        if os.path.isfile(log_path):
            scan_summary["export_history"] = _extract_export_history_from_log(log_path)

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
                    if _matches_ignore(fname, self.ignore_patterns):
                        continue

                    batch_id = None
                    retry_count = None
                    export_history = None

                    if kind == "state" and fname in STATE_FILES:
                        if fname == "batches.json":
                            bid_list = _extract_batch_ids_from_batches(src_file)
                            if bid_list:
                                batch_id = ", ".join(bid_list)
                        elif fname == "failure_queue.json":
                            meta = _extract_failure_meta(src_file)
                            if meta:
                                retry_count = max(
                                    (m.get("retry_count") or 0 for m in meta), default=0
                                )
                                bids = [m.get("batch_id") for m in meta if m.get("batch_id")]
                                if bids:
                                    batch_id = ", ".join(bids)

                    if kind == "logs" and fname == "archive_log.jsonl":
                        export_history = list(
                            _extract_export_history_from_log(src_file).keys()
                        )

                    size = 0
                    try:
                        size = os.path.getsize(src_file)
                    except OSError:
                        pass

                    conflict = None
                    if os.path.exists(tgt_file):
                        conflict = "same_name"

                    entries.append({
                        "source_path": src_file,
                        "target_path": tgt_file,
                        "kind": kind,
                        "filename": fname,
                        "size": size,
                        "batch_id": batch_id,
                        "retry_count": retry_count,
                        "export_history": export_history,
                        "status": "pending",
                        "conflict": conflict,
                        "error": None,
                    })

        state = WizardState(
            source_root=self.source_root,
            target_root=self.target_root,
            rules_file=self.rules_file,
            ignore_patterns=self.ignore_patterns,
            entries=entries,
            phase="scanned",
            created_at=_now(),
            updated_at=_now(),
        )
        self._save_state(state)

        self._get_audit().log("scan", f"扫描完成: {len(entries)} 项, "
                              f"目录={list(scan_summary['dirs_found'].keys())}, "
                              f"文件={list(scan_summary['files_found'].keys())}")

        return {
            "status": "scanned",
            "summary": scan_summary,
            "entries_count": len(entries),
            "entries": entries,
            "has_conflicts": any(e.get("conflict") for e in entries),
        }

    def plan(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {
                "status": "no_state",
                "message": "未找到扫描状态，请先执行 wizard-scan",
            }

        entries = state.entries
        if not entries:
            return {
                "status": "empty",
                "message": "扫描结果为空，无需搬家",
            }

        plan_items = []
        conflicts = []
        batch_preservation = []
        failure_preservation = []
        export_preservation = []

        for e in entries:
            item = {
                "source_path": e["source_path"],
                "target_path": e["target_path"],
                "kind": e["kind"],
                "filename": e["filename"],
                "size": e["size"],
                "batch_id": e.get("batch_id"),
                "retry_count": e.get("retry_count"),
                "export_history": e.get("export_history"),
                "conflict": e.get("conflict"),
            }
            plan_items.append(item)

            if e.get("conflict"):
                resolution = state.conflict_resolutions.get(e["source_path"], "ask")
                conflicts.append({
                    "source_path": e["source_path"],
                    "target_path": e["target_path"],
                    "filename": e["filename"],
                    "conflict_type": e["conflict"],
                    "resolution": resolution,
                })

            if e.get("batch_id"):
                batch_preservation.append({
                    "source_path": e["source_path"],
                    "batch_id": e["batch_id"],
                    "kind": e["kind"],
                })

            if e.get("retry_count") is not None and e["retry_count"] > 0:
                failure_preservation.append({
                    "source_path": e["source_path"],
                    "retry_count": e["retry_count"],
                    "kind": e["kind"],
                })

            if e.get("export_history"):
                export_preservation.append({
                    "source_path": e["source_path"],
                    "export_history": e["export_history"],
                    "kind": e["kind"],
                })

        state.phase = "planned"
        state.updated_at = _now()
        self._save_state(state)

        self._get_audit().log("plan", f"计划生成: {len(plan_items)} 项, "
                              f"冲突 {len(conflicts)} 项")

        return {
            "status": "plan_ready",
            "total_items": len(plan_items),
            "plan": plan_items,
            "conflicts": conflicts,
            "conflicts_count": len(conflicts),
            "preservation": {
                "batch_ids": batch_preservation,
                "failure_meta": failure_preservation,
                "export_history": export_preservation,
            },
            "source_root": self.source_root,
            "target_root": self.target_root,
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

    def apply(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到扫描状态，请先执行 wizard-scan"}

        if state.phase == "applied":
            return {"status": "already_applied", "message": "搬家已完成，可用 wizard-undo 撤销"}
        if state.phase == "undone":
            return {"status": "already_undone", "message": "搬家已撤销，请重新 wizard-scan"}

        entries = state.entries
        applied = 0
        skipped = 0
        failed = 0
        details = []

        for i, e in enumerate(entries):
            if e.get("status") == "applied":
                applied += 1
                continue
            if e.get("status") == "skipped":
                skipped += 1
                continue

            src = e["source_path"]
            dst = e["target_path"]

            if not os.path.exists(src):
                e["status"] = "skipped"
                e["error"] = "源文件不存在"
                skipped += 1
                details.append({"source": src, "target": dst, "status": "skipped",
                                "reason": "source_missing"})
                continue

            if os.path.exists(dst):
                resolution = state.conflict_resolutions.get(src, "rename")
                if resolution == "skip":
                    e["status"] = "skipped"
                    e["error"] = "目标已存在，策略=跳过"
                    skipped += 1
                    details.append({"source": src, "target": dst, "status": "skipped",
                                    "reason": "conflict_skip"})
                    continue
                elif resolution == "rename":
                    base, ext = os.path.splitext(dst)
                    counter = 1
                    while True:
                        alt = f"{base}_wiz{counter}{ext}"
                        if not os.path.exists(alt):
                            dst = alt
                            e["target_path"] = dst
                            break
                        counter += 1
                elif resolution == "overwrite":
                    try:
                        os.remove(dst)
                    except OSError as ex:
                        e["status"] = "failed"
                        e["error"] = f"无法覆盖目标文件: {ex}"
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
                e["status"] = "applied"
                applied += 1
                details.append({"source": src, "target": dst, "status": "applied"})
                self._get_audit().log("apply", f"搬家: {src} -> {dst}",
                                      file_path=src,
                                      batch_id=e.get("batch_id"))
            except PermissionError as ex:
                e["status"] = "failed"
                e["error"] = f"权限不足: {ex}"
                failed += 1
                details.append({"source": src, "target": dst, "status": "failed",
                                "reason": "permission_denied"})
                self._get_audit().log("apply_failed", f"权限不足: {src} -> {ex}",
                                      file_path=src, success=False)
            except OSError as ex:
                e["status"] = "failed"
                e["error"] = str(ex)
                failed += 1
                details.append({"source": src, "target": dst, "status": "failed",
                                "reason": str(ex)})
                self._get_audit().log("apply_failed", f"搬家失败: {src} -> {ex}",
                                      file_path=src, success=False)

        all_done = all(e.get("status") in ("applied", "skipped") for e in entries)
        any_failed = any(e.get("status") == "failed" for e in entries)

        if all_done and not any_failed:
            state.phase = "applied"
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
            "total": len(entries),
            "details": details,
        }

    def undo(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到搬家状态"}

        if not state.undo_available:
            return {"status": "error", "message": "无可撤销的搬家操作"}

        entries = state.entries
        undone = 0
        failed_undo = 0
        details = []

        for e in reversed(entries):
            if e.get("status") != "applied":
                continue

            dst = e["target_path"]
            if not os.path.exists(dst):
                e["status"] = "undone"
                undone += 1
                details.append({"target": dst, "status": "undone", "reason": "target_missing"})
                continue

            src = e["source_path"]
            try:
                if os.path.exists(src):
                    if os.path.isfile(dst):
                        chk_src = _file_checksum(src)
                        chk_dst = _file_checksum(dst)
                        if chk_src == chk_dst and chk_src:
                            os.remove(dst)
                            e["status"] = "undone"
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
                    e["status"] = "undone"
                    e["source_path"] = alt
                    undone += 1
                    details.append({"target": dst, "restored_to": alt,
                                    "status": "undone"})
                else:
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    shutil.move(dst, src)
                    e["status"] = "undone"
                    undone += 1
                    details.append({"target": dst, "restored_to": src,
                                    "status": "undone"})

                self._get_audit().log("undo", f"撤销搬家: {dst} -> {src}",
                                      file_path=dst,
                                      batch_id=e.get("batch_id"))
            except OSError as ex:
                e["error"] = f"撤销失败: {ex}"
                failed_undo += 1
                details.append({"target": dst, "status": "undo_failed", "reason": str(ex)})
                self._get_audit().log("undo_failed", f"撤销失败: {dst} -> {ex}",
                                      file_path=dst, success=False)

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

    def status(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {
                "status": "no_state",
                "message": "未找到搬家状态，请先执行 wizard-scan",
            }

        entries = state.entries
        by_status = {}
        by_kind = {}
        for e in entries:
            st = e.get("status", "pending")
            by_status[st] = by_status.get(st, 0) + 1
            k = e.get("kind", "unknown")
            by_kind[k] = by_kind.get(k, 0) + 1

        return {
            "status": "ok",
            "phase": state.phase,
            "source_root": state.source_root,
            "target_root": state.target_root,
            "total_entries": len(entries),
            "applied_count": state.applied_count,
            "skipped_count": state.skipped_count,
            "failed_count": state.failed_count,
            "undo_available": state.undo_available,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "by_status": by_status,
            "by_kind": by_kind,
            "conflicts_unresolved": [
                e["source_path"] for e in entries
                if e.get("conflict") and e["source_path"] not in state.conflict_resolutions
            ],
        }

    def export_result(self, fmt: str, output_path: str) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到搬家状态"}

        entries = state.entries
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        if fmt == "csv":
            fields = [
                "source_path", "target_path", "kind", "filename", "size",
                "batch_id", "retry_count", "export_history", "status",
                "conflict", "error",
            ]
            with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for e in entries:
                    row = dict(e)
                    if row.get("export_history") and isinstance(row["export_history"], list):
                        row["export_history"] = "; ".join(row["export_history"])
                    writer.writerow(row)
        else:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump({
                    "wizard_state": state.to_dict(),
                    "entries": entries,
                }, f, indent=2, ensure_ascii=False)

        size_kb = round(os.path.getsize(output_path) / 1024, 2) if os.path.isfile(output_path) else 0
        self._get_audit().log("export", f"导出搬家结果: {output_path} ({fmt})")

        return {
            "status": "exported",
            "format": fmt,
            "output": os.path.abspath(output_path),
            "size_kb": size_kb,
            "entries_count": len(entries),
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

    def _save_state(self, state: WizardState) -> None:
        _write_json_atomic(state.to_dict(), self._state_path)

    def _load_state(self) -> Optional[WizardState]:
        data = _safe_read_json(self._state_path)
        if data is None:
            return None
        return WizardState(**data)

    def has_previous_state(self) -> bool:
        return os.path.isfile(self._state_path)

    def resume(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "no_state", "message": "无可续跑的搬家状态"}

        if state.phase == "applied":
            return {"status": "already_applied", "message": "搬家已全部完成"}
        if state.phase == "undone":
            return {"status": "undone", "message": "搬家已撤销"}

        return self.apply()
