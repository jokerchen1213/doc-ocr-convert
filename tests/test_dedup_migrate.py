import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cc_convert as cc

def test_gate3_detects_mineru_uuid(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "a.pdf-12345678-1234-1234-1234-123456789abc").mkdir()
    assert cc.converted_state(tmp_path / "a.pdf") == "legacy"   # 不再误判 new

def test_origin_unique_copy_kept(tmp_path):
    # 原件不在别处 → origin.pdf 判为疑似唯一副本，保留
    uuid = tmp_path / "b.pdf-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"; uuid.mkdir()
    (uuid / "x_origin.pdf").write_bytes(b"%PDF")
    plan = cc.purge_plan(uuid, case_root=tmp_path)
    assert any(p["keep"] and "origin" in p["file"] for p in plan)

def test_migrate_idempotent(tmp_path):
    src = tmp_path / "c.pdf"; src.write_bytes(b"%PDF")
    j = tmp_path / "c.pdf_by_PaddleOCR-VL-1.5.json"
    j.write_text(json.dumps([{"markdown": {"text": "# T\n页一"}}]), encoding="utf-8")
    cc.migrate_one(src)
    cc.migrate_one(src)                                          # 再跑不重复
    md = tmp_path / "_md" / "c.md"
    assert md.exists() and "<!-- PAGE 1 -->" in md.read_text(encoding="utf-8")


def test_converted_state_done_for_disambiguated(tmp_path):
    # 跨格式重名：foo.docx 后转被消歧为 _md/foo.docx.md；此前硬编码路径会误判为 "new"
    (tmp_path / "foo.pdf").write_bytes(b"%PDF")
    (tmp_path / "foo.docx").write_bytes(b"PK")
    mddir = tmp_path / "_md"; mddir.mkdir()
    (mddir / "foo.md").write_text("---\nsource: ../foo.pdf\n---\n\n正文\n", encoding="utf-8")
    (mddir / "foo.docx.md").write_text("---\nsource: ../foo.docx\n---\n\n正文\n", encoding="utf-8")
    assert cc.converted_state(tmp_path / "foo.docx") == "done"
