import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Set, Tuple

import yaml


def _app_data_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA", str(Path.home()))
    else:
        base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return os.path.join(base, "InvoiceArchiver")


def _project_source_root() -> str:
    if hasattr(sys, "frozen"):
        top = os.path.dirname(sys.executable)
    else:
        try:
            top = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        except NameError:
            top = os.getcwd()
    return os.path.abspath(top)


def _expand(path: str) -> str:
    if not path:
        return path
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def _paths_overlap(p1: str, p2: str) -> bool:
    try:
        p1a = os.path.abspath(p1)
        p2a = os.path.abspath(p2)
        return p1a == p2a or p1a.startswith(p2a + os.sep) or p2a.startswith(p1a + os.sep)
    except Exception:
        return False


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
        self.project_source_root = _project_source_root()
        self.source_dir = _expand(self.source_dir)
        self.archive_dir = _expand(self.archive_dir)
        self.state_dir = _expand(self.state_dir)
        self.log_dir = _expand(self.log_dir)

    def paths_separated_from_source(self) -> Tuple[bool, List[str]]:
        issues: List[str] = []
        root = self.project_source_root
        checks = [
            ("source_dir", self.source_dir),
            ("archive_dir", self.archive_dir),
            ("state_dir", self.state_dir),
            ("log_dir", self.log_dir),
        ]
        for name, path in checks:
            if _paths_overlap(path, root) and path != root:
                issues.append(
                    f"{name}={path} 位于源码目录 {root} 之下，运行产物可能污染 git 仓库"
                )
        return (len(issues) == 0), issues

    @property
    def active_extensions(self) -> Set[str]:
        enabled = set(e.lower() for e in self.enabled_extensions)
        disabled = set(e.lower() for e in self.disabled_extensions)
        return enabled - disabled

    @property
    def compiled_pattern(self) -> re.Pattern:
        return re.compile(self.supplier_pattern, re.IGNORECASE)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("project_source_root", None)
        return d


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
        "# ============================================================\n"
        "# 采购发票附件归档工具配置  v2.0+  (源码/运行目录分离版)\n"
        "# ============================================================\n"
        "#\n"
        "# 目录布局说明 (强烈建议保持默认，不要改回源码目录):\n"
        "#   - source_dir:  待归档文件收件箱 (发票、验收单、合同附件等)\n"
        "#   - archive_dir: 归档后文件 (按 供应商编码/批次ID 两级目录组织)\n"
        "#   - state_dir:   状态文件 (batches.json / failure_queue.json)\n"
        "#   - log_dir:     操作日志与导出 (archive_log.jsonl)\n"
        "#\n"
        "# 默认全部位于用户数据目录，与源码仓库彻底分离：\n"
        "#   Windows: %APPDATA%\\InvoiceArchiver\n"
        "#   Linux/macOS: ~/.local/share/InvoiceArchiver\n"
        "#\n"
        "# 可自定义为其他绝对路径；但请勿指向源码仓库内部，\n"
        "# 否则可能污染 git status / 触发 precheck 拦截。\n"
        "#\n"
        "# 源文件文件名格式:  供应商编码_类型_说明.扩展名\n"
        "# 例: SUP001_invoice_202606.pdf\n"
        "#\n"
        "# conflict_policy: 遇到同名归档文件的处理策略\n"
        "#   rename    -> 自动追加 _1, _2, ... 后缀 (默认，最安全)\n"
        "#   overwrite -> 直接覆盖目标文件 (谨慎使用)\n"
        "#   skip      -> 跳过，并写入失败队列\n"
        "#\n"
    )
    if os.name == "nt":
        placeholder = "%APPDATA%\\InvoiceArchiver"
        sep = "\\"
    else:
        placeholder = "~/.local/share/InvoiceArchiver"
        sep = "/"
    data = {
        "source_dir": placeholder + sep + "temp_inbox",
        "archive_dir": placeholder + sep + "archive",
        "state_dir": placeholder + sep + "state",
        "log_dir": placeholder + sep + "logs",
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
