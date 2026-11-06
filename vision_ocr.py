"""vision_ocr.py — Apple Vision OCR 适配器（经 ocrmac）。本地、免费、秒级、带置信度。
仅用于复核层的"第二意见"，绝不替换 MinerU 主引擎。
移植自 chat-video-forensics/ocr_adapters/vision.py，去掉对该工具专属 models 的依赖。"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class VBlock:
    text: str
    confidence: float
    bbox: tuple[float, float, float, float]  # (x1, y1, x2, y2) 像素、左上原点


def norm_bbox_to_pixel(bbox, width, height):
    """ocrmac 归一化 bbox (x, y, w, h, 左下原点) → 像素 [x1,y1,x2,y2] 左上原点。"""
    x, y, w, h = bbox
    x1 = x * width
    x2 = (x + w) * width
    y1 = (1.0 - (y + h)) * height   # 顶边
    y2 = (1.0 - y) * height          # 底边
    return (round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1))


def blocks_from_ocrmac(res, width, height):
    """ocrmac 原始返回 [(text, conf, bbox_归一化)] → 排序后的 VBlock 列表（按 y 再 x 升序）。"""
    blocks = [
        VBlock(text=t, confidence=float(c), bbox=norm_bbox_to_pixel(b, width, height))
        for t, c, b in res
    ]
    blocks.sort(key=lambda b: (b.bbox[1], b.bbox[0]))
    return blocks


def ocr_image(image, languages=None, target_min_width=900):
    """单图（BGR ndarray 或路径）→ list[VBlock]。小图自适应上采样，改善小字识别。"""
    import cv2
    from ocrmac import ocrmac
    from PIL import Image

    if isinstance(image, (str,)) or hasattr(image, "__fspath__"):
        image = cv2.imread(str(image))
    if image is None:
        return []
    langs = languages or ["zh-Hans", "zh-Hant", "en-US"]
    h, w = image.shape[:2]
    scale = max(1.0, target_min_width / w) if w else 1.0
    proc = cv2.resize(image, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_CUBIC) if scale != 1.0 else image
    rgb = cv2.cvtColor(proc, cv2.COLOR_BGR2RGB)
    res = ocrmac.OCR(Image.fromarray(rgb), language_preference=langs).recognize()
    return blocks_from_ocrmac(res, w, h)   # 归一化与缩放无关，用原始 w,h
