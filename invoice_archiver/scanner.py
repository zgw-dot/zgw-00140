import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .config import ArchiveConfig


@dataclass
class ScannedFile:
    path: str
    filename: str
    extension: str
    supplier_code: Optional[str]
    parse_error: Optional[str]


def scan_source_dir(config: ArchiveConfig) -> Tuple[List[ScannedFile], List[ScannedFile]]:
    valid: List[ScannedFile] = []
    skipped: List[ScannedFile] = []
    if not os.path.isdir(config.source_dir):
        return valid, skipped

    for root, _dirs, files in os.walk(config.source_dir):
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            ext = os.path.splitext(fname)[1].lower()

            if ext not in config.active_extensions:
                skipped.append(ScannedFile(
                    path=fpath,
                    filename=fname,
                    extension=ext,
                    supplier_code=None,
                    parse_error=f"Extension {ext} not in active set (disabled or not enabled)",
                ))
                continue

            match = config.compiled_pattern.search(fname)
            if match:
                supplier_code = match.group(1).upper()
                valid.append(ScannedFile(
                    path=fpath,
                    filename=fname,
                    extension=ext,
                    supplier_code=supplier_code,
                    parse_error=None,
                ))
            else:
                skipped.append(ScannedFile(
                    path=fpath,
                    filename=fname,
                    extension=ext,
                    supplier_code=None,
                    parse_error=f"Cannot parse supplier code from filename: {fname}",
                ))

    return valid, skipped
