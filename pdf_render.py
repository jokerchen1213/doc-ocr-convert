"""pdf_render.py — 用 pypdfium2 把源 PDF 某页渲染成图（复核层用）。
只读源 PDF，不改原件。pypdfium2 是 MinerU 自带渲染器，venv 已有。"""
from __future__ import annotations
from pathlib import Path


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".jp2"}


def is_image(path):
    """源件是否为图片（2026-09-18 起复核层也接图片证据：jpg/png 截图、拍照）。"""
    return Path(path).suffix.lower() in _IMAGE_EXTS


def _render_pil(pdf_path, page_idx0, scale):
    if is_image(pdf_path):                       # 图片源：只有第 0 页，直接读图（scale 对图片无意义）
        from PIL import Image, ImageOps
        if page_idx0 != 0:
            raise IndexError(f"图片源只有 1 页，请求页 {page_idx0}")
        with Image.open(str(pdf_path)) as im:
            return ImageOps.exif_transpose(im).convert("RGB")
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_idx0]
        bitmap = page.render(scale=scale)   # scale=1 ≈ 72dpi；2 ≈ 144dpi
        return bitmap.to_pil().convert("RGB")
    finally:
        pdf.close()


def render_page_ndarray(pdf_path, page_idx0, scale=2.0):
    """源 PDF 第 page_idx0 页(0-based) → BGR ndarray（供 cv2/Vision）。"""
    import numpy as np
    import cv2
    pil = _render_pil(pdf_path, page_idx0, scale)
    return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


def render_page_png(pdf_path, page_idx0, out_dir, scale=2.0):
    """渲染并落盘为 PNG，返回路径。文件名 page-<1based>.png。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pil = _render_pil(pdf_path, page_idx0, scale)
    png = out_dir / f"page-{page_idx0 + 1}.png"
    pil.save(png)
    return png


def page_count(pdf_path):
    """源 PDF 真实页数（守恒守卫用）。打不开（加密/损坏/非 PDF）一律 fail-soft
    返回 None——守卫本身绝不能让转换/复核流程失败。图片源恒 1。"""
    if is_image(pdf_path):
        return 1
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception:
        return None
    try:
        return len(pdf)
    except Exception:
        return None
    finally:
        pdf.close()


_TEXT_LAYER_MIN_CHARS = 30   # 抽样页可抽出字符 ≥ 此 → 该页有文本层
                             # （正文页数百字符起；扫描页 0 或仅水印/页码注入的个位-十几字符）


def classify_text_layer(char_counts):
    """纯函数：抽样页字符数列表 → 'none' | 'partial' | 'full'。
    全部 ≥ 阈值 → full；全部 < 阈值 → none；混合 → partial；空列表 → 'none'。"""
    if not char_counts:
        return "none"
    has = [c >= _TEXT_LAYER_MIN_CHARS for c in char_counts]
    if all(has):
        return "full"
    if not any(has):
        return "none"
    return "partial"


def detect_text_layer(pdf_path, sample=8):
    """pypdfium2 抽样判定源 PDF 文本层：抽首页、末页 + 均匀间隔页（最多 sample 页），
    每页 page.get_textpage().count_chars() 计字符数 → classify_text_layer。
    打不开/任何异常返回 None（fail-soft，与 page_count 同纪律）。图片源恒 'none'。"""
    if is_image(pdf_path):
        return "none"
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception:
        return None
    try:
        n = len(pdf)
        if n <= 0:
            return None
        if n <= sample:
            idxs = list(range(n))
        else:
            idxs = sorted({round(i * (n - 1) / (sample - 1)) for i in range(sample)})
        char_counts = []
        for i in idxs:
            page = pdf[i]
            try:
                textpage = page.get_textpage()
                try:
                    char_counts.append(textpage.count_chars())
                finally:
                    textpage.close()
            finally:
                page.close()
        return classify_text_layer(char_counts)
    except Exception:
        return None
    finally:
        pdf.close()
