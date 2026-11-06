import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import review as RV


def test_clean_page_not_suspect():
    text = "本院认为，被告应于本判决生效之日起十日内向原告支付货款人民币二十八万元。"
    assert RV.is_suspect(RV.page_suspect_signals(text)) is False


def test_garbled_page_suspect():
    # 大量孤立单字 + 空格断裂 = 典型糊字页
    text = "本 院 认 为 兹 因 甲 乙 丙 丁 戊 己 庚 辛 壬 癸 子 丑 寅 卯 辰 巳"
    assert RV.is_suspect(RV.page_suspect_signals(text)) is True


def test_replacement_chars_suspect():
    text = "货款金额为二十�万元整，付款期限�十日。" * 3
    assert RV.page_suspect_signals(text)["replacement_chars"] >= 2
    assert RV.is_suspect(RV.page_suspect_signals(text)) is True


def test_empty_page_not_suspect():
    # 空白页不直接判存疑，但必须送 Vision 交叉（避免误报，同时不放过 MinerU 整页吞掉的情形）
    assert RV.is_suspect(RV.page_suspect_signals("")) is False
    assert RV.needs_vision(RV.page_suspect_signals("")) is True


def test_short_page_needs_vision():
    signals = RV.page_suspect_signals("第3页")
    assert RV.is_suspect(signals) is False
    assert RV.needs_vision(signals) is True


def test_normal_page_no_vision():
    text = "本院认为，被告应于本判决生效之日起十日内向原告支付货款人民币二十八万元。"
    assert RV.needs_vision(RV.page_suspect_signals(text)) is False


from dataclasses import dataclass


@dataclass
class _FakeVB:      # 替身，避免依赖真实 Vision
    text: str
    confidence: float
    bbox: tuple = (0, 0, 1, 1)


def test_divergence_agree_not_diverged():
    mineru = "被告应支付货款二十八万元"
    vb = [_FakeVB("被告应支付货款二十八万元", 1.0)]
    d = RV.page_divergence(mineru, vb)
    assert d["diverged"] is False and d["similarity"] > 0.95


def test_divergence_missing_content():
    # 主信号：MinerU 漏抽整段（Vision 中文显著多于 MinerU）→ 分歧（真实校准抓到过整条条款被吞）
    mineru = "第十四条乙方向甲方采购产品应当提前十五个工作日通知。"
    vb = [_FakeVB("第十三条乙方所有核心配料均需在甲方购买连续三个月无进货记录则本合同自动解除不得继续使用品牌。", 1.0),
          _FakeVB("第十四条乙方向甲方采购产品应当提前十五个工作日通知。", 1.0)]
    d = RV.page_divergence(mineru, vb)
    assert d["diverged"] is True
    assert d["miss_ratio"] >= 1.25 and d["score"] > 0


def test_low_conf_alone_not_diverged():
    # 低置信块单独存在不再触发分歧（真实扫描件几乎每页都有，会淹没）——只记录
    mineru = "被告应支付货款二十八万元整"
    vb = [_FakeVB("被告应支付货款二十八万元整", 0.3)]
    d = RV.page_divergence(mineru, vb)
    assert d["low_conf_count"] == 1
    assert d["diverged"] is False


def test_tiny_char_diff_not_flagged():
    # 干净页上的单字认错（玉/王）本层不追（真实数据无法在不误报海量下单独拎出，诚实局限）
    mineru = "原告王某某与被告李某某因合同纠纷一案"
    vb = [_FakeVB("原告玉某某与被告李某某因合同纠纷一案", 1.0)]
    d = RV.page_divergence(mineru, vb)
    assert d["diverged"] is False


def test_empty_mineru_vision_rich_diverged():
    # MinerU 该页 CJK 为 0（整页被吞）、Vision 读出 ≥10 汉字 → 判漏抽（miss_ratio 分母为零时的兜底主信号）
    vb = [_FakeVB("某30字以上合同条款汉字文本内容示例用以确保超过十字阈值判定", 1.0)]
    d = RV.page_divergence("", vb)
    assert d["diverged"] is True
    assert d["empty_mineru"] is True
    assert d["vision_cjk_len"] >= 10
    assert d["score"] > 0


def test_empty_mineru_vision_stamp_only_not_diverged():
    # MinerU CJK 为 0，但 Vision 只读出戳记/页脚级别的少量字（<10）→ 真版式空页，不判漏抽
    vb = [_FakeVB("第三页共十页", 1.0)]
    d = RV.page_divergence("", vb)
    assert d["diverged"] is False


def test_empty_mineru_no_vision_blocks_not_diverged():
    # MinerU 与 Vision 均无内容 → 真空页，不判漏抽
    d = RV.page_divergence("", [])
    assert d["diverged"] is False


def test_table_page_low_sim_not_garble_flagged():
    # MinerU 出表格的页：Vision 无法对齐表结构、相似度低，但属误报，不因"乱码"触发
    mineru = ("<table><tr><td>购买方信息</td><td>黄某某</td></tr>"
              "<tr><td>销售方信息</td><td>某地某某餐饮管理有限公司</td></tr></table>"
              "价税合计柒万伍仟捌佰圆整")
    vb = [_FakeVB("黄某某柒万", 0.9)]      # Vision 中文更少（不触发漏内容），相似度低
    d = RV.page_divergence(mineru, vb)
    assert d["diverged"] is False          # 含 <table> → 豁免强乱码触发；亦无漏内容


# ── 行级包含信号（2026-09-18） ─────────────────────────────────────────
def test_missing_single_clause_line_detected():
    # 整页 375 字只漏一行 31 字：miss_ratio≈1.08 抓不到，行级信号必须抓到
    body = "第九部分推广与宣传。" + "第二十三条甲方在开展推广宣传活动前应当将有关活动信息或要求及时通知乙方。" * 6
    missing = "第二十二条甲方开展推广与宣传活动应当严格遵守法律法规的相关规定。"
    vb = [_FakeVB(missing, 0.9), _FakeVB(body, 0.9)]
    d = RV.page_divergence(body, vb)
    assert d["missing_lines"] == 1 and d["missing_line_cjk"] >= 25
    assert d["diverged"] is True and d["miss_ratio"] < RV._MISS_RATIO


def test_stamp_and_footer_lines_do_not_trigger():
    body = "本院认为被告应于本判决生效之日起十日内向原告支付货款人民币二十八万元。" * 5
    vb = [_FakeVB(body, 0.9), _FakeVB("某某商贸管理有限公司", 0.8),   # 印章 13 字，单行不够 25
          _FakeVB("第8页共12页", 0.9)]                                       # 页脚 <8 字不参与
    d = RV.page_divergence(body, vb)
    assert d["missing_lines"] == 1 and d["diverged"] is False


def test_line_coverage_tolerates_single_char_ocr_errors():
    page = "乙方所有核心配料均需在甲方购买如连续三个月无进货记录则本合同自动解除"
    line = "乙方所有核心配料均需在甲方购买如连续三个月无进货记綠则本合同自动解除"   # 一字之差
    assert RV._line_coverage(line, page) >= 0.9
    assert RV.missing_lines(page, [_FakeVB(line, 0.9)]) == []


def test_two_short_vision_misreads_do_not_trigger():
    # 实测 p.8：Vision 繁简/多字误读让两条 ~11 字短行覆盖率跌破线，合计 22 字 <25 → 不触发（MinerU 实际齐全）
    body = "第八部分消费者投诉处理。第二十一条乙方对消费者直接投诉乙方店的应当及时处理对造成消费者权益损害的应当及时采取补救措施。" * 3
    vb = [_FakeVB(body, 0.9), _FakeVB("第八部分消費者投訴處理", 0.9), _FakeVB("害的应当及时来取补教機", 0.9)]
    d = RV.page_divergence(body, vb)
    assert d["missing_line_cjk"] < RV._LINE_CJK_MIN and d["diverged"] is False


# ── Vision 补行草稿（2026-09-18） ──────────────────────────────────────
def test_draft_inserts_missing_line_after_anchor():
    page = "## 第九部分 推广与宣传\n\n第二十三条 甲方在开展推广宣传活动前应当及时通知乙方。\n\n第二十四条 乙方可自行策划推广活动。"
    vb = [_FakeVB("第九部分 推广与宣传", 0.9),
          _FakeVB("第二十二条 甲方开展推广与宣传活动，应当严格遵守法律、法规的相关规定。", 0.9),
          _FakeVB("第二十三条 甲方在开展推广宣传活动前应当及时通知乙方。", 0.9),
          _FakeVB("第二十四条 乙方可自行策划推广活动。", 0.9)]
    out = RV.draft_page_text(page, vb)
    assert out is not None
    i22, i23, i9 = out.index("第二十二条"), out.index("第二十三条"), out.index("第九部分")
    assert i9 < i22 < i23                      # 插在标题之后、第二十三条之前
    assert out.count("第二十三条") == 1        # 不重复原有行


def test_draft_appends_when_anchor_not_found():
    # 前块只有 6 字（不够 8 字、不算缺行）且在原文里对不上 → 锚定失败 → 缺行追加到页末
    page = "正文甲乙丙丁戊己庚辛。"
    vb = [_FakeVB("无关六个字块", 0.9), _FakeVB("这一整行在原文里找不到需要补回来的内容", 0.9)]
    out = RV.draft_page_text(page, vb)
    assert out.startswith(page) and out.rstrip().endswith("这一整行在原文里找不到需要补回来的内容")


def test_draft_uses_vision_fulltext_for_swallowed_page():
    vb = [_FakeVB("第一行内容", 0.9), _FakeVB("第二行内容", 0.9)]
    assert RV.draft_page_text("", vb) == "第一行内容\n\n第二行内容"
    assert RV.draft_page_text("   ", []) is None


def test_no_draft_when_nothing_missing():
    page = "本院认为被告应于本判决生效之日起十日内向原告支付货款人民币二十八万元。"
    assert RV.draft_page_text(page, [_FakeVB(page, 0.9)]) is None


def test_draft_prepends_page_top_continuation():
    # p.11 实测形态：承接上页的续段是页首 Vision 块，无前锚 → 必须插到页首而非页末
    page = "## 第十三部分 合同争议的解决\n\n第三十三条 凡因本合同引起的争议应当协商解决。\n\n10"
    vb = [_FakeVB("失。遇有不可抗力的一方，应尽快将事件的情况以书面形式通知对方，并在事件发生的", 0.9),
          _FakeVB("合理时间内，提交不能履行或者部分不能履行本合同以及需要延期履行的理由的证明。", 0.9),
          _FakeVB("第十三部分 合同争议的解决", 0.9),
          _FakeVB("第三十三条 凡因本合同引起的争议应当协商解决。", 0.9), _FakeVB("10", 0.9)]
    out = RV.draft_page_text(page, vb)
    assert out.startswith("失。遇有不可抗力的一方")
    assert out.index("合理时间内") < out.index("第十三部分")     # 两条续段按序都在页首
    assert out.rstrip().endswith("10")


def test_draft_anchor_skips_short_blocks():
    page = "甲段落内容一二三四五六七八。\n\n10\n\n乙段落内容一二三四五六七八。"
    vb = [_FakeVB("甲段落内容一二三四五六七八。", 0.9), _FakeVB("10", 0.9),
          _FakeVB("这是被漏掉的整行内容需要补回原文之中去", 0.9), _FakeVB("乙段落内容一二三四五六七八。", 0.9)]
    out = RV.draft_page_text(page, vb)
    assert out.index("甲段落") < out.index("这是被漏掉") < out.index("乙段落")
