"""把客户端传来的纯文本整理成可写入 DOCX 的形式。

Office.js 的 `range.text` 会把脚注标记（\\x02）、单元格结束符（\\x07）、软回车
（\\x0b）等控制字符原样带出，而 python-docx 拒绝 XML 非法字符，直接抛 ValueError。

替换必须等长：选区核查靠"临时 DOCX 内的字符偏移 + 座位表起点 = 原文位置"这一加法
把结果映射回 Word（见 app.py 的 _rebase_selection_locations），增删任何一个字符都会
让该行后续锚点整体错位。
"""

from __future__ import annotations

import re

# python-docx（lxml）实际拒绝的码位，\x09 \x0a \x0d 合法故排除在外。
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_for_docx(text: str) -> str:
    """把 XML 非法控制字符逐个替换为半角空格，长度保持不变。"""
    return _XML_ILLEGAL.sub(" ", text)
