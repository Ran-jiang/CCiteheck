from pathlib import Path

from ccitecheck.domain.citation import ClaimType
from ccitecheck.domain.claims import RawCaseMention, RawClaim, RawLegalMention
from ccitecheck.domain.evidence import (
    ArticleEvidence,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
import pytest

from ccitecheck.domain.runs import RunState, VerificationRun
from ccitecheck.infrastructure.database import init_db
from ccitecheck.orchestration import SchedulerContext, verify_claim
from ccitecheck.retrieval import SourceRegistry
from ccitecheck.retrieval.sources import LookupResult
from ccitecheck.retrieval.sources.pkulaw.models import PkulawCaseRecord


class CandidateSource:
    def __init__(self):
        self.requested_titles: list[str] = []

    def lookup(self, request):
        self.requested_titles.append(request.law_title)
        if request.law_title == "中华人民共和国正确法":
            trace = SourceTrace(
                tier=SourceTier.LOCAL_SQLITE,
                source_name="fake",
                status=LookupStatus.ARTICLE_FOUND,
            )
            return LookupResult(
                LookupStatus.ARTICLE_FOUND,
                ArticleEvidence(
                    law_title=request.law_title,
                    article_no=request.article_no,
                    data_source=trace,
                ),
                trace,
            )
        trace = SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="fake",
            status=LookupStatus.LAW_NOT_FOUND,
            metadata={"candidate_titles": ["中华人民共和国正确法"]},
        )
        return LookupResult(LookupStatus.LAW_NOT_FOUND, None, trace)


def test_run_state_rejects_invalid_transition():
    run = VerificationRun(claim_id="claim-1")

    with pytest.raises(ValueError, match="new -> output"):
        run.transition(RunState.OUTPUT)


def test_scheduler_rebuilds_hypothesis_without_mutating_raw_claim(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim = RawClaim(
        claim_id="claim-1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        raw_text="依据《错法名》第一条处理。",
        legal_mentions=[RawLegalMention(
            mention_id="mention-1",
            raw_title="错法名",
            article_raw="第一条",
            recognition_form="explicit",
        )],
    )
    raw_snapshot = claim.model_dump()
    source = CandidateSource()
    context = SchedulerContext(
        law_db=db_path,
        source_registry=SourceRegistry({
            "local_laws": source,
            "pkulaw": source,
        }),
    )

    run = verify_claim(claim, context)

    assert claim.model_dump() == raw_snapshot
    assert source.requested_titles == [
        "错法名",
        "错法名",
        "中华人民共和国正确法",
    ]
    assert len(run.hypothesis_history) == 2
    assert run.hypothesis_retry_count == 1
    assert run.state == RunState.OUTPUT
    assert run.terminal_reason == "verification_complete"


class CaseSource:
    def search_keyword(self, title, fulltext):
        return [PkulawCaseRecord(
            title="甲公司诉乙公司合同纠纷案",
            case_number="（2024）京01民终100号",
            gid="case-1",
            court="北京市第一中级人民法院",
        )]

    def search_semantic(self, text):
        raise AssertionError("精准案号已命中时不应语义补查")


def test_scheduler_uses_case_source_and_case_identity_verification(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim = RawClaim(
        claim_id="case-claim-1",
        claim_type=ClaimType.CASE_CITATION,
        raw_text="参见（2024）京01民终100号民事判决。",
        case_mentions=[RawCaseMention(
            mention_id="case-mention-1",
            raw_case_number="（2024）京01民终100号",
            raw_court="北京市第一中级人民法院",
        )],
    )

    run = verify_claim(claim, SchedulerContext(
        law_db=db_path,
        source_registry=SourceRegistry({"pkulaw_cases": CaseSource()}),
    ))

    assert len(run.hypothesis_history) == 1
    assert len(run.evidence_history) == 1
    assert len(run.verification_history) == 1
    assert run.state == RunState.OUTPUT
    assert run.terminal_reason == "verification_complete"
