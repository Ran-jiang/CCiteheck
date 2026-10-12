import pytest

from ccitecheck.application import DocumentPipelineError, verify_document_claims
from ccitecheck.domain.citation import ClaimDocument, ClaimMeta
from ccitecheck.verification import SemanticCheckError


def test_semantic_initialization_error_is_not_replaced_by_stale_fallback(
    tmp_path, monkeypatch
):
    def fail_from_env(model=None):
        raise SemanticCheckError("DASHSCOPE_API_KEY is required for semantic checks")

    monkeypatch.setattr(
        "ccitecheck.application.check_document.QwenSemanticChecker.from_env",
        fail_from_env,
    )

    with pytest.raises(DocumentPipelineError) as caught:
        verify_document_claims(
            ClaimDocument(claim_meta=ClaimMeta()),
            tmp_path / "laws.sqlite",
            semantic_check=True,
        )

    assert str(caught.value) == "DASHSCOPE_API_KEY is required for semantic checks"
    assert "语义核查默认开启" not in str(caught.value)
