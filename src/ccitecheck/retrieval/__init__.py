"""权威证据检索层。"""

from .registry import SourceRegistry
from .service import RetrievalTask, execute

__all__ = ["RetrievalTask", "SourceRegistry", "execute"]
