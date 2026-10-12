from ccitecheck.verification.markers import strip_internal_markers


def test_strips_internal_markers_without_damaging_legal_text():
    text = "line00023[[内部]]⟦a⟧【anchor:x】保留【裁判要旨】[2019]京73民初1234号 guideline2024"
    cleaned = strip_internal_markers(text)
    assert "line00023" not in cleaned
    assert "[[内部]]" not in cleaned
    assert "⟦a⟧" not in cleaned
    assert "【anchor:x】" not in cleaned
    assert "【裁判要旨】" in cleaned
    assert "[2019]京73民初1234号" in cleaned
    assert "guideline2024" in cleaned
