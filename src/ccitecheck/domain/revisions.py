"""供后端和前端共同遵守的安全修订协议。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class RevisionProposal(BaseModel):
    strategy: Literal[
        "replace_exact_text",
        "replace_citation_locator",
        "replace_case_metadata",
        "manual_edit",
    ]
    original_text: str
    revised_text: str | None = None
    rationale: str
    machine_applicable: bool = False
    preconditions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_machine_revision(self) -> "RevisionProposal":
        if self.machine_applicable:
            if self.strategy == "manual_edit":
                raise ValueError("manual_edit cannot be machine applicable")
            if not self.original_text or not self.revised_text:
                raise ValueError("machine-applicable revision requires exact original and revised text")
            if self.original_text == self.revised_text:
                raise ValueError("revision must change the original text")
        return self


def replacement_revision(
    text: str,
    original: str,
    revised: str,
    rationale: str,
    *,
    preconditions: list[str] | None = None,
) -> RevisionProposal | None:
    if not original or not revised or original == revised or text.count(original) != 1:
        return None
    return RevisionProposal(
        strategy="replace_exact_text",
        original_text=text,
        revised_text=text.replace(original, revised, 1),
        rationale=rationale,
        machine_applicable=True,
        preconditions=[
            "original_text_unique",
            "document_unchanged",
            *(preconditions or []),
        ],
    )


__all__ = ["RevisionProposal", "replacement_revision"]
