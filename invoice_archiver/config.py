import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Set

import yaml


@dataclass
class ArchiveConfig:
    source_dir: str = "./temp_inbox"
    archive_dir: str = "./archive"
    state_dir: str = "./state"
    log_dir: str = "./logs"
    supplier_pattern: str = r"^([A-Z]{2,5}\d{3,6})[_-]"
    batch_size: int = 50
    enabled_extensions: List[str] = field(default_factory=lambda: [
        ".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".docx", ".ofd",
    ])
    disabled_extensions: List[str] = field(default_factory=list)
    conflict_policy: str = "rename"

    @property
    def active_extensions(self) -> Set[str]:
        enabled = set(e.lower() for e in self.enabled_extensions)
        disabled = set(e.lower() for e in self.disabled_extensions)
        return enabled - disabled

    @property
    def compiled_pattern(self) -> re.Pattern:
        return re.compile(self.supplier_pattern, re.IGNORECASE)

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: str) -> ArchiveConfig:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if path.endswith(".json"):
        data = json.loads(raw)
    else:
        data = yaml.safe_load(raw) or {}
    valid_keys = set(ArchiveConfig.__dataclass_fields__.keys())
    filtered = {k: v for k, v in data.items() if k in valid_keys}
    return ArchiveConfig(**filtered)


def save_config(config: ArchiveConfig, path: str) -> None:
    data = config.to_dict()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if path.endswith(".json"):
            json.dump(data, f, indent=2, ensure_ascii=False)
        else:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


def init_sample_config(path: str = "config.yaml") -> str:
    config = ArchiveConfig()
    save_config(config, path)
    return os.path.abspath(path)
