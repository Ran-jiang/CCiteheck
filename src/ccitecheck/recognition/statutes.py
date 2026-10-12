"""
CCiteheck 法源与条款号识别。

负责：
  1. 从文本中识别《》书名号引用的法律规范
  2. 提取条款号（条/款/项）并归属到对应的法源

设计决策：
  - 条款号必须出现在对应法源书名号之后、下一个法源书名号之前
  - 排除明显非法律规范文件（合同、协议、授权书等）
  - 法源后无条款号时仍构成法源，articles 为空
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from ..domain.legal_numbers import chinese_number_to_int, int_to_chinese_number
from ..domain.citation import (
    AliasDeclaration,
    ArticleRef,
    LegalSource,
    LegalSourceRecognition,
    StructureRef,
    StructureUnit,
)
from ..domain.law_titles import canonical_cn_title_shape, cn_title_shape_key


# ============================================================
# 正则模式
# ============================================================

# 法源引用：《...》书名号对
# 支持中文书名号和全角书名号
# 注意：书名号内文本可能包含空格、标点、数字等
LEGAL_SOURCE_PATTERN = re.compile(r"《([^》]+)》")

_RAW_TIME_PATTERN = re.compile(
    r"现行|(?<!\d)\d{4}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?"
)
_VERSION_TIME_PATTERN = re.compile(
    r"^[（(]\s*((?:19|20)\d{2})\s*年?\s*(修正|修订|修改)\s*[）)]"
)
_BIBLIOGRAPHIC_PAGE = re.compile(
    r"第\s*[一二三四五六七八九十百千零两〇0-9]+\s*页"
)
_BIBLIOGRAPHIC_PUBLISHER = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9·]{2,30}出版社"
    r"(?=\s*(?:[，,。；;：:]|(?:19|20)\d{2}\s*年|第[^，,。；;]{1,10}版|$))"
)
_BIBLIOGRAPHIC_EDITION = re.compile(
    r"(?:19|20)\d{2}\s*年\s*(?:第\s*[一二三四五六七八九十百千零两〇0-9]+\s*版|版)"
)
_BIBLIOGRAPHIC_EDITOR = re.compile(
    r"[\u4e00-\u9fff·]{2,20}(?:\s*[、,，]\s*[\u4e00-\u9fff·]{2,20})*"
    r"\s*主编\s*[：:]\s*《"
)
_BIBLIOGRAPHIC_JOURNAL_ISSUE = re.compile(
    r"《[^》\n]{2,80}》\s*(?:19|20)\d{2}\s*年\s*"
    r"第\s*[一二三四五六七八九十百千零两〇0-9]+\s*期"
)
_BIBLIOGRAPHIC_VOLUME_ISSUE = re.compile(
    r"第\s*[一二三四五六七八九十百千零两〇0-9]+\s*卷\s*"
    r"第\s*[一二三四五六七八九十百千零两〇0-9]+\s*期"
)
_BIBLIOGRAPHIC_DOI = re.compile(
    r"(?<![A-Za-z0-9])DOI\s*[:：]?\s*10\.\d{4,9}/[-._;()/:A-Z0-9]+",
    re.IGNORECASE,
)
_BIBLIOGRAPHIC_ISSN = re.compile(
    r"(?<![A-Za-z0-9])ISSN\s*[:：]?\s*\d{4}-\d{3}[\dX]",
    re.IGNORECASE,
)

_ALIAS_DECLARATION_PATTERN = re.compile(
    r"《(?P<full>[^》]+)》\s*[（(]\s*"
    r"(?:(?!以下简称|下称|简称为|简称).){0,100}?"
    r"(?:以下简称|下称|简称为|简称)\s*"
    r"[“\"'‘]?\s*(?:《(?P<book_alias>[^》]+)》|(?P<plain_alias>[^”\"'’）)]{1,40}))"
    r"\s*[”\"'’]?\s*[）)]"
)

# 国家标准/行业标准模式：GB/T XXXXX-XXXX 等（无书名号）
# 例：GB/T 35273-2020 / GB/T 45674-2025 / GB 12345-2020
# 标准编号本身就是唯一标识，不需要《》
STANDARD_PATTERN = re.compile(
    r'(GB(?:/T|/Z)?|GM/T|GA/T|GY/T|LD/T|MZ/T|NY/T|HJ/T|HJ|'
    r'YY/T|YY|WS/T|WS|DB\d{2}/T|DB\d{2})'
    r'\s*\d{4,6}(?:\.\d+)?[—\-]\d{2,4}'
)

# 条款号正则
# 条：第X条 或 第X条之Y
# 支持中文数字（一～十百千）和阿拉伯数字（0-9）
_CN_NUM = r"[一二三四五六七八九十百千零〇两\d]+"
_CN_NUM_EXTRA = _CN_NUM  # "之"后面的数字通常较小

_FOREIGN_BARE_ACT_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])(?P<title>[A-Za-z][A-Za-z0-9 .'-]{{1,60}}?\s+Act)"
    rf"\s*[）)]?\s*"
    rf"(?P<locator>第{_CN_NUM}条(?:第{_CN_NUM}款)?(?:第[（(]?{_CN_NUM}[）)]?项)?)"
)

ARTICLE_PATTERN = re.compile(
    rf"第({_CN_NUM})条(?:之({_CN_NUM_EXTRA}))?"
)

# 条号范围：第X条至第Y条
ARTICLE_RANGE_PATTERN = re.compile(
    rf"第({_CN_NUM})条(?:至|到)第?({_CN_NUM})条"
)

# 省略“第”的阿拉伯数字范围，如“58-69条”“58—69条”。范围仅在已经
# 归属于某个明确法源的 segment 内抽取，并限制跨度，避免年份和编号误报。
COMPACT_ARTICLE_RANGE_PATTERN = re.compile(
    r"(?<![\d])([1-9]\d{0,3})\s*[-—–~～至]\s*([1-9]\d{0,3})条"
)

# 款：第X款
PARAGRAPH_PATTERN = re.compile(
    rf"第({_CN_NUM})款"
)
STANDALONE_PARAGRAPH_PATTERN = re.compile(
    rf"^\s*(第({_CN_NUM})款)(?=\s*(?:规定|明确|指出|载明|要求|所称|[，,:：]))"
)

# 项：第（X）项 / 第(X)项 / 第X项
ITEM_PATTERN = re.compile(
    rf"第[（(]?({_CN_NUM})[）)]?项"
)

# 法源引导词：用于辅助判断句子是否含法律依据
LEGAL_BASIS_WORDS = {"依据", "根据", "依照", "按照", "参照", "适用"}

_PARTIAL_REFERENCE_PREDICATE = re.compile(r"(?:之规定|规定|所称|明确)")


@dataclass(frozen=True)
class PartialArticleRef:
    """尚需从上文补齐条号的款、项引用。"""

    paragraphs: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)


# ============================================================
# 法律规范后缀白名单
# ============================================================
#
# 白名单内的标题才被识别为法律规范——宁紧勿松。
# 白名单外的《》书名号文本（如《※※收藏》《某某号MCN运营手册》
# 《法院回复函》《XX合同》等）一律不作为法源。
#
# 后缀来源：
#   - 《行政法规制定程序条例》(国务院令第321号) 第5条
#   - 《规章制定程序条例》
#   - 《党政机关公文处理工作条例》
#
LEGAL_TITLE_SUFFIXES = [
    # Law 级
    "法典",

    # 外文法规标题，如 TAKE IT DOWN Act。法域在 Query Construction 判定。
    "Act",

    # 暂行办法/规定/规则 等（须优先于"法"匹配）
    "暂行实施办法",
    "暂行实施细则",
    "暂行条例",
    "暂行规定",
    "暂行规则",
    "暂行细则",
    "暂行通知",
    "暂行意见",
    "暂行标准",
    "暂行规程",

    # 试行办法/规定/规则 等
    "试行实施办法",
    "试行实施细则",
    "试行条例",
    "试行规定",
    "试行规则",
    "试行细则",
    "试行通知",
    "试行意见",
    "试行标准",
    "试行规程",

    # 实施/施行细则
    "实施办法",
    "实施细则",
    "施行细则",
    "施行办法",

    # 司法解释法定形式
    "解释",     # 最高人民法院关于...的解释
    "批复",     # 最高人民法院关于...的批复

    # 行政法规/规章/规范性文件后缀
    "条例",     # 行政法规、地方性法规
    "办法",     # 规章（不包括"实施办法"/"暂行办法"）
    "规定",     # 行政法规/规章/规范性文件
    "规则",
    "细则",
    "规程",
    "规范",
    "标准",
    "决定",
    "意见",
    "通知",
    "纪要",
    "通告",
    "公告",

    # Law 级；伪法名后缀由 _NON_LEGAL_LAW_SUFFIXES 单独排除。
    "法",
]

_PRECISE_LEGAL_TITLE_SUFFIX = re.compile(
    r"(?:的决议|的命令|的复函|的函|的答复|条令)$"
)

# 只在标题需要依靠通用“法”后缀时排除；不能包含“司法”，否则会误伤“公司法”。
_NON_LEGAL_LAW_SUFFIXES = (
    "方法", "做法", "手法", "历法", "语法", "书法", "笔法", "技法",
    "用法", "玩法", "疗法", "说法", "看法", "想法", "算法", "写法",
    "读法", "乘法", "除法", "加法", "减法",
)

# 非法律规范关键词——白名单外的标题若有这些词，明确排除
# 注意：此列表只对不在白名单内的标题生效。
# 白名单内的标题（如"劳动合同法"）即使含"合同"也会保留。
NON_LEGAL_KEYWORDS = [
    "合同", "协议", "授权", "确认函", "公证书", "通知书",
    "证据目录", "发票", "订单", "截图", "收据", "催告函",
    "承诺函", "担保函", "询证函", "报价单", "验收单",
    "送货单", "结算单", "对账单", "欠条", "借条",
    "委托书", "声明书", "告知书", "答复书", "申请", "登记表",
    "营业执照", "章程", "股东名册", "出资证明",
    # 平台或企业发布的非法律文件。
    "手册", "公约", "服务协议", "合作政策",
    "运营手册", "运营规范", "入驻协议",
    "回复函", "答复函",
    "账号管理", "用户协议",
]

# ============================================================
# 法源和条款识别
# ============================================================

def _has_legal_title_suffix(title: str) -> bool:
    """
    判断标题是否具有法律规范后缀（白名单匹配）。

    处理括号注解：如"反不正当竞争法（2019年修正）"先剥离"（2019年修正）"
    再检查"反不正当竞争法"的后缀。

    只有后缀在白名单 LEGAL_TITLE_SUFFIXES 中的标题才被识别为法源。
    白名单外的书名号文本（文章标题、平台文档、合同名、作品名等）
    一律过滤。

    仅白名单中的规范性文件后缀会被识别。

    Args:
        title: 书名号内文本

    Returns:
        True 如果标题后缀在白名单内
    """
    stripped = _strip_parenthetical(title)
    if _PRECISE_LEGAL_TITLE_SUFFIX.search(stripped):
        return True

    # 明确的长后缀优先；通用“法”最后单独裁定。
    for suffix in LEGAL_TITLE_SUFFIXES:
        if suffix != "法" and stripped.endswith(suffix):
            return True
    if any(stripped.endswith(suffix) for suffix in _NON_LEGAL_LAW_SUFFIXES):
        return False
    return stripped.endswith("法")


def _strip_parenthetical(title: str) -> str:
    """
    剥离标题末尾的括号注解。

    例：
      "反不正当竞争法（2019年修正）" → "反不正当竞争法"
      "商标法（修订）" → "商标法"
      "公司法（2023修订）" → "公司法"

    Args:
        title: 原始标题

    Returns:
        剥离后的标题
    """
    # 连续剥离末尾注解，如“某法（2026年修订）（试行）”。
    return re.sub(r'(?:[（(][^（）()]*[）)]\s*)+$', '', title).strip()


def _is_legal_source(title: str) -> bool:
    """
    判断书名号内文本是否为法律规范文件。

    采用后缀白名单机制：
      1. 标题后缀在白名单内 → 直接保留（如"劳动合同法"含"合同"也保留）
      2. 标题后缀不在白名单内 → 检查 NON_LEGAL_KEYWORDS
         （进一步排除合同、协议、手册、公约、回复函等）

    Args:
        title: 书名号内文本

    Returns:
        True 如果是法律规范文件
    """
    # 后缀白名单匹配 → 直接保留
    if _has_legal_title_suffix(title):
        return True

    # 不在白名单内 → 非法律规范文件
    # 但有些可能尚未收录到白名单，用关键词做二次确认
    for keyword in NON_LEGAL_KEYWORDS:
        if keyword in title:
            return False

    # 白名单外且无排除关键词 → 仍不作为法源（宁紧勿松）
    # 识别层只输出明确法源，不引入不确定状态。
    return False


def _extract_articles_from_text(text: str) -> list[ArticleRef]:
    """
    从文本中提取所有条款号引用。

    提取条、款、项，并建立归属关系。
    返回 ArticleRef 列表，每个 ArticleRef 包含条号及对应的款和项。

    款和项仅归属到它前面最近的条号；遇到下一个条号即停止。

    Args:
        text: 待分析的文本片段（通常是两个书名号之间的文本）

    Returns:
        ArticleRef 列表
    """
    # 查找所有"条"引用
    article_matches = list(ARTICLE_PATTERN.finditer(text))
    # 查找所有"款"引用
    paragraph_matches = list(PARAGRAPH_PATTERN.finditer(text))
    # 查找所有"项"引用
    item_matches = list(ITEM_PATTERN.finditer(text))

    compact_ranges = list(COMPACT_ARTICLE_RANGE_PATTERN.finditer(text))
    if not article_matches and not compact_ranges:
        # 没有明确的条款号，返回空列表
        # 法源仍会被保留（articles 为空）
        return []

    articles: list[ArticleRef] = []
    for index, am in enumerate(article_matches):
        article_num = am.group(1)
        suffix = am.group(2)
        if suffix:
            article_text = f"第{article_num}条之{suffix}"
        else:
            article_text = f"第{article_num}条"

        article_end = am.end()
        next_article_start = (
            article_matches[index + 1].start()
            if index + 1 < len(article_matches)
            else len(text)
        )

        # 收集属于此条号的款（位于此条之后、下一条之前或文本末尾）
        paras: list[str] = []
        for pm in paragraph_matches:
            if article_end <= pm.start() < next_article_start:
                paras.append(f"第{pm.group(1)}款")

        # 收集属于此条号的项
        items: list[str] = []
        for im in item_matches:
            if article_end <= im.start() < next_article_start:
                items.append(f"第{im.group(1)}项")

        articles.append(ArticleRef(
            article=article_text,
            paragraphs=paras,
            items=items,
        ))

    from ..infrastructure.database import normalize_article_key
    original_refs = {normalize_article_key(ref.article): ref for ref in articles}
    events = [(match.start(), [article]) for match, article in zip(article_matches, articles)]
    for pattern in (ARTICLE_RANGE_PATTERN, COMPACT_ARTICLE_RANGE_PATTERN):
        for rm in pattern.finditer(text):
            start = chinese_number_to_int(rm.group(1))
            end = chinese_number_to_int(rm.group(2))
            if start is None or end is None or not (0 < start <= end and end - start <= 50):
                continue
            events = [(position, refs) for position, refs in events if not rm.start() <= position < rm.end()]
            events.append((rm.start(), [original_refs.get(str(number), ArticleRef(article=f"第{number}条")).model_copy(update={"article": f"第{str(number) if rm.group(1).isdigit() else int_to_chinese_number(number)}条"})
                                       for number in range(start, end + 1)]))
    articles = []
    seen = set()
    for _, refs in sorted(events, key=lambda event: event[0]):
        for ref in refs:
            key = (normalize_article_key(ref.article), tuple(ref.paragraphs), tuple(ref.items))
            if key not in seen:
                articles.append(ref)
                seen.add(key)

    return articles


def extract_legal_sources(
    text: str,
) -> list[LegalSource]:
    """
    从文本中提取所有法律规范引用。

    找所有《》书名号对，过滤非法律规范文件，
    提取每个法源后的条款号并建立归属关系。

    条款号归属规则：
      - 条款号必须出现在对应法源书名号之后、下一个法源书名号之前
      - 法源后无条款号时（如"依据《民法典》及相关规定"），仍构成法源，articles 为空

    Args:
        text: 待分析的文本（通常是一个 anchor 的文本）

    Returns:
        LegalSource 列表，按原文出现顺序排列
    """
    # 查找所有《》引用
    matches = list(LEGAL_SOURCE_PATTERN.finditer(text))
    bare_matches = _find_bare_citations(text)

    legal_sources: list[LegalSource] = []
    explicit_by_key: dict[str, LegalSource] = {}

    if matches:
        for i, m in enumerate(matches):
            title = m.group(1).strip()
            if not title or not _is_legal_source(title):
                continue
            if _is_bibliographic_reference(text, m):
                continue

            # 确定条款号搜索范围：从当前法源结束位置到下一个法源开始位置
            search_start = m.end()
            if i + 1 < len(matches):
                search_end = matches[i + 1].start()
            else:
                search_end = len(text)
            next_bare_start = next(
                (bare.title_start for bare in bare_matches if search_start <= bare.title_start < search_end),
                None,
            )
            if next_bare_start is not None:
                search_end = next_bare_start

            segment = text[search_start:search_end]

            # 从该区间提取条款号
            articles = _extract_articles_from_text(segment)
            if not articles and (paragraph := STANDALONE_PARAGRAPH_PATTERN.search(segment)):
                articles = [ArticleRef(
                    article=f"第{paragraph.group(2)}条",
                    raw_locator=paragraph.group(1),
                )]

            source = LegalSource(
                title=title,
                canonical_title=canonical_cn_title_shape(title),
                raw_time=_raw_time_for_mention(text, m, title),
                recognition={"form": "explicit", "mention_span": m.span(1)},
                articles=articles,
                # 章节引用只在无条款引用时抽取（有条号时章节仅是定位前缀）
                structures=(
                    _extract_structure_refs(segment) if not articles else []
                ),
            )
            key = cn_title_shape_key(title)
            existing = explicit_by_key.get(key)
            if existing is None:
                legal_sources.append(source)
                explicit_by_key[key] = source
                continue
            had_articles = bool(existing.articles)
            _merge_articles(existing.articles, source.articles)
            _merge_structures(existing.structures, source.structures)
            if source.articles and not had_articles:
                existing.title = source.title

    # ---- 补充：裸法条引用（无《》书名号）----
    # 例："……认定为反不正当竞争法第九条第四款所称的……"
    # 这里只记录原文字面法名候选；标准法名由 Query Construction 解释。
    for source in _extract_bare_law_citations(bare_matches):
        existing = next(
            (
                candidate for candidate in legal_sources
                if cn_title_shape_key(candidate.title) == cn_title_shape_key(source.title)
            ),
            None,
        )
        if existing is None:
            legal_sources.append(source)
        else:
            _merge_articles(existing.articles, source.articles)

    # ---- 补充：国家标准/行业标准（无书名号）----
    # 例：GB/T 35273-2020 / GB/T 45674-2025
    # 标准编号本身就是唯一标识，归入 other_normative_document
    seen_titles = {source.title for source in legal_sources if source.title}
    standard_sources = _extract_standard_citations(text, seen_titles)
    legal_sources.extend(standard_sources)

    # ---- 补充：外文裸法规简称（无书名号）----
    # 例：EU AI Act第50条第4款。文内简称声明由后续查询构造层解释。
    for source in _extract_foreign_bare_act_citations(text):
        existing = next((item for item in legal_sources if item.title == source.title), None)
        if existing is None:
            legal_sources.append(source)
        else:
            _merge_articles(existing.articles, source.articles)

    return legal_sources


def extract_alias_declarations(text: str) -> list[AliasDeclaration]:
    """记录文内简称声明；不在 Recognition 阶段解释后续简称。"""
    declarations: list[AliasDeclaration] = []
    for match in _ALIAS_DECLARATION_PATTERN.finditer(text):
        full_title = match.group("full").strip()
        short_title = (match.group("book_alias") or match.group("plain_alias") or "").strip()
        if not _is_legal_source(full_title) or not short_title:
            continue
        declarations.append(AliasDeclaration(
            full_name_raw=full_title,
            alias_raw=short_title,
            declaration_span=match.span(),
        ))
    return declarations


def _extract_foreign_bare_act_citations(text: str) -> list[LegalSource]:
    results: list[LegalSource] = []
    for match in _FOREIGN_BARE_ACT_PATTERN.finditer(text):
        title = match.group("title").strip()
        results.append(LegalSource(
            title=title,
            canonical_title=title,
            raw_title_candidate=title,
            recognition=LegalSourceRecognition(
                form="bare",
                mention_span=match.span("title"),
                resolver="structure",
            ),
            articles=_extract_articles_from_text(match.group("locator")),
        ))
    return results


def _raw_time_for_mention(text: str, match: re.Match, title: str) -> str | None:
    """只抄录与法名相邻的时间表达，不把它解释为适用版本。"""
    suffix = text[match.end():match.end() + 30].lstrip()
    if version := _VERSION_TIME_PATTERN.match(suffix):
        return f"{version.group(1)}年{version.group(2)}"
    prefix = text[max(0, match.start() - 100):match.start()]
    candidates = list(_RAW_TIME_PATTERN.finditer(prefix))
    if candidates:
        candidate = candidates[-1]
        tail = prefix[candidate.end():]
        if len(tail) <= 50 and not re.search(r"[。！？；\n]", tail):
            return candidate.group(0)
    inside = _RAW_TIME_PATTERN.search(title)
    return inside.group(0) if inside else None


def _is_bibliographic_reference(text: str, match: re.Match) -> bool:
    """识别并排除二手文献；不因“主编”“期刊”等孤立词误判。"""
    sentence_start = max(
        [0, *(item.end() for item in re.finditer(r"[。！？；\n]", text[:match.start()]))]
    )
    boundary = re.search(r"[。！？；\n]", text[match.end():])
    sentence_end = match.end() + boundary.start() if boundary else len(text)
    context = text[sentence_start:sentence_end]
    publisher = bool(_BIBLIOGRAPHIC_PUBLISHER.search(context))
    page = bool(_BIBLIOGRAPHIC_PAGE.search(context))
    editor = bool(_BIBLIOGRAPHIC_EDITOR.search(context))
    journal_issue = bool(_BIBLIOGRAPHIC_JOURNAL_ISSUE.search(context))
    volume_issue = bool(_BIBLIOGRAPHIC_VOLUME_ISSUE.search(context))
    doi = bool(_BIBLIOGRAPHIC_DOI.search(context))
    issn = bool(_BIBLIOGRAPHIC_ISSN.search(context))

    # DOI、ISSN、完整期刊年期/卷期和“姓名主编：《书名》”本身足够确定；
    # 出版社或页码则须与版次/其他出版字段组合，不能凭单个字眼过滤。
    return (
        doi
        or issn
        or journal_issue
        or volume_issue
        or editor
        or (publisher and (page or bool(_BIBLIOGRAPHIC_EDITION.search(context))))
    )


# 章节引用链：紧跟在《法名》之后的 第X编/分编/章/节 连写（无条号时）
_STRUCTURE_CHAIN_PATTERN = re.compile(
    r"^[\s　的]*((?:第[一二三四五六七八九十百千零两0-9]+(?:编|分编|章|节))+)"
)
_STRUCTURE_UNIT_PATTERN = re.compile(
    r"第([一二三四五六七八九十百千零两0-9]+)(编|分编|章|节)"
)


def _extract_structure_refs(segment: str) -> list[StructureRef]:
    """从法名后的紧邻文本抽取章节引用（如"第三编第四章"）。"""
    match = _STRUCTURE_CHAIN_PATTERN.match(segment)
    if not match:
        return []
    chain = match.group(1)
    units = []
    for unit_match in _STRUCTURE_UNIT_PATTERN.finditer(chain):
        number = chinese_number_to_int(unit_match.group(1))
        units.append(StructureUnit(
            unit=unit_match.group(2),
            number=number,
            number_text=unit_match.group(0),
        ))
    if not units:
        return []
    return [StructureRef(label=chain, units=units)]


# ============================================================
# 裸法条引用（无《》书名号）
# ============================================================

# 右锚点定位较窄的规范后缀 + 首个条款号；法名左边界另行裁定。
BARE_ARTICLE_ANCHOR = re.compile(
    r'(?P<law_suffix>法典|条例|办法|的规定|的规则|细则|法)'
    r'(?P<article>'
    r'第[一二三四五六七八九十百千零\d]+条'
    r'(?:之[一二三四五六七八九十]+)?'
    r'(?:第[一二三四五六七八九十零\d]+款)?'
    r'(?:第[（(]?[一二三四五六七八九十零\d]+[）)]?项)?'
    r')'
)

# 同一法名支配的后续并列/范围条款。连接词后必须立即出现完整“第X条”，
# 因而不会跨入普通正文或另一部法律的引用。
_BARE_FOLLOWING_ARTICLE = re.compile(
    r"\s*(?:[、,，]|及|和|或|至|到)\s*"
    rf"第{_CN_NUM}条(?:之{_CN_NUM_EXTRA})?"
    rf"(?:第{_CN_NUM}款)?"
    rf"(?:第[（(]?{_CN_NUM}[）)]?项)?"
)

# 伪法名后缀：以“法”结尾但不是法律名的词。
# “司法”不能列入：公司法以“司法”结尾；“办法”现已是合法裸引用后缀。
BARE_LAW_EXCLUDE_SUFFIXES = [
    "方法", "做法", "手法", "历法", "语法",
    "书法", "笔法", "技法", "用法", "玩法", "疗法",
    "说法", "看法", "想法", "算法", "写法", "读法",
    "乘法", "除法", "加法", "减法",
]

# 指代词不作为独立法名，但其条款号仍会进入 rules.py 的承前继承。
BARE_LAW_ANAPHORS = {
    "本法", "该法", "此法", "前法", "上述法律",
    "本法典", "该法典", "本条例", "该条例", "本办法", "该办法",
    "本规定", "该规定", "本规则", "该规则", "本细则", "该细则",
    "现行法", "相关法", "有关法", "其他法",
}

_BARE_SUFFIX_ONLY = {"法", "法典", "条例", "办法", "规定", "规则", "细则"}


@dataclass(frozen=True)
class BareCitationMatch:
    raw_title_candidate: str
    title_start: int
    title_end: int
    citation_end: int
    article_text: str
    raw_time: str | None = None


_BARE_WINDOW_BOUNDARY = re.compile(r"[，。！？；：、\n]")
_BARE_LEADING_CONTEXT = re.compile(
    r"^.*(?:依据|根据|依照|按照|参照|适用|请求|认定为|属于|违反了?|为|以|按|依)"
)
_BARE_TITLE_MODIFIER = re.compile(r"^(?:现行|我国|中国的?|相关|有关|及|和|或)+")
_BARE_YEAR_PREFIX = re.compile(
    r"^(?P<time>(?:\d{4})?年)(?P<title>.+(?:法典|条例|办法|的规定|的规则|细则|法))$"
)


def _find_bare_citations(text: str) -> list[BareCitationMatch]:
    results: list[BareCitationMatch] = []
    previous_end = 0
    for anchor in BARE_ARTICLE_ANCHOR.finditer(text):
        citation_end = _bare_article_chain_end(text, anchor.end())
        law_end = anchor.end("law_suffix")
        window_start = max(previous_end, law_end - 120)
        prefix = text[window_start:law_end]
        boundaries = [match.end() for match in _BARE_WINDOW_BOUNDARY.finditer(prefix)]
        book_end = prefix.rfind("》") + 1
        relative_start = max([0, book_end, *boundaries])
        window_start += relative_start
        window = text[window_start:law_end]

        raw = window.strip()
        raw = _BARE_LEADING_CONTEXT.sub("", raw)
        raw = _BARE_TITLE_MODIFIER.sub("", raw).strip()
        raw_time = None
        if year_match := _BARE_YEAR_PREFIX.fullmatch(raw):
            candidate = year_match.group("title")
            if _is_valid_bare_law_name(candidate):
                raw = candidate
                time_text = year_match.group("time")
                raw_time = time_text if time_text != "年" else None
        if not raw or not _is_valid_bare_law_name(raw):
            previous_end = anchor.end()
            continue
        raw_start = window_start + window.rfind(raw)
        results.append(BareCitationMatch(
            raw_title_candidate=raw,
            title_start=raw_start,
            title_end=law_end,
            citation_end=citation_end,
            article_text=text[anchor.start("article"):citation_end],
            raw_time=raw_time,
        ))
        previous_end = citation_end
    return results


def _bare_article_chain_end(text: str, start: int) -> int:
    """返回同一裸法名支配的连续并列或范围条款末尾。"""
    end = start
    while match := _BARE_FOLLOWING_ARTICLE.match(text, end):
        end = match.end()
    return end


def _extract_bare_law_citations(matches: list[BareCitationMatch]) -> list[LegalSource]:
    results: list[LegalSource] = []
    for match in matches:
        source = next(
            (
                item for item in results
                if cn_title_shape_key(item.title)
                == cn_title_shape_key(match.raw_title_candidate)
            ),
            None,
        )
        articles = _extract_articles_from_text(match.article_text)
        if source is not None:
            _merge_articles(source.articles, articles)
            continue
        results.append(LegalSource(
            title=match.raw_title_candidate,
            canonical_title=canonical_cn_title_shape(match.raw_title_candidate),
            raw_title_candidate=match.raw_title_candidate,
            recognition=LegalSourceRecognition(
                form="bare",
                mention_span=(match.title_start, match.title_end),
                resolver="structure",
            ),
            raw_time=match.raw_time,
            articles=articles,
        ))
    return results


def _merge_articles(target: list[ArticleRef], incoming: list[ArticleRef]) -> None:
    for article in incoming:
        existing = next((
            item for item in target
            if item.article == article.article
        ), None)
        if existing is None:
            target.append(article)
            continue
        existing.paragraphs = list(dict.fromkeys([*existing.paragraphs, *article.paragraphs]))
        existing.items = list(dict.fromkeys([*existing.items, *article.items]))


def _merge_structures(target: list[StructureRef], incoming: list[StructureRef]) -> None:
    existing_labels = {structure.label for structure in target}
    for structure in incoming:
        if structure.label in existing_labels:
            continue
        target.append(structure)
        existing_labels.add(structure.label)


def _is_valid_bare_law_name(title: str) -> bool:
    """
    判断裸法名是否为有效法律名称。

    排除：
      - 以伪法名后缀结尾的（方法、做法等）
      - 纯后缀和承前指代词

    Args:
        title: 待检查的法名

    Returns:
        True 如果是有效法律名称
    """
    if len(title) < 2 or title in _BARE_SUFFIX_ONLY or title in BARE_LAW_ANAPHORS:
        return False
    for exclude in BARE_LAW_EXCLUDE_SUFFIXES:
        if title.endswith(exclude):
            return False
    return True


def has_article_reference(text: str) -> bool:
    """
    判断文本是否包含条款号引用（条/款/项）但不一定含《》法源名。

    用于法源前向继承：当前 anchor 有条款号但无法源名时，
    向上查找最近的法源引用 anchor，继承其法源。

    Args:
        text: 待检查文本

    Returns:
        True 如果包含条款号引用
    """
    if ARTICLE_PATTERN.search(text):
        return True
    if PARAGRAPH_PATTERN.search(text):
        return True
    if ITEM_PATTERN.search(text):
        return True
    return False


def extract_articles_only(text: str) -> list:
    """
    从文本中提取条款号但不要求存在《》法源。

    用于法源前向继承场景：条款号会继承前一个 anchor 的法源名。

    Args:
        text: 待分析文本

    Returns:
        ArticleRef 列表
    """
    return _extract_articles_from_text(text)


def extract_partial_refs(text: str) -> PartialArticleRef | None:
    """保守提取省略条号的款、项；结果不能脱离承前法源独立使用。"""
    if ARTICLE_PATTERN.search(text):
        return None
    paragraph_matches = list(PARAGRAPH_PATTERN.finditer(text))
    item_matches = list(ITEM_PATTERN.finditer(text))
    if not paragraph_matches and not item_matches:
        return None

    last_end = max(match.end() for match in [*paragraph_matches, *item_matches])
    predicate_window = text[last_end:last_end + 8]
    has_legal_predicate = bool(_PARTIAL_REFERENCE_PREDICATE.search(predicate_window))
    if not has_legal_predicate and not any(word in text for word in LEGAL_BASIS_WORDS):
        return None

    return PartialArticleRef(
        paragraphs=[f"第{match.group(1)}款" for match in paragraph_matches],
        items=[f"第{match.group(1)}项" for match in item_matches],
    )


# ============================================================
# 国家标准/行业标准引用（无书名号）
# ============================================================

def _extract_standard_citations(
    text: str,
    seen_titles: set[str],
) -> list[LegalSource]:
    """
    提取国家标准/行业标准引用（无《》书名号形式）。

    标准编号如 GB/T 35273-2020、GB/T 45674-2025 本身即是唯一标识，
    归入 other_normative_document。

    Args:
        text: 待分析文本
        seen_titles: 已通过《》或裸引用提取过的标题集合（避免重复）

    Returns:
        LegalSource 列表
    """
    results: list[LegalSource] = []
    seen: set[str] = set()

    for m in STANDARD_PATTERN.finditer(text):
        std_id = m.group(0).strip()

        # 去重
        if std_id in seen_titles or std_id in seen:
            continue
        seen.add(std_id)

        # 尝试提取标准名称（紧跟在编号后的中文描述）
        # 例："GB/T 35273-2020    信息安全技术 个人信息安全规范"
        # 标准名称部分不做强制要求，有则更好
        results.append(LegalSource(
            title=std_id,
            articles=[],
            recognition=LegalSourceRecognition(form="explicit", resolver="direct"),
        ))

    return results


def invalid_number_tokens(raw: str) -> list[str]:
    """保留原写法；仅标记能够确定为非规范写法的编号。"""
    result = []
    for match in re.finditer(r"(?:第[（(]?|之)([0-9零〇两一二三四五六七八九十百千]+)(?=条|款|[）)]?项|$)", raw):
        token = match.group(1)
        number = chinese_number_to_int(token)
        valid = number is not None and 0 < number <= 9999 and (
            (token.isascii() and token.isdigit() and str(number) == token)
            or token == int_to_chinese_number(number))
        if not valid:
            result.append(token)
    return result
