import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple


def _handoff_now() -> str:
    return datetime.now().isoformat()


def _handoff_safe_read_json(path: str):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _handoff_write_json_atomic(data, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


HANDOFF_SEALER_DIR = ".handoff_sealer"
HANDOFF_SEALER_STATE_FILE = "sealer_state.json"
HANDOFF_SEALER_AUDIT_LOG = "sealer_audit.jsonl"
HANDOFF_SEALER_HISTORY_FILE = "sealer_history.json"


@dataclass
class HandoffImportItem:
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
class HandoffImportConflict:
    kind: str
    description: str
    source_item: Optional[Dict]
    target_item: Optional[Dict]
    resolution: str = "pending"
    severity: str = "warning"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HandoffOriginalState:
    existing_files: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    batches_path: str = ""
    fq_path: str = ""
    log_path: str = ""
    snapshot_time: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HandoffSealerState:
    current_pack_id: Optional[str] = None
    current_action: Optional[str] = None
    pack_path: Optional[str] = None
    target_runtime_root: Optional[str] = None
    phase: str = "init"
    import_dry_run: bool = False
    import_items: List[Dict[str, Any]] = field(default_factory=list)
    import_conflicts: List[Dict[str, Any]] = field(default_factory=list)
    original_target_state: Dict[str, Any] = field(default_factory=dict)
    completed_paths: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    undo_available: bool = False
    version: str = "2.0"

    def to_dict(self) -> dict:
        return asdict(self)


class HandoffStateManager:
    def __init__(self, runtime_root: str):
        self.runtime_root = os.path.abspath(runtime_root)
        self.sealer_dir = os.path.join(self.runtime_root, HANDOFF_SEALER_DIR)
        self.state_path = os.path.join(self.sealer_dir, HANDOFF_SEALER_STATE_FILE)
        os.makedirs(self.sealer_dir, exist_ok=True)

    def load(self) -> Optional[HandoffSealerState]:
        data = _handoff_safe_read_json(self.state_path)
        if data is None:
            return None
        return HandoffSealerState(**data)

    def save(self, state: HandoffSealerState) -> None:
        state.updated_at = _handoff_now()
        if not state.created_at:
            state.created_at = _handoff_now()
        _handoff_write_json_atomic(state.to_dict(), self.state_path)

    def new_state(self, pack_id: str = None, action: str = None,
                  target_runtime_root: str = None, phase: str = "init",
                  pack_path: str = None) -> HandoffSealerState:
        state = HandoffSealerState(
            current_pack_id=pack_id,
            current_action=action,
            pack_path=pack_path,
            target_runtime_root=target_runtime_root,
            phase=phase,
        )
        self.save(state)
        return state

    def has_progress(self) -> bool:
        state = self.load()
        if state is None:
            return False
        return len(state.completed_paths) > 0 and state.phase in ("importing", "imported", "prechecked")

    def get_progress_count(self) -> int:
        state = self.load()
        if state is None:
            return 0
        return len(state.completed_paths)

    def add_completed_path(self, relative_path: str) -> None:
        state = self.load()
        if state is None:
            return
        if relative_path not in state.completed_paths:
            state.completed_paths.append(relative_path)
            self.save(state)

    def get_completed_paths(self) -> List[str]:
        state = self.load()
        if state is None:
            return []
        return list(state.completed_paths)

    def clear_progress(self) -> None:
        state = self.load()
        if state is None:
            return
        state.completed_paths = []
        self.save(state)

    def cleanup_all(self) -> List[str]:
        cleaned = []
        if os.path.isdir(self.sealer_dir):
            for fname in os.listdir(self.sealer_dir):
                fpath = os.path.join(self.sealer_dir, fname)
                try:
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                        cleaned.append(os.path.join(HANDOFF_SEALER_DIR, fname))
                    elif os.path.isdir(fpath):
                        import shutil
                        shutil.rmtree(fpath, ignore_errors=True)
                        cleaned.append(os.path.join(HANDOFF_SEALER_DIR, fname) + "/")
                except OSError:
                    pass
            try:
                if not os.listdir(self.sealer_dir):
                    os.rmdir(self.sealer_dir)
                    cleaned.append(HANDOFF_SEALER_DIR + "/")
            except OSError:
                pass
        return cleaned

    def wipe(self) -> List[str]:
        state = self.load()
        pack_path = state.pack_path if state else None
        cleaned = self.cleanup_all()
        if pack_path and os.path.isfile(pack_path):
            try:
                os.remove(pack_path)
                cleaned.append(pack_path)
            except OSError:
                pass
        return cleaned


@dataclass
class FailureEntry:
    source_path: str
    target_path: str
    supplier_code: Optional[str]
    error: str
    retry_count: int = 0
    last_retry_at: Optional[str] = None
    batch_id: Optional[str] = None
    created_at: str = ""


class FailureQueue:
    def __init__(self, state_dir: str):
        self.queue_file = os.path.join(state_dir, "failure_queue.json")
        os.makedirs(state_dir, exist_ok=True)

    def load(self) -> List[FailureEntry]:
        if not os.path.exists(self.queue_file):
            return []
        try:
            with open(self.queue_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [FailureEntry(**e) for e in data]
        except (json.JSONDecodeError, OSError):
            return []

    def save(self, entries: List[FailureEntry]) -> None:
        tmp = self.queue_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([asdict(e) for e in entries], f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.queue_file)

    def add(self, entry: FailureEntry) -> None:
        entries = self.load()
        entry.created_at = datetime.now().isoformat()
        entries.append(entry)
        self.save(entries)

    def add_batch(self, new_entries: List[FailureEntry]) -> None:
        entries = self.load()
        now = datetime.now().isoformat()
        for e in new_entries:
            e.created_at = now
            entries.append(e)
        self.save(entries)

    def remove_by_source(self, source_paths: List[str]) -> None:
        entries = self.load()
        path_set = set(source_paths)
        remaining = [e for e in entries if e.source_path not in path_set]
        self.save(remaining)

    def increment_retry(self, source_path: str) -> None:
        entries = self.load()
        now = datetime.now().isoformat()
        for e in entries:
            if e.source_path == source_path:
                e.retry_count += 1
                e.last_retry_at = now
        self.save(entries)

    def list_all(self) -> List[FailureEntry]:
        return self.load()
