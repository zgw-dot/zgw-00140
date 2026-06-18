import csv
import json
import os
import shutil
import hashlib
import zipfile
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from .state import (
    HandoffStateManager,
    HANDOFF_SEALER_DIR,
    HANDOFF_SEALER_STATE_FILE,
    HANDOFF_SEALER_AUDIT_LOG,
    HANDOFF_SEALER_HISTORY_FILE,
    HandoffImportItem,
    HandoffImportConflict,
)


SEALER_DIR = HANDOFF_SEALER_DIR
SEALER_STATE_FILE = HANDOFF_SEALER_STATE_FILE
SEALER_AUDIT_LOG = HANDOFF_SEALER_AUDIT_LOG
SEALER_HISTORY_FILE = HANDOFF_SEALER_HISTORY_FILE

DIR_KINDS = ("temp_inbox", "archive", "state", "logs")
STATE_FILES = ("batches.json", "failure_queue.json")
LOG_FILES = ("archive_log.jsonl",)
CONFIG_FILES = ("config.yaml", "config.yml", "config.json")
EXPORT_FILES = ("export_logs.csv", "export_logs.json")

PACK_MANIFEST = "manifest.json"
PACK_CONFIG_SNAPSHOT = "config_snapshot.json"
PACK_BATCHES = "batches.json"
PACK_FAILURE_QUEUE = "failure_queue.json"
PACK_AUDIT_LOG = "audit_log.jsonl"
PACK_ITEMS_CSV = "items_manifest.csv"
PACK_ITEMS_JSON = "items_manifest.json"
PACK_CHECKSUM = "checksums.json"
PACK_EXPORT_CSV = "export_records.csv"
PACK_EXPORT_JSON = "export_records.json"


def _now() -> str:
    return datetime.now().isoformat()


def _norm_zip_path(path: str) -> str:
    return path.replace("\\", "/")


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _dir_sha256(path: str) -> str:
    h = hashlib.sha256()
    try:
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for fname in sorted(files):
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, path)
                h.update(rel.encode("utf-8"))
                h.update(_file_sha256(fpath).encode("utf-8"))
        return h.hexdigest()
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
            "target_path": e.get("target_path", ""),
            "batch_id": e.get("batch_id"),
            "retry_count": e.get("retry_count", 0),
            "error": e.get("error", ""),
        })
    return result


def _extract_export_records(log_path: str) -> List[Dict]:
    if not os.path.isfile(log_path):
        return []
    records = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                records.append(rec)
            except json.JSONDecodeError:
                continue
    return records


def _detect_duplicate_exports(records: List[Dict]) -> Dict[str, int]:
    history: Dict[str, int] = {}
    for rec in records:
        bid = rec.get("batch_id", "")
        if not bid:
            continue
        action = rec.get("action", "")
        if action in ("move", "retry"):
            history[bid] = history.get(bid, 0) + 1
    return {k: v for k, v in history.items() if v > 1}


def _find_config_file(runtime_root: str) -> Optional[str]:
    for fname in CONFIG_FILES:
        fpath = os.path.join(runtime_root, fname)
        if os.path.isfile(fpath):
            return fpath
    return None


def _config_to_dict(config_path: str) -> Dict:
    if not config_path or not os.path.isfile(config_path):
        return {}
    try:
        from .config import load_config
        cfg = load_config(config_path)
        return cfg.to_dict()
    except Exception:
        data = _safe_read_json(config_path)
        if data:
            return data
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}


@dataclass
class PackItem:
    batch_id: Optional[str]
    failure_count: int
    source_path: str
    relative_path: str
    target_kind: str
    filename: str
    size: int
    duplicate_export: bool = False
    duplicate_export_count: int = 0
    checksum: str = ""
    included: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PackManifest:
    pack_id: str
    created_at: str
    source_runtime_root: str
    source_machine: str
    batch_ids: List[str]
    failure_count: int
    total_items: int
    total_size: int
    duplicate_export_count: int
    duplicate_export_batches: Dict[str, int]
    config_paths: Dict[str, str]
    version: str = "1.0"
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PackChecksum:
    file_path: str
    relative_path: str
    sha256: str
    size: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuditEntry:
    timestamp: str
    action: str
    detail: str
    pack_id: Optional[str] = None
    file_path: Optional[str] = None
    batch_id: Optional[str] = None
    success: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HistoryRecord:
    pack_id: str
    action: str
    timestamp: str
    runtime_root: str
    details: Dict[str, Any]
    success: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ImportConflict:
    kind: str
    description: str
    source_item: Optional[Dict]
    target_item: Optional[Dict]
    resolution: str = "pending"
    severity: str = "warning"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ImportItem:
    relative_path: str
    target_path: str
    target_kind: str
    batch_id: Optional[str]
    checksum: str
    size: int
    status: str = "pending"
    conflict: Optional[str] = None
    resolution: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SealerState:
    current_pack_id: Optional[str]
    current_action: Optional[str]
    target_runtime_root: Optional[str]
    phase: str
    import_dry_run: bool = False
    import_items: List[Dict] = field(default_factory=list)
    import_conflicts: List[Dict] = field(default_factory=list)
    original_target_state: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    undo_available: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class AuditLogger:
    def __init__(self, store_dir: str):
        self.audit_file = os.path.join(store_dir, SEALER_AUDIT_LOG)
        os.makedirs(store_dir, exist_ok=True)

    def log(self, action: str, detail: str, pack_id: str = None,
            file_path: str = None, batch_id: str = None, success: bool = True) -> None:
        entry = AuditEntry(
            timestamp=_now(),
            action=action,
            detail=detail,
            pack_id=pack_id,
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
        fields = ["timestamp", "action", "detail", "pack_id", "file_path", "batch_id", "success"]
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for e in entries:
                writer.writerow(e)
        return output_path


class HistoryManager:
    def __init__(self, store_dir: str):
        self.history_file = os.path.join(store_dir, SEALER_HISTORY_FILE)
        os.makedirs(store_dir, exist_ok=True)

    def load(self) -> List[Dict]:
        data = _safe_read_json(self.history_file)
        if data and isinstance(data, list):
            return data
        return []

    def add(self, record: HistoryRecord) -> None:
        records = self.load()
        records.append(record.to_dict())
        _write_json_atomic(records, self.history_file)

    def list(self, pack_id: str = None, limit: int = 100) -> List[Dict]:
        records = self.load()
        if pack_id:
            records = [r for r in records if r.get("pack_id") == pack_id]
        return records[-limit:]


class HandoffSealer:
    def __init__(self, runtime_root: str):
        self.runtime_root = os.path.abspath(runtime_root)
        self.sealer_dir = os.path.join(self.runtime_root, SEALER_DIR)
        self.state_path = os.path.join(self.sealer_dir, SEALER_STATE_FILE)
        os.makedirs(self.sealer_dir, exist_ok=True)
        self._state_mgr = HandoffStateManager(runtime_root)
        self._audit = AuditLogger(self.sealer_dir)
        self._history = HistoryManager(self.sealer_dir)

    def _get_state(self):
        return self._state_mgr.load()

    def _save_state(self, state) -> None:
        self._state_mgr.save(state)

    def _new_state(self, pack_id: str = None, action: str = None,
                   target_runtime_root: str = None, phase: str = "init",
                   pack_path: str = None):
        return self._state_mgr.new_state(
            pack_id=pack_id,
            action=action,
            target_runtime_root=target_runtime_root,
            phase=phase,
            pack_path=pack_path,
        )

    def pack(self, output_path: str = None, notes: str = "",
             include_samples: bool = True) -> Dict:
        pack_id = f"PACK_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        state = self._new_state(pack_id=pack_id, action="pack", phase="packing")

        state_dir = os.path.join(self.runtime_root, "state")
        log_dir = os.path.join(self.runtime_root, "logs")
        archive_dir = os.path.join(self.runtime_root, "archive")
        inbox_dir = os.path.join(self.runtime_root, "temp_inbox")

        items: List[PackItem] = []
        checksums: List[PackChecksum] = []
        all_batch_ids = set()
        total_failure_count = 0

        batches_path = os.path.join(state_dir, "batches.json")
        batch_ids = _extract_batch_ids(batches_path)
        all_batch_ids.update(batch_ids)

        fq_path = os.path.join(state_dir, "failure_queue.json")
        failure_meta = _extract_failure_meta(fq_path)
        total_failure_count = sum(1 for m in failure_meta if m.get("retry_count", 0) > 0)
        for m in failure_meta:
            bid = m.get("batch_id")
            if bid:
                all_batch_ids.add(bid)

        log_path = os.path.join(log_dir, "archive_log.jsonl")
        export_records = _extract_export_records(log_path)
        dup_exports = _detect_duplicate_exports(export_records)
        for bid in dup_exports:
            all_batch_ids.add(bid)

        config_path = _find_config_file(self.runtime_root)
        config_snapshot = _config_to_dict(config_path) if config_path else {}

        config_paths = {
            "source_dir": config_snapshot.get("source_dir", inbox_dir),
            "archive_dir": config_snapshot.get("archive_dir", archive_dir),
            "state_dir": config_snapshot.get("state_dir", state_dir),
            "log_dir": config_snapshot.get("log_dir", log_dir),
            "config_file": config_path or "",
        }

        dir_collections = [
            ("state", state_dir, STATE_FILES, True),
            ("logs", log_dir, LOG_FILES, True),
            ("archive", archive_dir, None, include_samples),
            ("temp_inbox", inbox_dir, None, include_samples),
        ]

        for kind, dir_path, specific_files, include_all in dir_collections:
            if not os.path.isdir(dir_path):
                continue

            if specific_files:
                for fname in specific_files:
                    fpath = os.path.join(dir_path, fname)
                    if os.path.isfile(fpath):
                        self._add_pack_item(fpath, kind, dir_path, items, checksums,
                                            batch_ids, failure_meta, dup_exports)
            elif include_all:
                for root, dirs, files in os.walk(dir_path):
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        self._add_pack_item(fpath, kind, dir_path, items, checksums,
                                            batch_ids, failure_meta, dup_exports)

        if config_path:
            self._add_pack_item(config_path, "config", self.runtime_root, items, checksums,
                                [], [], {})

        for log_fname in os.listdir(log_dir) if os.path.isdir(log_dir) else []:
            if log_fname in EXPORT_FILES or log_fname.endswith(".csv"):
                fpath = os.path.join(log_dir, log_fname)
                if os.path.isfile(fpath):
                    already = any(i.source_path == fpath for i in items)
                    if not already:
                        self._add_pack_item(fpath, "logs", log_dir, items, checksums,
                                            [], [], {})

        total_size = sum(i.size for i in items)

        manifest = PackManifest(
            pack_id=pack_id,
            created_at=_now(),
            source_runtime_root=self.runtime_root,
            source_machine=os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
            batch_ids=sorted(all_batch_ids),
            failure_count=total_failure_count,
            total_items=len(items),
            total_size=total_size,
            duplicate_export_count=len(dup_exports),
            duplicate_export_batches=dup_exports,
            config_paths=config_paths,
            notes=notes,
        )

        if output_path is None:
            output_path = os.path.join(tempfile.gettempdir(), f"{pack_id}.zip")
        output_path = os.path.abspath(output_path)
        state.pack_path = output_path
        self._save_state(state)

        try:
            self._create_pack_zip(output_path, manifest, items, checksums,
                                  config_snapshot, export_records, batches_path, fq_path)
        except PermissionError as e:
            state.phase = "pack_failed"
            self._save_state(state)
            self._audit.log("pack", f"打包失败: 权限不足 {e}", pack_id=pack_id, success=False)
            return {"status": "error", "error": f"权限不足: {e}", "pack_id": pack_id}
        except OSError as e:
            state.phase = "pack_failed"
            self._save_state(state)
            self._audit.log("pack", f"打包失败: {e}", pack_id=pack_id, success=False)
            return {"status": "error", "error": str(e), "pack_id": pack_id}

        zip_checksum = _file_sha256(output_path)

        state.phase = "packed"
        self._save_state(state)

        self._audit.log("pack", f"打包完成: {len(items)}项, {total_size}字节", pack_id=pack_id)
        self._history.add(HistoryRecord(
            pack_id=pack_id,
            action="pack",
            timestamp=_now(),
            runtime_root=self.runtime_root,
            details={"output_path": output_path, "item_count": len(items), "total_size": total_size},
        ))

        return {
            "status": "packed",
            "pack_id": pack_id,
            "output_path": output_path,
            "zip_checksum": zip_checksum,
            "manifest": manifest.to_dict(),
            "item_count": len(items),
            "total_size_kb": round(total_size / 1024, 2),
            "batch_count": len(all_batch_ids),
            "failure_count": total_failure_count,
            "duplicate_export_count": len(dup_exports),
        }

    def _add_pack_item(self, fpath: str, kind: str, base_dir: str,
                       items: List[PackItem], checksums: List[PackChecksum],
                       batch_ids: List[str], failure_meta: List[Dict],
                       dup_exports: Dict[str, int]) -> None:
        rel = os.path.relpath(fpath, base_dir)
        zip_rel = _norm_zip_path(os.path.join(kind, rel))
        size = 0
        try:
            size = os.path.getsize(fpath)
        except OSError:
            pass
        checksum = _file_sha256(fpath)

        item_batch_id = None
        failure_count = 0
        is_dup_export = False
        dup_export_count = 0

        if kind == "state" and os.path.basename(fpath) == "batches.json":
            if batch_ids:
                item_batch_id = ", ".join(batch_ids)
        elif kind == "state" and os.path.basename(fpath) == "failure_queue.json":
            failure_count = sum(1 for m in failure_meta if m.get("retry_count", 0) > 0)
            bids = [m.get("batch_id") for m in failure_meta if m.get("batch_id")]
            if bids:
                item_batch_id = ", ".join(set(bids))
        elif kind == "logs" and os.path.basename(fpath) == "archive_log.jsonl":
            export_records = _extract_export_records(fpath)
            export_bids = list(_detect_duplicate_exports(export_records).keys())
            if export_bids:
                item_batch_id = ", ".join(export_bids)
            is_dup_export = len(dup_exports) > 0
            dup_export_count = max(dup_exports.values()) if dup_exports else 0

        item = PackItem(
            batch_id=item_batch_id,
            failure_count=failure_count,
            source_path=fpath,
            relative_path=zip_rel,
            target_kind=kind,
            filename=os.path.basename(fpath),
            size=size,
            duplicate_export=is_dup_export,
            duplicate_export_count=dup_export_count,
            checksum=checksum,
            included=True,
        )
        items.append(item)
        checksums.append(PackChecksum(
            file_path=fpath,
            relative_path=zip_rel,
            sha256=checksum,
            size=size,
        ))

    def _create_pack_zip(self, output_path: str, manifest: PackManifest,
                         items: List[PackItem], checksums: List[PackChecksum],
                         config_snapshot: Dict, export_records: List[Dict],
                         batches_path: str, fq_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(PACK_MANIFEST, json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False))
            zf.writestr(PACK_CONFIG_SNAPSHOT, json.dumps(config_snapshot, indent=2, ensure_ascii=False))

            checksums_data = [c.to_dict() for c in checksums]
            zf.writestr(PACK_CHECKSUM, json.dumps(checksums_data, indent=2, ensure_ascii=False))

            items_data = [i.to_dict() for i in items]
            zf.writestr(PACK_ITEMS_JSON, json.dumps(items_data, indent=2, ensure_ascii=False))

            csv_path = os.path.join(tempfile.gettempdir(), f"items_{manifest.pack_id}.csv")
            fields = ["batch_id", "failure_count", "source_path", "relative_path",
                      "target_kind", "filename", "size", "duplicate_export",
                      "duplicate_export_count", "checksum", "included"]
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for i in items:
                    writer.writerow(i.to_dict())
            zf.write(csv_path, PACK_ITEMS_CSV)
            try:
                os.remove(csv_path)
            except OSError:
                pass

            if export_records:
                zf.writestr(PACK_EXPORT_JSON, json.dumps(export_records, indent=2, ensure_ascii=False))
                csv_path = os.path.join(tempfile.gettempdir(), f"export_{manifest.pack_id}.csv")
                if export_records:
                    fields2 = list(export_records[0].keys()) if export_records else []
                    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=fields2, extrasaction="ignore")
                        writer.writeheader()
                        for r in export_records:
                            writer.writerow(r)
                    zf.write(csv_path, PACK_EXPORT_CSV)
                    try:
                        os.remove(csv_path)
                    except OSError:
                        pass

            audit_entries = self._audit.load_entries()
            if audit_entries:
                audit_lines = "\n".join(json.dumps(e, ensure_ascii=False) for e in audit_entries[-100:])
                zf.writestr(PACK_AUDIT_LOG, audit_lines)

            for item in items:
                if os.path.isfile(item.source_path):
                    zf.write(item.source_path, item.relative_path)

    def precheck_pack(self, pack_path: str) -> Dict:
        pack_path = os.path.abspath(pack_path)
        if not os.path.isfile(pack_path):
            return {"status": "error", "error": f"交接包不存在: {pack_path}"}

        issues = []
        warnings = []

        try:
            with zipfile.ZipFile(pack_path, "r") as zf:
                namelist = zf.namelist()

                required = [PACK_MANIFEST, PACK_CHECKSUM, PACK_ITEMS_JSON, PACK_CONFIG_SNAPSHOT]
                for req in required:
                    if req not in namelist:
                        issues.append(f"缺少必要文件: {req}")

                if PACK_MANIFEST in namelist:
                    manifest_data = json.loads(zf.read(PACK_MANIFEST).decode("utf-8"))
                    manifest = PackManifest(**manifest_data)
                else:
                    manifest = None

                if PACK_CHECKSUM in namelist:
                    checksums = json.loads(zf.read(PACK_CHECKSUM).decode("utf-8"))
                else:
                    checksums = []

                verified = 0
                corrupted = []
                for cs in checksums:
                    rel = cs["relative_path"]
                    expected_sha = cs["sha256"]
                    if rel in namelist:
                        actual_sha = hashlib.sha256(zf.read(rel)).hexdigest()
                        if actual_sha != expected_sha:
                            corrupted.append(rel)
                        else:
                            verified += 1
                    else:
                        corrupted.append(f"缺失: {rel}")

                if corrupted:
                    issues.append(f"文件校验失败: {len(corrupted)}个")

                items_count = manifest.total_items if manifest else 0
                if manifest and len(checksums) != items_count:
                    warnings.append(f"清单项数({items_count})与校验项数({len(checksums)})不一致")

        except zipfile.BadZipFile:
            return {"status": "error", "error": "不是有效的ZIP文件或文件已损坏"}
        except json.JSONDecodeError as e:
            return {"status": "error", "error": f"JSON解析失败: {e}"}
        except PermissionError as e:
            return {"status": "error", "error": f"权限不足无法读取: {e}"}

        self._audit.log("precheck", f"预检完成: issues={len(issues)}, warnings={len(warnings)}",
                        pack_id=manifest.pack_id if manifest else None)

        return {
            "status": "prechecked" if not issues else "has_issues",
            "pack_path": pack_path,
            "pack_id": manifest.pack_id if manifest else None,
            "manifest": manifest.to_dict() if manifest else None,
            "verified_items": verified,
            "corrupted_items": corrupted,
            "issues": issues,
            "warnings": warnings,
            "is_valid": len(issues) == 0,
        }

    def precheck_import(self, pack_path: str, target_runtime_root: str,
                        dry_run: bool = False) -> Dict:
        pre = self.precheck_pack(pack_path)
        if pre["status"] == "error":
            return pre

        target_runtime_root = os.path.abspath(target_runtime_root)

        conflicts: List[ImportConflict] = []
        import_items: List[ImportItem] = []

        with zipfile.ZipFile(pack_path, "r") as zf:
            manifest = PackManifest(**json.loads(zf.read(PACK_MANIFEST).decode("utf-8")))
            pack_items = json.loads(zf.read(PACK_ITEMS_JSON).decode("utf-8"))
            checksums = json.loads(zf.read(PACK_CHECKSUM).decode("utf-8"))
            config_snapshot = json.loads(zf.read(PACK_CONFIG_SNAPSHOT).decode("utf-8"))

            checksum_map = {c["relative_path"]: c for c in checksums}

            existing_batches = set()
            target_state_dir = os.path.join(target_runtime_root, "state")
            target_batches_path = os.path.join(target_state_dir, "batches.json")
            if os.path.isfile(target_batches_path):
                existing_batches = set(_extract_batch_ids(target_batches_path))

            target_batches_data = _safe_read_json(target_batches_path) or []
            target_fq_path = os.path.join(target_state_dir, "failure_queue.json")
            target_fq_data = _safe_read_json(target_fq_path) or []

            target_log_dir = os.path.join(target_runtime_root, "logs")
            target_log_path = os.path.join(target_log_dir, "archive_log.jsonl")
            target_export_records = _extract_export_records(target_log_path)
            target_dup_exports = _detect_duplicate_exports(target_export_records)

            target_config_path = _find_config_file(target_runtime_root)
            target_config = _config_to_dict(target_config_path) if target_config_path else {}

            for pi in pack_items:
                rel = pi["relative_path"]
                kind = pi["target_kind"]
                fname = pi["filename"]

                if kind == "config":
                    tgt_path = os.path.join(target_runtime_root, fname)
                else:
                    base_rel = os.path.relpath(rel, kind)
                    if base_rel.startswith(".."):
                        base_rel = os.path.basename(rel)
                    tgt_path = os.path.join(target_runtime_root, kind, base_rel)

                cs = checksum_map.get(rel, {})
                imp_item = ImportItem(
                    relative_path=rel,
                    target_path=tgt_path,
                    target_kind=kind,
                    batch_id=pi.get("batch_id"),
                    checksum=cs.get("sha256", ""),
                    size=cs.get("size", 0),
                    status="pending",
                )

                if os.path.exists(tgt_path):
                    existing_sha = _file_sha256(tgt_path)
                    if existing_sha == cs.get("sha256", ""):
                        imp_item.status = "identical"
                        imp_item.conflict = None
                    else:
                        imp_item.conflict = "file_exists"
                        conflicts.append(ImportConflict(
                            kind="file_exists",
                            description=f"目标目录已存在同名文件且内容不同: {tgt_path}",
                            source_item={"path": rel, "checksum": cs.get("sha256", "")},
                            target_item={"path": tgt_path, "checksum": existing_sha},
                            severity="warning",
                        ))
                else:
                    parent_dir = os.path.dirname(tgt_path)
                    if parent_dir and not os.path.exists(parent_dir):
                        try:
                            os.makedirs(parent_dir, exist_ok=True)
                            test_file = os.path.join(parent_dir, ".write_test_" + manifest.pack_id)
                            with open(test_file, "w") as tf:
                                tf.write("test")
                            os.remove(test_file)
                        except PermissionError:
                            imp_item.conflict = "permission_denied"
                            imp_item.status = "blocked"
                            conflicts.append(ImportConflict(
                                kind="permission",
                                description=f"无权限写入目录: {parent_dir}",
                                source_item=None,
                                target_item={"path": parent_dir},
                                severity="error",
                            ))
                        except OSError as e:
                            imp_item.conflict = "os_error"
                            imp_item.status = "blocked"
                            conflicts.append(ImportConflict(
                                kind="os_error",
                                description=f"创建目录失败: {e}",
                                source_item=None,
                                target_item={"path": parent_dir},
                                severity="error",
                            ))

                import_items.append(imp_item)

            pack_bid_list = manifest.batch_ids
            overlapping_batches = [bid for bid in pack_bid_list if bid in existing_batches]
            if overlapping_batches:
                conflicts.append(ImportConflict(
                    kind="duplicate_batch_id",
                    description=f"目标目录已存在同名批次: {', '.join(overlapping_batches)}",
                    source_item={"batch_ids": pack_bid_list},
                    target_item={"batch_ids": sorted(existing_batches)},
                    severity="warning",
                ))

            pack_export_bids = set(manifest.duplicate_export_batches.keys())
            for bid in pack_export_bids:
                if bid in target_dup_exports:
                    conflicts.append(ImportConflict(
                        kind="duplicate_export_record",
                        description=f"批次 {bid} 在目标目录也有重复导出记录",
                        source_item={"batch_id": bid, "count": manifest.duplicate_export_batches[bid]},
                        target_item={"batch_id": bid, "count": target_dup_exports[bid]},
                        severity="warning",
                    ))

            if target_config_path and config_snapshot:
                for key in ("source_dir", "archive_dir", "state_dir", "log_dir"):
                    tgt_val = target_config.get(key, "")
                    src_val = config_snapshot.get(key, "")
                    if tgt_val and src_val and tgt_val != src_val:
                        conflicts.append(ImportConflict(
                            kind="config_modified",
                            description=f"配置项 {key} 与包内快照不一致: 目标={tgt_val}, 包内={src_val}",
                            source_item={"key": key, "value": src_val},
                            target_item={"key": key, "value": tgt_val},
                            severity="info",
                        ))

        state = self._get_state() or self._new_state()
        state.current_pack_id = manifest.pack_id
        state.current_action = "import"
        state.pack_path = pack_path
        state.target_runtime_root = target_runtime_root
        state.phase = "prechecked"
        state.import_dry_run = dry_run
        state.import_items = [i.to_dict() for i in import_items]
        state.import_conflicts = [c.to_dict() for c in conflicts]
        state.completed_paths = []
        self._save_state(state)

        self._audit.log("precheck_import",
                        f"导入预检完成: 冲突={len(conflicts)}, 项数={len(import_items)}",
                        pack_id=manifest.pack_id)
        self._history.add(HistoryRecord(
            pack_id=manifest.pack_id,
            action="precheck_import",
            timestamp=_now(),
            runtime_root=target_runtime_root,
            details={"conflict_count": len(conflicts), "item_count": len(import_items),
                     "dry_run": dry_run},
            success=len([c for c in conflicts if c.severity == "error"]) == 0,
        ))

        return {
            "status": "prechecked",
            "pack_id": manifest.pack_id,
            "target_runtime_root": target_runtime_root,
            "manifest": manifest.to_dict(),
            "import_items": [i.to_dict() for i in import_items],
            "conflicts": [c.to_dict() for c in conflicts],
            "conflict_count": len(conflicts),
            "error_conflicts": len([c for c in conflicts if c.severity == "error"]),
            "warning_conflicts": len([c for c in conflicts if c.severity == "warning"]),
            "info_conflicts": len([c for c in conflicts if c.severity == "info"]),
            "identical_items": len([i for i in import_items if i.status == "identical"]),
            "pending_items": len([i for i in import_items if i.status == "pending"]),
            "blocked_items": len([i for i in import_items if i.status == "blocked"]),
            "dry_run": dry_run,
        }

    def do_import(self, pack_path: str = None, target_runtime_root: str = None,
                  dry_run: bool = False, resume: bool = False,
                  conflict_policy: str = "skip") -> Dict:
        state = self._get_state()

        if pack_path is None:
            if state and state.pack_path:
                pack_path = state.pack_path
            else:
                return {"status": "error", "error": "未指定交接包路径，且状态中无记录"}

        pack_path = os.path.abspath(pack_path)

        if target_runtime_root is None:
            if state and state.target_runtime_root:
                target_runtime_root = state.target_runtime_root
            else:
                target_runtime_root = self.runtime_root
        target_runtime_root = os.path.abspath(target_runtime_root)

        if not resume:
            pre = self.precheck_import(pack_path, target_runtime_root, dry_run)
            if pre["status"] == "error":
                return pre
            state = self._get_state()
        else:
            if state is None or len(state.completed_paths) == 0:
                return {"status": "error", "error": "未找到续跑进度，请先执行导入再使用--resume"}

        if state is None:
            return {"status": "error", "error": "状态丢失"}

        state.phase = "importing"
        self._save_state(state)

        with zipfile.ZipFile(pack_path, "r") as zf:
            manifest = PackManifest(**json.loads(zf.read(PACK_MANIFEST).decode("utf-8")))
            checksums = json.loads(zf.read(PACK_CHECKSUM).decode("utf-8"))
            checksum_map = {c["relative_path"]: c for c in checksums}

            original_target_state = self._snapshot_target_state(target_runtime_root)
            state.original_target_state = original_target_state

            completed_items = []
            skipped_items = []
            failed_items = []
            written_items: List[Tuple[str, str]] = []

            items_to_process = state.import_items[:]
            completed_paths = set(state.completed_paths)

            for imp_item_dict in items_to_process:
                imp_item = ImportItem(**imp_item_dict)
                if imp_item.relative_path in completed_paths:
                    completed_items.append(imp_item)
                    continue

                rel = imp_item.relative_path
                tgt_path = imp_item.target_path

                if imp_item.status == "blocked":
                    skipped_items.append(imp_item)
                    continue

                if imp_item.conflict == "file_exists":
                    if conflict_policy == "skip":
                        imp_item.status = "skipped"
                        imp_item.resolution = "skip"
                        skipped_items.append(imp_item)
                        continue
                    elif conflict_policy == "rename":
                        base, ext = os.path.splitext(tgt_path)
                        counter = 1
                        new_path = f"{base}_imported_{counter}{ext}"
                        while os.path.exists(new_path):
                            counter += 1
                            new_path = f"{base}_imported_{counter}{ext}"
                        tgt_path = new_path
                        imp_item.target_path = tgt_path
                        imp_item.resolution = "rename"
                    elif conflict_policy == "overwrite":
                        imp_item.resolution = "overwrite"
                    else:
                        imp_item.status = "skipped"
                        imp_item.resolution = "skip"
                        skipped_items.append(imp_item)
                        continue

                if dry_run:
                    imp_item.status = "dry_run_ok"
                    completed_items.append(imp_item)
                    continue

                try:
                    parent_dir = os.path.dirname(tgt_path)
                    if parent_dir:
                        os.makedirs(parent_dir, exist_ok=True)

                    if rel in zf.namelist():
                        with zf.open(rel) as src, open(tgt_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)

                        actual_sha = _file_sha256(tgt_path)
                        expected_sha = checksum_map.get(rel, {}).get("sha256", "")
                        if expected_sha and actual_sha != expected_sha:
                            imp_item.status = "checksum_mismatch"
                            imp_item.error = f"校验和不匹配: 期望={expected_sha[:16]}, 实际={actual_sha[:16]}"
                            failed_items.append(imp_item)
                            self._audit.log("import_checksum_fail",
                                            f"校验和不匹配: {tgt_path}",
                                            pack_id=manifest.pack_id,
                                            file_path=tgt_path, success=False)
                            continue

                    imp_item.status = "imported"
                    completed_items.append(imp_item)
                    written_items.append((rel, tgt_path))
                    completed_paths.add(rel)

                    state.completed_paths = sorted(completed_paths)
                    self._save_state(state)

                except PermissionError as e:
                    imp_item.status = "permission_denied"
                    imp_item.error = str(e)
                    failed_items.append(imp_item)
                    self._audit.log("import_permission_fail",
                                    f"权限不足: {tgt_path}: {e}",
                                    pack_id=manifest.pack_id,
                                    file_path=tgt_path, success=False)
                except OSError as e:
                    imp_item.status = "os_error"
                    imp_item.error = str(e)
                    failed_items.append(imp_item)
                    self._audit.log("import_os_fail",
                                    f"写入失败: {tgt_path}: {e}",
                                    pack_id=manifest.pack_id,
                                    file_path=tgt_path, success=False)

            state.import_items = [i.to_dict() for i in (completed_items + skipped_items + failed_items)]
            state.undo_available = len(written_items) > 0 and not dry_run
            state.phase = "imported" if not dry_run else "dry_run_completed"
            self._save_state(state)

        success = len(failed_items) == 0 and not dry_run

        self._audit.log("import",
                        f"导入完成: 成功={len(completed_items)}, 跳过={len(skipped_items)}, "
                        f"失败={len(failed_items)}, dry_run={dry_run}",
                        pack_id=manifest.pack_id, success=success or dry_run)
        self._history.add(HistoryRecord(
            pack_id=manifest.pack_id,
            action="import",
            timestamp=_now(),
            runtime_root=target_runtime_root,
            details={"completed": len(completed_items), "skipped": len(skipped_items),
                     "failed": len(failed_items), "dry_run": dry_run,
                     "conflict_policy": conflict_policy, "resume": resume},
            success=success or dry_run,
        ))

        return {
            "status": "imported" if success else ("dry_run_completed" if dry_run else "partial"),
            "pack_id": manifest.pack_id,
            "target_runtime_root": target_runtime_root,
            "manifest": manifest.to_dict(),
            "completed_count": len(completed_items),
            "skipped_count": len(skipped_items),
            "failed_count": len(failed_items),
            "total_count": len(state.import_items),
            "dry_run": dry_run,
            "resume": resume,
            "conflict_policy": conflict_policy,
            "completed_items": [i.to_dict() for i in completed_items],
            "skipped_items": [i.to_dict() for i in skipped_items],
            "failed_items": [i.to_dict() for i in failed_items],
            "undo_available": state.undo_available,
            "note": "执行 handoff-confirm 确认落地，或 handoff-undo 撤销导入" if not dry_run else None,
        }

    def _snapshot_target_state(self, target_runtime_root: str) -> Dict:
        snapshot = {
            "existing_files": {},
            "batches_path": os.path.join(target_runtime_root, "state", "batches.json"),
            "fq_path": os.path.join(target_runtime_root, "state", "failure_queue.json"),
            "log_path": os.path.join(target_runtime_root, "logs", "archive_log.jsonl"),
            "snapshot_time": _now(),
        }

        for kind in DIR_KINDS:
            kind_dir = os.path.join(target_runtime_root, kind)
            if not os.path.isdir(kind_dir):
                continue
            for root, dirs, files in os.walk(kind_dir):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    rel = os.path.relpath(fpath, target_runtime_root)
                    snapshot["existing_files"][rel] = {
                        "exists": True,
                        "checksum": _file_sha256(fpath),
                        "size": os.path.getsize(fpath) if os.path.isfile(fpath) else 0,
                    }
        return snapshot

    def confirm(self) -> Dict:
        state = self._get_state()
        if state is None:
            return {"status": "error", "error": "未找到状态，请先执行导入"}

        if state.phase not in ("imported",):
            return {"status": "error", "error": f"当前阶段={state.phase}，无法确认"}

        state.phase = "confirmed"
        state.undo_available = False
        self._save_state(state)

        pack_id = state.current_pack_id
        self._audit.log("confirm", f"落地确认完成，撤销通道已关闭", pack_id=pack_id)
        self._history.add(HistoryRecord(
            pack_id=pack_id or "unknown",
            action="confirm",
            timestamp=_now(),
            runtime_root=state.target_runtime_root or self.runtime_root,
            details={"phase_before": "imported", "phase_after": "confirmed"},
        ))

        return {
            "status": "confirmed",
            "pack_id": pack_id,
            "target_runtime_root": state.target_runtime_root,
            "note": "导入已确认落地，撤销功能已关闭",
        }

    def undo(self) -> Dict:
        state = self._get_state()
        if state is None:
            return {"status": "error", "error": "未找到状态"}

        if not state.undo_available:
            return {"status": "error", "error": "当前无撤销可用（可能已确认落地或为dry-run）"}

        if not state.target_runtime_root:
            return {"status": "error", "error": "未记录目标运行目录"}

        original = state.original_target_state or {}
        existing_files = original.get("existing_files", {})

        restored_count = 0
        removed_count = 0
        skipped_count = 0
        errors = []

        for imp_item_dict in state.import_items:
            imp = ImportItem(**imp_item_dict)
            if imp.status != "imported":
                continue

            tgt_path = imp.target_path
            rel = os.path.relpath(tgt_path, state.target_runtime_root)

            try:
                if not os.path.exists(tgt_path):
                    skipped_count += 1
                    continue

                if rel in existing_files:
                    orig_info = existing_files[rel]
                    if orig_info.get("checksum") and orig_info["checksum"] != imp.checksum:
                        if imp.resolution == "rename":
                            try:
                                os.remove(tgt_path)
                                removed_count += 1
                            except OSError as e:
                                errors.append(f"删除重命名文件失败 {tgt_path}: {e}")
                        else:
                            backup_path = tgt_path + ".sealer_backup"
                            try:
                                shutil.copy2(tgt_path, backup_path)
                                removed_count += 1
                            except OSError as e:
                                errors.append(f"备份原文件失败 {tgt_path}: {e}")
                    else:
                        try:
                            os.remove(tgt_path)
                            removed_count += 1
                        except OSError as e:
                            errors.append(f"删除导入文件失败 {tgt_path}: {e}")
                else:
                    try:
                        os.remove(tgt_path)
                        removed_count += 1
                    except OSError as e:
                        errors.append(f"删除新文件失败 {tgt_path}: {e}")

                restored_count += 1
            except Exception as e:
                errors.append(f"处理 {tgt_path} 异常: {e}")

        state.phase = "undone"
        state.undo_available = False
        self._save_state(state)

        pack_id = state.current_pack_id
        success = len(errors) == 0
        self._audit.log("undo",
                        f"撤销完成: 恢复={restored_count}, 删除={removed_count}, "
                        f"跳过={skipped_count}, 错误={len(errors)}",
                        pack_id=pack_id, success=success)
        self._history.add(HistoryRecord(
            pack_id=pack_id or "unknown",
            action="undo",
            timestamp=_now(),
            runtime_root=state.target_runtime_root or self.runtime_root,
            details={"restored": restored_count, "removed": removed_count,
                     "skipped": skipped_count, "errors": errors},
            success=success,
        ))

        return {
            "status": "undone" if success else "undo_partial",
            "pack_id": pack_id,
            "target_runtime_root": state.target_runtime_root,
            "restored_count": restored_count,
            "removed_count": removed_count,
            "skipped_count": skipped_count,
            "error_count": len(errors),
            "errors": errors,
        }

    def list_history(self, pack_id: str = None, limit: int = 100) -> Dict:
        records = self._history.list(pack_id=pack_id, limit=limit)
        return {
            "status": "ok",
            "total": len(records),
            "pack_id_filter": pack_id,
            "records": records,
        }

    def status(self) -> Dict:
        state = self._get_state()
        if state is None:
            return {
                "status": "no_state",
                "message": "尚未执行交接封箱包操作",
                "sealer_dir": self.sealer_dir,
            }

        import_counts = {}
        for i in state.import_items:
            s = i.get("status", "unknown")
            import_counts[s] = import_counts.get(s, 0) + 1

        return {
            "status": "ok",
            "phase": state.phase,
            "current_pack_id": state.current_pack_id,
            "current_action": state.current_action,
            "pack_path": state.pack_path,
            "target_runtime_root": state.target_runtime_root,
            "import_dry_run": state.import_dry_run,
            "undo_available": state.undo_available,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "import_item_count": len(state.import_items),
            "import_status_counts": import_counts,
            "conflict_count": len(state.import_conflicts),
            "sealer_dir": self.sealer_dir,
            "resume_available": len(state.completed_paths) > 0 and state.phase in ("importing", "prechecked"),
            "completed_count": len(state.completed_paths),
        }

    def cleanup(self, wipe_pack: bool = False) -> Dict:
        if wipe_pack:
            cleaned = self._state_mgr.wipe()
        else:
            cleaned = self._state_mgr.cleanup_all()
        return {"status": "cleaned", "cleaned": cleaned, "wipe_pack": wipe_pack}
