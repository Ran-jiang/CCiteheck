"""裁判说理清洗、切句、截断检测与引文构建。"""

from ccitecheck.verification.reasoning import (
    build_excerpt,
    clean_reasoning_text,
    reasoning_is_truncated,
    split_reasoning_sentences,
)


def test_clean_removes_vendor_hard_wraps_inside_sentences():
    wrapped = "由于万××司在运输过程\n未尽相关义务，存在违约行为，造成曹某某受伤。"
    assert clean_reasoning_text(wrapped) == (
        "由于万××司在运输过程未尽相关义务，存在违约行为，造成曹某某受伤。"
    )


def test_split_keeps_terminal_punctuation_and_order():
    text = "第一句。第二句；第三句！"
    assert split_reasoning_sentences(text) == ["第一句。", "第二句；", "第三句！"]


def test_truncated_when_last_sentence_lacks_terminal_punctuation():
    sentences = split_reasoning_sentences("第一句。损失赔偿额应当相当于因违约所造")
    assert sentences[-1] == "损失赔偿额应当相当于因违约所造"
    assert reasoning_is_truncated(sentences)
    assert not reasoning_is_truncated(split_reasoning_sentences("第一句。第二句。"))


def test_excerpt_merges_adjacent_hits_and_marks_gaps():
    sentences = ["甲。", "乙。", "丙。", "丁。"]
    assert build_excerpt(sentences, [1, 2]) == "……甲。乙。……"
    assert build_excerpt(sentences, [1, 4]) == "……甲。……丁。……"
    assert build_excerpt(sentences, [3]) == "……丙。……"


def test_excerpt_ignores_out_of_range_and_duplicate_ids():
    sentences = ["甲。", "乙。"]
    assert build_excerpt(sentences, [2, 2, 9, 0]) == "……乙。……"
    assert build_excerpt(sentences, [99]) == ""
