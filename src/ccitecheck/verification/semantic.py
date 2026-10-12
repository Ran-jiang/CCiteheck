"""使用大模型执行法律适用核查、案例核查与检索规划。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from ..infrastructure.config import load_project_env
from ..infrastructure.http import (
    HttpRequestError,
    HttpResponseJSONError,
    RetryPolicy,
    post_json_with_retry,
)

from ..domain.queries import (
    FidelityTriage,
    QueryExtractionFill,
    QueryInference,
    RepairPlan,
    RerankBatch,
    RerankCandidate,
    RetrievalQueryPlan,
)
from ..domain.case_results import CaseErrorCode, CaseFinding, CaseHoldingCheck
from ..domain.checks import CheckVerdict
from ..domain.revisions import RevisionProposal
from ..domain.statute_results import (
    LegalApplicationCheck,
    LegalApplicationReview,
)
from ..query_construction.versioning import explicit_version_hint
from .legal_application import ApplicationAuthority
from .markers import strip_internal_markers
from .reasoning import (
    build_excerpt,
    clean_reasoning_text,
    reasoning_is_truncated,
    split_reasoning_sentences,
)

DEFAULT_MODEL = "qwen3.7-plus"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
REASONING_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "case_reasoning_check.md"
APPLICATION_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "statute_application_check.md"
FIDELITY_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "fidelity_triage.md"
RELATED_PLANNER_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "query_construction" / "prompts" / "related_planner.md"
)
REPAIR_PLANNER_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "query_construction" / "prompts" / "repair_planner.md"
)
EXTRACTION_FILL_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "query_construction" / "prompts" / "extraction_fill.md"
)
QUERY_INFERENCE_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "query_construction" / "prompts" / "query_inference.md"
)
RELATED_RERANKER_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "retrieval" / "prompts" / "related_reranker.md"
)
_APPLICATION_ERROR_TYPES = {
    "rule_fact_mismatch",
}
_StructuredModel = TypeVar("_StructuredModel", bound=BaseModel)


class SemanticChecker(Protocol):
    def triage_fidelity(
        self,
        quote: str,
        cited: dict[str, Any] | None,
        candidates: list[dict[str, Any]],
    ) -> FidelityTriage: ...

    def compare_application(
        self,
        original_text: str,
        authorities: list[ApplicationAuthority],
    ) -> LegalApplicationCheck: ...

    def plan_related(
        self, *, raw_text: str, raw_title: str, raw_time: str | None,
        target_name: str,
    ) -> RetrievalQueryPlan: ...

    def plan_repair(self, **kwargs) -> RepairPlan: ...

    def extract_query_facts(self, **kwargs) -> QueryExtractionFill: ...

    def infer_query(self, **kwargs) -> QueryInference: ...

    def rerank_related(
        self, query_text: str, candidates: list[RerankCandidate]
    ) -> RerankBatch: ...


class SemanticCheckError(RuntimeError):
    error_code = "semantic_error"


class SemanticTransportError(SemanticCheckError):
    def __init__(self, message: str, error_code: str = "transport_error"):
        super().__init__(message)
        self.error_code = error_code


class SemanticResponseError(SemanticCheckError):
    def __init__(self, message: str, error_code: str = "invalid_schema"):
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class ModelOption:
    """可选模型：同一套提示词，按厂商协议差异发送请求。"""

    key: str          # 前端与 API 使用的标识
    label: str        # 展示名
    provider: str     # dashscope（通义千问 /responses）| deepseek（DeepSeek 官方 /chat/completions）
    env_model: str    # 读取实际 model id 的环境变量
    default_model: str
    env_keys: tuple[str, ...]  # 该模型的密钥环境变量，按优先级


SUPPORTED_MODELS: tuple[ModelOption, ...] = (
    ModelOption("qwen3.7-plus", "通义千问 3.7 Plus", "dashscope", "QWEN_MODEL", "qwen3.7-plus",
                ("QWEN_PLUS_API_KEY", "DASHSCOPE_API_KEY", "LLM_API_KEY")),
    ModelOption("qwen3.7-max", "通义千问 3.7 Max", "dashscope", "QWEN_MAX_MODEL", "qwen3.7-max",
                ("QWEN_MAX_API_KEY", "DASHSCOPE_API_KEY", "LLM_API_KEY")),
    ModelOption("deepseek", "DeepSeek V4 Pro", "deepseek", "DEEPSEEK_MODEL", "deepseek-v4-pro",
                ("DEEPSEEK_API_KEY",)),
)


def model_api_key(option: "ModelOption") -> str | None:
    """按优先级取该模型的密钥：专用 key 优先，未设则回退共用 key。"""
    for name in option.env_keys:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def resolve_model_option(key: str | None) -> ModelOption:
    """按标识取模型；未指定时用 LLM_DEFAULT_MODEL，再退回第一个。"""
    wanted = (key or os.getenv("LLM_DEFAULT_MODEL") or "").strip()
    for option in SUPPORTED_MODELS:
        if option.key == wanted:
            return option
    if wanted:
        # 兼容直接传实际 model id（如 CLI 历史用法）
        for option in SUPPORTED_MODELS:
            if os.getenv(option.env_model, option.default_model) == wanted:
                return option
    return SUPPORTED_MODELS[0]


@dataclass(frozen=True)
class QwenSemanticChecker:
    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout: int = 60
    retry_budget_seconds: float = 90.0
    retry_max_attempts: int = 4
    provider: str = "dashscope"

    @classmethod
    def from_env(cls, model: str | None = None) -> "QwenSemanticChecker":
        load_project_env()
        option = resolve_model_option(model)
        api_key = model_api_key(option)
        if not api_key:
            raise SemanticCheckError(
                f"{option.env_keys[0]} is required for semantic checks with {option.label}"
            )
        if option.provider == "deepseek":
            base_url = (
                os.getenv("DEEPSEEK_BASE_URL")
                or os.getenv("LLM_BASE_URL")
                or DEFAULT_DEEPSEEK_BASE_URL
            )
        else:
            base_url = (
                os.getenv("QWEN_BASE_URL")
                or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL)
            )
        return cls(
            api_key=api_key,
            model=os.getenv(option.env_model, option.default_model),
            base_url=base_url.rstrip("/"),
            timeout=int(os.getenv("QWEN_TIMEOUT_SECONDS", "60")),
            retry_budget_seconds=float(
                os.getenv("QWEN_RETRY_BUDGET_SECONDS", "90")
            ),
            retry_max_attempts=int(os.getenv("QWEN_RETRY_MAX_ATTEMPTS", "4")),
            provider=option.provider,
        )

    def _chat(
        self, system_prompt: str, user_content: str,
        *, temperature: float | None = None,
    ) -> str:
        """按厂商协议发起一次对话并返回模型输出文本。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        if self.provider == "dashscope":
            payload = {
                "model": self.model, "input": messages, "enable_thinking": False,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            return _extract_output_text(
                self._post("/responses", payload)
            )
        payload = {
            "model": self.model,
            "messages": messages,
            # DeepSeek V4 默认开启高强度思考。CCitecheck 的这些调用均要求
            # 结构化 JSON，关闭思考可避免短任务被推理耗时拖至超时。
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        # DeepSeek 要求 JSON 模式下提示词必须出现 "json" 字样，否则直接 400；
        # 个别提示词（如修复类系统提示）不满足，缺字时自动补齐。
        if "json" not in (system_prompt + user_content).lower():
            payload["messages"] = [
                *messages[:1],
                {"role": "user", "content": f"{user_content}\n（请以 JSON 格式输出）"},
            ]
        if temperature is not None:
            payload["temperature"] = temperature
        return _extract_chat_text(
            self._post("/chat/completions", payload)
        )

    def triage_fidelity(
        self,
        quote: str,
        cited: dict[str, Any] | None,
        candidates: list[dict[str, Any]],
    ) -> FidelityTriage:
        allowed = {
            *(candidate["id"] for candidate in candidates),
            *({"cited"} if cited is not None else set()),
            "none",
        }
        return self._structured(
            FIDELITY_PROMPT_PATH,
            {"quote": quote, "cited": cited, "candidates": candidates},
            FidelityTriage,
            "fidelity triage",
            temperature=0.0,
            validation_context={"allowed_verdicts": sorted(allowed)},
        )

    def compare_application(
        self,
        original_text: str,
        authorities: list[ApplicationAuthority],
    ) -> LegalApplicationCheck:
        """一次比较同一原文与其全部已确认法条。"""
        if not authorities or any(not item.article_text for item in authorities):
            raise SemanticCheckError("未取得完整法条原文，无法进行法律适用核查")
        user_input = {
            "original_text": original_text,
            "statutes": [item.as_prompt_input() for item in authorities],
        }
        output_text = self._chat(
            APPLICATION_PROMPT_PATH.read_text(encoding="utf-8"),
            json.dumps(user_input, ensure_ascii=False),
            temperature=0.0,
        )
        try:
            raw = _load_json_object(output_text)
        except (json.JSONDecodeError, ValueError):
            repaired = self._chat(
                "将用户提供的内容修复为一个语义等价、可由 JSON.parse() 直接解析的"
                "合法 JSON 对象。不得增删事实，不得输出 Markdown 或解释。",
                output_text,
            )
            try:
                raw = _load_json_object(repaired)
            except (json.JSONDecodeError, ValueError) as exc:
                raise SemanticResponseError(
                    f"Qwen returned invalid application JSON: {exc}", "invalid_json"
                ) from exc
        try:
            return _application_check_from_raw(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise SemanticResponseError(
                f"Qwen returned invalid application JSON: {exc}", "invalid_schema"
            ) from exc

    def compare_holding(
        self,
        paraphrase_text: str,
        authoritative_holding_text: str,
        case_title: str,
    ) -> CaseHoldingCheck:
        assertions = split_reasoning_sentences(paraphrase_text) or [clean_reasoning_text(paraphrase_text)]
        sentences = split_reasoning_sentences(authoritative_holding_text)
        truncated = reasoning_is_truncated(sentences)
        raw_text = self._chat(
            REASONING_PROMPT_PATH.read_text(encoding="utf-8"),
            json.dumps({
                    "case_title": case_title,
                    "assertions": [
                        {"id": index, "text": text}
                        for index, text in enumerate(assertions, start=1)
                    ],
                    "reasoning_sentences": [
                        {"id": index, "text": text}
                        for index, text in enumerate(sentences, start=1)
                    ],
                "reasoning_truncated": truncated,
            }, ensure_ascii=False),
            temperature=0.0,
        )
        raw = _load_json_object(raw_text)
        try:
            return _case_reasoning_check_from_raw(raw, assertions, sentences, truncated)
        except (ValueError, TypeError) as exc:
            raise SemanticResponseError(f"Qwen returned invalid case reasoning JSON: {exc}") from exc

    def plan_related(
        self, *, raw_text: str, raw_title: str, raw_time: str | None,
        target_name: str,
    ) -> RetrievalQueryPlan:
        return self._structured(
            RELATED_PLANNER_PROMPT_PATH,
            {
                "raw_text": raw_text,
                "raw_title": raw_title,
                "raw_time": raw_time,
                "target_name": target_name,
            },
            RetrievalQueryPlan,
            "related planner",
        )

    def extract_query_facts(
        self, *, claim_text: str, mention: dict, missing_fields: list[str],
    ) -> QueryExtractionFill:
        return self._structured(
            EXTRACTION_FILL_PROMPT_PATH,
            {
                "claim_text": claim_text,
                "mention": mention,
                "missing_fields": missing_fields,
            },
            QueryExtractionFill,
            "query extraction fill",
            temperature=0.0,
        )

    def infer_query(
        self, *, claim_text: str, context_text: str, extracted: dict,
        alias_context: dict | None, inherited_context: dict | None,
        allowed_title_candidates: list[str],
    ) -> QueryInference:
        return self._structured(
            QUERY_INFERENCE_PROMPT_PATH,
            {
                "claim_text": claim_text,
                "context_text": context_text,
                "extracted": extracted,
                "alias_context": alias_context,
                "inherited_context": inherited_context,
                "allowed_title_candidates": allowed_title_candidates,
            },
            QueryInference,
            "query inference",
            temperature=0.0,
        )

    def plan_repair(
        self, *, raw_text: str, raw_title: str, raw_time: str | None,
        article_no: str | None, retrieval_status: str,
        candidate_titles: list[str], fuzzy_candidates: list[str],
        allow_unlisted: bool = False,
        authoritative_text: str | None = None,
    ) -> RepairPlan:
        plan = self._structured(
            REPAIR_PLANNER_PROMPT_PATH,
            {
                "raw_text": raw_text,
                "raw_title": raw_title,
                "raw_time": raw_time,
                "article_no": article_no,
                "retrieval_status": retrieval_status,
                "candidate_titles": candidate_titles,
                "fuzzy_candidates": fuzzy_candidates,
                "allow_unlisted": allow_unlisted,
                "authoritative_text": authoritative_text,
            },
            RepairPlan,
            "repair planner",
        )
        if plan.retry_request and not plan.retry_request.version_hint:
            hint = _raw_version_hint(raw_time)
            if hint:
                plan = plan.model_copy(update={
                    "retry_request": plan.retry_request.model_copy(
                        update={"version_hint": hint}
                    )
                })
        return plan

    def rerank_related(
        self, query_text: str, candidates: list[RerankCandidate]
    ) -> RerankBatch:
        result = self._structured(
            RELATED_RERANKER_PROMPT_PATH,
            {
                "query_text": query_text,
                "candidates": [item.model_dump(mode="json") for item in candidates],
            },
            RerankBatch,
            "related reranker",
        )
        expected = {item.candidate_id for item in candidates}
        returned = [item.candidate_id for item in result.results]
        if len(returned) != len(set(returned)) or set(returned) != expected:
            raise SemanticResponseError(
                "related reranker returned missing, duplicate, or unknown candidate_id",
                "invalid_schema",
            )
        return result

    def _structured(
        self,
        prompt_path: Path,
        payload: dict[str, Any],
        model_type: type[_StructuredModel],
        label: str,
        *,
        temperature: float = 0.1,
        validation_context: dict[str, Any] | None = None,
    ) -> _StructuredModel:
        output = self._chat(
            prompt_path.read_text(encoding="utf-8"),
            json.dumps(payload, ensure_ascii=False),
            temperature=temperature,
        )
        for attempt in range(2):
            try:
                return model_type.model_validate(
                    _load_json_object(output), context=validation_context
                )
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                if attempt:
                    raise SemanticResponseError(
                        f"LLM returned invalid {label} JSON: {exc}", "invalid_schema"
                    ) from exc
                output = self._chat(
                    "只修复输入 JSON，使其严格符合给定 JSON Schema。不得增删语义，"
                    "不得输出 Markdown 或解释。",
                    json.dumps({
                        "schema": model_type.model_json_schema(),
                        "constraints": validation_context,
                        "invalid_output": output,
                    }, ensure_ascii=False),
                    temperature=0.0,
                )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return post_json_with_retry(
                f"{self.base_url}{path}",
                payload,
                {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                },
                policy=RetryPolicy(
                    max_attempts=self.retry_max_attempts,
                    budget_seconds=self.retry_budget_seconds,
                    read_timeout=float(self.timeout),
                ),
            )
        except HttpRequestError as exc:
            raise SemanticTransportError(
                f"LLM API request failed: {exc}", exc.error_code
            ) from exc
        except HttpResponseJSONError as exc:
            raise SemanticResponseError(
                f"Qwen returned invalid response JSON: {exc}", "invalid_json"
            ) from exc


def _risk_level(issue: dict[str, Any]) -> str:
    """模型偶尔返回小写风险等级，统一大写后再交给领域模型校验。"""
    return str(issue.get("risk_level", "")).strip().upper()


def _application_check_from_raw(raw: dict[str, Any]) -> LegalApplicationCheck:
    raw_reviews = raw.get("reviews", [])
    if not isinstance(raw_reviews, list):
        raise ValueError("reviews must be a list")
    reviews: list[LegalApplicationReview] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_reviews:
        if not isinstance(item, dict):
            raise ValueError("review item must be an object")
        error_type = str(item.get("error_type", ""))
        if error_type not in _APPLICATION_ERROR_TYPES:
            raise ValueError(f"unsupported application error_type: {error_type}")
        sources = item.get("related_sources") or []
        if not isinstance(sources, list):
            raise ValueError("related_sources must be a list")
        summary = _user_text(str(item["summary"]), limit=300)
        key = (error_type, re.sub(r"\s+", "", summary))
        if key in seen:
            continue
        seen.add(key)
        reviews.append(LegalApplicationReview(
            error_type=error_type,
            review_level="待核查",
            summary=summary,
            suggestion=strip_internal_markers(str(item["suggestion"])),
            related_sources=[
                strip_internal_markers(str(source)) for source in sources
            ],
        ))
    # 模型层没有输出“未通过”的权限：只要提出事项，统一收敛为待核查；
    # 没有具体事项时统一为 pass，避免空泛提示。
    return LegalApplicationCheck(
        verdict="review" if reviews else "pass",
        reviews=reviews,
        comparison=_user_text(str(raw.get("comparison", "")), limit=300),
        notes=(
            strip_internal_markers(str(raw.get("notes", "")))
            if reviews else ""
        ),
    )


def _case_reasoning_check_from_raw(
    raw: dict[str, Any],
    assertions: list[str],
    sentences: list[str],
    truncated: bool,
) -> CaseHoldingCheck:
    """按定位校验结果分流两类错误；模型给的是材料，分支由程序决定。"""
    if raw.get("verdict") == CheckVerdict.INSUFFICIENT_INPUT.value:
        return CaseHoldingCheck(
            verdict=CheckVerdict.INSUFFICIENT_INPUT,
            notes=strip_internal_markers(str(raw.get("notes", ""))),
        )
    items = raw.get("assertions")
    if not isinstance(items, list):
        raise ValueError("missing assertions list")
    findings: list[CaseFinding] = []
    truncation_notes: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("assertion item is not an object")
        assertion_id = item.get("id")
        if not isinstance(assertion_id, int) or not 1 <= assertion_id <= len(assertions):
            raise ValueError(f"assertion id {assertion_id!r} out of range")
        assertion_text = assertions[assertion_id - 1]
        judgment = item.get("judgment")
        hits = [hit for hit in item.get("hit_sentence_ids") or [] if isinstance(hit, int)]
        valid_hits = [hit for hit in hits if 1 <= hit <= len(sentences)]
        if judgment in {"supported", "not_holding"}:
            continue
        if judgment == "distorted" and valid_hits:
            finding = CaseFinding(
                code=CaseErrorCode.HOLDING_DISTORTED,
                risk_level=_risk_level_or_medium(item),
                summary=_user_text(item.get("diff_summary"), limit=300)
                or "文书转述与裁判说理原文含义不符。",
                suggestion=_user_text(item.get("suggestion"))
                or "文书转述改变了裁判说理的含义，请对照说理原句修改。",
                matched_excerpt=build_excerpt(sentences, valid_hits),
            )
            proposed = item.get("revised_text")
            if _safe_llm_revision(assertion_text, proposed):
                finding.revision = RevisionProposal(
                    strategy="replace_exact_text",
                    original_text=assertion_text,
                    revised_text=proposed,
                    rationale=finding.suggestion,
                    machine_applicable=True,
                    preconditions=["original_text_unique", "document_unchanged"],
                )
            findings.append(finding)
        elif judgment == "unsupported" and not hits:
            if truncated:
                truncation_notes.append(
                    f"裁判说理文本疑似截断，观点「{assertion_text}」未能核验，请人工核对原文书。"
                )
                continue
            findings.append(CaseFinding(
                code=CaseErrorCode.HOLDING_UNSUPPORTED,
                risk_level=_risk_level_or_medium(item),
                summary=_user_text(item.get("diff_summary"), limit=300)
                or "该观点在北大法宝收录的裁判说理中没有对应内容。",
                suggestion=_user_text(item.get("suggestion"))
                or "该观点在该案裁判说理中无对应依据，请核对是否引用了正确案例。",
            ))
        else:
            # 判定与定位证据自相矛盾（曲解却无有效句号 / 无依据却给了句号），
            # 结论无法确定性验证，保守转人工。
            findings.append(CaseFinding(
                code=CaseErrorCode.HOLDING_UNSUPPORTED,
                risk_level="MEDIUM",
                summary=f"观点「{_user_text(assertion_text, limit=120)}」的核查结论缺少可验证的说理定位。",
                suggestion="未能在裁判说理中定位该观点的依据句段，请人工核对原文书。",
            ))
    notes = strip_internal_markers(str(raw.get("notes", "")))
    if truncation_notes:
        notes = " ".join(filter(None, [notes, *truncation_notes]))
    if findings:
        verdict = CheckVerdict.ISSUE
    elif truncation_notes:
        verdict = CheckVerdict.INSUFFICIENT_INPUT
    else:
        verdict = CheckVerdict.PASS
    return CaseHoldingCheck(verdict=verdict, findings=findings, notes=notes)


def _risk_level_or_medium(item: dict[str, Any]) -> str:
    level = _risk_level(item)
    return level if level in ("HIGH", "MEDIUM") else "MEDIUM"


def _user_text(value: Any, limit: int | None = None) -> str:
    text = strip_internal_markers(value.strip()) if isinstance(value, str) else ""
    if limit is not None and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


_LEGAL_CITATION = re.compile(r"《[^》]+》(?:第[^，。；\s]{1,30}条(?:第[^，。；\s]{1,12}[款项])?)?")
_CASE_NUMBER = re.compile(r"[（(〔]\d{4}[）)〕][^，。；\s]{2,24}?号")


def _safe_llm_revision(original: str, revised: Any) -> bool:
    """LLM 只能最小改写论述，不得顺手改变已确定的引用身份。"""
    if not isinstance(revised, str) or not revised.strip() or revised == original:
        return False
    if len(revised) < max(4, len(original) // 2) or len(revised) > len(original) * 2 + 80:
        return False
    for pattern in (_LEGAL_CITATION, _CASE_NUMBER):
        if pattern.findall(original) != pattern.findall(revised):
            return False
    return True


def _raw_version_hint(raw_time: str | None) -> str | None:
    return explicit_version_hint(raw_time)


def _load_json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = (
            value.removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("semantic response must be a JSON object")
    return parsed


def _extract_chat_text(data: dict[str, Any]) -> str:
    """解析 OpenAI 兼容的 chat/completions 响应（DeepSeek 官方等）。"""
    for choice in data.get("choices", []):
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content
    raise SemanticResponseError(
        "LLM response did not include message content", "invalid_schema"
    )


def _extract_output_text(data: dict[str, Any]) -> str:
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return content["text"]
    raise SemanticResponseError(
        "Qwen response did not include output_text", "invalid_schema"
    )
