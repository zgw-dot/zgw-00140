import os
import shutil
from datetime import datetime
from typing import List, Optional, Dict

from .config import ArchiveConfig
from .scanner import ScannedFile, scan_source_dir
from .batch import BatchManager, BatchFileRecord, Batch
from .state import FailureQueue, FailureEntry
from .logger import ArchiveLogger, LogEntry


class Archiver:
    def __init__(self, config: ArchiveConfig):
        self.config = config
        self.batch_mgr = BatchManager(config.state_dir)
        self.failure_queue = FailureQueue(config.state_dir)
        self.logger = ArchiveLogger(config.log_dir)

    def _resolve_target_path(self, sf: ScannedFile, batch_id: str) -> str:
        supplier_dir = os.path.join(self.config.archive_dir, sf.supplier_code)
        batch_dir = os.path.join(supplier_dir, batch_id)
        return os.path.join(batch_dir, sf.filename)

    def _resolve_conflict(self, target_path: str) -> str:
        if not os.path.exists(target_path):
            return target_path
        policy = self.config.conflict_policy
        if policy == "overwrite":
            return target_path
        elif policy == "skip":
            return target_path
        else:
            base, ext = os.path.splitext(target_path)
            counter = 1
            new_path = f"{base}_{counter}{ext}"
            while os.path.exists(new_path):
                counter += 1
                new_path = f"{base}_{counter}{ext}"
            return new_path

    def precheck(self) -> Dict:
        valid, skipped = scan_source_dir(self.config)
        plan = []
        warnings = []

        for sf in valid:
            target = self._resolve_target_path(sf, "PREVIEW")
            conflict = os.path.exists(target)
            if conflict:
                resolved = self._resolve_conflict(target)
                warnings.append(
                    f"Target exists: {target} -> will resolve to {resolved}"
                )
            plan.append({
                "source": sf.path,
                "target": target,
                "supplier_code": sf.supplier_code,
                "conflict": conflict,
            })

        for sf in skipped:
            warnings.append(f"Skipped: {sf.path} - {sf.parse_error}")

        return {
            "would_archive": len(plan),
            "would_skip": len(skipped),
            "plan": plan,
            "warnings": warnings,
        }

    def archive(self) -> Dict:
        valid, skipped = scan_source_dir(self.config)

        if not valid:
            for sf in skipped:
                self.logger.log(LogEntry(
                    timestamp="",
                    source_path=sf.path,
                    target_path="",
                    action="skip",
                    batch_id="NO_BATCH",
                    status="fail",
                    error_reason=sf.parse_error or "Unknown",
                ))
            return {
                "batch_id": None,
                "archived": 0,
                "failed": 0,
                "skipped": len(skipped),
            }

        batch_records = []
        for sf in valid:
            batch_records.append(BatchFileRecord(
                source_path=sf.path,
                target_path="",
                filename=sf.filename,
                supplier_code=sf.supplier_code,
                status="pending",
            ))

        batch = self.batch_mgr.create_batch(batch_records)
        batch_id = batch.batch_id

        failures: List[FailureEntry] = []
        archived = 0
        failed = 0

        for f_rec in batch.files:
            sf_match = next(
                (sf for sf in valid if sf.path == f_rec["source_path"]), None
            )
            if sf_match is None:
                continue

            target = self._resolve_target_path(sf_match, batch_id)

            if self.config.conflict_policy == "skip" and os.path.exists(target):
                f_rec["target_path"] = target
                f_rec["status"] = "failed"
                f_rec["error"] = f"Target file already exists (skip policy): {target}"
                failures.append(FailureEntry(
                    source_path=f_rec["source_path"],
                    target_path=target,
                    supplier_code=f_rec["supplier_code"],
                    error=f_rec["error"],
                    batch_id=batch_id,
                ))
                self.logger.log(LogEntry(
                    timestamp="",
                    source_path=f_rec["source_path"],
                    target_path=target,
                    action="skip",
                    batch_id=batch_id,
                    status="fail",
                    error_reason=f_rec["error"],
                ))
                failed += 1
                continue

            target = self._resolve_conflict(target)
            f_rec["target_path"] = target

            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.move(f_rec["source_path"], target)
                f_rec["status"] = "moved"
                archived += 1
                self.logger.log(LogEntry(
                    timestamp="",
                    source_path=f_rec["source_path"],
                    target_path=target,
                    action="move",
                    batch_id=batch_id,
                    status="success",
                ))
            except Exception as e:
                f_rec["status"] = "failed"
                f_rec["error"] = str(e)
                failures.append(FailureEntry(
                    source_path=f_rec["source_path"],
                    target_path=target,
                    supplier_code=f_rec["supplier_code"],
                    error=str(e),
                    batch_id=batch_id,
                ))
                self.logger.log(LogEntry(
                    timestamp="",
                    source_path=f_rec["source_path"],
                    target_path=target,
                    action="move",
                    batch_id=batch_id,
                    status="fail",
                    error_reason=str(e),
                ))
                failed += 1

        if failed > 0 and archived > 0:
            batch.status = "partial_fail"
        elif failed > 0:
            batch.status = "partial_fail"
        else:
            batch.status = "committed"
            batch.committed_at = datetime.now().isoformat()
        self.batch_mgr.update_batch(batch_id, batch)

        if failures:
            self.failure_queue.add_batch(failures)

        for sf in skipped:
            self.logger.log(LogEntry(
                timestamp="",
                source_path=sf.path,
                target_path="",
                action="skip",
                batch_id=batch_id,
                status="fail",
                error_reason=sf.parse_error or "Unknown",
            ))

        return {
            "batch_id": batch_id,
            "archived": archived,
            "failed": failed,
            "skipped": len(skipped),
        }

    def retry_failures(self) -> Dict:
        entries = self.failure_queue.load()
        if not entries:
            return {"retried": 0, "succeeded": 0, "still_failed": 0}

        succeeded_paths: List[str] = []
        still_failed = 0
        retried = 0

        for entry in entries:
            if not os.path.exists(entry.source_path):
                still_failed += 1
                self.failure_queue.increment_retry(entry.source_path)
                self.logger.log(LogEntry(
                    timestamp="",
                    source_path=entry.source_path,
                    target_path=entry.target_path,
                    action="retry",
                    batch_id=entry.batch_id or "",
                    status="fail",
                    error_reason="Source file no longer exists",
                ))
                continue

            try:
                target = entry.target_path
                os.makedirs(os.path.dirname(target), exist_ok=True)
                resolved_target = self._resolve_conflict(target)
                shutil.move(entry.source_path, resolved_target)
                succeeded_paths.append(entry.source_path)
                retried += 1
                self.logger.log(LogEntry(
                    timestamp="",
                    source_path=entry.source_path,
                    target_path=resolved_target,
                    action="retry",
                    batch_id=entry.batch_id or "",
                    status="success",
                ))
            except Exception as e:
                still_failed += 1
                self.failure_queue.increment_retry(entry.source_path)
                self.logger.log(LogEntry(
                    timestamp="",
                    source_path=entry.source_path,
                    target_path=entry.target_path,
                    action="retry",
                    batch_id=entry.batch_id or "",
                    status="fail",
                    error_reason=str(e),
                ))

        self.failure_queue.remove_by_source(succeeded_paths)

        return {
            "retried": retried,
            "succeeded": len(succeeded_paths),
            "still_failed": still_failed,
        }

    def rollback(self, batch_id: str) -> Dict:
        batch = self.batch_mgr.rollback_batch(batch_id)
        if not batch:
            return {"success": False, "error": f"Batch {batch_id} not found"}

        rolled = 0
        failed = 0
        for f in batch.files:
            if f["status"] == "rolled_back":
                self.logger.log(LogEntry(
                    timestamp="",
                    source_path=f["target_path"],
                    target_path=f["source_path"],
                    action="rollback",
                    batch_id=batch_id,
                    status="success",
                ))
                rolled += 1
            elif f["status"] == "moved":
                self.logger.log(LogEntry(
                    timestamp="",
                    source_path=f["target_path"],
                    target_path=f["source_path"],
                    action="rollback",
                    batch_id=batch_id,
                    status="fail",
                    error_reason="File could not be moved back",
                ))
                failed += 1

        return {
            "success": True,
            "batch_id": batch_id,
            "rolled_back": rolled,
            "failed": failed,
        }

    def query_batch(self, batch_id: str) -> Optional[Dict]:
        batch = self.batch_mgr.get_batch(batch_id)
        if not batch:
            return None
        logs = self.logger.query_by_batch(batch_id)
        return {
            "batch": batch.to_dict(),
            "logs": [l.to_dict() for l in logs],
        }

    def list_batches(self) -> List[Dict]:
        return [b.to_dict() for b in self.batch_mgr.list_batches()]

    def list_failures(self) -> List[Dict]:
        entries = self.failure_queue.load()
        return [e.__dict__ for e in entries]

    def check_duplicate_batch_ids(self) -> List[str]:
        ids = [b.batch_id for b in self.batch_mgr.list_batches()]
        seen = set()
        dupes = []
        for bid in ids:
            if bid in seen:
                dupes.append(bid)
            seen.add(bid)
        return sorted(set(dupes))

    def export_logs(self, fmt: str, output_path: str) -> str:
        out_abs = os.path.abspath(output_path)
        if os.path.isdir(out_abs):
            raise ValueError(
                f"导出路径指向了目录而非文件: {out_abs}"
            )
        if fmt == "csv":
            return self.logger.export_csv(out_abs)
        return self.logger.export_json(out_abs)
