"""保真守卫：docvortex 的全角→半角规范化在 cc-convert 进程内必须被短路为恒等。"""
import pytest
import cc_convert

docvortex = pytest.importorskip("docvortex")


def test_guard_makes_normalization_identity():
    from docvortex.content import normalization as n
    sample = [[{"type": "text", "content": "原告：张三，利率１４.６％，编号Ａ－１"}]]
    assert cc_convert._disable_upstream_text_normalization() is True
    n.normalize_pdf_model_text(sample)
    assert sample[0][0]["content"] == "原告：张三，利率１４.６％，编号Ａ－１"


def test_guard_covers_table_cells():
    from docvortex.content import normalization as n
    html = "<table><tr><td>金额：１００％</td></tr></table>"
    cc_convert._disable_upstream_text_normalization()
    sample = [[{"type": "table", "content": html}]]
    n.normalize_pdf_model_text(sample)
    assert sample[0][0]["content"] == html
