"""确定性验证法规引用的款、项位置。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ...domain.statute_results import StatuteLocator, StructuredArticle
from .structure import locator_ordinal


class LocationStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    STRUCTURE_UNAVAILABLE = "structure_unavailable"


@dataclass(frozen=True)
class LocationAssessment:
    status: LocationStatus
    message: str = ""
    authoritative_text: str | None = None


def assess_location(
    structure: StructuredArticle | None,
    locators: list[StatuteLocator],
) -> LocationAssessment:
    if not locators or all(
        locator.paragraph_no is None and locator.item_no is None
        for locator in locators
    ):
        return LocationAssessment(
            LocationStatus.VALID,
            authoritative_text=structure.raw_text if structure else None,
        )
    if structure is None:
        return LocationAssessment(
            LocationStatus.STRUCTURE_UNAVAILABLE,
            "权威条文未保留可验证的款项结构",
        )

    selected: list[str] = []
    for locator in locators:
        if locator.paragraph_no is None and locator.item_no is not None:
            # 引用只写"项"未写"款"（如"第五条第一项"，条文为单引言款 + 各项）：
            # 定位到唯一含项的款；仅当含项的款不唯一时才无法确定。
            item_paragraphs = [p for p in structure.paragraphs if p.items]
            if len(item_paragraphs) == 1:
                paragraph = item_paragraphs[0]
            elif len(structure.paragraphs) == 1:
                paragraph = structure.paragraphs[0]
            else:
                return LocationAssessment(
                    LocationStatus.STRUCTURE_UNAVAILABLE,
                    f"条文含多个列项款，无法确定{locator.item_no}所属款",
                )
            item_index = locator_ordinal(locator.item_no, "项")
            selected_item = next((item for item in paragraph.items if locator_ordinal(item.item_no, "项") == item_index), None)
            if item_index is None or item_index < 1 or selected_item is None:
                return LocationAssessment(
                    LocationStatus.INVALID,
                    f"该条共{len(paragraph.items)}项，其中不存在{locator.item_no}",
                )
            selected.append(paragraph.introduction + selected_item.text)
            continue
        paragraph_index = locator_ordinal(locator.paragraph_no or "", "款")
        if paragraph_index is None or paragraph_index < 1:
            return LocationAssessment(
                LocationStatus.STRUCTURE_UNAVAILABLE,
                f"无法识别款编号：{locator.paragraph_no}",
            )
        paragraph = next((
            item for item in structure.paragraphs
            if locator_ordinal(item.paragraph_no, "款") == paragraph_index
        ), None)
        if paragraph is None and not structure.paragraph_boundaries_reliable:
            return LocationAssessment(
                LocationStatus.STRUCTURE_UNAVAILABLE,
                "权威条文未保留足以核验该款号的自然段边界",
            )
        if paragraph is None:
            return LocationAssessment(
                LocationStatus.INVALID,
                f"权威条文中不存在{locator.paragraph_no}",
            )
        if locator.item_no is None:
            selected.append(paragraph.text)
            continue
        item_index = locator_ordinal(locator.item_no, "项")
        selected_item = next((item for item in paragraph.items if locator_ordinal(item.item_no, "项") == item_index), None)
        if item_index is None or item_index < 1 or selected_item is None:
            return LocationAssessment(
                LocationStatus.INVALID,
                f"{paragraph.paragraph_no}共{len(paragraph.items)}项，其中不存在{locator.item_no}",
            )
        selected.append(paragraph.introduction + selected_item.text)
    return LocationAssessment(
        LocationStatus.VALID,
        authoritative_text="\n\n".join(selected),
    )


__all__ = ["LocationAssessment", "LocationStatus", "assess_location"]
