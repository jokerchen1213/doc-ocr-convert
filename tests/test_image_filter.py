import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cc_convert as cc


def test_strip_image_refs_removes_only_skipped():
    md = ("正文一\n![](x.assets/images/a.jpg)\n"
          "正文二 ![](x.assets/images/b.jpg) 尾\n![](x.assets/images/c.jpg)")
    out = cc._strip_image_refs(md, {"a.jpg", "c.jpg"})
    assert "a.jpg" not in out and "c.jpg" not in out      # 被跳过的删除
    assert "b.jpg" in out                                 # 未跳过的保留
    assert "正文一" in out and "正文二" in out and "尾" in out  # 正文不受损


def test_place_pdf_size_filter(tmp_path):
    src = tmp_path / "doc.pdf"; src.write_bytes(b"%PDF")
    out = tmp_path / "out"; (out / "images").mkdir(parents=True)
    page_json = out / "doc_content_list.json"; page_json.write_text("[]", encoding="utf-8")
    imgs = out / "images"
    (imgs / "icon.jpg").write_bytes(b"x" * 2000)         # 2KB 图标 → 跳过
    (imgs / "shot.jpg").write_bytes(b"x" * 50000)        # 50KB 截图 → 保留
    md_text = "页\n![](doc.assets/images/icon.jpg)\n![](doc.assets/images/shot.jpg)"
    md_path = cc.place_pdf(src, page_json, imgs, md_text, full_assets=False,
                           min_image_bytes=8 * 1024)
    kept = tmp_path / "_md" / "doc.assets" / "images"
    assert (kept / "shot.jpg").exists()                  # 大图保留
    assert not (kept / "icon.jpg").exists()              # 小图跳过
    txt = md_path.read_text(encoding="utf-8")
    assert "shot.jpg" in txt and "icon.jpg" not in txt   # 失效图链已清


def test_place_pdf_no_images(tmp_path):
    src = tmp_path / "d2.pdf"; src.write_bytes(b"%PDF")
    out = tmp_path / "o2"; (out / "images").mkdir(parents=True)
    pj = out / "d2_content_list.json"; pj.write_text("[]", encoding="utf-8")
    (out / "images" / "big.jpg").write_bytes(b"x" * 99999)
    md_text = "页\n![](d2.assets/images/big.jpg)"
    mp = cc.place_pdf(src, pj, out / "images", md_text, full_assets=False, no_images=True)
    img_dir = tmp_path / "_md" / "d2.assets" / "images"
    assert (not img_dir.exists()) or (not list(img_dir.iterdir()))  # 无图保留
    assert "big.jpg" not in mp.read_text(encoding="utf-8")


def test_full_assets_keeps_small_images(tmp_path):
    src = tmp_path / "d3.pdf"; src.write_bytes(b"%PDF")
    out = tmp_path / "o3"; (out / "images").mkdir(parents=True)
    pj = out / "d3_content_list.json"; pj.write_text("[]", encoding="utf-8")
    (out / "images" / "tiny.jpg").write_bytes(b"x" * 500)  # 0.5KB
    md_text = "页\n![](d3.assets/images/tiny.jpg)"
    # min_image_bytes=0 模拟 --full-assets 不过滤
    cc.place_pdf(src, pj, out / "images", md_text, full_assets=False, min_image_bytes=0)
    assert (tmp_path / "_md" / "d3.assets" / "images" / "tiny.jpg").exists()
