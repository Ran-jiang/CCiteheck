"""裸法名识别词典。

识别层只使用本地法规正式名与别名裁定法名左边界，不调用远程数据源。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from threading import RLock

from ..infrastructure.database import generate_aliases, normalize_title
from ..infrastructure.paths import PROJECT_ROOT


@dataclass(frozen=True)
class LawLexiconEntry:
    surface_title: str
    canonical_title: str
    law_id: int | None = None


@dataclass(frozen=True)
class LawLexiconMatch:
    surface_title: str
    canonical_title: str
    start: int
    end: int


class LawLexicon:
    _cache: dict[tuple, "LawLexicon"] = {}
    _lock = RLock()

    def __init__(self, entries: list[LawLexiconEntry]):
        unique: dict[str, LawLexiconEntry] = {}
        ambiguous: set[str] = set()
        for entry in entries:
            surface = "".join(entry.surface_title.split())
            if not surface or surface in ambiguous:
                continue
            current = unique.get(surface)
            if current is not None and current.canonical_title != entry.canonical_title:
                # 有歧义的别名不参与自动裁定。
                unique.pop(surface, None)
                ambiguous.add(surface)
                continue
            unique[surface] = LawLexiconEntry(surface, entry.canonical_title, entry.law_id)
        self.entries = sorted(unique.values(), key=lambda item: len(item.surface_title), reverse=True)
        self.max_surface_length = max((len(item.surface_title) for item in self.entries), default=0)

    @classmethod
    def load(cls, db_path: str | Path | None = None) -> "LawLexicon":
        path = Path(db_path or PROJECT_ROOT / "data" / "laws.sqlite").resolve()
        key = cls._cache_key(path)
        with cls._lock:
            cached = cls._cache.get(key)
            if cached is not None:
                return cached
            # 同一路径的旧版缓存失效。
            cls._cache = {old_key: value for old_key, value in cls._cache.items() if old_key[0] != str(path)}
            lexicon = cls(cls._load_sqlite(path) if path.exists() else cls._load_json())
            cls._cache[key] = lexicon
            return lexicon

    @classmethod
    def clear_cache(cls) -> None:
        with cls._lock:
            cls._cache.clear()

    @staticmethod
    def _cache_key(path: Path) -> tuple:
        def signature(candidate: Path) -> tuple[int, int]:
            try:
                stat = candidate.stat()
                return stat.st_mtime_ns, stat.st_size
            except OSError:
                return 0, 0

        return (str(path), signature(path), signature(Path(f"{path}-wal")))

    @staticmethod
    def _load_sqlite(path: Path) -> list[LawLexiconEntry]:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT id, title FROM laws").fetchall()
            entries = [LawLexiconEntry(row["title"], row["title"], int(row["id"])) for row in rows]
            entries.extend(
                LawLexiconEntry(row["alias"], row["title"], int(row["id"]))
                for row in conn.execute(
                    """
                    SELECT a.alias, l.title, l.id
                    FROM law_aliases a JOIN laws l ON l.id = a.law_id
                    """
                )
            )
            for row in rows:
                entries.extend(
                    LawLexiconEntry(alias, row["title"], int(row["id"]))
                    for alias in generate_aliases(row["title"])
                )
            return entries
        finally:
            conn.close()

    @staticmethod
    def _load_json() -> list[LawLexiconEntry]:
        catalog = PROJECT_ROOT / "laws" / "common_laws.json"
        records = json.loads(catalog.read_text(encoding="utf-8"))
        entries: list[LawLexiconEntry] = []
        for record in records:
            canonical = record["title"]
            entries.append(LawLexiconEntry(canonical, canonical))
            entries.extend(LawLexiconEntry(alias, canonical) for alias in record.get("aliases", []))
            entries.extend(LawLexiconEntry(alias, canonical) for alias in generate_aliases(canonical))
        return entries

    def longest_suffix_match(self, window: str, *, offset: int = 0) -> LawLexiconMatch | None:
        for entry in self.entries:
            if window.endswith(entry.surface_title):
                start = offset + len(window) - len(entry.surface_title)
                return LawLexiconMatch(
                    surface_title=entry.surface_title,
                    canonical_title=entry.canonical_title,
                    start=start,
                    end=offset + len(window),
                )

    def canonical_title_for(self, title: str) -> str | None:
        """对完整显式法名做符号归一化后的唯一映射，不承担文本坐标计算。"""
        normalized = normalize_title(title)
        canonicals = {
            entry.canonical_title
            for entry in self.entries
            if normalize_title(entry.surface_title) == normalized
        }
        return next(iter(canonicals)) if len(canonicals) == 1 else None


__all__ = ["LawLexicon", "LawLexiconEntry", "LawLexiconMatch"]
