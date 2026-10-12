"""
测试标题识别器。

测试覆盖：
  - 样式识别
  - 中文章节标题 pattern fallback（Test 6）
  - 数字编号标题
  - "第X条"不识别为 heading
  - 层级动态对齐
"""

from ccitecheck.parsing.headings import (
    detect_heading,
    detect_heading_level_from_style,
    is_pseudo_heading,
    scan_chapter_types,
    normalize_heading_levels,
    HeadingSource,
)


class TestStyleDetection:
    """样式识别测试"""

    def test_heading_1_style(self):
        """Heading 1 样式识别。"""
        assert detect_heading_level_from_style("Heading 1") == 1
        assert detect_heading_level_from_style("Heading 2") == 2
        assert detect_heading_level_from_style("Heading 3") == 3

    def test_chinese_heading_style(self):
        """标题 N 样式识别。"""
        assert detect_heading_level_from_style("标题 1") == 1
        assert detect_heading_level_from_style("标题 2") == 2

    def test_normal_style(self):
        """Normal 样式不识别为标题。"""
        assert detect_heading_level_from_style("Normal") is None
        assert detect_heading_level_from_style(None) is None
        assert detect_heading_level_from_style("") is None


class TestPseudoHeading:
    """Test 6：伪标题识别"""

    def test_chapter_heading_pattern(self):
        """第一章 总则 识别为 heading"""
        result = is_pseudo_heading("第一章 总则")
        assert result is not None
        level, source = result
        assert source == HeadingSource.PATTERN

    def test_section_heading_pattern(self):
        """第一节 基本原则 识别为 heading"""
        result = is_pseudo_heading("第一节 基本原则")
        assert result is not None
        level, source = result
        assert source == HeadingSource.PATTERN

    def test_article_not_heading(self):
        """第三十七条 不是 heading（应返回 None）。"""
        # 长度超过40字吗？不，"第三十七条 劳动者提前三十日……"可能超过40字
        # 但短文本"第三十七条"应匹配第X条模式
        result = is_pseudo_heading("第三十七条")
        assert result is None  # 匹配第X条，返回 None

    def test_article_not_heading_long_text(self):
        """第三十七条 + 长文本也不应是 heading。"""
        # 第X条 即使配上长文本，也应通过 is_pseudo_heading 中的检查排除
        text = "第三十七条 劳动者提前三十日以书面形式通知用人单位，可以解除劳动合同。"
        # 先检查长度
        if len(text) <= 40:
            result = is_pseudo_heading(text)
            assert result is None  # 匹配第X条模式
        # 如果长度 > 40，is_pseudo_heading 直接返回 None

    def test_numbered_heading(self):
        """数字编号标题 1 总则 识别。"""
        result = is_pseudo_heading("1 总则")
        assert result is not None
        level, source = result
        assert level == 1
        assert source == HeadingSource.PATTERN

    def test_numbered_subheading(self):
        """1.1 适用范围 识别为二级标题。"""
        result = is_pseudo_heading("1.1 适用范围")
        assert result is not None
        level, source = result
        assert level == 2

    def test_compact_numbered_heading(self):
        assert is_pseudo_heading("1.概述")[0] == 1

    def test_chinese_numbered_heading(self):
        assert is_pseudo_heading("一、概述")[0] == 1
        assert is_pseudo_heading("一、劳动者严重违纪")[0] == 1

    def test_punctuated_chinese_item_is_not_heading(self):
        assert is_pseudo_heading("一、劳动者严重违纪；") is None

    def test_explicit_unnumbered_headings(self):
        for text in ("概述", "前言", "引言", "总结", "结语", "附录"):
            assert is_pseudo_heading(text)[0] == 1

    def test_long_text_not_heading(self):
        """超过40字的段落不识别为伪标题。"""
        long_text = "第X章 " + "很" * 50
        result = is_pseudo_heading(long_text)
        assert result is None


class TestDetectHeading:
    """综合标题检测测试"""

    def test_style_priority(self):
        """样式优先于 pattern。"""
        # 即使文本匹配 pattern，样式识别也应优先
        result = detect_heading("第一章 总则", "Heading 1")
        assert result is not None
        level, source = result
        assert source == HeadingSource.STYLE
        assert level == 1

    def test_pattern_fallback(self):
        """无标题样式时使用 pattern。"""
        result = detect_heading("第一章 总则", "Normal")
        assert result is not None
        level, source = result
        assert source == HeadingSource.PATTERN

    def test_not_heading(self):
        """普通段落不识别。"""
        result = detect_heading("这是一段普通文本。", "Normal")
        assert result is None


class TestChapterTypeScan:
    """章节类型扫描测试"""

    def test_scan_chapter_types(self):
        """扫描文档中出现过的章节类型。"""
        texts = ["第一章 总则", "第一节 基本原则", "第二章 合同"]
        types = scan_chapter_types(texts)
        assert types == {"章", "节"}

    def test_level_normalization(self):
        """层级对齐：文档只有章和节时，章变成一级。"""
        types = {"章", "节"}
        # 章的原始层级是2，对齐后应为1
        assert normalize_heading_levels(types, 2) == 1
        # 节的原始层级是3，对齐后应为2
        assert normalize_heading_levels(types, 3) == 2

    def test_level_normalization_full(self):
        """编章节都有时保持原层级。"""
        types = {"编", "章", "节"}
        assert normalize_heading_levels(types, 1) == 1
        assert normalize_heading_levels(types, 2) == 2
        assert normalize_heading_levels(types, 3) == 3
