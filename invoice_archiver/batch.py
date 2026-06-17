import json
import os
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional, Dict


@dataclass
class BatchFileRecord:
    source_path: str
    target_path: str
    filename: str
    supplier_code: str
    status: str
    error: Optional[str] = None


@dataclass
class Batch:
    batch_id: str
    created_at: str
    committed_at: Optional[str] = None
    rolled_back_at: Optional[str] = None
    status: str = "pending"
    files: List[Dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Batch":
        return cls(**data)


def generate_batch_id() -> str:
    return f"BATCH_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


class BatchManager:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.batches_file = os.path.join(state_dir, "batches.json")
        os.makedirs(state_dir, exist_ok=True)

    def load_batches(self) -> List[Batch]:
        if not os.path.exists(self.batches_file):
            return []
        try:
            with open(self.batches_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [Batch.from_dict(b) for b in data]
        except (json.JSONDecodeError, OSError):
            return []

    def save_batches(self, batches: List[Batch]) -> None:
        tmp = self.batches_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                [b.to_dict() for b in batches],
                f, indent=2, ensure_ascii=False,
            )
        os.replace(tmp, self.batches_file)

    def create_batch(self, file_records: List[BatchFileRecord]) -> Batch:
        batch_id = generate_batch_id()
        now = datetime.now().isoformat()
        batch = Batch(
            batch_id=batch_id,
            created_at=now,
            status="pending",
            files=[asdict(r) for r in file_records],
        )
        batches = self.load_batches()
        batches.append(batch)
        self.save_batches(batches)
        return batch

    def update_batch(self, batch_id: str, updated: Batch) -> None:
        batches = self.load_batches()
        for i, b in enumerate(batches):
            if b.batch_id == batch_id:
                batches[i] = updated
                break
        self.save_batches(batches)

    def commit_batch(self, batch_id: str) -> Optional[Batch]:
        batches = self.load_batches()
        for b in batches:
            if b.batch_id == batch_id:
                b.status = "committed"
                b.committed_at = datetime.now().isoformat()
                self.save_batches(batches)
                return b
        return None

    def mark_partial_fail(self, batch_id: str) -> Optional[Batch]:
        batches = self.load_batches()
        for b in batches:
            if b.batch_id == batch_id:
                b.status = "partial_fail"
                self.save_batches(batches)
                return b
        return None

    def rollback_batch(self, batch_id: str) -> Optional[Batch]:
        batches = self.load_batches()
        for b in batches:
            if b.batch_id == batch_id:
                for f in b.files:
                    if f["status"] == "moved":
                        target = f["target_path"]
                        source = f["source_path"]
                        if os.path.exists(target):
                            os.makedirs(os.path.dirname(source), exist_ok=True)
                            try:
                                shutil.move(target, source)
                                f["status"] = "rolled_back"
                            except OSError:
                                pass
                        else:
                            f["status"] = "rolled_back"
                b.status = "rolled_back"
                b.rolled_back_at = datetime.now().isoformat()
                self.save_batches(batches)
                return b
        return None

    def get_batch(self, batch_id: str) -> Optional[Batch]:
        for b in self.load_batches():
            if b.batch_id == batch_id:
                return b
        return None

    def list_batches(self) -> List[Batch]:
        return self.load_batches()
