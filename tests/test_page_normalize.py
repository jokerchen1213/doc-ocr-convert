"""超大页面归一化：PDF 标注页面尺寸远超真实开本时（扫描软件记错 DPI，如《公司法司法解释（四）理解与适用》
标成 32.6×47 cm），MinerU 按 200 dpi 渲染出 4 倍像素，VLM 慢 4.6 倍（26 秒/页）且不增信息。
归一化只改页面放置比例（整页作 XObject 等比缩放），不重采样扫描图；正常开本原样放行。"""
import pytest
import engine

pdfium = pytest.importorskip("pypdfium2")


def _make_pdf(path, sizes):
    doc = pdfium.PdfDocument.new()
    for w, h in sizes:
        doc.new_page(w, h)
    doc.save(str(path))
    return path


def test_normal_book_passes_through_untouched(tmp_path):
    src = _make_pdf(tmp_path / "正常开本.pdf", [(470, 680), (470, 680)])   # ≈16.6×24 cm
    assert engine.normalize_oversized_pdf(src, tmp_path / "work") == src


def test_a4_is_not_touched(tmp_path):
    src = _make_pdf(tmp_path / "A4.pdf", [(595, 842)])
    assert engine.normalize_oversized_pdf(src, tmp_path / "work") == src


def test_oversized_pages_scaled_to_book_width(tmp_path):
    src = _make_pdf(tmp_path / "超大（四）.pdf", [(924, 1332), (924, 1332), (470, 680)])   # 32.6×47 cm + 1 正常页
    out = engine.normalize_oversized_pdf(src, tmp_path / "work")
    assert out != src and out.name == src.name          # 同名：下游产物命名仍取原书名
    doc = pdfium.PdfDocument(str(out))
    assert len(doc) == 3
    w0, h0 = doc[0].get_size()
    assert abs(w0 - engine.NORMALIZED_PAGE_WIDTH_PT) < 1
    assert abs(h0 / w0 - 1332 / 924) < 0.01               # 等比
    w2, _ = doc[2].get_size()
    assert abs(w2 - 470) < 1                               # 正常页不动
    assert src.exists()                                    # 原件不动


def test_normalized_page_keeps_rendered_content(tmp_path):
    """缩放后的页渲染出来不是白页（XObject 内容确实带过去了）。"""
    from PIL import Image
    doc = pdfium.PdfDocument.new()
    page = doc.new_page(924, 1332)
    img = pdfium.PdfImage.new(doc)                      # 扫描件的形态：一页一张整页图
    img.set_bitmap(pdfium.PdfBitmap.from_pil(Image.new("RGB", (400, 600), "black")))
    img.set_matrix(pdfium.PdfMatrix().scale(500, 800).translate(100, 100))
    page.insert_obj(img)
    page.gen_content()
    src = tmp_path / "有内容.pdf"
    doc.save(str(src))
    out = engine.normalize_oversized_pdf(src, tmp_path / "work")
    img = pdfium.PdfDocument(str(out))[0].render(scale=0.2).to_pil().convert("L")
    dark = sum(img.histogram()[:128]) / (img.width * img.height)
    assert dark > 0.1
