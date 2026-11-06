"""stamp.py — 引擎按页 JSON → 带 <!-- PAGE n --> 的单一 Markdown。
兼容三真实形态：PaddleOCR-VL（顶层 list，markdown.text）、
MinerU v1（扁平 blocks，text + page_idx）、MinerU v2（顶层=页，content 可嵌套）。

防缺陷加固（Task 0）：未知块类型不再静默吞掉——记入 stats['dropped'] 并在
front-matter 响亮警告；完整性哨兵在"非空块/总块" 低于阈值时提示人工核验。"""
import json
from pathlib import Path

PAGE_MARKER = "<!-- PAGE {n} -->"

# 已知块类型白名单：据真实 MinerU 3.2.3 content_list 实测扩充（判决书/微信截图样本）。
# 不在此集合且产出为空的块 → 记为 dropped 并响亮警告。新增类型在此登记即可。
KNOWN_BLOCK_TYPES = {
    "text", "title", "table", "list", "index", "equation",   # index＝MinerU 4.0 目录块，形同 list
    "image", "figure", "chart",            # chart 实测含 img_path，按图片处理
    "header", "footer", "page_number", "code",
}
COMPLETENESS_THRESHOLD = 0.70   # 有载荷块的提取成功率低于此值 → 完整性哨兵提示人工核验

# 结构性/元数据键：判断"块是否承载正文载荷"时忽略这些（bbox、页码、标题级别、
# caption/footnote 等不算正文主体）。实测：MinerU 对空白/版式区域会吐 text:"" 空块，
# 这些无载荷块不应拖低提取率——否则正常判决书(53 个空 text 块)会误报。
_STRUCTURAL_KEYS = {
    "type", "bbox", "page_idx", "text_level", "sub_type", "text_format", "level",
    "index", "page_size", "lines", "score", "angle",
    "image_caption", "image_footnote", "table_caption", "table_footnote",
    "chart_caption", "chart_footnote",
}


def _has_payload(b):
    """块是否承载正文载荷（用于完整性哨兵的分母）。
    判据：存在非结构键、其值非空。table_body 在不被 _block_text 读取的字段里时
    （历史表格 bug 即此形态：已知类型 table 但正文落在未处理字段）仍会被识别为有载荷，
    从而被哨兵计入"应提取却空"。而 text:'' 这类纯空白块无载荷，不计分母。"""
    if not isinstance(b, dict):
        return False
    for k, v in b.items():
        if k in _STRUCTURAL_KEYS:
            continue
        if isinstance(v, str):
            if v.strip() and v.strip() not in ("[]", "{}", "null", "None"):
                return True
        elif isinstance(v, (list, dict)):
            if _extract_content(v):
                return True
        elif v not in (None, 0, False, ""):
            return True
    return False


def _extract_content(v):
    """v2 的 content 可能是 str / 嵌套 dict（title_content）/ list。递归提取纯文本。"""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        if isinstance(v.get("title_content"), list):
            return " ".join(_extract_content(x) for x in v["title_content"]).strip()
        if isinstance(v.get("text"), str):
            return v["text"].strip()
        if "content" in v:
            return _extract_content(v["content"])
    if isinstance(v, list):
        return " ".join(_extract_content(x) for x in v).strip()
    return ""


def _as_text(v):
    """caption/footnote 可能是 list 或 str。"""
    if isinstance(v, list):
        return " ".join(str(x).strip() for x in v if str(x).strip())
    return str(v).strip() if v else ""


def _heading_level(b):
    """取标题级别：v1 用块上的 text_level；v2 用嵌套 content.level。无则 None。
    clamp 到 markdown 合法的 1–6 级。"""
    lvl = b.get("text_level")
    if lvl is None:
        c = b.get("content")
        if isinstance(c, dict):
            lvl = c.get("level")
    if lvl is None:
        return None
    try:
        return max(1, min(6, int(lvl)))
    except (ValueError, TypeError):
        return None


def _block_text_inner(b, assets_stem):
    """纯提取：块 → markdown 文本（不含统计副作用）。"""
    if not isinstance(b, dict):
        return ""
    btype = b.get("type", "")
    if btype in ("image", "figure", "chart"):
        # chart 实测仅 img_path 承载内容（content 多为空），与 image 同处理
        path = b.get("img_path") or b.get("image_path") or ""
        if not path:
            return ""
        # B3：重写到归位后的相对路径
        name = Path(path).name
        return f"![]({assets_stem}.assets/images/{name})"
    if btype == "table":
        # ⭐ 表格：table_body 是结构化 HTML，必须保留（含 caption/footnote）
        cap = _as_text(b.get("table_caption"))
        body = (b.get("table_body") or b.get("html") or "").strip()
        foot = _as_text(b.get("table_footnote"))
        return "\n".join(x for x in (cap, body, foot) if x)
    if btype in ("list", "index"):
        # ⭐ 列表 / 目录：list_items 是字符串数组，逐项保留（index 实测：书籍目录页，条目自带 "- " 前缀）
        items = b.get("list_items")
        if isinstance(items, list):
            return "\n".join(str(x).strip() for x in items if str(x).strip())
    for key in ("content", "text", "md", "latex"):
        if key in b:
            t = _extract_content(b[key])
            if t:
                # Task 3：text/title 块带层级 → 还原 markdown 标题（正文不加 #）
                if btype in ("text", "title"):
                    lvl = _heading_level(b)
                    if lvl is not None:
                        return "#" * lvl + " " + t
                return t
    return ""


def _block_text(b, assets_stem, stats=None):
    """提取 + 记账：产出非空则计 nonempty；未知类型且产出空 → 记 dropped。"""
    result = _block_text_inner(b, assets_stem)
    if stats is not None:
        stats["total"] = stats.get("total", 0) + 1
        if _has_payload(b):
            stats["payload"] = stats.get("payload", 0) + 1
        if result:
            stats["nonempty"] = stats.get("nonempty", 0) + 1
        elif isinstance(b, dict) and b.get("type", "") not in KNOWN_BLOCK_TYPES:
            d = stats.setdefault("dropped", {})
            t = b.get("type", "") or "(no-type)"
            d[t] = d.get(t, 0) + 1
    return result


def _pages_from_json(data, assets_stem, stats=None):
    if not isinstance(data, list) or not data:
        raise ValueError("JSON 顶层应为非空 list")
    head = data[0]
    # A：PaddleOCR-VL（页级 markdown.text，无 block 粒度 → 按页统计完整性）
    if isinstance(head, dict) and "markdown" in head:
        pages = [((p.get("markdown") or {}).get("text", "") or "").strip() for p in data]
        if stats is not None:
            ne = sum(1 for x in pages if x)
            stats["total"] = stats.get("total", 0) + len(pages)
            stats["payload"] = stats.get("payload", 0) + len(pages)   # 页级形态：每页都算应有内容
            stats["nonempty"] = stats.get("nonempty", 0) + ne
        return pages
    # B：MinerU v2（顶层=页，每页=blocks 列表）
    if isinstance(head, list):
        return ["\n\n".join(t for t in (_block_text(b, assets_stem, stats) for b in pg) if t) for pg in data]
    # C：MinerU v1（扁平 blocks，含 page_idx）
    if isinstance(head, dict) and "page_idx" in head:
        by = {}
        for b in data:
            by.setdefault(int(b.get("page_idx", 0)), []).append(_block_text(b, assets_stem, stats))
        # D3：按 0..max(page_idx) 遍历补位——若某中间页 page_idx 缺失（该页无任何块），
        # 用 sorted(by) 会让后续页码整体前移（静默重编号，比丢尾页更危险）；
        # 遍历完整区间为缺失页产出空串页，页码保持与源 PDF 对齐。
        return ["\n\n".join(t for t in by.get(i, []) if t) for i in range(max(by) + 1)]
    raise ValueError("无法识别的 JSON 形态")


def _build_warnings(stats):
    """据 stats 拼装响亮警告串（空串=无警告）。供 front-matter 与转换回报共用。"""
    msgs = []
    dropped = stats.get("dropped") or {}
    if dropped:
        detail = ", ".join(f"{k}×{v}" for k, v in sorted(dropped.items()))
        n = sum(dropped.values())
        msgs.append(f"⚠️ 丢弃 {n} 个未知类型块 [{detail}]，请升级 stamp.py 处理")
    # 分母用"有载荷块数"而非全部块——空白版式块(text:'')不拖低提取率，避免正常文档误报
    payload = stats.get("payload", 0)
    nonempty = stats.get("nonempty", 0)
    if payload > 0 and nonempty / payload < COMPLETENESS_THRESHOLD:
        msgs.append(f"⚠️ 低提取率 {nonempty}/{payload}={nonempty / payload:.0%}"
                    f"（阈值 {COMPLETENESS_THRESHOLD:.0%}），疑似大量内容未提取，请人工核验")
    # D3：页数守恒——源 PDF 真实页数 vs 引擎产出页数，不一致响亮警告
    expected = stats.get("expected_pages")
    if expected is not None:
        engine_n = stats.get("engine_pages", expected)
        if engine_n < expected:
            padded = stats.get("padded_pages") or []
            plist = "、".join(f"p.{n}" for n in padded)
            msgs.append(f"⚠️ 页数不守恒：源 PDF {expected} 页，引擎产出 {engine_n} 页，"
                        f"已补空页占位（{plist}），请 --review 核验")
        elif engine_n > expected:
            msgs.append(f"⚠️ 页数不守恒：源 PDF {expected} 页，引擎产出 {engine_n} 页"
                        f"（多于源页数，异常），请 --review 核验")
    return " | ".join(msgs)


def _front_matter(source_rel, pages, engine, date, warnings="", text_layer=None):
    fm = (f"---\nsource: {source_rel}\npages: {pages}\n"
          f"engine: {engine}\nconverted: {date}\n")
    if text_layer is not None:
        fm += f"text_layer: {text_layer}\n"
    if warnings:
        fm += f"warnings: {warnings}\n"
    return fm + "---"


def stamp(json_path, source_rel, engine, date, assets_stem, stats=None, expected_pages=None,
          text_layer=None):
    """转换主入口。stats 为可选的可变 dict：调用方传入即可回收
    total / nonempty / dropped / warnings 诊断信息（用于转换回报响亮提示）。
    expected_pages（可选，D3）：源 PDF 真实页数。若引擎产出页数少于此，尾部补空页
    占位以保持页码与源 PDF 对齐，并在 warnings 响亮提示；不传则行为与此前完全一致。
    text_layer（可选）：源 PDF 原生文本层探测结果（none/partial/full），写入
    front-matter 供下游"扫描证据→建议复核"规则读取；不传则 front-matter 无该行。"""
    if stats is None:
        stats = {}
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    pages = _pages_from_json(data, assets_stem, stats)
    if expected_pages is not None:
        engine_n = len(pages)
        stats["expected_pages"] = expected_pages
        stats["engine_pages"] = engine_n
        if engine_n < expected_pages:
            stats["padded_pages"] = list(range(engine_n + 1, expected_pages + 1))
            pages = pages + [""] * (expected_pages - engine_n)
    warnings = _build_warnings(stats)
    stats["warnings"] = warnings          # 暴露给调用方（cc_convert 转换回报）
    parts = [_front_matter(source_rel, len(pages), engine, date, warnings, text_layer=text_layer)]
    for i, text in enumerate(pages, start=1):
        parts.append(PAGE_MARKER.format(n=i))
        if text:
            parts.append(text)
    return "\n\n".join(parts).rstrip() + "\n"
