import json

import httpx
import pytest

from ccitecheck.infrastructure import http as http_module
from ccitecheck.infrastructure.http import (
    HttpRequestError,
    RetryPolicy,
    post_json_with_retry,
)
from ccitecheck.domain.checks import CheckVerdict
from ccitecheck.verification.semantic import QwenSemanticChecker
from ccitecheck.verification.legal_application import ApplicationAuthority


def _install_mock_client(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_module, "_HTTP_CLIENT", client)
    return client


def _qwen_response(text='{"verdict":"pass"}'):
    return {
        "output": [{"content": [{"type": "output_text", "text": text}]}]
    }


def test_application_check_receives_all_statutes_once_and_normalizes_to_review(monkeypatch):
    captured = {}
    response = json.dumps({
        "comparison": "事实不足以适用法条",
        "verdict": "issue",
        "reviews": [{
            "error_type": "rule_fact_mismatch",
            "review_level": "HIGH",
            "summary": "原文没有给出适用该法条的事实。",
            "suggestion": "补充事实依据或核对法条适用。",
            "related_sources": ["《甲法》第一条"],
        }],
        "notes": "",
    }, ensure_ascii=False)

    def handler(request):
        payload = json.loads(request.content)
        captured["input"] = json.loads(payload["input"][1]["content"])
        return httpx.Response(200, json=_qwen_response(response))

    _install_mock_client(monkeypatch, handler)
    checker = QwenSemanticChecker(api_key="test-key")
    authorities = [
        ApplicationAuthority("《甲法》第一条", "甲法", "第一条", "承担违约责任。", {}),
        ApplicationAuthority("《乙法》第二条", "乙法", "第二条", "合同依法成立。", {}),
    ]

    result = checker.compare_application("据此，合同当然无效。", authorities)

    assert captured["input"]["original_text"] == "据此，合同当然无效。"
    assert [item["cited_source"] for item in captured["input"]["statutes"]] == [
        "《甲法》第一条", "《乙法》第二条",
    ]
    assert result.verdict == "review"
    assert result.reviews[0].error_type == "rule_fact_mismatch"
    assert result.reviews[0].review_level == "待核查"


def test_fidelity_triage_repairs_unknown_source_id(monkeypatch):
    responses = iter([
        json.dumps({
            "verdict": "candidate_99",
            "checks": dict.fromkeys(
                ["subject", "condition", "numbers", "negation", "consequence"], True
            ),
            "differences": [],
        }),
        json.dumps({
            "verdict": "cited",
            "checks": dict.fromkeys(
                ["subject", "condition", "numbers", "negation", "consequence"], True
            ),
            "differences": [],
        }),
    ])
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_qwen_response(next(responses)))

    _install_mock_client(monkeypatch, handler)
    result = QwenSemanticChecker(api_key="test-key").triage_fidelity(
        "当事人应当履行义务",
        {
            "id": "cited", "sources": [{"law_title": "甲法", "article_no": "第一条"}],
            "article_text": "当事人应当履行义务",
        },
        [],
    )

    assert result.verdict == "cited"
    assert len(calls) == 2


_REASONING_TEXT = "公司章程可以限制股权转让。该约定系公司自治的体现。该约定不违反公司法的禁止性规定。"


def _reasoning_response(assertions):
    return json.dumps(
        {"verdict": "issue", "assertions": assertions, "notes": ""},
        ensure_ascii=False,
    )


def test_case_distorted_with_valid_hits_returns_excerpt_from_source(monkeypatch):
    response = _reasoning_response([{
        "id": 1, "judgment": "distorted", "hit_sentence_ids": [1, 2],
        "risk_level": "HIGH",
        "diff_summary": "文书将章程限制股权转让扩张为股东失权。",
        "suggestion": "按说理原句改写为章程限制转让。",
    }])
    _install_mock_client(
        monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)),
    )

    check = QwenSemanticChecker(api_key="test-key").compare_holding(
        "法院认可章程对股东失权的安排。", _REASONING_TEXT, "某案",
    )

    assert check.verdict == CheckVerdict.ISSUE
    finding = check.findings[0]
    assert finding.code.value == "holding_distorted"
    assert finding.matched_excerpt == "……公司章程可以限制股权转让。该约定系公司自治的体现。……"


def test_case_distorted_with_fabricated_hits_downgrades_to_manual(monkeypatch):
    response = _reasoning_response([{
        "id": 1, "judgment": "distorted", "hit_sentence_ids": [99],
        "risk_level": "HIGH", "diff_summary": "编造的定位", "suggestion": "改写。",
    }])
    _install_mock_client(
        monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)),
    )

    check = QwenSemanticChecker(api_key="test-key").compare_holding(
        "法院认可章程对股东失权的安排。", _REASONING_TEXT, "某案",
    )

    finding = check.findings[0]
    assert finding.code.value == "holding_unsupported"
    assert finding.matched_excerpt is None
    assert "人工核对" in finding.suggestion


def test_case_unsupported_reports_missing_basis(monkeypatch):
    response = _reasoning_response([{
        "id": 1, "judgment": "unsupported", "hit_sentence_ids": [],
        "risk_level": "MEDIUM",
        "diff_summary": "说理只讨论股权转让限制，未涉及公司解散。",
        "suggestion": "删除该观点或核对案例来源。",
    }])
    _install_mock_client(
        monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)),
    )

    check = QwenSemanticChecker(api_key="test-key").compare_holding(
        "法院认为公司应当解散。", _REASONING_TEXT, "某案",
    )

    assert check.verdict == CheckVerdict.ISSUE
    assert check.findings[0].code.value == "holding_unsupported"


def test_case_unsupported_on_truncated_reasoning_defers_to_manual(monkeypatch):
    truncated_reasoning = "公司章程可以限制股权转让。损失赔偿额应当相当于因违约所造"
    response = _reasoning_response([{
        "id": 1, "judgment": "unsupported", "hit_sentence_ids": [],
        "risk_level": "MEDIUM", "diff_summary": "未讨论", "suggestion": "核对。",
    }])
    _install_mock_client(
        monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)),
    )

    check = QwenSemanticChecker(api_key="test-key").compare_holding(
        "法院认为公司应当解散。", truncated_reasoning, "某案",
    )

    assert check.verdict == CheckVerdict.INSUFFICIENT_INPUT
    assert check.findings == []
    assert "截断" in check.notes and "人工核对" in check.notes


def test_case_all_supported_passes(monkeypatch):
    response = json.dumps({
        "verdict": "pass",
        "assertions": [{"id": 1, "judgment": "supported", "hit_sentence_ids": [2]}],
        "notes": "",
    }, ensure_ascii=False)
    _install_mock_client(
        monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)),
    )

    check = QwenSemanticChecker(api_key="test-key").compare_holding(
        "章程限制转让系公司自治。", _REASONING_TEXT, "某案",
    )

    assert check.verdict == CheckVerdict.PASS
    assert check.findings == []


def test_retry_after_429_then_success(monkeypatch):
    calls = 0
    sleeps = []

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"ok": True})

    _install_mock_client(monkeypatch, handler)
    result = post_json_with_retry(
        "https://example.test/responses",
        {},
        {},
        policy=RetryPolicy(),
        sleep=sleeps.append,
    )
    assert result == {"ok": True}
    assert calls == 2
    assert sleeps == [3.0]


def test_two_connect_errors_then_success(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("broken", request=request)
        return httpx.Response(200, json={"ok": True})

    _install_mock_client(monkeypatch, handler)
    result = post_json_with_retry(
        "https://example.test/responses",
        {},
        {},
        policy=RetryPolicy(),
        sleep=lambda delay: None,
    )
    assert result == {"ok": True}
    assert calls == 3


def test_http_400_is_not_retried(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(HttpRequestError) as caught:
        post_json_with_retry(
            "https://example.test/responses", {}, {}, policy=RetryPolicy()
        )
    assert caught.value.error_code == "http_4xx"
    assert calls == 1


def test_retry_after_beyond_budget_fails_without_sleep(monkeypatch):
    sleeps = []
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(HttpRequestError) as caught:
        post_json_with_retry(
            "https://example.test/responses",
            {},
            {},
            policy=RetryPolicy(budget_seconds=90),
            sleep=sleeps.append,
        )
    assert caught.value.error_code == "rate_limited"
    assert calls == 1
    assert sleeps == []


def test_retry_budget_stops_attempts_without_exceeding_deadline(monkeypatch):
    now = [0.0]
    calls = 0

    def clock():
        return now[0]

    def handler(request):
        nonlocal calls
        calls += 1
        now[0] += 43.0
        raise httpx.ReadTimeout("slow upstream", request=request)

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(HttpRequestError) as caught:
        post_json_with_retry(
            "https://example.test/responses",
            {},
            {},
            policy=RetryPolicy(budget_seconds=90, max_attempts=4),
            sleep=lambda delay: now.__setitem__(0, now[0] + delay),
            clock=clock,
        )
    assert caught.value.error_code == "timeout"
    assert calls == 2
    assert now[0] <= 90.0


def test_model_selection_switches_provider_and_protocol(monkeypatch):
    """三个可选模型：千问走 DashScope /responses，DeepSeek 走 /chat/completions。"""
    from ccitecheck.verification.semantic import QwenSemanticChecker, resolve_model_option

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.delenv("LLM_DEFAULT_MODEL", raising=False)

    plus = QwenSemanticChecker.from_env("qwen3.7-plus")
    assert plus.provider == "dashscope" and plus.model == "qwen3.7-plus"

    mx = QwenSemanticChecker.from_env("qwen3.7-max")
    assert mx.provider == "dashscope" and mx.model == "qwen3.7-max"

    deepseek = QwenSemanticChecker.from_env("deepseek")
    assert deepseek.provider == "deepseek"
    assert deepseek.base_url == "https://api.deepseek.com"

    # 未知标识回退到第一个模型
    assert resolve_model_option("不存在的模型").key == "qwen3.7-plus"


def test_deepseek_uses_chat_completions_endpoint(monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"verdict":"pass"}'}}]})

    _install_mock_client(monkeypatch, handler)
    checker = QwenSemanticChecker(api_key="k", model="deepseek-v4-pro",
                                  base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                                  provider="deepseek")
    text = checker._chat("系统提示", "用户内容")

    assert captured["url"].endswith("/chat/completions")
    assert "messages" in captured["body"] and "input" not in captured["body"]
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert text == '{"verdict":"pass"}'
