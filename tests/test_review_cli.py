import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yaml
import review as RV
import vision_ocr as V
import cc_convert


def _setup_case(tmp_path):
    """造一个"已转"的最小案件：源 PDF + _md/<stem>.md（页2糊字）。"""
    pdf = tmp_path / "证据.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%stub\n")     # 内容不重要：渲染被 monkeypatch 掉
    md = tmp_path / "_md" / "证据.md"
    md.parent.mkdir(parents=True)
    md.write_text(
        "---\nsource: ../证据.pdf\npages: 2\n---\n\n"
        "<!-- PAGE 1 -->\n\n本院认为，被告应支付货款二十八万元整，付款期限十日。\n\n"
        "<!-- PAGE 2 -->\n\n本 院 认 为 甲 乙 丙 丁 戊 己 庚 辛 壬 癸 子 丑 寅 卯\n",
        encoding="utf-8")
    return pdf


def test_build_review_flags_garbled_page(tmp_path, monkeypatch):
    pdf = _setup_case(tmp_path)
    # 替身：渲染返回假 ndarray，OCR 返回与 MinerU 一致文本（隔离测"启发式挑页"）
    import pdf_render
    monkeypatch.setattr(pdf_render, "render_page_ndarray", lambda *a, **k: object())
    monkeypatch.setattr(pdf_render, "render_page_png",
                        lambda p, i, out, **k: Path(out).joinpath(f"page-{i+1}.png"))
    fake_ocr = lambda img, **k: [V.VBlock("占位", 1.0, (0, 0, 1, 1))]

    result = RV.build_review(pdf, deep=False, ocr_fn=fake_ocr)
    assert 2 in result["suspect_pages"] and 1 not in result["suspect_pages"]
    assert result["reviewed_coverage"] == "suspect"
    packet = yaml.safe_load(Path(result["packet_path"]).read_text(encoding="utf-8"))
    pages = {p["page"] for p in packet["suspect_pages"]}
    assert pages == {2}
    assert packet["suspect_pages"][0]["mineru_text"].startswith("本 院")


def test_review_reads_disambiguated_md(tmp_path, monkeypatch):
    # 跨格式重名：foo.pdf 后写 _md/foo.pdf.md（消歧），foo.md 属于 foo.docx；
    # build_review 必须靠 source 权威解析读对文书，不能踩着 stem 硬编码路径读错。
    pdf = tmp_path / "foo.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    (tmp_path / "foo.docx").write_bytes(b"PK")
    mddir = tmp_path / "_md"; mddir.mkdir()
    (mddir / "foo.md").write_text(
        "---\nsource: ../foo.docx\npages: 1\n---\n\n"
        "<!-- PAGE 1 -->\n\nDOCX独有内容：本 院 认 为 甲 乙 丙 丁 戊 己 庚 辛 壬 癸。\n",
        encoding="utf-8")
    (mddir / "foo.pdf.md").write_text(
        "---\nsource: ../foo.pdf\npages: 1\n---\n\n"
        "<!-- PAGE 1 -->\n\nPDF独有条款：本 院 认 为 甲 乙 丙 丁 戊 己 庚 辛 壬 癸 子 丑 寅 卯。\n",
        encoding="utf-8")

    import pdf_render
    monkeypatch.setattr(pdf_render, "render_page_ndarray", lambda *a, **k: object())
    monkeypatch.setattr(pdf_render, "render_page_png",
                        lambda p, i, out, **k: Path(out).joinpath(f"page-{i+1}.png"))
    fake_ocr = lambda img, **k: [V.VBlock("占位", 1.0, (0, 0, 1, 1))]

    result = RV.build_review(pdf, deep=False, ocr_fn=fake_ocr)
    assert result["suspect_pages"], "应至少挑出一页存疑"
    packet = yaml.safe_load(Path(result["packet_path"]).read_text(encoding="utf-8"))
    mineru_text = packet["suspect_pages"][0]["mineru_text"]
    assert "PDF独有条款" in mineru_text
    assert "DOCX独有内容" not in mineru_text
    packet_posix = Path(result["packet_path"]).as_posix()
    assert packet_posix.endswith("_md/foo.pdf.assets/review/packet.yaml")


def test_vision_text_joined_by_newline(tmp_path, monkeypatch):
    # packet 的 vision_text 按块换行拼接，便于人工/LM 按块裁断（而非无缝粘连）
    pdf = _setup_case(tmp_path)
    import pdf_render
    monkeypatch.setattr(pdf_render, "render_page_ndarray", lambda *a, **k: object())
    monkeypatch.setattr(pdf_render, "render_page_png",
                        lambda p, i, out, **k: Path(out).joinpath(f"page-{i+1}.png"))
    fake_ocr = lambda img, **k: [V.VBlock("块一", 1.0, (0, 0, 1, 1)),
                                  V.VBlock("块二", 1.0, (0, 0, 1, 1))]

    result = RV.build_review(pdf, deep=False, ocr_fn=fake_ocr)
    packet = yaml.safe_load(Path(result["packet_path"]).read_text(encoding="utf-8"))
    entry = next(p for p in packet["suspect_pages"] if p["page"] == 2)
    assert entry["vision_text"] == "块一\n块二"


def test_build_review_catches_swallowed_page(tmp_path, monkeypatch):
    # 3 页：第1、2页正常长文本，第3页 MinerU 空（被整页吞掉）；Vision 在第3页读出长条款 → 该页应入包
    pdf = tmp_path / "证据.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    md = tmp_path / "_md" / "证据.md"
    md.parent.mkdir(parents=True)
    md.write_text(
        "---\nsource: ../证据.pdf\npages: 3\n---\n\n"
        "<!-- PAGE 1 -->\n\n本院认为，被告应支付货款二十八万元整，付款期限十日整。\n\n"
        "<!-- PAGE 2 -->\n\n原告主张被告未按期支付货款，要求被告承担违约责任并支付利息。\n\n"
        "<!-- PAGE 3 -->\n\n",
        encoding="utf-8")

    import pdf_render
    monkeypatch.setattr(pdf_render, "render_page_ndarray", lambda p, idx: idx)
    monkeypatch.setattr(pdf_render, "render_page_png",
                        lambda p, i, out, **k: Path(out).joinpath(f"page-{i+1}.png"))
    ocr_table = {
        0: [V.VBlock("本院认为，被告应支付货款二十八万元整，付款期限十日整。", 1.0, (0, 0, 1, 1))],
        1: [V.VBlock("原告主张被告未按期支付货款，要求被告承担违约责任并支付利息。", 1.0, (0, 0, 1, 1))],
        2: [V.VBlock("第十四条乙方向甲方采购产品应当提前十五个工作日通知违约金按合同总额百分之十计算。", 1.0, (0, 0, 1, 1))],
    }
    fake_ocr = lambda img, **k: ocr_table[img]

    result = RV.build_review(pdf, deep=False, ocr_fn=fake_ocr)
    assert 3 in result["suspect_pages"]
    packet = yaml.safe_load(Path(result["packet_path"]).read_text(encoding="utf-8"))
    entry = next(p for p in packet["suspect_pages"] if p["page"] == 3)
    assert entry["reason"]["heuristic"] is False
    assert entry["reason"]["empty_mineru"] is True


def test_build_review_blank_page_stays_out(tmp_path, monkeypatch):
    # 同上但第3页 Vision 也读不到内容（真版式空页）→ 不入包，零误报锁
    pdf = tmp_path / "证据.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    md = tmp_path / "_md" / "证据.md"
    md.parent.mkdir(parents=True)
    md.write_text(
        "---\nsource: ../证据.pdf\npages: 3\n---\n\n"
        "<!-- PAGE 1 -->\n\n本院认为，被告应支付货款二十八万元整，付款期限十日整。\n\n"
        "<!-- PAGE 2 -->\n\n原告主张被告未按期支付货款，要求被告承担违约责任并支付利息。\n\n"
        "<!-- PAGE 3 -->\n\n",
        encoding="utf-8")

    import pdf_render
    monkeypatch.setattr(pdf_render, "render_page_ndarray", lambda p, idx: idx)
    monkeypatch.setattr(pdf_render, "render_page_png",
                        lambda p, i, out, **k: Path(out).joinpath(f"page-{i+1}.png"))
    ocr_table = {
        0: [V.VBlock("本院认为，被告应支付货款二十八万元整，付款期限十日整。", 1.0, (0, 0, 1, 1))],
        1: [V.VBlock("原告主张被告未按期支付货款，要求被告承担违约责任并支付利息。", 1.0, (0, 0, 1, 1))],
        2: [],
    }
    fake_ocr = lambda img, **k: ocr_table[img]

    result = RV.build_review(pdf, deep=False, ocr_fn=fake_ocr)
    assert 3 not in result["suspect_pages"]


# ── D3：复核侧独立守卫——MD 缺尾页标记（防御纵深，兜存量旧 MD）───────────────
def test_review_flags_missing_tail_page(tmp_path, monkeypatch):
    # front-matter 声称 2 页，但源 PDF 真实 3 页（monkeypatch page_count）→
    # Vision 在第 3 页读到长文本 → 应入包，missing_marker=True，missing_pages==[3]
    pdf = tmp_path / "证据.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    md = tmp_path / "_md" / "证据.md"
    md.parent.mkdir(parents=True)
    md.write_text(
        "---\nsource: ../证据.pdf\npages: 2\n---\n\n"
        "<!-- PAGE 1 -->\n\n本院认为，被告应支付货款二十八万元整，付款期限十日整。\n\n"
        "<!-- PAGE 2 -->\n\n原告主张被告未按期支付货款，要求被告承担违约责任并支付利息。\n",
        encoding="utf-8")

    import pdf_render
    monkeypatch.setattr(pdf_render, "page_count", lambda p: 3)
    monkeypatch.setattr(pdf_render, "render_page_ndarray", lambda p, idx: idx)
    monkeypatch.setattr(pdf_render, "render_page_png",
                        lambda p, i, out, **k: Path(out).joinpath(f"page-{i+1}.png"))
    long_text = "第十四条乙方向甲方采购产品应当提前十五个工作日通知违约金按合同总额百分之十计算。"
    ocr_table = {2: [V.VBlock(long_text, 1.0, (0, 0, 1, 1))]}   # idx0=2 → 第3页
    fake_ocr = lambda img, **k: ocr_table.get(img, [])

    result = RV.build_review(pdf, deep=False, ocr_fn=fake_ocr)
    assert result["missing_pages"] == [3]
    assert 3 in result["suspect_pages"]
    packet = yaml.safe_load(Path(result["packet_path"]).read_text(encoding="utf-8"))
    entry = next(p for p in packet["suspect_pages"] if p["page"] == 3)
    assert entry["missing_marker"] is True
    assert entry["mineru_text"] == ""


def test_apply_review_writes_back(tmp_path):
    pdf = tmp_path / "证据.pdf"; pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    md = tmp_path / "_md" / "证据.md"; md.parent.mkdir(parents=True)
    md.write_text("---\nsource: ../证据.pdf\npages: 1\n---\n\n"
                  "<!-- PAGE 1 -->\n\n原告玉某某。\n", encoding="utf-8")
    corr = tmp_path / "corr.yaml"
    corr.write_text(
        "coverage: deep\ncorrections:\n"
        "  - page: 1\n    corrected_text: 原告王某某。\n"
        "    changes:\n      - old: 玉\n        new: 王\n        basis: 原件 p.1\n",
        encoding="utf-8")
    rc = cc_convert.main([str(pdf), "--apply-review", str(corr)])
    assert rc == 0
    out = md.read_text(encoding="utf-8")
    assert "原告王某某。" in out and "reviewed: " in out and "## 复核溯源" in out


def test_force_refuses_to_clobber_reviewed(tmp_path, capsys):
    pdf = tmp_path / "证据.pdf"; pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    md = tmp_path / "_md" / "证据.md"; md.parent.mkdir(parents=True)
    md.write_text("---\nsource: ../证据.pdf\npages: 1\nreviewed: 2026-07-01\n"
                  "corrections: 1\n---\n\n<!-- PAGE 1 -->\n\n原告王某某。\n", encoding="utf-8")
    rc = cc_convert.main([str(pdf), "--force"])       # 无 --force-drop-corrections
    assert rc == 0                                     # 跳过该文件、正常收尾
    assert "已复核" in capsys.readouterr().out         # 响亮拒绝、未重转
    assert "reviewed: 2026-07-01" in md.read_text(encoding="utf-8")   # 修正没被清掉


def test_apply_review_requires_single_pdf(tmp_path):
    # 目录含多个 PDF → 拒绝，防某文书的按页修正被误写进其它 PDF
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n%stub\n")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4\n%stub\n")
    corr = tmp_path / "corr.yaml"
    corr.write_text("coverage: deep\ncorrections: []\n", encoding="utf-8")
    rc = cc_convert.main([str(tmp_path), "--apply-review", str(corr)])
    assert rc == 2


def test_apply_review_missing_yaml(tmp_path):
    # 修正清单缺失 → 干净报错、返回码 2，不吐 traceback
    pdf = tmp_path / "证据.pdf"; pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    rc = cc_convert.main([str(pdf), "--apply-review", str(tmp_path / "nope.yaml")])
    assert rc == 2


def test_apply_review_warns_empty_changes(tmp_path, capsys):
    # 改写却未列 changes → 溯源表缺该页审计行，应提示（不阻断）
    pdf = tmp_path / "证据.pdf"; pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    md = tmp_path / "_md" / "证据.md"; md.parent.mkdir(parents=True)
    md.write_text("---\nsource: ../证据.pdf\npages: 1\n---\n\n"
                  "<!-- PAGE 1 -->\n\n原告玉某某。\n", encoding="utf-8")
    corr = tmp_path / "corr.yaml"
    corr.write_text("coverage: deep\ncorrections:\n"
                    "  - page: 1\n    corrected_text: 原告王某某。\n    changes: []\n",
                    encoding="utf-8")
    rc = cc_convert.main([str(pdf), "--apply-review", str(corr)])
    assert rc == 0
    assert "溯源表将缺该页审计行" in capsys.readouterr().err


def test_heuristic_page_vision_agrees_marked(tmp_path, monkeypatch):
    # 启发式误报形态（证书/编号页孤立单字比高）但 Vision 交叉逐字一致 → 标 vision_agrees
    pdf = tmp_path / "证据.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    md = tmp_path / "_md" / "证据.md"
    md.parent.mkdir(parents=True)
    garbled_layout = "证 书 编 号 甲 乙 丙 丁 戊 己 庚 辛 壬 癸 子 丑 寅 卯 辰 巳"
    md.write_text(
        "---\nsource: ../证据.pdf\npages: 1\n---\n\n"
        f"<!-- PAGE 1 -->\n\n{garbled_layout}\n\n",
        encoding="utf-8")
    import pdf_render
    monkeypatch.setattr(pdf_render, "render_page_ndarray", lambda p, idx: idx)
    monkeypatch.setattr(pdf_render, "render_page_png",
                        lambda p, i, out, **k: Path(out).joinpath(f"page-{i+1}.png"))
    # Vision 读出同样的字（无空格排版差异经 CJK 提取后一致）→ sim 1.0
    fake_ocr = lambda img, **k: [V.VBlock(garbled_layout.replace(" ", ""), 1.0, (0, 0, 1, 1))]

    result = RV.build_review(pdf, deep=False, ocr_fn=fake_ocr)
    assert result["suspect_pages"] == [1]            # 启发式页仍入包（保守不删）
    assert result["vision_agrees_pages"] == [1]
    packet = yaml.safe_load(Path(result["packet_path"]).read_text(encoding="utf-8"))
    assert packet["suspect_pages"][0]["reason"]["vision_agrees"] is True


def test_heuristic_page_vision_differs_not_marked(tmp_path, monkeypatch):
    # 启发式存疑且 Vision 读出明显不同（中度乱码 sim<0.95）→ 不标 vision_agrees
    pdf = tmp_path / "证据.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    md = tmp_path / "_md" / "证据.md"
    md.parent.mkdir(parents=True)
    md.write_text(
        "---\nsource: ../证据.pdf\npages: 1\n---\n\n"
        "<!-- PAGE 1 -->\n\n本 院 认 为 兹 因 甲 乙 丙 丁 戊 己 庚 辛 壬 癸 子 丑 寅 卯\n\n",
        encoding="utf-8")
    import pdf_render
    monkeypatch.setattr(pdf_render, "render_page_ndarray", lambda p, idx: idx)
    monkeypatch.setattr(pdf_render, "render_page_png",
                        lambda p, i, out, **k: Path(out).joinpath(f"page-{i+1}.png"))
    fake_ocr = lambda img, **k: [V.VBlock("本院认为被告应当支付原告货款并且承担相应违约责任", 1.0, (0, 0, 1, 1))]

    result = RV.build_review(pdf, deep=False, ocr_fn=fake_ocr)
    assert result["suspect_pages"] == [1]
    assert result["vision_agrees_pages"] == []
    packet = yaml.safe_load(Path(result["packet_path"]).read_text(encoding="utf-8"))
    assert "vision_agrees" not in packet["suspect_pages"][0]["reason"]
