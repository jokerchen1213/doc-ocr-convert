import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import index

def test_create_contains_row(tmp_path):
    idx = tmp_path / "_转换索引.md"
    index.upsert(idx, "民事判决书.pdf", "61", "_md/民事判决书.md", "✅已转", "MinerU 3.2.x", "2026-06-03")
    t = idx.read_text(encoding="utf-8")
    assert "| 原件 |" in t and "民事判决书.pdf" in t and "61" in t

def test_office_row_sections(tmp_path):     # Office 页数列填 —（N节）
    idx = tmp_path / "_转换索引.md"
    index.upsert(idx, "服务合同.docx", "—（12节）", "_md/服务合同.md", "✅已转", "native-docx", "2026-06-03")
    assert "—（12节）" in idx.read_text(encoding="utf-8")

def test_upsert_no_duplicate(tmp_path):
    idx = tmp_path / "_转换索引.md"
    index.upsert(idx, "a.pdf", "0", "—", "⬜未转", "—", "—")
    index.upsert(idx, "a.pdf", "61", "_md/a.md", "✅已转", "MinerU 3.2.x", "2026-06-03")
    t = idx.read_text(encoding="utf-8")
    assert t.count("a.pdf") == 1 and "✅已转" in t and "⬜未转" not in t
