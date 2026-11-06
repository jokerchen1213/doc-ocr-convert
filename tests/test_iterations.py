"""2026-09-18 四项迭代的单元测试：图片证据入转换、OFD/旧 Office 走引擎 + Office 兜底、
复核层第三意见 + 草稿、--to 重渲染。MinerU 全部用替身，测试不依赖模型。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import json
import yaml
import pytest
import cc_convert as cc
import engine
import pdf_render
import review as RV
import vision_ocr as V
import mdpages


# ── engine 纯函数 ────────────────────────────────────────────────────────
def test_tier_for_only_pdf_and_images_take_tier(tmp_path):
    assert engine.tier_for(tmp_path / "a.pdf", "standard") == "standard"
    assert engine.tier_for(tmp_path / "a.JPG", "advanced") == "advanced"
    for ext in (".docx", ".doc", ".ofd", ".xls", ".rtf", ".html", ".epub"):
        assert engine.tier_for(tmp_path / f"a{ext}", "standard") == "flash"


def test_pages_text_from_content_list_keeps_original_page_idx():
    cl = [{"type": "text", "text": "第七页正文", "page_idx": 6},
          {"type": "text", "text": "标题", "text_level": 1, "page_idx": 8},
          {"type": "text", "text": "", "page_idx": 8},          # 空块不产出
          {"type": "table", "table_body": "<table><tr><td>x</td></tr></table>", "page_idx": 8}]
    out = engine.pages_text_from_content_list(cl)
    assert set(out) == {7, 9}
    assert out[7] == "第七页正文"
    assert out[9].startswith("# 标题") and "<table>" in out[9]


def test_asset_resolver_falls_back_to_placeholder(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "a.jpg").write_bytes(b"JPEGDATA")
    r = engine.make_asset_resolver(tmp_path / "images")
    assert r("images/a.jpg") == b"JPEGDATA"
    assert r("images/filtered_out.jpg").startswith(b"\x89PNG")      # 被小图过滤的素材 → 占位 PNG
    assert engine.make_asset_resolver(None)("images/x.jpg").startswith(b"\x89PNG")


def test_render_from_middle_rejects_unknown_format(tmp_path):
    with pytest.raises(ValueError):
        engine.render_from_middle(tmp_path / "middle_json.json", "xlsx")


# ── 图片证据：discover opt-in、pdf_render 图片支持 ───────────────────────
def test_discover_images_only_with_flag_or_explicit_file(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "shot.jpg").write_bytes(b"x")
    (tmp_path / "doc.doc").write_bytes(b"x")
    (tmp_path / ".hidden.jpg").write_bytes(b"x")
    names = lambda lst: sorted(p.name for p in lst)
    assert names(cc.discover(tmp_path, False)) == ["a.pdf", "doc.doc"]
    assert names(cc.discover(tmp_path, False, image_sources=True)) == ["a.pdf", "doc.doc", "shot.jpg"]
    assert names(cc.discover(tmp_path / "shot.jpg", False)) == ["shot.jpg"]   # 单文件指名无需开关
    assert "shot.jpg" not in names(cc.discover(tmp_path, True))


def test_source_exts_exclude_images_so_scan_pending_stays_quiet():
    assert not (cc.IMAGE_EXTS & cc.SOURCE_EXTS)
    assert {".ofd", ".doc", ".xls", ".ppt", ".rtf"} <= cc.SOURCE_EXTS


def _png(path, size=(40, 20)):
    from PIL import Image
    Image.new("RGB", size, "white").save(path)


def test_pdf_render_handles_image_sources(tmp_path):
    img = tmp_path / "shot.png"; _png(img)
    assert pdf_render.is_image(img) and not pdf_render.is_image(tmp_path / "a.pdf")
    assert pdf_render.page_count(img) == 1
    assert pdf_render.detect_text_layer(img) == "none"
    arr = pdf_render.render_page_ndarray(img, 0)
    assert arr.shape[:2] == (20, 40)
    png = pdf_render.render_page_png(img, 0, tmp_path / "review")
    assert png.name == "page-1.png" and png.exists()
    with pytest.raises(IndexError):
        pdf_render.render_page_ndarray(img, 1)


def _fake_engine(monkeypatch, tmp_path, text="图片正文", calls=None):
    """替身：engine.parse_to_dir 写一份 v1 content_list（不跑 MinerU）。"""
    def fake(src, out, tier, page_range=""):
        out = Path(out); out.mkdir(parents=True, exist_ok=True)
        cl = [{"type": "text", "text": text, "page_idx": 0}]
        (out / f"{Path(src).stem}_content_list.json").write_text(json.dumps(cl, ensure_ascii=False), encoding="utf-8")
        (out / "middle_json.json").write_text("{}", encoding="utf-8")
        if calls is not None:
            calls.append((Path(src).name, tier))
        return out / f"{Path(src).stem}_content_list.json", None
    monkeypatch.setattr(engine, "parse_to_dir", fake)
    monkeypatch.setattr(cc, "Path", cc.Path)      # no-op, keep explicit


def test_convert_image_source_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.shutil, "rmtree", lambda *a, **k: None)
    img = tmp_path / "微信截图.jpg"; _png(img)
    calls = []
    _fake_engine(monkeypatch, tmp_path, calls=calls)
    monkeypatch.setattr(cc, "run_mineru", lambda src, tmp, tier: engine.parse_to_dir(src, tmp / src.stem / tier, tier))
    assert cc.main([str(img)]) == 0
    md = mdpages.md_for(img)
    assert md is not None
    fm = mdpages.parse_front_matter(md.read_text(encoding="utf-8"))
    assert fm["pages"] == "1" and fm["text_layer"] == "none" and "(standard)" in fm["engine"]
    assert calls == [("微信截图.jpg", "standard")]
    assert (md.parent / "微信截图.assets" / "middle_json.json").exists()


def test_convert_legacy_doc_goes_flash(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.shutil, "rmtree", lambda *a, **k: None)
    doc = tmp_path / "所函.doc"; doc.write_bytes(b"\xd0\xcf\x11\xe0")
    calls = []
    _fake_engine(monkeypatch, tmp_path, calls=calls)
    monkeypatch.setattr(cc, "run_mineru", lambda src, tmp, tier: engine.parse_to_dir(src, tmp / src.stem / tier, tier))
    assert cc.main([str(doc), "--tier", "advanced"]) == 0
    assert calls == [("所函.doc", "flash")]
    fm = mdpages.parse_front_matter(mdpages.md_for(doc).read_text(encoding="utf-8"))
    assert "(flash)" in fm["engine"] and "text_layer" not in fm
    idx = (tmp_path / cc.INDEX_NAME).read_text(encoding="utf-8")
    assert "所函.doc" in idx and "✅已转" in idx


def test_office_native_failure_falls_back_to_engine(tmp_path, monkeypatch):
    import convert_office
    monkeypatch.setattr(cc.shutil, "rmtree", lambda *a, **k: None)
    docx = tmp_path / "坏文件.docx"; docx.write_bytes(b"not a zip")
    def boom(*a, **k): raise ValueError("File is not a zip file")
    monkeypatch.setattr(convert_office, "to_markdown", boom)
    calls = []
    _fake_engine(monkeypatch, tmp_path, calls=calls)
    monkeypatch.setattr(cc, "run_mineru", lambda src, tmp, tier: engine.parse_to_dir(src, tmp / src.stem / tier, tier))
    assert cc.main([str(docx)]) == 0
    assert calls == [("坏文件.docx", "flash")]
    fm = mdpages.parse_front_matter(mdpages.md_for(docx).read_text(encoding="utf-8"))
    assert "fallback" in fm["engine"]


# ── 复核层第三意见 ────────────────────────────────────────────────────────
def _stamped(tmp_path, name, pages):
    pdf = tmp_path / name; pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    md_dir = tmp_path / "_md"; md_dir.mkdir(exist_ok=True)
    body = "\n\n".join(f"<!-- PAGE {i} -->\n\n{t}" for i, t in enumerate(pages, 1))
    (md_dir / (pdf.stem + ".md")).write_text(
        f"---\nsource: ../{name}\npages: {len(pages)}\nengine: x\nconverted: d\n---\n\n{body}\n", encoding="utf-8")
    return pdf


def test_third_opinion_marks_recovery_and_writes_draft(tmp_path, monkeypatch):
    # p.1 MinerU 漏抽整段；Vision 读全；advanced 重识别与 Vision 一致 → recovers + 草稿
    pdf = _stamped(tmp_path, "证据.pdf", ["第十四条乙方应当提前十五个工作日通知。", "第二页完整正文内容没有问题的一页。"])
    full = "第十三条乙方所有核心配料均需在甲方购买如连续三个月无进货记录则本合同自动解除。第十四条乙方应当提前十五个工作日通知。"
    monkeypatch.setattr(pdf_render, "render_page_ndarray", lambda p, idx: idx)
    monkeypatch.setattr(pdf_render, "render_page_png", lambda p, idx, d: (Path(d) / f"page-{idx+1}.png"))
    monkeypatch.setattr(pdf_render, "page_count", lambda p: 2)
    ocr = {0: [V.VBlock(full, 0.95, (0, 0, 1, 1))], 1: [V.VBlock("第二页完整正文内容没有问题的一页。", 0.95, (0, 0, 1, 1))]}
    asked = {}
    def reparse(src, pages, tier):
        asked["pages"], asked["tier"] = pages, tier
        return {1: full}
    r = RV.build_review(pdf, deep=True, ocr_fn=lambda img, **k: ocr[img], third_opinion=True, reparse_fn=reparse)
    assert r["suspect_pages"] == [1]
    assert asked == {"pages": [1], "tier": "advanced"}
    assert r["advanced_recovered_pages"] == [1]
    packet = yaml.safe_load(Path(r["packet_path"]).read_text(encoding="utf-8"))
    e = packet["suspect_pages"][0]
    assert e["advanced_text"] == full and e["reason"]["advanced_recovers"] is True
    draft = yaml.safe_load(Path(r["draft_path"]).read_text(encoding="utf-8"))
    assert draft["draft"] is True and draft["corrections"][0]["page"] == 1
    assert "第十三条" in draft["corrections"][0]["corrected_text"]      # Vision 补行草稿优先
    assert r["drafted_pages"] == [1]


def test_third_opinion_failure_is_soft(tmp_path, monkeypatch):
    pdf = _stamped(tmp_path, "证据.pdf", [""])
    monkeypatch.setattr(pdf_render, "render_page_ndarray", lambda p, idx: idx)
    monkeypatch.setattr(pdf_render, "render_page_png", lambda p, idx, d: (Path(d) / f"page-{idx+1}.png"))
    monkeypatch.setattr(pdf_render, "page_count", lambda p: 1)
    ocr = {0: [V.VBlock("整页被吞掉的正文内容这里有十几个汉字", 0.9, (0, 0, 1, 1))]}
    def boom(*a, **k): raise RuntimeError("model missing")
    r = RV.build_review(pdf, ocr_fn=lambda img, **k: ocr[img], third_opinion=True, reparse_fn=boom)
    assert r["suspect_pages"] == [1] and r["advanced_recovered_pages"] == []
    assert r["draft_path"] is not None and r["drafted_pages"] == [1]      # 第三意见失败不影响 Vision 草稿
    assert "RuntimeError" in r["third_opinion_error"]
    packet = yaml.safe_load(Path(r["packet_path"]).read_text(encoding="utf-8"))
    assert "third_opinion_error" in packet


def test_third_opinion_no_recovery_when_advanced_also_diverges(tmp_path):
    entries = [{"page": 1, "mineru_text": "甲", "reason": {}}]
    vb = {1: [V.VBlock("完整的一段很长的正文内容在这里有很多字", 0.9, (0, 0, 1, 1))]}
    assert RV.third_opinion_merge(entries, vb, {1: "乙"}) == []
    assert entries[0]["reason"]["advanced_recovers"] is False
    assert RV.third_opinion_merge(entries, vb, {}) == []       # 重识别无该页 → 不 recovers


# ── --to 重渲染 CLI 接线 ─────────────────────────────────────────────────
def test_to_renders_from_archived_middle_json(tmp_path, monkeypatch):
    pdf = _stamped(tmp_path, "判决.pdf", ["正文"])
    assets = tmp_path / "_md" / "判决.assets"; assets.mkdir()
    (assets / "middle_json.json").write_text("{}", encoding="utf-8")
    seen = {}
    def fake_render(mj, fmt, images_dir=None, asset_base_url="", title=None):
        seen.update(mj=Path(mj), fmt=fmt, base=asset_base_url, title=title)
        return b"DOCX" if fmt == "docx" else "<html/>"
    monkeypatch.setattr(engine, "render_from_middle", fake_render)
    assert cc.main([str(pdf), "--to", "docx"]) == 0
    assert (tmp_path / "_md" / "判决.docx").read_bytes() == b"DOCX"
    assert seen["fmt"] == "docx" and seen["mj"] == assets / "middle_json.json" and seen["title"] == "判决"
    assert cc.main([str(pdf), "--to", "html"]) == 0
    assert (tmp_path / "_md" / "判决.html").read_text(encoding="utf-8") == "<html/>"
    assert seen["base"] == "判决.assets/"


def test_to_requires_middle_json(tmp_path, monkeypatch, capsys):
    pdf = _stamped(tmp_path, "旧件.pdf", ["正文"])           # 3.x 转换件：无 middle_json
    assert cc.main([str(pdf), "--to", "docx"]) == 1
    assert "middle_json.json" in capsys.readouterr().err
    pdf2 = tmp_path / "未转.pdf"; pdf2.write_bytes(b"%PDF")
    assert cc.main([str(pdf2), "--to", "html"]) == 1


def test_to_skips_native_office(tmp_path, capsys):
    docx = tmp_path / "合同.docx"; docx.write_bytes(b"x")
    assert cc.main([str(docx), "--to", "docx"]) == 0
    assert "跳过" in capsys.readouterr().out


def test_engine_failure_returns_nonzero(tmp_path, monkeypatch, capsys):
    # 回归：引擎失败（如 Codex 沙箱无 Metal → load_model failed）曾打印「完成」且退出码 0，调用方无法据退出码判定失败
    monkeypatch.setattr(cc.shutil, "rmtree", lambda *a, **k: None)
    img = tmp_path / "坏图.jpg"; _png(img)
    def boom(src, tmp, tier): raise RuntimeError("load_model failed")
    monkeypatch.setattr(cc, "run_mineru", boom)
    assert cc.main([str(img)]) == 1
    out = capsys.readouterr()
    assert "[fail] 坏图.jpg: load_model failed" in out.err
    assert "1 个失败" in out.out
    assert mdpages.md_for(img) is None
