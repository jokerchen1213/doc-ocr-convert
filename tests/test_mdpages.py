import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mdpages as M

_MD = """---
source: ../x.pdf
pages: 2
---

<!-- PAGE 1 -->

第一页正文。

<!-- PAGE 2 -->

第二页正文。
"""


def test_split_pages():
    pages = M.split_pages(_MD)
    assert [n for n, _ in pages] == [1, 2]
    assert "第一页正文。" in pages[0][1] and "第二页正文。" in pages[1][1]
    assert "<!-- PAGE" not in pages[0][1]


def test_parse_front_matter():
    fm = M.parse_front_matter(_MD)
    assert fm["source"] == "../x.pdf" and fm["pages"] == "2"


def test_parse_front_matter_absent():
    assert M.parse_front_matter("no front matter here") == {}


def test_inline_page_marker_not_split():
    # 页正文里 inline 的 <!-- PAGE 9 --> 不得被切成幻影页（只认独占一行的标记）
    md = ("<!-- PAGE 1 -->\n\n脚注参见 <!-- PAGE 9 --> 后文。\n\n"
          "<!-- PAGE 2 -->\n\n第二页。\n")
    pages = M.split_pages(md)
    assert [n for n, _ in pages] == [1, 2]        # 无幻影页 9
    assert "PAGE 9" in pages[0][1]                 # inline 标记留在正文里


def test_md_for_plain(tmp_path):
    src = tmp_path / "foo.pdf"; src.write_bytes(b"%PDF")
    mddir = tmp_path / "_md"; mddir.mkdir()
    (mddir / "foo.md").write_text("---\nsource: ../foo.pdf\n---\n\n正文\n", encoding="utf-8")
    assert M.md_for(src) == mddir / "foo.md"


def test_md_for_disambiguated(tmp_path):
    (tmp_path / "foo.pdf").write_bytes(b"%PDF")
    (tmp_path / "foo.docx").write_bytes(b"PK")
    mddir = tmp_path / "_md"; mddir.mkdir()
    (mddir / "foo.md").write_text("---\nsource: ../foo.docx\n---\n\n正文\n", encoding="utf-8")
    (mddir / "foo.pdf.md").write_text("---\nsource: ../foo.pdf\n---\n\n正文\n", encoding="utf-8")
    assert M.md_for(tmp_path / "foo.pdf") == mddir / "foo.pdf.md"
    assert M.md_for(tmp_path / "foo.docx") == mddir / "foo.md"


def test_md_for_none(tmp_path):
    src = tmp_path / "foo.pdf"; src.write_bytes(b"%PDF")
    mddir = tmp_path / "_md"; mddir.mkdir()
    (mddir / "foo.md").write_text("---\nsource: ../other.pdf\n---\n\n正文\n", encoding="utf-8")
    assert M.md_for(src) is None
