import csv
import json
import os
import shutil
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DRILL_STATE_FILE = ".drill_state.json"
DRILL_SESSION_DIR = ".drill_sessions"
DRILL_POINTER_FILE = ".drill_pointer"
DRILL_AUDIT_LOG = "drill_audit.jsonl"

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
class DrillItem:
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
    drill_status: str = "planned"
    replay_result: Optional[Dict] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DrillAuditEntry:
    timestamp: str
    action: str
    detail: str
    file_path: Optional[str] = None
    batch_id: Optional[str] = None
    success: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DrillReplayResult:
    command: str
    target_dir: str
    success: bool
    item_count: int = 0
    data_keys: List[str] = field(default_factory=list)
    error: Optional[str] = None
    batch_ids: List[str] = field(default_factory=list)
    failure_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConfigSnapshotDiff:
    key: str
    old_value: str
    new_value: str
    changed: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DrillState:
    source_root: str
    target_root: str
    session_id: str
    config_path: Optional[str] = None
    conflict_policy: str = "rename"
    duplicate_policy: str = "merge"
    items: List[Dict] = field(default_factory=list)
    phase: str = "scanned"
    created_at: str = ""
    updated_at: str = ""
    replay_results: List[Dict] = field(default_factory=list)
    config_diffs: List[Dict] = field(default_factory=list)
    original_config_paths: Dict[str, str] = field(default_factory=dict)
    projected_config_paths: Dict[str, str] = field(default_factory=dict)
    snapshot_checksums: Dict[str, str] = field(default_factory=dict)
    undo_available: bool = False
    saved_session: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class DrillAuditLogger:
    def __init__(self, store_dir: str):
        self.audit_file = os.path.join(store_dir, DRILL_AUDIT_LOG)
        os.makedirs(store_dir, exist_ok=True)

    def log(self, action: str, detail: str, file_path: str = None,
            batch_id: str = None, success: bool = True) -> None:
        entry = DrillAuditEntry(
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


class DrillCenter:
    def __init__(self, source_root: str, target_root: str,
                 config_path: Optional[str] = None,
                 conflict_policy: str = "rename",
                 duplicate_policy: str = "merge",
                 session_id: Optional[str] = None):
        self.source_root = os.path.abspath(source_root)
        self.target_root = os.path.abspath(target_root)
        self.config_path = config_path
        self.conflict_policy = conflict_policy
        self.duplicate_policy = duplicate_policy
        self.session_id = session_id or f"drill_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._state_path = os.path.join(self.source_root, DRILL_STATE_FILE)
        self._session_dir = os.path.join(self.source_root, DRILL_SESSION_DIR)
        self._target_drill_dir = os.path.join(self.target_root, ".drill")
        self._audit: Optional[DrillAuditLogger] = None

    def _effective_target_root(self) -> str:
        if self.target_root != self.source_root:
            return self.target_root
        pointer_path = os.path.join(self.source_root, DRILL_POINTER_FILE)
        pdata = _safe_read_json(pointer_path)
        if pdata and pdata.get("target_root"):
            return pdata["target_root"]
        data = _safe_read_json(self._state_path)
        if data and data.get("target_root"):
            return data["target_root"]
        target_state_path = os.path.join(self._target_drill_dir, DRILL_STATE_FILE)
        data2 = _safe_read_json(target_state_path)
        if data2 and data2.get("target_root"):
            return data2["target_root"]
        return self.target_root

    def _effective_drill_dir(self) -> str:
        etr = self._effective_target_root()
        if etr and etr != self.source_root:
            return os.path.join(etr, ".drill")
        return self._target_drill_dir

    def _get_audit(self) -> DrillAuditLogger:
        if self._audit is None:
            drill_dir = self._effective_drill_dir()
            os.makedirs(drill_dir, exist_ok=True)
            self._audit = DrillAuditLogger(drill_dir)
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
            "session_id": self.session_id,
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
                        "drill_status": "planned",
                        "replay_result": None,
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

        projected_config_paths = {
            "source_dir": os.path.join(self.target_root, "temp_inbox"),
            "archive_dir": os.path.join(self.target_root, "archive"),
            "state_dir": os.path.join(self.target_root, "state"),
            "log_dir": os.path.join(self.target_root, "logs"),
        }

        config_diffs = []
        for key in ("source_dir", "archive_dir", "state_dir", "log_dir"):
            old_val = original_config_paths.get(key, "")
            new_val = projected_config_paths.get(key, "")
            config_diffs.append({
                "key": key,
                "old_value": old_val,
                "new_value": new_val,
                "changed": old_val != new_val,
            })

        snapshot_checksums = {}
        for item in items:
            if item["checksum"]:
                snapshot_checksums[item["source_path"]] = item["checksum"]

        state = DrillState(
            source_root=self.source_root,
            target_root=self.target_root,
            session_id=self.session_id,
            config_path=self.config_path,
            conflict_policy=self.conflict_policy,
            duplicate_policy=self.duplicate_policy,
            items=items,
            phase="scanned",
            created_at=_now(),
            updated_at=_now(),
            config_diffs=config_diffs,
            original_config_paths=original_config_paths,
            projected_config_paths=projected_config_paths,
            snapshot_checksums=snapshot_checksums,
        )
        self._save_state(state)

        self._get_audit().log("drill_scan",
                              f"演练扫描完成: {len(items)} 项, "
                              f"session={self.session_id}, "
                              f"目录={list(scan_summary['dirs_found'].keys())}, "
                              f"文件={list(scan_summary['files_found'].keys())}")

        return {
            "status": "scanned",
            "session_id": self.session_id,
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
            "config_diffs": config_diffs,
        }

    def plan(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到演练扫描状态，请先执行 drill-scan"}

        plan_items = []
        for i in state.items:
            plan_items.append({
                "batch_id": i.get("batch_id"),
                "failure_count": i.get("failure_count", 0),
                "source_path": i["source_path"],
                "target_path": i["target_path"],
                "kind": i["kind"],
                "filename": i["filename"],
                "duplicate_export": i.get("duplicate_export", False),
                "duplicate_export_count": i.get("duplicate_export_count", 0),
                "conflict": i.get("conflict"),
                "drill_status": i.get("drill_status", "planned"),
            })

        by_batch = {}
        all_batch_ids = set()
        for i in state.items:
            bid = i.get("batch_id")
            if bid:
                for b in bid.split(", "):
                    b = b.strip()
                    if b:
                        all_batch_ids.add(b)
                        by_batch.setdefault(b, []).append(i)

        batch_summaries = {}
        for bid, batch_items in by_batch.items():
            batch_summaries[bid] = {
                "item_count": len(batch_items),
                "failure_count": sum(i.get("failure_count", 0) for i in batch_items),
                "sources": [i["source_path"] for i in batch_items],
                "destinations": [i["target_path"] for i in batch_items],
                "duplicate_export": any(i.get("duplicate_export") for i in batch_items),
            }

        effective_target = state.target_root or self.target_root
        replay_plan = [
            {"command": "list-batches", "target_dir": os.path.join(effective_target, "state"),
             "description": "演练 list-batches 读取目标目录批次列表"},
            {"command": "list-failures", "target_dir": os.path.join(effective_target, "state"),
             "description": "演练 list-failures 读取目标目录失败队列"},
            {"command": "export-logs", "target_dir": os.path.join(effective_target, "logs"),
             "description": "演练 export-logs 从目标目录导出日志"},
        ]

        self._get_audit().log("drill_plan",
                              f"演练计划生成: {len(plan_items)} 项, "
                              f"{len(all_batch_ids)} 个批次, "
                              f"{len(replay_plan)} 个回放命令")

        return {
            "status": "plan_ready",
            "session_id": state.session_id,
            "total_items": len(plan_items),
            "items": plan_items,
            "all_batch_ids": sorted(all_batch_ids),
            "batch_summaries": batch_summaries,
            "replay_plan": replay_plan,
            "config_diffs": state.config_diffs,
            "original_config_paths": state.original_config_paths,
            "projected_config_paths": state.projected_config_paths,
            "conflict_policy": state.conflict_policy,
            "duplicate_policy": state.duplicate_policy,
        }

    def replay(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到演练扫描状态，请先执行 drill-scan"}

        replay_results: List[Dict] = []

        effective_target = state.target_root or self.target_root
        target_state_dir = os.path.join(effective_target, "state")
        target_log_dir = os.path.join(effective_target, "logs")

        batches_path = os.path.join(target_state_dir, "batches.json")
        if os.path.isfile(batches_path):
            batch_ids = _extract_batch_ids(batches_path)
            data_keys = ["batch_id", "status", "files", "created_at", "committed_at"]
            replay_results.append(DrillReplayResult(
                command="list-batches",
                target_dir=target_state_dir,
                success=True,
                item_count=len(batch_ids),
                data_keys=data_keys,
                batch_ids=batch_ids,
            ).to_dict())
            self._get_audit().log("drill_replay",
                                  f"list-batches 回放: {len(batch_ids)} 个批次",
                                  batch_id=", ".join(batch_ids) if batch_ids else None)
        else:
            replay_results.append(DrillReplayResult(
                command="list-batches",
                target_dir=target_state_dir,
                success=True,
                item_count=0,
                error="目标目录尚无 batches.json (演练预演正常)",
            ).to_dict())
            self._get_audit().log("drill_replay",
                                  "list-batches 回放: 目标目录尚无 batches.json (演练预演正常)")

        fq_path = os.path.join(target_state_dir, "failure_queue.json")
        if os.path.isfile(fq_path):
            failure_meta = _extract_failure_meta(fq_path)
            failure_count = sum(1 for m in failure_meta if m.get("retry_count", 0) > 0)
            data_keys = ["source_path", "batch_id", "retry_count", "error"]
            replay_results.append(DrillReplayResult(
                command="list-failures",
                target_dir=target_state_dir,
                success=True,
                item_count=len(failure_meta),
                data_keys=data_keys,
                failure_count=failure_count,
            ).to_dict())
            self._get_audit().log("drill_replay",
                                  f"list-failures 回放: {len(failure_meta)} 条, 失败={failure_count}")
        else:
            replay_results.append(DrillReplayResult(
                command="list-failures",
                target_dir=target_state_dir,
                success=True,
                item_count=0,
                error="目标目录尚无 failure_queue.json (演练预演正常)",
            ).to_dict())
            self._get_audit().log("drill_replay",
                                  "list-failures 回放: 目标目录尚无 failure_queue.json (演练预演正常)")

        log_path = os.path.join(target_log_dir, "archive_log.jsonl")
        if os.path.isfile(log_path):
            history = _extract_export_history(log_path)
            dupes = _detect_duplicate_exports(log_path)
            data_keys = ["timestamp", "source_path", "target_path", "action", "batch_id", "status"]
            replay_results.append(DrillReplayResult(
                command="export-logs",
                target_dir=target_log_dir,
                success=True,
                item_count=sum(len(v) for v in history.values()),
                data_keys=data_keys,
                batch_ids=list(history.keys()),
            ).to_dict())
            if dupes:
                self._get_audit().log("drill_replay_duplicate",
                                      f"export-logs 回放检测到重复导出: {dupes}")
        else:
            replay_results.append(DrillReplayResult(
                command="export-logs",
                target_dir=target_log_dir,
                success=True,
                item_count=0,
                error="目标目录尚无 archive_log.jsonl (演练预演正常)",
            ).to_dict())
            self._get_audit().log("drill_replay",
                                  "export-logs 回放: 目标目录尚无 archive_log.jsonl (演练预演正常)")

        source_batches_path = os.path.join(self.source_root, "state", "batches.json")
        source_fq_path = os.path.join(self.source_root, "state", "failure_queue.json")
        source_log_path = os.path.join(self.source_root, "logs", "archive_log.jsonl")

        source_only_bids = set()
        target_only_bids = set()
        source_bids = set(_extract_batch_ids(source_batches_path)) if os.path.isfile(source_batches_path) else set()
        target_bids = set(_extract_batch_ids(batches_path)) if os.path.isfile(batches_path) else set()
        source_only_bids = source_bids - target_bids
        target_only_bids = target_bids - source_bids

        source_dupes = _detect_duplicate_exports(source_log_path) if os.path.isfile(source_log_path) else {}
        target_dupes = _detect_duplicate_exports(log_path) if os.path.isfile(log_path) else {}

        for i in state.items:
            if i.get("drill_status") == "planned":
                i["drill_status"] = "replayed"
                src = i["source_path"]
                matching = [r for r in replay_results if r.get("target_dir") and src.startswith(os.path.join(self.source_root, "state"))]
                if matching:
                    i["replay_result"] = matching[0]

        state.replay_results = replay_results
        state.updated_at = _now()
        self._save_state(state)

        all_target = all(
            r.get("target_dir", "").startswith(effective_target)
            for r in replay_results
        )

        return {
            "status": "replayed",
            "session_id": state.session_id,
            "replay_results": replay_results,
            "all_target_dirs_confirmed": all_target,
            "source_only_batch_ids": sorted(source_only_bids),
            "target_only_batch_ids": sorted(target_only_bids),
            "source_duplicate_exports": source_dupes,
            "target_duplicate_exports": target_dupes,
            "config_diffs": state.config_diffs,
        }

    def report(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到演练状态，请先执行 drill-scan"}

        by_batch = {}
        all_batch_ids = set()
        for i in state.items:
            bid = i.get("batch_id")
            if bid:
                for b in bid.split(", "):
                    b = b.strip()
                    if b:
                        all_batch_ids.add(b)
                        by_batch.setdefault(b, []).append(i)

        batch_report = {}
        for bid, batch_items in by_batch.items():
            batch_report[bid] = {
                "item_count": len(batch_items),
                "failure_count": sum(i.get("failure_count", 0) for i in batch_items),
                "sources": [i["source_path"] for i in batch_items],
                "destinations": [i["target_path"] for i in batch_items],
                "duplicate_export": any(i.get("duplicate_export") for i in batch_items),
                "duplicate_export_count": max(
                    (i.get("duplicate_export_count", 0) for i in batch_items), default=0
                ),
            }

        config_diff_summary = []
        for diff in state.config_diffs:
            config_diff_summary.append({
                "key": diff["key"],
                "old_value": diff["old_value"],
                "new_value": diff["new_value"],
                "changed": diff["changed"],
            })

        replay_summary = []
        for r in state.replay_results:
            replay_summary.append({
                "command": r.get("command"),
                "target_dir": r.get("target_dir"),
                "success": r.get("success"),
                "item_count": r.get("item_count", 0),
                "batch_ids": r.get("batch_ids", []),
                "failure_count": r.get("failure_count", 0),
                "error": r.get("error"),
            })

        items_with_dup = [i for i in state.items if i.get("duplicate_export")]

        self._get_audit().log("drill_report",
                              f"演练报告生成: {len(state.items)} 项, "
                              f"{len(all_batch_ids)} 个批次, "
                              f"重复导出={len(items_with_dup)}")

        return {
            "status": "report_ready",
            "session_id": state.session_id,
            "phase": state.phase,
            "total_items": len(state.items),
            "all_batch_ids": sorted(all_batch_ids),
            "batch_report": batch_report,
            "config_diffs": config_diff_summary,
            "replay_results": replay_summary,
            "duplicate_export_items": len(items_with_dup),
            "duplicate_export_markers": [
                {
                    "source_path": i["source_path"],
                    "batch_id": i.get("batch_id"),
                    "duplicate_export_count": i.get("duplicate_export_count", 0),
                }
                for i in items_with_dup
            ],
            "conflict_policy": state.conflict_policy,
            "duplicate_policy": state.duplicate_policy,
            "original_config_paths": state.original_config_paths,
            "projected_config_paths": state.projected_config_paths,
            "undo_available": state.undo_available,
        }

    def save_session(self, label: Optional[str] = None) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到演练状态"}

        session_dir = self._effective_drill_dir()
        os.makedirs(session_dir, exist_ok=True)
        session_name = label or state.session_id
        session_path = os.path.join(session_dir, f"{session_name}.json")
        state.saved_session = True
        self._save_state(state)
        _write_json_atomic(state.to_dict(), session_path)

        self._get_audit().log("drill_save",
                              f"演练会话保存: {session_name}")

        return {
            "status": "saved",
            "session_id": state.session_id,
            "label": session_name,
            "path": os.path.abspath(session_path),
            "phase": state.phase,
        }

    def load_session(self, label: str) -> Dict:
        session_dir = self._effective_drill_dir()
        if not os.path.isdir(session_dir):
            session_dir = self._session_dir
        session_path = os.path.join(session_dir, f"{label}.json")
        if not os.path.isfile(session_path):
            alt_dir = self._session_dir
            alt_path = os.path.join(alt_dir, f"{label}.json")
            if os.path.isfile(alt_path):
                session_path = alt_path
            else:
                return {"status": "error", "message": f"会话文件不存在: {label}"}

        data = _safe_read_json(session_path)
        if data is None:
            return {"status": "error", "message": f"会话文件损坏: {session_path}"}

        state = DrillState(**data)
        self._save_state(state)
        self.session_id = state.session_id

        self._get_audit().log("drill_load",
                              f"演练会话加载: {label}, phase={state.phase}")

        return {
            "status": "loaded",
            "session_id": state.session_id,
            "label": label,
            "phase": state.phase,
            "total_items": len(state.items),
        }

    def list_sessions(self) -> Dict:
        all_sessions = []
        effective_dir = self._effective_drill_dir()
        for session_dir in (effective_dir, self._session_dir):
            if not os.path.isdir(session_dir):
                continue
            for fname in os.listdir(session_dir):
                if not fname.endswith(".json"):
                    continue
                path = os.path.join(session_dir, fname)
                data = _safe_read_json(path)
                if data:
                    all_sessions.append({
                        "label": fname[:-5],
                        "session_id": data.get("session_id", ""),
                        "phase": data.get("phase", ""),
                        "created_at": data.get("created_at", ""),
                        "total_items": len(data.get("items", [])),
                    })
        seen = set()
        unique = []
        for s in all_sessions:
            if s["label"] not in seen:
                seen.add(s["label"])
                unique.append(s)
        return {"status": "ok", "sessions": unique}

    def undo(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到演练状态"}

        if not state.undo_available:
            return {"status": "error", "message": "无可撤销的演练操作 (演练为预演模式，不实际移动文件)"}

        undone = 0
        for i in state.items:
            if i.get("drill_status") != "pending":
                i["drill_status"] = "undone"
                undone += 1

        state.phase = "undone"
        state.undo_available = False
        state.updated_at = _now()
        self._save_state(state)

        self._get_audit().log("drill_undo",
                              f"演练撤销: {undone} 项状态回退")

        return {
            "status": "undone",
            "session_id": state.session_id,
            "undone": undone,
        }

    def apply_takeover(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到演练状态，请先执行 drill-scan"}

        if state.phase not in ("scanned", "replayed"):
            return {"status": "error", "message": f"当前阶段={state.phase}，无法执行接管"}

        from .takeover_ledger import TakeoverLedger
        ledger = TakeoverLedger(
            source_root=self.source_root,
            target_root=self.target_root,
            config_path=self.config_path,
            conflict_policy=self.conflict_policy,
            duplicate_policy=self.duplicate_policy,
        )

        scan_result = ledger.scan()
        confirm_result = ledger.confirm()
        apply_result = ledger.apply()

        state.phase = "takeover_applied"
        state.undo_available = apply_result.get("applied", 0) > 0
        state.updated_at = _now()
        self._save_state(state)

        self._get_audit().log("drill_apply_takeover",
                              f"演练确认后执行接管: applied={apply_result.get('applied')}, "
                              f"failed={apply_result.get('failed')}")

        return {
            "status": "takeover_applied",
            "session_id": state.session_id,
            "scan": scan_result.get("status"),
            "confirm": confirm_result.get("status"),
            "apply": apply_result,
        }

    def cleanup(self) -> Dict:
        cleaned = []
        pointer_path = os.path.join(self.source_root, DRILL_POINTER_FILE)
        if os.path.isfile(pointer_path):
            try:
                os.remove(pointer_path)
                cleaned.append("pointer")
            except OSError:
                pass
        legacy_state = os.path.join(self.source_root, DRILL_STATE_FILE)
        if os.path.isfile(legacy_state):
            try:
                os.remove(legacy_state)
                cleaned.append("legacy_state")
            except OSError:
                pass
        legacy_sessions = os.path.join(self.source_root, DRILL_SESSION_DIR)
        if os.path.isdir(legacy_sessions):
            import shutil
            try:
                shutil.rmtree(legacy_sessions)
                cleaned.append("legacy_sessions")
            except OSError:
                pass
        return {"status": "cleaned", "cleaned": cleaned}

    def export_evidence(self, fmt: str, output_path: str) -> Dict:
        state = self._load_state()
        if state is None:
            return {"status": "error", "message": "未找到演练状态"}

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        evidence = {
            "session_id": state.session_id,
            "phase": state.phase,
            "source_root": state.source_root,
            "target_root": state.target_root,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "config_diffs": state.config_diffs,
            "original_config_paths": state.original_config_paths,
            "projected_config_paths": state.projected_config_paths,
            "conflict_policy": state.conflict_policy,
            "duplicate_policy": state.duplicate_policy,
            "items": state.items,
            "replay_results": state.replay_results,
            "snapshot_checksums": state.snapshot_checksums,
        }

        if fmt == "csv":
            fields = [
                "batch_id", "failure_count", "source_path", "target_path",
                "kind", "filename", "size", "duplicate_export",
                "duplicate_export_count", "checksum", "status",
                "conflict", "error", "drill_status",
            ]
            with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for i in state.items:
                    writer.writerow(i)
        else:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(evidence, f, indent=2, ensure_ascii=False)

        size_kb = round(os.path.getsize(output_path) / 1024, 2) if os.path.isfile(output_path) else 0
        self._get_audit().log("drill_export_evidence",
                              f"导出演练证据包: {output_path} ({fmt}), {size_kb} KB")

        return {
            "status": "exported",
            "format": fmt,
            "output": os.path.abspath(output_path),
            "size_kb": size_kb,
            "entries_count": len(state.items),
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

    def status(self) -> Dict:
        state = self._load_state()
        if state is None:
            return {
                "status": "no_state",
                "message": "未找到演练状态，请先执行 drill-scan",
            }

        by_status = {}
        for i in state.items:
            st = i.get("drill_status", "planned")
            by_status[st] = by_status.get(st, 0) + 1

        return {
            "status": "ok",
            "phase": state.phase,
            "session_id": state.session_id,
            "source_root": state.source_root,
            "target_root": state.target_root,
            "total_entries": len(state.items),
            "undo_available": state.undo_available,
            "saved_session": state.saved_session,
            "conflict_policy": state.conflict_policy,
            "duplicate_policy": state.duplicate_policy,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "by_status": by_status,
            "config_diffs": state.config_diffs,
            "replay_results_count": len(state.replay_results),
        }

    def _save_state(self, state: DrillState) -> None:
        drill_dir = self._effective_drill_dir()
        target_state_path = os.path.join(drill_dir, DRILL_STATE_FILE)
        os.makedirs(drill_dir, exist_ok=True)
        _write_json_atomic(state.to_dict(), target_state_path)
        if os.path.isfile(self._state_path):
            try:
                os.remove(self._state_path)
            except OSError:
                pass
        pointer_path = os.path.join(self.source_root, DRILL_POINTER_FILE)
        _write_json_atomic({"target_root": state.target_root, "source_root": state.source_root}, pointer_path)

    def _load_state(self) -> Optional[DrillState]:
        drill_dir = self._effective_drill_dir()
        target_state_path = os.path.join(drill_dir, DRILL_STATE_FILE)
        if os.path.isdir(drill_dir) and os.path.isfile(target_state_path):
            data = _safe_read_json(target_state_path)
            if data is not None:
                return DrillState(**data)
        data = _safe_read_json(self._state_path)
        if data is not None:
            return DrillState(**data)
        return None

    def has_previous_state(self) -> bool:
        drill_dir = self._effective_drill_dir()
        target_state_path = os.path.join(drill_dir, DRILL_STATE_FILE)
        return os.path.isfile(target_state_path) or os.path.isfile(self._state_path)
