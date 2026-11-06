import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stamp
FIX = Path(__file__).resolve().parent / "fixtures"

def _md(name):
    return stamp.stamp(FIX / name, source_rel="../民事判决书.pdf",
                       engine="MinerU test", date="2026-06-03", assets_stem="民事判决书")

def test_three_shapes_two_pages():
    for name in ("paddleocr_sample.json", "mineru_v1_sample.json", "mineru_v2_sample.json"):
        md = _md(name)
        assert "<!-- PAGE 1 -->" in md and "<!-- PAGE 2 -->" in md, name
        assert "<!-- PAGE 3 -->" not in md, name

def test_front_matter():
    md = _md("paddleocr_sample.json")
    assert md.startswith("---\n") and "source: ../民事判决书.pdf" in md
    assert "pages: 2" in md and "engine: MinerU test" in md

def test_v2_title_not_swallowed():   # B2 回归：v2 嵌套 content 的标题必须保留
    md = _md("mineru_v2_sample.json")
    assert "判决书" in md and "第一页正文。" in md and "第二页正文。" in md

def test_v1_image_path_rewritten():  # B3 回归：图片引用重写到 .assets/images
    md = _md("mineru_v1_sample.json")
    assert "民事判决书.assets/images/a.jpg" in md

def test_order_and_marker_precedes_content():
    md = _md("mineru_v1_sample.json")
    assert md.index("第一页正文。") < md.index("<!-- PAGE 2 -->") < md.index("第二页正文。")

def test_table_block_preserved():    # 回归：type:table 的 table_body(HTML) 不得被吞
    md = _md("mineru_table_list_sample.json")
    assert "<table>" in md and "金额" in md and "2807898.97" in md, "表格 table_body 丢失"
    assert "表1 资产" in md and "注：单位元" in md, "表格 caption/footnote 丢失"

def test_list_block_preserved():     # 回归：type:list 的 list_items 不得被吞
    md = _md("mineru_table_list_sample.json")
    assert "1. 第一项材料" in md and "2. 第二项材料" in md, "list_items 丢失"

# ── Task 0：防缺陷加固（静默失败→响亮失败 + 完整性哨兵）──────────────────────
def test_unknown_block_type_warns():      # 未知块类型 → 计数并在 front-matter 响亮警告
    stats = {}
    md = stamp.stamp(FIX / "unknown_block_sample.json", source_rel="../x.pdf",
                     engine="t", date="d", assets_stem="x", stats=stats)
    assert stats["dropped"]["weird_new_type"] == 1, "未记录被丢弃的未知类型块"
    assert "weird_new_type" in md and "warnings:" in md, "warnings 未进入 front-matter"

def test_all_known_block_types_nonempty():  # 8+类已知块每类都产出非空（total==nonempty）
    stats = {}
    md = stamp.stamp(FIX / "all_block_types.json", source_rel="../x.pdf",
                     engine="t", date="d", assets_stem="x", stats=stats)
    assert stats["total"] == 11 and stats["nonempty"] == 11, stats   # 无块被吞
    assert not stats.get("dropped"), stats                            # 无未知类型
    assert "warnings:" not in md, "已知类型全覆盖却误报警告"
    assert "<table>" in md and "甲" in md          # table_body
    assert "条目一" in md and "条目二" in md         # list_items
    assert "E = mc^2" in md                         # equation
    assert "x.assets/images/c.jpg" in md            # chart 当图片处理，不被吞

def test_completeness_sentinel_low_ratio():  # 已知类型但大量空 → 完整性哨兵
    stats = {}
    md = stamp.stamp(FIX / "low_extraction_sample.json", source_rel="../x.pdf",
                     engine="t", date="d", assets_stem="x", stats=stats)
    assert not stats.get("dropped"), "无未知类型，dropped 应为空"
    assert "疑似大量内容未提取" in md, "完整性哨兵未触发"
    assert "warnings:" in md

# ── Task 3：还原 markdown 标题层级（text_level → #）──────────────────────────
def test_v1_text_level_becomes_heading():    # v1：text_level:1 → "# "
    md = _md("mineru_v1_sample.json")
    assert "# 判决书" in md and "## 判决书" not in md
    assert "# 第一页正文。" not in md          # 无 text_level 的正文不加 #

def test_v2_title_level_becomes_heading():   # v2：嵌套 content.level → "# "
    md = _md("mineru_v2_sample.json")
    assert "# 判决书" in md

def test_heading_clamp_and_plain():          # 级别 clamp 至 6；普通正文不加 #
    md = stamp.stamp(FIX / "heading_levels_sample.json", source_rel="../x.pdf",
                     engine="t", date="d", assets_stem="x")
    assert "# 一级标题" in md and "## 二级标题" in md
    assert "###### 超限九级" in md            # text_level:9 → clamp 6
    assert "####### " not in md               # 永不超过 6 级
    assert "普通正文段落" in md and "# 普通正文段落" not in md

# ── D3：页数守恒双端守卫 ──────────────────────────────────────────────────
def test_v1_gap_fill_keeps_numbering(tmp_path):   # 形态C：中间页 page_idx 缺失不得使页码前移
    data = [
        {"type": "text", "text": "第一页", "bbox": [0, 0, 1, 1], "page_idx": 0},
        {"type": "text", "text": "第三页", "bbox": [0, 0, 1, 1], "page_idx": 2},
    ]
    jf = tmp_path / "gap.json"
    jf.write_text(json.dumps(data), encoding="utf-8")
    md = stamp.stamp(jf, source_rel="../x.pdf", engine="t", date="d", assets_stem="x")
    assert "<!-- PAGE 1 -->" in md and "<!-- PAGE 2 -->" in md and "<!-- PAGE 3 -->" in md
    assert "<!-- PAGE 4 -->" not in md
    assert "pages: 3" in md
    p2 = md.index("<!-- PAGE 2 -->")
    p3 = md.index("<!-- PAGE 3 -->")
    assert "第三页" not in md[p2:p3]     # PAGE 2 后（page_idx 1，缺失）无正文
    assert "第三页" in md[p3:]           # PAGE 3 后是 page_idx 2 的内容，页码未前移

def test_expected_pages_pads_and_warns():   # 引擎产出少于源 PDF 页数 → 补空页 + 响亮警告
    md = stamp.stamp(FIX / "paddleocr_sample.json", source_rel="../x.pdf",
                     engine="t", date="d", assets_stem="x", expected_pages=4)
    assert md.count("<!-- PAGE ") == 4
    assert "pages: 4" in md
    assert "页数不守恒" in md and "warnings:" in md

def test_expected_pages_match_no_warning():   # 误报锁：expected 与实际一致时不警告
    md = stamp.stamp(FIX / "paddleocr_sample.json", source_rel="../x.pdf",
                     engine="t", date="d", assets_stem="x", expected_pages=2)
    assert "页数不守恒" not in md

def test_front_matter_text_layer():   # text_layer 透传 front-matter；不传则无该行（兼容锁）
    md = stamp.stamp(FIX / "paddleocr_sample.json", source_rel="../x.pdf",
                     engine="t", date="d", assets_stem="x", text_layer="none")
    assert "text_layer: none" in md
    md_no = stamp.stamp(FIX / "paddleocr_sample.json", source_rel="../x.pdf",
                        engine="t", date="d", assets_stem="x")
    assert "text_layer:" not in md_no

def test_index_block_preserved():    # 回归：MinerU 4.0 目录块 type:index（list_items）不得被当未知类型丢弃
    stats = {}
    md = stamp.stamp(FIX / "mineru_index_sample.json", source_rel="../x.pdf",
                     engine="t", date="d", assets_stem="x", stats=stats)
    assert "- 第一章 法典化背景下的民事法律适用规则……3" in md, "目录 list_items 丢失"
    assert "- 第一节 民事法律规范内部的适用问题……5" in md
    assert md.index("第一节 民事法律规范") < md.index("<!-- PAGE 2 -->"), "目录须留在原页"
    assert not stats.get("dropped") and "warnings:" not in md, stats
