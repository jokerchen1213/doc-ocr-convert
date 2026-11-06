"""index.py — 维护每目录 _转换索引.md（原件名为主键，幂等 upsert）。"""
from pathlib import Path
COLUMNS = ["原件", "页数", "MD", "状态", "引擎", "日期"]
HEADER = "| " + " | ".join(COLUMNS) + " |"
SEP = "|" + "|".join(["---"] * len(COLUMNS)) + "|"

def _parse(text):
    rows = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| 原件"):
            continue
        if set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) == len(COLUMNS) and cells[0]:
            rows[cells[0]] = cells
    return rows

def upsert(index_path, original, pages, md_rel, status, engine, date):
    p = Path(index_path)
    rows = _parse(p.read_text(encoding="utf-8")) if p.exists() else {}
    rows[original] = [original, str(pages), md_rel, status, engine, date]
    lines = [HEADER, SEP] + ["| " + " | ".join(c) + " |" for c in rows.values()]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
