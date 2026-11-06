import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest
import apply_review as A

_MD = ("---\nsource: ../x.pdf\npages: 2\nengine: MinerU\n---\n\n"
       "<!-- PAGE 1 -->\n\n原告玉某某诉被告。\n\n"
       "<!-- PAGE 2 -->\n\n第二页正文。\n")

_CORR = [{
    "page": 1,
    "corrected_text": "原告王某某诉被告。",
    "changes": [{"old": "玉", "new": "王", "basis": "原件 p.1"}],
}]


def test_page_body_replaced():
    out = A.apply_corrections(_MD, _CORR, coverage="deep", date="2026-07-01")
    assert "原告王某某诉被告。" in out and "原告玉某某" not in out
    assert "第二页正文。" in out                       # 未改页保持


def test_front_matter_marked():
    out = A.apply_corrections(_MD, _CORR, coverage="deep", date="2026-07-01")
    fm = __import__("mdpages").parse_front_matter(out)
    assert fm["reviewed"] == "2026-07-01"
    assert fm["reviewed_coverage"] == "deep"
    assert fm["corrections"] == "1"


def test_provenance_block():
    out = A.apply_corrections(_MD, _CORR, coverage="deep", date="2026-07-01")
    assert "## 复核溯源" in out
    assert "玉" in out and "王" in out and "原件 p.1" in out    # 原文可回溯


def test_page_markers_preserved():
    out = A.apply_corrections(_MD, _CORR, coverage="deep", date="2026-07-01")
    assert "<!-- PAGE 1 -->" in out and "<!-- PAGE 2 -->" in out


# ── 审查修复回归 ──────────────────────────────────────────────────────────

def test_body_containing_prov_header_not_truncated():
    # CRITICAL：页正文里 inline 出现 "## 复核溯源" 不得导致后续页被静默删掉
    md = ("---\nsource: ../x.pdf\npages: 3\n---\n\n"
          "<!-- PAGE 1 -->\n\n第一页原文玉。\n\n"
          "<!-- PAGE 2 -->\n\n本页正文提到 ## 复核溯源 相关内容如下。\n\n"
          "<!-- PAGE 3 -->\n\n第三页正文，重要，不应丢失。\n")
    corr = [{"page": 1, "corrected_text": "第一页原文王。",
             "changes": [{"old": "玉", "new": "王", "basis": "原件 p.1"}]}]
    out = A.apply_corrections(md, corr, coverage="deep", date="2026-07-01")
    assert "第三页正文，重要，不应丢失。" in out and "<!-- PAGE 3 -->" in out
    assert "第一页原文王。" in out


def test_missing_front_matter_raises():
    with pytest.raises(ValueError):
        A.apply_corrections("没有 front-matter 的纯文本。", [], coverage="deep", date="2026-07-01")


def test_bad_page_number_raises():
    corr = [{"page": 9, "corrected_text": "x", "changes": []}]
    with pytest.raises(ValueError):
        A.apply_corrections(_MD, corr, coverage="deep", date="2026-07-01")


def test_page_count_mismatch_raises():
    # front-matter 声明 3 页，实际只有 2 个页码标记 → 中止，防损坏
    md = ("---\nsource: ../x.pdf\npages: 3\n---\n\n"
          "<!-- PAGE 1 -->\n\n甲。\n\n<!-- PAGE 2 -->\n\n乙。\n")
    with pytest.raises(ValueError):
        A.apply_corrections(md, [], coverage="deep", date="2026-07-01")


def test_pipe_escaped_in_provenance():
    corr = [{"page": 1, "corrected_text": "原告王某某诉被告。",
             "changes": [{"old": "a|b", "new": "c|d", "basis": "原件 p.1"}]}]
    out = A.apply_corrections(_MD, corr, coverage="deep", date="2026-07-01")
    assert "a\\|b" in out and "c\\|d" in out


def test_idempotent_no_stacking():
    once = A.apply_corrections(_MD, _CORR, coverage="deep", date="2026-07-01")
    twice = A.apply_corrections(once, _CORR, coverage="deep", date="2026-07-01")
    assert twice.count("## 复核溯源") == 1          # 溯源块不堆叠
    assert twice.count("<!-- CC-REVIEW-PROVENANCE -->") == 1
    assert twice == once                            # 完全幂等


def test_standalone_prov_header_in_last_page_body_survives():
    # 末页正文里「独占一行」的 ## 复核溯源 也不得被吞（哨兵剥离只认机器串，非该标题）
    md = ("---\nsource: ../x.pdf\npages: 2\n---\n\n"
          "<!-- PAGE 1 -->\n\n第一页玉。\n\n"
          "<!-- PAGE 2 -->\n\n本页有一节\n\n## 复核溯源\n\n属于正文，不该丢。\n")
    corr = [{"page": 1, "corrected_text": "第一页王。",
             "changes": [{"old": "玉", "new": "王", "basis": "p.1"}]}]
    once = A.apply_corrections(md, corr, coverage="deep", date="2026-07-01")
    assert "属于正文，不该丢。" in once
    twice = A.apply_corrections(once, corr, coverage="deep", date="2026-07-01")
    assert "属于正文，不该丢。" in twice                     # 再次复核也不吞正文
    assert twice.count("<!-- CC-REVIEW-PROVENANCE -->") == 1
