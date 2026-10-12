"""构建法律适用核查任务；不负责法源查询或法律判断。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from ..domain.evidence import ArticleEvidence


@dataclass(frozen=True)
class ApplicationAuthority:
    cited_source: str
    law_title: str
    article_no: str | None
    article_text: str
    source_metadata: dict

    @classmethod
    def from_evidence(
        cls, cited_source: str, evidence: ArticleEvidence
    ) -> "ApplicationAuthority":
        return cls(
            cited_source=cited_source,
            law_title=evidence.law_title,
            article_no=evidence.article_no,
            article_text=evidence.article_text or "",
            source_metadata={
                "version_label": evidence.version_label,
                "version_status": evidence.version_status,
                "effective_from": evidence.effective_from,
                "effective_to": evidence.effective_to,
                **evidence.source_metadata,
            },
        )

    def as_prompt_input(self) -> dict:
        return {
            "cited_source": self.cited_source,
            "law_title": self.law_title,
            "article_no": self.article_no,
            "article_text": self.article_text,
            "source_metadata": self.source_metadata,
        }


@dataclass(frozen=True)
class ApplicationCandidate:
    item_index: int
    claim_id: str
    original_text: str
    authority: ApplicationAuthority


@dataclass(frozen=True)
class ApplicationJob:
    claim_id: str
    original_text: str
    authorities: tuple[ApplicationAuthority, ...]
    result_item_index: int

    @property
    def job_id(self) -> str:
        material = [self.original_text]
        for authority in self.authorities:
            material.extend((
                authority.law_title,
                authority.article_no or "",
                authority.article_text,
            ))
        return "aj_" + hashlib.sha256("\0".join(material).encode("utf-8")).hexdigest()[:16]


def build_application_jobs(
    candidates: list[ApplicationCandidate],
) -> list[ApplicationJob]:
    """同一原文的全部已确认条文合并为一次模型任务，并稳定去重。"""
    grouped: dict[str, list[ApplicationCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.claim_id, []).append(candidate)

    jobs: list[ApplicationJob] = []
    for claim_candidates in grouped.values():
        authorities: list[ApplicationAuthority] = []
        seen: set[tuple[str, str | None, str]] = set()
        for candidate in claim_candidates:
            authority = candidate.authority
            identity = (
                authority.law_title,
                authority.article_no,
                authority.article_text,
            )
            if identity in seen:
                continue
            seen.add(identity)
            authorities.append(authority)
        if authorities:
            first = claim_candidates[0]
            jobs.append(ApplicationJob(
                claim_id=first.claim_id,
                original_text=first.original_text,
                authorities=tuple(authorities),
                result_item_index=first.item_index,
            ))
    return jobs


__all__ = [
    "ApplicationAuthority",
    "ApplicationCandidate",
    "ApplicationJob",
    "build_application_jobs",
]
