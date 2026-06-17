import csv
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional


@dataclass
class LogEntry:
    timestamp: str
    source_path: str
    target_path: str
    action: str
    batch_id: str
    status: str
    error_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


_LOG_FIELDS = [
    "timestamp", "source_path", "target_path",
    "action", "batch_id", "status", "error_reason",
]


class ArchiveLogger:
    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self.log_file = os.path.join(log_dir, "archive_log.jsonl")
        os.makedirs(log_dir, exist_ok=True)

    def log(self, entry: LogEntry) -> None:
        if not entry.timestamp:
            entry.timestamp = datetime.now().isoformat()
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def load_entries(self) -> List[LogEntry]:
        if not os.path.exists(self.log_file):
            return []
        entries: List[LogEntry] = []
        with open(self.log_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        entries.append(LogEntry(**data))
                    except json.JSONDecodeError:
                        continue
        return entries

    def export_csv(self, output_path: str) -> str:
        entries = self.load_entries()
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_LOG_FIELDS)
            writer.writeheader()
            for e in entries:
                writer.writerow(e.to_dict())
        return output_path

    def export_json(self, output_path: str) -> str:
        entries = self.load_entries()
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump([e.to_dict() for e in entries], f, indent=2, ensure_ascii=False)
        return output_path

    def query_by_batch(self, batch_id: str) -> List[LogEntry]:
        return [e for e in self.load_entries() if e.batch_id == batch_id]
