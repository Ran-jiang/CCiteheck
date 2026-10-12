"""基础设施适配器共享的文件系统路径。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

__all__ = ["PROJECT_ROOT"]
