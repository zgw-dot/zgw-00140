import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional


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
