import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cc_convert import _review_hint

# 四象限：有 warnings → 非 None 且含"警告"；text_layer=none 无 warnings → 非 None 且含"扫描"；
# (full, 空 warnings) → None；(partial, 空) → 非 None。接线的一行 print 不单测。

def test_warnings_present_hints_warning():
    r = _review_hint("⚠️ 低提取率 3/10=30%（阈值 70%），疑似大量内容未提取，请人工核验", "full")
    assert r is not None and "警告" in r

def test_text_layer_none_no_warnings_hints_scan():
    r = _review_hint("", "none")
    assert r is not None and "扫描" in r

def test_full_no_warnings_no_hint():
    assert _review_hint("", "full") is None

def test_text_layer_partial_no_warnings_hints():
    r = _review_hint("", "partial")
    assert r is not None and "扫描" in r
