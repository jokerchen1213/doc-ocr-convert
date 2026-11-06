import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yaml
import pdf_render
import review as RV
import second_opinion as SO
import vision_ocr as V
import cc_convert


# ── diff_flags 纯函数 ──────────────────────────────────────────────────────
def test_identical_text_no_flags():
    t = "本院认为，被告应支付货款二十八万元。"
    assert SO.diff_flags(t, "本院认为 被告应支付货款二十八万元") == []      # 标点/空白差异不算


def test_glm_reads_missing_segment():
    m = "第十四条乙方应当提前十五个工作日通知。"
    g = "第十三条乙方所有核心配料均需在甲方购买。第十四条乙方应当提前十五个工作日通知。"
    f = SO.diff_flags(m, g)
    assert len(f) == 1 and f[0]["kind"] == "glm_only" and "第十三条" in f[0]["glm"]


def test_single_char_disagreement_flagged():
    f = SO.diff_flags("证据提交日期二〇二六年四月三十八日", "证据提交日期二〇二六年四月二十八日")
    assert len(f) == 1 and f[0]["kind"] == "differ" and (f[0]["mineru"], f[0]["glm"]) == ("三", "二")


def test_table_reorder_not_flagged():
    # 表格单元格阅读顺序不同：整段在对方别处出现 → 不算分歧
    m = "序号一出行数据可视化高精度地图符合序号二信息发布管理符合"
    g = "序号二信息发布管理符合序号一出行数据可视化高精度地图符合"
    assert SO.diff_flags(m, g) == []


def test_glm_empty_means_no_flags():
    assert SO.diff_flags("正文内容正文内容", "") == []


def test_mineru_runaway_flagged_short_extra_ignored():
    base = "甲方用人单位名称地址联系电话"
    runaway = base + "".join(str(i) for i in range(160, 180))
    f = SO.diff_flags(runaway, base)
    assert len(f) == 1 and f[0]["kind"] == "mineru_only"
    assert SO.diff_flags(base + "第7页", base) == []                     # 短的 MinerU 独有（页码等）不报


# ── build_review 集成 ─────────────────────────────────────────────────────
def _stamped(tmp_path, name, pages):
    pdf = tmp_path / name; pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    md_dir = tmp_path / "_md"; md_dir.mkdir(exist_ok=True)
    body = "\n\n".join(f"<!-- PAGE {i} -->\n\n{t}" for i, t in enumerate(pages, 1))
    (md_dir / (pdf.stem + ".md")).write_text(
        f"---\nsource: ../{name}\npages: {len(pages)}\nengine: x\nconverted: d\n---\n\n{body}\n", encoding="utf-8")
    return pdf


def _patch_render(monkeypatch, n):
    monkeypatch.setattr(pdf_render, "render_page_ndarray", lambda p, idx, **k: idx)
    monkeypatch.setattr(pdf_render, "render_page_png", lambda p, idx, d, **k: (Path(d) / f"page-{idx+1}.png"))
    monkeypatch.setattr(pdf_render, "page_count", lambda p: n)


P1 = "本院认为，被告应支付货款二十八万元整，付款期限为本判决生效之日起十日内。"
P2 = "证据提交日期二〇二六年四月三十八日，提交人签名。"


def test_second_opinion_adds_flagged_page_without_draft(tmp_path, monkeypatch):
    pdf = _stamped(tmp_path, "证据.pdf", [P1, P2])
    _patch_render(monkeypatch, 2)
    ocr = {0: [V.VBlock(P1, 0.95, (0, 0, 1, 1))], 1: [V.VBlock(P2, 0.95, (0, 0, 1, 1))]}   # Vision 与 MinerU 一致
    asked = {}
    def so(src, pages):
        asked["pages"] = pages
        return {1: P1, 2: P2.replace("三十八", "二十八")}
    r = RV.build_review(pdf, deep=True, ocr_fn=lambda img, **k: ocr[img], second_opinion_fn=so)
    assert asked["pages"] == [1, 2]                         # deep：全部页送第二意见
    assert r["second_opinion_pages"] == [2] and r["second_opinion_checked"] == 2
    assert r["suspect_pages"] == [2]                        # 只因 GLM 分歧入包
    assert r["draft_path"] is None                          # GLM 文本绝不进草稿
    packet = yaml.safe_load(Path(r["packet_path"]).read_text(encoding="utf-8"))
    e = packet["suspect_pages"][0]
    assert e["reason"]["second_opinion_only"] is True and e["reason"]["glm_flag_count"] == 1
    assert e["glm_flags"][0]["kind"] == "differ" and e["glm_text"].startswith("证据提交日期")
    assert packet["second_opinion"] == {"engine": "glm-ocr", "pages_checked": 2, "flagged_pages": [2]}


def test_second_opinion_failure_is_soft(tmp_path, monkeypatch):
    pdf = _stamped(tmp_path, "证据.pdf", [""])
    _patch_render(monkeypatch, 1)
    ocr = {0: [V.VBlock("整页被吞掉的正文内容这里有十几个汉字", 0.9, (0, 0, 1, 1))]}
    def boom(*a, **k): raise SO.SecondOpinionUnavailable("Windows 不在线")
    r = RV.build_review(pdf, ocr_fn=lambda img, **k: ocr[img], second_opinion_fn=boom)
    assert r["suspect_pages"] == [1] and r["drafted_pages"] == [1]      # Vision 路径照常
    assert "Windows 不在线" in r["second_opinion_error"]
    packet = yaml.safe_load(Path(r["packet_path"]).read_text(encoding="utf-8"))
    assert "second_opinion_error" in packet


def test_default_path_has_no_second_opinion_keys(tmp_path, monkeypatch):
    pdf = _stamped(tmp_path, "证据.pdf", [P1, P2])
    _patch_render(monkeypatch, 2)
    ocr = {0: [V.VBlock(P1, 0.95, (0, 0, 1, 1))], 1: [V.VBlock(P2, 0.95, (0, 0, 1, 1))]}
    r = RV.build_review(pdf, deep=True, ocr_fn=lambda img, **k: ocr[img])
    packet = yaml.safe_load(Path(r["packet_path"]).read_text(encoding="utf-8"))
    assert "second_opinion" not in packet and "second_opinion_error" not in packet
    assert all("glm_text" not in e for e in packet["suspect_pages"])


def test_quick_mode_sends_only_suspect_pages(tmp_path, monkeypatch):
    garbled = "本 院 认 为 甲 乙 丙 丁 戊 己 庚 辛 壬 癸 子 丑 寅 卯"
    pdf = _stamped(tmp_path, "证据.pdf", [P1, garbled])
    _patch_render(monkeypatch, 2)
    asked = {}
    def so(src, pages):
        asked["pages"] = pages
        return {p: "" for p in pages}
    RV.build_review(pdf, deep=False, ocr_fn=lambda img, **k: [V.VBlock("占位", 1.0, (0, 0, 1, 1))],
                    second_opinion_fn=so)
    assert asked["pages"] == [2]


# ── CLI ───────────────────────────────────────────────────────────────────
def test_cli_accepts_second_opinion_flag():
    args = cc_convert.build_parser().parse_args(["x.pdf", "--review", "--second-opinion", "glm"])
    assert args.second_opinion == "glm"
    assert cc_convert.build_parser().parse_args(["x.pdf", "--review"]).second_opinion is None


def test_image_links_with_parentheses_stripped():
    m = "正文内容如下![](证据-样本(1).assets/images/f29488875072b868d099bf7542ba.jpg)继续正文"
    assert SO.normalize(m) == "正文内容如下继续正文"


def test_normal_long_footer_not_flagged_as_mineru_only():
    base = "本人于二〇二五年十二月通过函件要求公司披露完整的项目财务资料以进行账目核对"
    footer = "第2页共3页扫描全能王3亿人都在用的扫描App由用户上传仅供参考请以原件为准谢谢合作"
    assert SO.diff_flags(base + footer, base) == []


# ── --remote ─────────────────────────────────────────────────────────────
def test_remote_rejects_non_convert_modes(capsys):
    assert cc_convert.main(["x.pdf", "--remote", "--review"]) == 2
    assert "只用于转换" in capsys.readouterr().err


def test_remote_passthrough_flags(monkeypatch, tmp_path):
    import remote_convert as RC
    seen = {}
    monkeypatch.setattr(RC, "run", lambda target, passthrough: seen.update(t=target, p=passthrough) or 0)
    f = tmp_path / "a.pdf"; f.write_bytes(b"%PDF-1.4\n")
    assert cc_convert.main([str(f), "--remote", "--force", "-t", "advanced"]) == 0
    assert seen["p"] == ["--force", "-t", "advanced"]


def test_remote_win_path_mapping():
    import remote_convert as RC
    p = RC.CASES_MAC / "某案" / "证据" / "a.pdf"
    assert RC.win_path(p) == r"D:\SyncDrive\cases\某案\证据\a.pdf"
    import pytest
    with pytest.raises(RC.RemoteConvertError):
        RC.win_path(Path("/tmp/x.pdf"))


def test_remote_win_path_mapping_library():
    import remote_convert as RC
    import pytest
    p = RC.LIBRARY_MAC / "04_学习资料" / "电子书" / "示例电子书.pdf"
    assert RC.win_path(p) == r"D:\SyncDrive\library\04_学习资料\电子书\示例电子书.pdf"
    assert RC.win_path(RC.LIBRARY_MAC) == r"D:\SyncDrive\library"
    # 同名前缀的兄弟目录不得误配
    with pytest.raises(RC.RemoteConvertError):
        RC.win_path(RC.LIBRARY_MAC.parent / "library-old" / "a.pdf")
