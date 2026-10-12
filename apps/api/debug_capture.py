"""本地完整调试现场留档。包含原始文档，仅用于受信任的本机测试。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ccitecheck.infrastructure.paths import PROJECT_ROOT


DEBUG_ROOT = PROJECT_ROOT / "debug_runs"


def enabled() -> bool:
    return os.getenv("CCITECHECK_DEBUG_CAPTURE", "1") != "0"


def create_run(kind: str, document_bytes: bytes | None = None) -> str | None:
    if not enabled():
        return None
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    directory = DEBUG_ROOT / run_id
    directory.mkdir(parents=True, exist_ok=False)
    write_json(run_id, "run.json", {"run_id": run_id, "kind": kind})
    if document_bytes is not None:
        (directory / "document.docx").write_bytes(document_bytes)
    return run_id


def write_json(run_id: str | None, name: str, value: Any) -> None:
    if not run_id:
        return
    path = _run_dir(run_id) / name
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_event(run_id: str, value: Any) -> None:
    path = _run_dir(run_id) / "word-events.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_jsonable(value), ensure_ascii=False) + "\n")


def _run_dir(run_id: str) -> Path:
    if not run_id or any(ch not in "0123456789abcdef-" for ch in run_id.lower()):
        raise ValueError("invalid debug run id")
    path = DEBUG_ROOT / run_id
    if not path.is_dir():
        raise ValueError("debug run not found")
    return path


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
