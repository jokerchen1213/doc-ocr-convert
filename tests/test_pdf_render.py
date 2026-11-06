import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pdf_render as R

# 最小合法单页空白 PDF（612x792），用于渲染测试
_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000052 00000 n \n0000000101 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n164\n%%EOF\n"
)


def _fixture_pdf(tmp_path):
    p = tmp_path / "blank.pdf"
    p.write_bytes(_MINIMAL_PDF)
    return p


def test_render_ndarray_shape(tmp_path):
    arr = R.render_page_ndarray(_fixture_pdf(tmp_path), 0, scale=1.0)
    assert arr.ndim == 3 and arr.shape[2] == 3          # BGR
    assert arr.shape[0] > 100 and arr.shape[1] > 100    # 有实际像素


def test_render_png_written(tmp_path):
    out = tmp_path / "out"
    png = R.render_page_png(_fixture_pdf(tmp_path), 0, out, scale=1.0)
    assert png.exists() and png.suffix == ".png" and png.stat().st_size > 0


def test_page_count_minimal(tmp_path):
    assert R.page_count(_fixture_pdf(tmp_path)) == 1


def test_page_count_garbage_none(tmp_path):
    p = tmp_path / "garbage.pdf"
    p.write_bytes(b"not a pdf at all, just garbage bytes")
    assert R.page_count(p) is None


def test_classify_text_layer():
    assert R.classify_text_layer([0, 0]) == "none"
    assert R.classify_text_layer([500, 600]) == "full"
    assert R.classify_text_layer([500, 0]) == "partial"
    assert R.classify_text_layer([]) == "none"


def test_detect_text_layer_blank_none(tmp_path):
    # 既有 _MINIMAL_PDF fixture（无文本内容流）→ "none"
    assert R.detect_text_layer(_fixture_pdf(tmp_path)) == "none"


def _text_fixture_pdf(tmp_path):
    # reportlab 已在 venv 中（MinerU 依赖链带入），用其生成含真实文本流的单页 PDF，
    # 比手写 xref 更稳健、可读；DrawString 一行英文超过 30 字符触发 "full"。
    from reportlab.pdfgen import canvas
    p = tmp_path / "with_text.pdf"
    c = canvas.Canvas(str(p), pagesize=(612, 792))
    c.drawString(72, 700, "a long enough english text line over thirty chars for sure")
    c.showPage()
    c.save()
    return p


def test_detect_text_layer_text_full(tmp_path):
    assert R.detect_text_layer(_text_fixture_pdf(tmp_path)) == "full"


def test_detect_text_layer_garbage_none(tmp_path):
    p = tmp_path / "garbage.pdf"
    p.write_bytes(b"not a pdf at all, just garbage bytes")
    assert R.detect_text_layer(p) is None
