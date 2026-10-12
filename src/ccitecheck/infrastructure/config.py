"""从项目根目录的 .env 加载产品运行配置。"""

from __future__ import annotations

import os
from .paths import PROJECT_ROOT


def _read_env_text(env_path) -> str:
    """容错读取 .env：优先 UTF-8（含 BOM），失败时退回 GBK，再不行按 UTF-8
    忽略坏字节。.env 常在不同系统间编辑，个别非法字节不应拖垮整个服务。"""
    raw = env_path.read_bytes()
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def load_project_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in _read_env_text(env_path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
