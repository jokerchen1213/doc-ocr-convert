import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import vision_ocr as V


def test_norm_bbox_topleft():
    # ocrmac bbox = (x, y, w, h)，左下原点、归一化；转像素左上原点
    x1, y1, x2, y2 = V.norm_bbox_to_pixel((0.0, 0.0, 0.5, 0.5), 100, 200)
    assert (x1, x2) == (0.0, 50.0)
    assert (y1, y2) == (100.0, 200.0)   # 底部半幅 → 像素 100..200


def test_blocks_from_ocrmac_sorted_and_typed():
    res = [
        ("下面", 1.0, (0.0, 0.0, 0.3, 0.1)),   # 靠下
        ("上面", 0.5, (0.0, 0.8, 0.3, 0.1)),   # 靠上
    ]
    blocks = V.blocks_from_ocrmac(res, 100, 100)
    assert [b.text for b in blocks] == ["上面", "下面"]   # 按 y 升序（上在前）
    assert blocks[0].confidence == 0.5
    assert isinstance(blocks[0].bbox, tuple) and len(blocks[0].bbox) == 4
