import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Set

import yaml


def _app_data_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA", str(Path.home()))
    else:
        base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return os.path.join(base, "InvoiceArchiver")


def _expand(path: str) -> str:
    if not path:
        return path
    return os.path.expanduser(os.path.expandvars(path))


def _default_source_dir() -> str:
    return os.path.join(_app_data_dir(), "temp_inbox")


def _default_archive_dir() -> str:
    return os.path.join(_app_data_dir(), "archive")


def _default_state_dir() -> str:
    return os.path.join(_app_data_dir(), "state")


def _default_log_dir() -> str:
    return os.path.join(_app_data_dir(), "logs")


@dataclass
class ArchiveConfig:
    source_dir: str = field(default_factory=_default_source_dir)
    archive_dir: str = field(default_factory=_default_archive_dir)
    state_dir: str = field(default_factory=_default_state_dir)
    log_dir: str = field(default_factory=_default_log_dir)
    supplier_pattern: str = r"^([A-Z]{2,5}\d{3,6})[_-]"
    batch_size: int = 50
    enabled_extensions: List[str] = field(default_factory=lambda: [
        ".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".docx", ".ofd",
    ])
    disabled_extensions: List[str] = field(default_factory=list)
    conflict_policy: str = "rename"

    def __post_init__(self):
        self.source_dir = _expand(self.source_dir)
        self.archive_dir = _expand(self.archive_dir)
        self.state_dir = _expand(self.state_dir)
        self.log_dir = _expand(self.log_dir)

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
    comment = (
        "# 采购发票附件归档工具配置\n"
        "# 运行时产物默认保存在 %APPDATA%\\InvoiceArchiver (Windows) 或 ~/.local/share/InvoiceArchiver\n"
        "# source_dir: 请配置你的临时收件箱目录，存放待归档的发票、验收单、合同附件\n"
        "# 源文件文件名格式: 供应商编码_类型_说明.扩展名 例: SUP001_invoice_202606.pdf\n"
        "\n"
    )
    if os.name == "nt":
        placeholder = "%APPDATA%\\InvoiceArchiver"
    else:
        placeholder = "~/.local/share/InvoiceArchiver"
    data = {
        "source_dir": placeholder + "\\temp_inbox" if os.name == "nt" else placeholder + "/temp_inbox",
        "archive_dir": placeholder + "\\archive" if os.name == "nt" else placeholder + "/archive",
        "state_dir": placeholder + "\\state" if os.name == "nt" else placeholder + "/state",
        "log_dir": placeholder + "\\logs" if os.name == "nt" else placeholder + "/logs",
        "supplier_pattern": r"^([A-Z]{2,5}\d{3,6})[_-]",
        "batch_size": 50,
        "enabled_extensions": [".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".docx", ".ofd"],
        "disabled_extensions": [],
        "conflict_policy": "rename",
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if path.endswith(".json"):
            f.write(comment.replace("#", "//"))
            json.dump(data, f, indent=2, ensure_ascii=False)
        else:
            f.write(comment)
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
    return os.path.abspath(path)
