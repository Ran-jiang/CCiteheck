"""北大法宝案例检索适配器。

本适配器只把通用案例溯源操作转换为北大法宝客户端调用。候选选择以及
通过或人工复核判定属于判定层，不在这里处理。
"""

from __future__ import annotations

from typing import Optional, Protocol

from .client import PkulawCaseRecord, PkulawMcpClient


class CaseSearcher(Protocol):
    """案例精准检索和语义补查接口。"""

    def search_keyword(self, title: str, fulltext: str) -> list[PkulawCaseRecord]:
        ...

    def search_semantic(self, text: str) -> list[PkulawCaseRecord]:
        ...


class PkulawCaseSource:
    """延迟创建北大法宝客户端的案例溯源实现。"""

    def __init__(self, client: Optional[PkulawMcpClient] = None):
        self.client = client

    def search_keyword(self, title: str, fulltext: str) -> list[PkulawCaseRecord]:
        return self._client().get_case_list(title=title, fulltext=fulltext)

    def search_semantic(self, text: str) -> list[PkulawCaseRecord]:
        return self._client().search_cases(text)

    def _client(self) -> PkulawMcpClient:
        if self.client is None:
            self.client = PkulawMcpClient()
        return self.client


__all__ = ["CaseSearcher", "PkulawCaseSource"]
