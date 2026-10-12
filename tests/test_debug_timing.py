from concurrent.futures import ThreadPoolExecutor

import pytest

from ccitecheck.infrastructure.debug_timing import DebugTimer, measure
from ccitecheck.retrieval.sources.pkulaw.client import PkulawMcpClient


def test_debug_timer_accumulates_calls_and_propagates_to_workers():
    timer = DebugTimer()

    def worker(_):
        with measure("retrieval.pkulaw_semantic"):
            return "unchanged"

    with timer.activate():
        with timer.measure("retrieval.total"):
            pass
        with timer.measure("retrieval.total"):
            pass
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(timer.bind(worker), range(3)))

    timer.finish()
    stats = timer.snapshot()

    assert results == ["unchanged"] * 3
    assert stats["retrieval.total"].calls == 2
    assert stats["retrieval.pkulaw_semantic"].calls == 3
    assert stats["retrieval.total"].seconds >= 0
    assert "[CCITECHECK TIMING]" in timer.format_report()
    assert "TOTAL" in timer.format_report()


def test_debug_timer_records_failed_calls_without_swallowing_errors():
    timer = DebugTimer()

    with pytest.raises(RuntimeError, match="boom"):
        with timer.measure("comparison.llm"):
            raise RuntimeError("boom")

    assert timer.snapshot()["comparison.llm"].calls == 1


def test_debug_timer_does_not_double_count_nested_same_name():
    timer = DebugTimer()

    with timer.activate(), timer.measure("retrieval.total"):
        with timer.measure("retrieval.total"):
            pass
        with ThreadPoolExecutor(max_workers=1) as pool:
            list(pool.map(timer.bind(lambda _: _nested_measure()), [None]))

    assert timer.snapshot()["retrieval.total"].calls == 1


def _nested_measure():
    with measure("retrieval.total"):
        return None


def test_pkulaw_keyword_and_semantic_calls_have_separate_buckets(monkeypatch):
    client = PkulawMcpClient(access_token="test-token")
    monkeypatch.setattr(client, "_call_tool", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client._parse_law_list_response",
        lambda _: [],
    )
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client._parse_article_search_response",
        lambda _: [],
    )
    timer = DebugTimer()

    with timer.activate():
        client.get_law_list(title="民法典")
        client.search_law_articles("合同解除")

    stats = timer.snapshot()
    assert stats["retrieval.pkulaw_keyword"].calls == 1
    assert stats["retrieval.pkulaw_semantic"].calls == 1


def test_pkulaw_queue_wait_is_measured(monkeypatch):
    client = PkulawMcpClient(access_token="test-token")
    monkeypatch.setattr(client, "_perform_tool_call", lambda *args, **kwargs: {})
    timer = DebugTimer()

    with timer.activate():
        client._serialized_tool_call("/test", "test", {})

    assert timer.snapshot()["retrieval.pkulaw_queue_wait"].calls == 1
