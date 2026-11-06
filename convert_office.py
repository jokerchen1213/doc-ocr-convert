"""convert_office.py — Office 文档原生解析 → 章节锚点 Markdown（不 OCR、无固定页码）。"""
from pathlib import Path

def _front_matter(source_rel, sections, engine, date):
    return (f"---\nsource: {source_rel}\nsections: {sections}\n"
            f"engine: {engine}\nconverted: {date}\n---")

def _docx_md(src):
    from docx import Document
    doc = Document(str(src))
    parts, sections = [], 0
    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        style = (para.style.name or "") if para.style else ""
        if style.startswith("Heading"):
            try:
                lvl = int(style.split()[-1])
            except ValueError:
                lvl = 1
            sections += 1
            parts.append(f"<!-- SEC: {text} -->")
            parts.append("#" * max(1, lvl) + " " + text)
        else:
            parts.append(text)
    for tbl in doc.tables:                       # 表格 → markdown
        rows = [[c.text.strip() for c in r.cells] for r in tbl.rows]
        if rows:
            parts.append("| " + " | ".join(rows[0]) + " |")
            parts.append("|" + "|".join(["---"] * len(rows[0])) + "|")
            for r in rows[1:]:
                parts.append("| " + " | ".join(r) + " |")
    return "\n\n".join(parts), sections

def _xlsx_md(src):
    from openpyxl import load_workbook
    wb = load_workbook(str(src), read_only=True, data_only=True)
    parts, sections = [], 0
    for ws in wb.worksheets:
        sections += 1
        parts.append(f"<!-- SEC: {ws.title} -->")
        parts.append(f"## {ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(cells):
                parts.append("| " + " | ".join(cells) + " |")
    return "\n\n".join(parts), sections

def _pptx_md(src):
    from pptx import Presentation
    prs = Presentation(str(src))
    parts, sections = [], 0
    for i, slide in enumerate(prs.slides, 1):
        sections += 1
        parts.append(f"<!-- SEC: Slide {i} -->")
        parts.append(f"## Slide {i}")
        for shp in slide.shapes:
            if shp.has_text_frame and shp.text_frame.text.strip():
                parts.append(shp.text_frame.text.strip())
    return "\n\n".join(parts), sections

def to_markdown(src, source_rel, engine, date):
    src = Path(src)
    ext = src.suffix.lower()
    if ext in (".docx", ".doc"):
        body, n = _docx_md(src)
    elif ext in (".xlsx", ".xls"):
        body, n = _xlsx_md(src)
    elif ext in (".pptx", ".ppt"):
        body, n = _pptx_md(src)
    else:
        raise ValueError(f"非 Office 格式：{ext}")
    return _front_matter(source_rel, n, engine, date) + "\n" + body.rstrip() + "\n"
