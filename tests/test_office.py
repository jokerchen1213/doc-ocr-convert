import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import convert_office
from docx import Document

def _make_docx(p):
    d = Document()
    d.add_heading("第一章 总则", level=1)
    d.add_paragraph("本合同正文第一段。")
    d.add_heading("第二章 价款", level=1)
    d.add_paragraph("价款为人民币壹万元。")
    d.save(p)

def test_docx_headings_and_anchors(tmp_path):
    src = tmp_path / "服务合同.docx"; _make_docx(src)
    md = convert_office.to_markdown(src, source_rel="../服务合同.docx",
                                    engine="native-docx", date="2026-06-03")
    assert md.startswith("---\n") and "engine: native-docx" in md and "sections:" in md
    assert "# 第一章 总则" in md and "## 第二章 价款" not in md  # level1→#
    assert "<!-- SEC: 第一章 总则 -->" in md and "<!-- SEC: 第二章 价款 -->" in md
    assert "本合同正文第一段。" in md and "壹万元" in md
    assert "<!-- PAGE" not in md   # Office 无固定页码

def test_pages_field_is_sections(tmp_path):
    src = tmp_path / "x.docx"; _make_docx(src)
    md = convert_office.to_markdown(src, "../x.docx", "native-docx", "2026-06-03")
    assert "sections: 2" in md
