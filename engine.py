"""engine.py — MinerU 4.0 SDK 薄封装（cc_convert 主流程与 review 复核层共用）。

职责：① 保真守卫；② 一次解析落盘为 3.x 兼容的 content_list + images + middle_json；
③ 局部页重识别（复核层"第三意见"）；④ 从归档 middle_json 重渲染 DOCX / HTML / PDF 等。
本模块顶层只用 stdlib，MinerU 全部在函数内 lazy import（scan_pending.py 需 cc_convert 保持 stdlib-only）。

⚠️ docvortex 渲页用 ProcessPool(spawn)：任何调用本模块 parse 的入口必须是真实 .py + __main__ 守卫。"""
from __future__ import annotations
import json
import re
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".jp2"}
LEGACY_OFFICE_EXTS = {".doc", ".xls", ".ppt"}                # 二进制旧格式：python-docx 等打不开，走 4.0 原生解析
NATIVE_FLASH_EXTS = {".ofd", ".rtf", ".odt", ".ods", ".odp", ".epub", ".html", ".htm"} | LEGACY_OFFICE_EXTS
TIERED_EXTS = {".pdf"} | IMAGE_EXTS                           # 只有 PDF / 图片吃 tier；其余一律 flash
RENDER_FORMATS = ("docx", "html", "pdf", "epub", "latex")

# 1×1 透明 PNG：assets 里被 --min-image-kb 过滤掉的小图（图标/头像/水印）在重渲染时以此占位，
# 让 DOCX/PDF 渲染不因缺素材而整份失败；正文文字不受影响。
_PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def disable_upstream_text_normalization():
    """保真守卫（2026-09-18）：docvortex 0.4.12 在 PDF 模型输出上硬编码「全角英数 + 13 个符号
    （：．／＼－＿％＋＝＠＃＆＊）→ 半角」且无开关（docvortex/content/normalization.py），与本工具
    "逐字忠于源件"的定位冲突（法律文书引用要求原样，「原告：」不得变成「原告:」）。
    该模块各函数在调用时读模块全局 _FULLWIDTH_MODEL_TEXT 做前置判断，把它换成永不匹配的正则即
    整体短路为恒等，无论 MinerU 以何种方式 import 该函数都生效。docvortex 缺失或结构变动则静默跳过
    （届时输出可能被上游规范化——README 记有核验方法）。"""
    try:
        from docvortex.content import normalization as _n
        if hasattr(_n, "_FULLWIDTH_MODEL_TEXT"):
            _n._FULLWIDTH_MODEL_TEXT = re.compile(r"(?!)")
            return True
    except Exception:
        pass
    return False


def guard_truncated_utf8():
    """截断守卫（2026-09-30）：VLM 对个别文字块失控续写、撞上 n_predict 上限被截断时，截断点可能切在多字节
    汉字中间；mineru_llama_cpp 的 C 扩展把结果转 Python str 时严格解码，抛 UnicodeDecodeError，上游不接，
    整本书随之失败（《公司法司法解释（四）理解与适用》两次各 3.7 小时后在 p.471–480 同一处失败，10 页样本可复现）。
    守卫包住 Engine.generate：仅对 UnicodeDecodeError 返回空内容 + finish_reason="length"（上游按截断告警处理），
    只丢该文字块——失控续写的内容本就是复读垃圾——整本继续；该页随后由 --review 的 Vision 交叉标出漏字。
    其他异常原样抛出。幂等；mineru_llama_cpp 缺失或结构变动则静默跳过。"""
    try:
        from mineru_llama_cpp import engine as _e
        from mineru_llama_cpp.types import GenerateResult
    except Exception:
        return False
    orig = _e.Engine.generate
    if getattr(orig, "_cc_truncation_guard", False):
        return True

    def generate(self, messages, sampling_params=None):
        try:
            return orig(self, messages, sampling_params)
        except UnicodeDecodeError as err:
            import sys
            print(f"⚠️ [截断守卫] VLM 输出截断于多字节字符中间（{err.reason}），该文字块置空，整本继续",
                  file=sys.stderr)
            return GenerateResult(content="", finish_reason="length", tokens_evaluated=0,
                                  tokens_predicted=0, timings=None)

    generate._cc_truncation_guard = True
    _e.Engine.generate = generate
    return True


OVERSIZED_PAGE_WIDTH_PT = 624      # 22 cm：宽于此视为标注尺寸异常（A4 21 cm 不动）
NORMALIZED_PAGE_WIDTH_PT = 482     # 17 cm：常见法律图书开本宽度


def normalize_oversized_pdf(src, workdir):
    """超大页面归一化（2026-09-30）：扫描软件记错 DPI 时，PDF 标注页面尺寸远超真实开本（《公司法司法解释（四）
    理解与适用》标成 32.6×47 cm），MinerU 按固定 200 dpi 渲染 → 像素为正常书的 ~4 倍 → VLM 慢 4.6 倍（26 秒/页），
    低清扫描还会被无谓放大，均不增信息。宽于 22 cm 的页整页作 XObject 等比缩放到 17 cm 宽（不重采样扫描图，
    文本层随 XObject 保留），写到 workdir/<原文件名>；无超大页则原样返回 src。原件只读。"""
    src = Path(src)
    if src.suffix.lower() != ".pdf":
        return src
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(src))
    sizes = [doc[i].get_size() for i in range(len(doc))]
    if not any(w > OVERSIZED_PAGE_WIDTH_PT for w, _ in sizes):
        return src
    new = pdfium.PdfDocument.new()
    for i, (w, h) in enumerate(sizes):
        s = NORMALIZED_PAGE_WIDTH_PT / w if w > OVERSIZED_PAGE_WIDTH_PT else 1.0
        xo = doc.page_as_xobject(i, new)
        po = xo.as_pageobject()
        po.transform(pdfium.PdfMatrix().scale(s, s))
        page = new.new_page(w * s, h * s)
        page.insert_obj(po)
        page.gen_content()
    out = Path(workdir) / src.name
    out.parent.mkdir(parents=True, exist_ok=True)
    new.save(str(out))
    w0 = max(w for w, _ in sizes)
    print(f"📐 [页面归一化] 标注页宽 {w0 / 72 * 2.54:.1f} cm → {NORMALIZED_PAGE_WIDTH_PT / 72 * 2.54:.1f} cm"
          f"（{sum(w > OVERSIZED_PAGE_WIDTH_PT for w, _ in sizes)}/{len(sizes)} 页），扫描图不重采样")
    return out


def tier_for(src, tier: str) -> str:
    """PDF / 图片按用户 tier；Office / OFD / EPUB / HTML 等 4.0 只支持 flash（传高档位会被上游静默归一，
    这里显式归一以便 engine 标签如实记录）。"""
    return tier if Path(src).suffix.lower() in TIERED_EXTS else "flash"


def parse_to_dir(src, out, tier: str, page_range: str = ""):
    """解析 src → out/ 下落 markdown.md + middle_json.json（4.0 正典，可重渲染）+ structured_content.json
    + images/，再由落盘后的 middle_json 渲出扁平 Content List V1（<stem>_content_list.json，含 page_idx，
    3.x 形态，stamp.py 直接消费）。返回 (content_list_path, images_dir|None)。"""
    from mineru.parser import parse, ParseResult
    from mineru.parser.writer import FileBasedDataWriter
    from mineru.render import render, RenderFormat
    src, out = Path(src), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    disable_upstream_text_normalization()
    guard_truncated_utf8()
    parse_src = normalize_oversized_pdf(src, out.parent / "_normalized")   # 产物命名仍取原 src.stem
    result = parse(str(parse_src), tier=tier_for(src, tier), ocr_mode="auto", page_range=page_range)
    result.save(FileBasedDataWriter(str(out)))
    # 从落盘后的 middle_json 再渲染：图片引用已物化为 images/<name> 相对路径（内存态含 base64）
    materialized = ParseResult.from_json((out / "middle_json.json").read_text(encoding="utf-8"))
    content_list = render(materialized.middle_json, RenderFormat.CONTENT_LIST)
    cl_path = out / f"{src.stem}_content_list.json"
    cl_path.write_text(json.dumps(content_list, ensure_ascii=False, indent=1), encoding="utf-8")
    images = out / "images"
    return cl_path, (images if images.is_dir() else None)


def pages_text_from_content_list(content_list, assets_stem="_"):
    """纯函数：Content List V1 → {页码(1-based): 该页正文}（与 stamp 同一套块→文本规则）。
    page_range 局部解析时 page_idx 仍是源 PDF 的原始 0-based 序号，故页码可直接对齐 stamped MD。"""
    import stamp
    by = {}
    for b in content_list:
        if not isinstance(b, dict):
            continue
        t = stamp._block_text_inner(b, assets_stem)
        if t:
            by.setdefault(int(b.get("page_idx", 0)) + 1, []).append(t)
    return {p: "\n\n".join(v) for p, v in by.items()}


def parse_pages_text(src, pages, tier="advanced"):
    """复核层第三意见：只对指定页（1-based 列表）用高档位重识别，返回 {页码: 正文}。
    一次调用批量解析（page_range="7,9,11"），不逐页起模型。图片源只有一页，忽略 page_range。"""
    from mineru.parser import parse
    from mineru.render import render, RenderFormat
    src = Path(src)
    if not pages:
        return {}
    disable_upstream_text_normalization()
    guard_truncated_utf8()
    rng = "" if src.suffix.lower() in IMAGE_EXTS else ",".join(str(int(p)) for p in sorted(set(pages)))
    import tempfile
    with tempfile.TemporaryDirectory(prefix="cc-norm-") as td:
        result = parse(str(normalize_oversized_pdf(src, td)), tier=tier_for(src, tier), ocr_mode="auto",
                       page_range=rng)
    cl = render(result.middle_json, RenderFormat.CONTENT_LIST)
    return pages_text_from_content_list(cl)


def make_asset_resolver(images_dir):
    """images/<name> 相对路径 → 字节；缺文件（被小图过滤掉）回 1×1 透明 PNG 占位，不让整份渲染失败。"""
    images_dir = Path(images_dir) if images_dir else None

    def _resolve(rel):
        name = Path(str(rel)).name
        if images_dir is not None:
            f = images_dir / name
            if f.is_file():
                return f.read_bytes()
        return _PLACEHOLDER_PNG
    return _resolve


def render_from_middle(middle_json_path, fmt: str, images_dir=None, asset_base_url="", title=None):
    """从归档的 middle_json.json 重渲染。fmt ∈ RENDER_FORMATS；返回 bytes（docx/pdf/epub）或 str（html/latex）。
    不重跑 OCR——这是 4.0 正典中间表示的核心价值。"""
    from mineru.parser import ParseResult
    from mineru.render import render, RenderFormat
    from mineru.render.contracts import (DocxRenderOptions, HtmlRenderOptions, EpubRenderOptions,
                                         LatexRenderOptions)
    fmt = fmt.lower()
    if fmt not in RENDER_FORMATS:
        raise ValueError(f"不支持的重渲染格式 {fmt!r}，可选 {RENDER_FORMATS}")
    mj = ParseResult.from_json(Path(middle_json_path).read_text(encoding="utf-8")).middle_json
    resolver = make_asset_resolver(images_dir)
    if fmt == "docx":
        return render(mj, RenderFormat.DOCX, options=DocxRenderOptions(asset_resolver=resolver))
    if fmt == "html":
        return render(mj, RenderFormat.HTML,
                      options=HtmlRenderOptions(asset_base_url=asset_base_url, document_title=title))
    if fmt == "epub":
        return render(mj, RenderFormat.EPUB, options=EpubRenderOptions(title=title, asset_resolver=resolver))
    if fmt == "latex":
        return render(mj, RenderFormat.LATEX, options=LatexRenderOptions(document_title=title))
    from mineru.render import render_pdf, PdfRenderOptions
    return render_pdf(mj, asset_resolver=resolver) if _render_pdf_takes_resolver(render_pdf) else \
        render(mj, RenderFormat.PDF, options=PdfRenderOptions(asset_resolver=resolver, document_title=title))


def _render_pdf_takes_resolver(fn):
    import inspect
    try:
        return "asset_resolver" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
