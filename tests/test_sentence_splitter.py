"""
测试分句器。

测试覆盖：
  - 基础分句（Test 1）
  - 但书回归（Test 2）
  - 书名号与引号（Test 3）
  - 无损不变量
"""

from ccitecheck.parsing.sentences import split_sentences


class TestBasicSplitting:
    """Test 1：基础分句"""

    def test_two_sentences(self):
        """根据《劳动合同法》切为两句。"""
        text = "根据《劳动合同法》第三十七条，劳动者可以解除劳动合同。公司应在员工离职后三十日内办理交接手续。"

        sentences = split_sentences(text)

        assert len(sentences) == 2
        assert sentences[0].text == "根据《劳动合同法》第三十七条，劳动者可以解除劳动合同。"
        assert sentences[1].text == "公司应在员工离职后三十日内办理交接手续。"

    def test_offsets_consistent(self):
        """偏移与文本自洽。"""
        text = "根据《劳动合同法》第三十七条，劳动者可以解除劳动合同。公司应在员工离职后三十日内办理交接手续。"

        sentences = split_sentences(text)

        for sent in sentences:
            extracted = text[sent.char_start:sent.char_end]
            assert extracted == sent.text, (
                f"偏移错误: text[{sent.char_start}:{sent.char_end}]='{extracted}' "
                f"!= sent.text='{sent.text}'"
            )

    def test_lossless_join(self):
        """join 后等于原文。"""
        text = "根据《劳动合同法》第三十七条，劳动者可以解除劳动合同。公司应在员工离职后三十日内办理交接手续。"

        sentences = split_sentences(text)
        joined = "".join(s.text for s in sentences)

        assert joined == text

    def test_empty_text(self):
        """空文本返回空列表。"""
        assert split_sentences("") == []

    def test_single_sentence_no_break(self):
        """无切分符的文本返回单一句子。"""
        text = "这是一段没有标点符号的文本"
        sentences = split_sentences(text)

        assert len(sentences) == 1
        assert sentences[0].text == text
        assert sentences[0].char_start == 0
        assert sentences[0].char_end == len(text)


class TestButRegression:
    """Test 2：但书回归"""

    def test_semicolon_preserves_complete_legal_expression(self):
        """分号不切分，前提、义务和但书保持在同一句中。"""
        text = "劳动者提前三十日以书面形式通知用人单位，可以解除劳动合同；任何一方不得擅自解除合同，但法律另有规定的除外。"

        sentences = split_sentences(text)

        assert len(sentences) == 1
        assert sentences[0].text == text
        assert "但法律另有规定的除外" in sentences[0].text
        # 确保 join 无损
        assert "".join(s.text for s in sentences) == text

    def test_semicolon_is_not_a_strong_break(self):
        """分号不属于强切分符。"""
        text = "第一条规则；第二条规则。"
        sentences = split_sentences(text)

        assert len(sentences) == 1
        assert sentences[0].text == text


class TestBookmarksAndQuotes:
    """Test 3：书名号与引号"""

    def test_book_title_no_split_inside(self):
        """书名号内部不切分。"""
        text = "双方确认《中华人民共和国民法典》为本合同适用法律依据。争议解决适用中国法律。"

        sentences = split_sentences(text)

        assert len(sentences) == 2
        assert "《中华人民共和国民法典》" in sentences[0].text
        assert sentences[1].text == "争议解决适用中国法律。"
        assert "".join(s.text for s in sentences) == text

    def test_quote_with_closing(self):
        """引号内强切分符后紧跟关闭符号，关闭符号归属前句。"""
        text = '他在函件中称“本合同自始无效。”我方不予认可。'

        sentences = split_sentences(text)

        assert len(sentences) == 2
        # 第一句应以右引号结尾
        assert sentences[0].text.endswith('。”')
        # 确保 join 无损
        assert "".join(s.text for s in sentences) == text

    def test_chinese_left_and_right_quotes_do_not_merge_following_sentence(self):
        """右引号应关闭引号状态，后续法条句不得被并入同一 Anchor。"""
        text = (
            "《解释》第九条规定，足以使相关公众误认为与他人存在特定联系，即构成“容易导致混淆”。"
            "第十条规定，认定混淆还应考虑其他因素。"
        )

        sentences = split_sentences(text)

        assert [sentence.text for sentence in sentences] == [
            "《解释》第九条规定，足以使相关公众误认为与他人存在特定联系，即构成“容易导致混淆”。",
            "第十条规定，认定混淆还应考虑其他因素。",
        ]


class TestNoSplitScenarios:
    """不切分场景测试"""

    def test_halfwidth_period_no_split(self):
        """半角句点不切分。"""
        text = "日期2026.06.01，版本1.2，文件名为readme.txt，结束。"

        sentences = split_sentences(text)

        # 只有中文逗号，半角句点不触发切分，整段为一句
        assert len(sentences) == 1
        assert sentences[0].text == text

    def test_comma_no_split(self):
        """逗号不切分。"""
        text = "任何一方不得擅自解除合同，但法律另有规定的除外。"

        sentences = split_sentences(text)

        # 整句作为一个句子
        assert len(sentences) == 1
        assert sentences[0].text == text


class TestLosslessInvariant:
    """无损分句不变量"""

    def test_various_texts(self):
        """各种文本都满足无损不变量。"""
        # 使用 “ (") 和 ” (") 避免与 Python 字符串引号冲突
        LQ = "“"  # 左中文引号 "
        RQ = "”"  # 右中文引号 "
        test_cases = [
            "《合同》第一条。第二条。",
            f"他说{LQ}你好。{RQ}我说{LQ}再见。{RQ}",
            "（参见附件一）本协议一式两份。自签署之日起生效。",
            "根据【2023】民终字第001号判决，事实清楚。适用法律正确。",
            "",
            "单句",
            "第一；第二；第三；第四",
        ]

        for text in test_cases:
            sentences = split_sentences(text)
            joined = "".join(s.text for s in sentences)
            assert joined == text, f"无损不变量失败: input={repr(text)}, joined={repr(joined)}"

            for sent in sentences:
                extracted = text[sent.char_start:sent.char_end]
                assert extracted == sent.text, (
                    f"偏移不变量失败: text[{sent.char_start}:{sent.char_end}]="
                    f"'{extracted}' != '{sent.text}'"
                )
