"""review.py — OCR 复核核心：存疑页检测（启发式）+ Vision 交叉分歧 + 复核包构建。
检测便宜的 90% 交给这里，裁断修正的 10% 交给 LM（看原图）。"""
from __future__ import annotations
import re
import difflib
from pathlib import Path
import yaml
import mdpages

# 阈值默认值（实施期用真实烂扫描样本微调）
_MIN_LEN = 10                 # 短于此的非空页不判存疑（多为版式空页/短戳）
_ISOLATED_RATIO_THRESH = 0.55  # 孤立单字 token 占比超此 → 存疑
_REPLACEMENT_THRESH = 2        # 替换符/明显乱码字符数 ≥ 此 → 存疑

# 明显乱码信号：Unicode 替换符、OCR 常见占位、连续问号
_BAD_CHARS = re.compile(r"[�]|\[\?\]|\?{2,}")
_CJK = re.compile(r"[一-鿿]")


def page_suspect_signals(text):
    """纯函数：从一页正文算出存疑信号。"""
    stripped = text.strip()
    tokens = stripped.split()
    single = [t for t in tokens if len(t) == 1 and _CJK.match(t)]
    isolated_ratio = (len(single) / len(tokens)) if tokens else 0.0
    return {
        "length": len(re.sub(r"\s", "", stripped)),
        "isolated_char_ratio": round(isolated_ratio, 3),
        "replacement_chars": len(_BAD_CHARS.findall(stripped)),
    }


def is_suspect(signals):
    """据信号判定该页是否存疑。空/极短页一律不判存疑（避免版式空页误报）。"""
    if signals["length"] < _MIN_LEN:
        return False
    if signals["replacement_chars"] >= _REPLACEMENT_THRESH:
        return True
    if signals["isolated_char_ratio"] >= _ISOLATED_RATIO_THRESH:
        return True
    return False


def needs_vision(signals):
    """该页是否需要送 Vision 交叉：启发式存疑，或空/超短页（MinerU 整页吞掉的
    输出形态恰是空页；此类页每份文书个位数，Vision 成本近零，一律交叉核验）。"""
    return is_suspect(signals) or signals["length"] < _MIN_LEN


_LOW_CONF = 0.5           # Vision 置信度低于此算"低置信块"（仅记录：真实扫描件几乎每页都有，单独触发会淹没）
_SIM_THRESH = 0.95        # 相似度参考线（仅用于算可疑度分数，不作分歧触发）
_SIM_STRONG = 0.60        # CJK 相似度低于此 → 强分歧（严重乱码/大段错乱）
_MISS_RATIO = 1.25        # Vision 的 CJK 文本达 MinerU 的这么多倍 → 疑似 MinerU 漏抽整段（主信号）
_EMPTY_PAGE_MIN_CJK = 10   # MinerU 无 CJK 时，Vision 读出 ≥ 此数 CJK → 判漏抽
# 行级包含信号（2026-09-18，4.0 复测新增）：整页比率对"漏一行/漏一段"不敏感（40 页扫描件实测：MinerU 4.0
# 漏掉 p.9 第二十二条 31 字、p.11 不可抗力续段 71 字，miss_ratio 仅 1.08/1.15 不触发）。改为逐个 Vision 块
# 看它在 MinerU 页文本里的"匹配覆盖率"（只计 ≥4 字连续匹配，容忍 OCR 单字错）；覆盖率 <0.6 记一"缺行"。
# 单阈值防误报：只看缺行 CJK 合计 ≥25 字。实测 p.7/38/40 的印章 / 页脚 / 小标题单行 8–10 字不触发；
# p.8 两条短行（Vision 把"消费者"读成繁体"消費者"等多字错，覆盖率 0.45/0.55、合计 22 字）也不触发——
# 曾试"≥2 行也算"，正是被 p.8 这类 Vision 多字误读击穿，故弃用。真漏段覆盖率都在 0–0.16。
_LINE_MIN_CJK = 8          # Vision 块 CJK 少于此不参与（页码、短戳、水印噪声）
_LINE_COVERAGE = 0.50      # 覆盖率低于此 → 该行判"MinerU 缺"
_LINE_MATCH_RUN = 4        # 只计 ≥ 此长度的连续匹配块（避免"甲方""乙方"等碎片虚增覆盖）
_LINE_CJK_MIN = 25         # 缺行 CJK 合计 ≥ 此数 → 漏内容
                           # （页脚"第X页共X页"约7字、印章2-6字、水印4-8字均挡在外；
                           #  仅含≤10字手写批注的页会漏，属诚实局限，deep 模式可捞）


def _cjk_only(s):
    """从字符串中提取仅含 CJK 字符（汉字）。"""
    return "".join(_CJK.findall(s))


def _line_coverage(block_cjk, page_cjk):
    """Vision 块（CJK 串）在 MinerU 页 CJK 串中的匹配覆盖率，只计 ≥_LINE_MATCH_RUN 的连续匹配。"""
    if not block_cjk:
        return 1.0
    if not page_cjk:
        return 0.0
    sm = difflib.SequenceMatcher(None, page_cjk, block_cjk, autojunk=False)
    matched = sum(m.size for m in sm.get_matching_blocks() if m.size >= _LINE_MATCH_RUN)
    return matched / len(block_cjk)


def missing_lines(mineru_text, vision_blocks):
    """纯函数：MinerU 页文本里找不到的 Vision 行 → [(覆盖率, 行文本, 该块在 vision_blocks 中的序号)]。"""
    page_cjk = _cjk_only(mineru_text)
    out = []
    for i, b in enumerate(vision_blocks):
        c = _cjk_only(b.text)
        if len(c) < _LINE_MIN_CJK:
            continue
        cov = _line_coverage(c, page_cjk)
        if cov < _LINE_COVERAGE:
            out.append((round(cov, 2), b.text, i))
    return out


def _insert_after_anchor(page_text, anchor_cjk, line):
    """把 line 插到 page_text 中 anchor_cjk（前一 Vision 行的 CJK 串）最长匹配处所在段落之后。
    anchor_cjk 为空（缺行是页首块，如承接上页的续段）→ 插到页首；有锚但匹配 <4 字 → 追加到页末。"""
    if not anchor_cjk:
        return line + "\n\n" + page_text.lstrip("\n")
    positions = [i for i, ch in enumerate(page_text) if _CJK.match(ch)]
    page_cjk = "".join(page_text[i] for i in positions)
    if page_cjk:
        m = difflib.SequenceMatcher(None, page_cjk, anchor_cjk, autojunk=False).find_longest_match(
            0, len(page_cjk), 0, len(anchor_cjk))
        if m.size >= _LINE_MATCH_RUN:
            end = positions[m.a + m.size - 1] + 1
            nl = page_text.find("\n", end)
            cut = len(page_text) if nl == -1 else nl
            return page_text[:cut].rstrip() + "\n\n" + line + "\n\n" + page_text[cut:].lstrip("\n")
    return page_text.rstrip() + "\n\n" + line


def draft_page_text(mineru_text, vision_blocks):
    """纯函数：据 Vision 补回 MinerU 缺行的整页草稿。
    - MinerU 整页无 CJK（整页被吞）→ 直接用 Vision 全部块（按 y 序）；
    - 否则把每条缺行按其前一 Vision 块锚定插入；无缺行 → None（不出草稿）。"""
    if not _cjk_only(mineru_text):
        joined = "\n\n".join(b.text.strip() for b in vision_blocks if b.text.strip())
        return joined or None
    lines = missing_lines(mineru_text, vision_blocks)
    if not lines:
        return None
    text = mineru_text
    for _cov, line, i in lines:
        anchor = ""                                   # 回溯最近一个有 ≥_LINE_MATCH_RUN 字 CJK 的前块作锚（跳过页码/短戳）
        for j in range(i - 1, -1, -1):
            c = _cjk_only(vision_blocks[j].text)
            if len(c) >= _LINE_MATCH_RUN:
                anchor = c
                break
        text = _insert_after_anchor(text, anchor, line.strip())
    return text


def page_divergence(mineru_text, vision_blocks):
    """比对某页 MinerU 文本与 Vision 块，判定是否分歧 + 给可疑度分数（供排序）。
    实测校准（真实 40 页扫描件，2026-07）：
    - 主信号「MinerU 漏内容」：Vision 中文显著多于 MinerU（`miss_ratio≥1.25`）——抓到过 MinerU 静默吞整条条款；
      MinerU 该页 CJK 为 0（整页被吞的最严重形态）时改判 Vision CJK 数是否达 `_EMPTY_PAGE_MIN_CJK`（miss_ratio
      分母为零，比值判定失效）；
    - 辅信号「严重乱码」：CJK 相似度 `<0.60`；MinerU 出表格的页豁免（Vision 无法对齐表结构，属误报）；
    - 低置信块（`low_conf_count`）噪声大，只记录、不单独触发（否则全页皆标）；
    - 单字认错（干净页 sim≈0.9）真实数据上无法在不误报海量下单独拎出——本层不追，交人工/启发式。"""
    confs = [b.confidence for b in vision_blocks]
    mean_conf = round(sum(confs) / len(confs), 3) if confs else 1.0
    low_conf_count = sum(1 for c in confs if c < _LOW_CONF)
    m_cjk = _cjk_only(mineru_text)
    v_cjk = "".join(_cjk_only(b.text) for b in vision_blocks)
    sim = difflib.SequenceMatcher(None, m_cjk, v_cjk).ratio()
    miss_ratio = round(len(v_cjk) / len(m_cjk), 3) if m_cjk else (2.0 if v_cjk else 0.0)
    has_table = "<table>" in mineru_text
    empty_mineru = len(m_cjk) == 0
    lines = missing_lines(mineru_text, vision_blocks) if not empty_mineru else []
    lines_cjk = sum(len(_cjk_only(t)) for _, t, _i in lines)
    line_missing = lines_cjk >= _LINE_CJK_MIN
    missing_content = bool(vision_blocks) and (
        (not empty_mineru and (miss_ratio >= _MISS_RATIO or line_missing))
        or (empty_mineru and len(v_cjk) >= _EMPTY_PAGE_MIN_CJK))
    # empty_mineru 页交给上面的 missing_content 空页分支判定；sim(空串, 任意非空串) 恒为 0，
    # 若不豁免会把「真空页 + 短戳」也错判成"严重乱码"（乱码信号本就要求 MinerU 有文本可比）。
    strong_garble = bool(vision_blocks) and sim < _SIM_STRONG and not has_table and not empty_mineru
    diverged = missing_content or strong_garble
    # 可疑度分数（越大越该先看）：漏内容比重加权 2× + 低相似度余量 + 缺行字数/100（整段缺失排前）
    score = round(max(miss_ratio - 1.0, 0.0) * 2 + max(_SIM_THRESH - sim, 0.0)
                  + (lines_cjk / 100 if line_missing else 0.0), 3)
    return {
        "mean_conf": mean_conf,
        "low_conf_count": low_conf_count,
        "similarity": round(sim, 3),
        "miss_ratio": miss_ratio,
        "empty_mineru": empty_mineru,
        "vision_cjk_len": len(v_cjk),
        "missing_lines": len(lines),
        "missing_line_cjk": lines_cjk,
        "missing_line_samples": [t[:40] for _, t, _i in lines[:3]],
        "diverged": diverged,
        "score": score,
    }


_ADV_TIER = "advanced"


def third_opinion_merge(entries, vblocks_by_page, reparse_texts):
    """纯函数：把 advanced 档重识别结果并入入包条目。
    对每个入包页：advanced 文本 vs Vision 再算一次分歧；advanced 与 Vision 一致（不分歧）且 CJK 多于
    原 MinerU 文本 → 标 advanced_recovers（可作修正草稿）。返回 recovered 页码升序列表。"""
    recovered = []
    for e in entries:
        page = e["page"]
        adv = (reparse_texts or {}).get(page, "") or ""
        e["advanced_text"] = adv
        div = page_divergence(adv, vblocks_by_page.get(page, []))
        r = e["reason"]
        r["advanced_similarity"] = div["similarity"]
        r["advanced_miss_ratio"] = div["miss_ratio"]
        r["advanced_recovers"] = bool(adv) and not div["diverged"] \
            and len(_cjk_only(adv)) > len(_cjk_only(e.get("mineru_text", "")))
        if r["advanced_recovers"]:
            recovered.append(page)
    return sorted(recovered)


def draft_corrections(entries, coverage):
    """生成 corr.draft.yaml 结构（人工看原图确认后再 --apply-review）。来源优先级：
    ① Vision 补行草稿（entry['vision_draft']：MinerU 缺行按前一行锚定插回 / 整页被吞用 Vision 全文）——
       40 页基准实测，漏行是版面模型没框到，MinerU 任何档位重识别都补不回，只有 Vision 读到；
    ② advanced 档重识别与 Vision 一致且更全（entry['reason']['advanced_recovers']，需 --third-opinion）。"""
    corr = []
    for e in sorted(entries, key=lambda x: x["page"]):
        r = e["reason"]
        if e.get("vision_draft"):
            corr.append({
                "page": e["page"],
                "corrected_text": e["vision_draft"],
                "changes": [{"old": "（见 packet.yaml 该页 mineru_text）",
                             "new": t[:60],
                             "basis": f"Apple Vision 读出、MinerU 缺失（覆盖率 {c}）；插入位置按前一行锚定；"
                                      f"Vision 字对可能有错，须对照 page-{e['page']}.png 原图逐字核"}
                            for c, t, _i in r.get("_missing_line_items", [])] or
                           [{"old": "（整页被吞）", "new": "（Vision 全文）",
                             "basis": f"MinerU 整页无文本，Vision 全文作草稿，须对照 page-{e['page']}.png 原图"}],
            })
        elif r.get("advanced_recovers"):
            corr.append({
                "page": e["page"],
                "corrected_text": e["advanced_text"],
                "changes": [{"old": "（见 packet.yaml 该页 mineru_text）",
                             "new": "（采用 advanced 档重识别全文）",
                             "basis": f"advanced 档重识别，与 Apple Vision 一致（sim={r['advanced_similarity']}）；"
                                      f"草稿，须人工核对 page-{e['page']}.png 原图"}],
            })
    return {"coverage": coverage, "draft": True,
            "note": "由 cc-convert --review 自动生成的草稿：Vision 补行 / 整页 Vision 全文 / advanced 重识别；"
                    "逐页对照原图确认（Vision 字对可能有错）后删除本 note 与 draft 字段再 --apply-review。",
            "corrections": corr}


def build_review(pdf_path, deep=False, ocr_fn=None, third_opinion=False, reparse_fn=None,
                 second_opinion_fn=None):
    """读已转的 _md/<stem>.md，挑存疑页（deep=每页），跑 Vision 交叉，写复核包。
    ocr_fn 可注入替身；默认用 vision_ocr.ocr_image。
    third_opinion=True：对入包页用 advanced 档批量重识别（reparse_fn，默认 engine.parse_pages_text），
    并入 packet 并产出 corr.draft.yaml；重识别失败 fail-soft（packet 记 third_opinion_error）。
    second_opinion_fn(pdf_path, pages) -> {页码: 文本}：可选第二意见（--second-opinion glm，Windows 3090 上
    GLM-OCR 独立重识别），逐页与 MinerU 比对出分歧条写进 packet；只提示、绝不进草稿；失败 fail-soft
    （packet 记 second_opinion_error，本次仅 Vision）。为 None 时行为与之前完全一致。"""
    import pdf_render
    if ocr_fn is None:
        import vision_ocr
        ocr_fn = vision_ocr.ocr_image
    pdf_path = Path(pdf_path)
    md_path = mdpages.md_for(pdf_path)
    if md_path is None:
        raise FileNotFoundError(f"未找到 source 匹配的已转 MD（跨格式重名请核对 _md/），请先转换：{pdf_path.name}")
    pages = mdpages.split_pages(md_path.read_text(encoding="utf-8"))

    review_dir = md_path.parent / (md_path.stem + ".assets") / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    suspect_entries = []
    vblocks_by_page = {}
    for page_no, text in pages:
        signals = page_suspect_signals(text)
        # suspect 模式：启发式存疑页一律入包（现行为不变）；空/超短页额外送 Vision 交叉，
        # 仅 Vision 判分歧才入包（真版式空页 Vision 无块/CJK 太少 → 不入包，零误报）。
        # deep 模式：每页都送 Vision，行为不变。
        flagged = deep or needs_vision(signals)
        if not flagged:
            continue
        idx0 = page_no - 1
        img = pdf_render.render_page_ndarray(pdf_path, idx0)
        vblocks = ocr_fn(img)
        div = page_divergence(text, vblocks)
        # 非启发式存疑、且 Vision 也不分歧的页，跳过不入包（省 LM 负载 / 零误报）
        if not is_suspect(signals) and not div["diverged"]:
            continue
        vblocks_by_page[page_no] = vblocks
        # 只为真正入包的页落 PNG（避免 deep 模式给被跳过页留孤儿图）
        png = pdf_render.render_page_png(pdf_path, idx0, review_dir)
        reason = {"heuristic": is_suspect(signals), **div, **signals}
        # 启发式入包、但 Vision 交叉高度一致（sim≥0.95 且无漏内容）→ 标 vision_agrees：
        # 多为排版性误报（证书/编号页孤立单字比偏高），score 本就沉底，人工可整批略过。
        # 阈值取 _SIM_THRESH 而非"不分歧"——sim 0.6~0.95 的中度差异不配"一致"标注。
        if reason["heuristic"] and div["similarity"] >= _SIM_THRESH \
                and div["miss_ratio"] < _MISS_RATIO:
            reason["vision_agrees"] = True
        entry = {
            "page": page_no,
            "reason": reason,
            "png": str(png.relative_to(pdf_path.parent)),
            "mineru_text": text,
            "vision_text": "\n".join(b.text for b in vblocks),
        }
        if div["diverged"]:
            reason["_missing_line_items"] = missing_lines(text, vblocks)
            entry["vision_draft"] = draft_page_text(text, vblocks)
        suspect_entries.append(entry)

    # D3：复核侧独立守卫（防御纵深，兜存量旧 MD）——源 PDF 真实页数 > MD 已有最大页码，
    # 说明 MD 尾部缺页标记（早期转换丢页，未经 stamp() D3 补位守卫）。逐页渲染+Vision，
    # 与普通空页判定同一条 page_divergence 判定流；只有 Vision 也读到内容才入包，避免
    # 真实就是空白尾页（如封底）被误报。
    missing_pages = []
    real = pdf_render.page_count(pdf_path)
    max_page = max((p for p, _ in pages), default=0)
    if real is not None and real > max_page:
        for m in range(max_page + 1, real + 1):
            idx0 = m - 1
            img = pdf_render.render_page_ndarray(pdf_path, idx0)
            vblocks = ocr_fn(img)
            div = page_divergence("", vblocks)
            if not div["diverged"]:
                continue
            png = pdf_render.render_page_png(pdf_path, idx0, review_dir)
            signals = page_suspect_signals("")
            vblocks_by_page[m] = vblocks
            suspect_entries.append({
                "page": m,
                "reason": {"heuristic": is_suspect(signals), **div, **signals},
                "png": str(png.relative_to(pdf_path.parent)),
                "mineru_text": "",
                "vision_text": "\n".join(b.text for b in vblocks),
                "missing_marker": True,       # 缺页标记不能靠 corr.yaml 补，故不出 vision_draft
            })
            missing_pages.append(m)

    # 第三意见（可选 --third-opinion）：对入包页一次性 advanced 档重识别（一次 page_range 调用，不逐页起模型）。
    # 40 页基准实测：漏行源于版面模型没框到，advanced/basic 重识别都补不回、39 s 无增益，故默认关闭。
    recovered, draft_path, third_err = [], None, None
    coverage = "deep" if deep else "suspect"
    if third_opinion and suspect_entries:
        if reparse_fn is None:
            import engine
            reparse_fn = engine.parse_pages_text
        try:
            texts = reparse_fn(pdf_path, [e["page"] for e in suspect_entries], _ADV_TIER)
            recovered = third_opinion_merge(suspect_entries, vblocks_by_page, texts)
        except Exception as e:           # fail-soft：第三意见是增益层，绝不让复核包落不下
            third_err = f"{type(e).__name__}: {str(e)[:200]}"
    # 草稿：Vision 补行（默认）+ advanced recovers（可选）
    draft = draft_corrections(suspect_entries, coverage)
    for e in suspect_entries:
        e["reason"].pop("_missing_line_items", None)
    if draft["corrections"]:
        draft_path = review_dir / "corr.draft.yaml"
        draft_path.write_text(yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), encoding="utf-8")
    drafted = [c["page"] for c in draft["corrections"]]

    # 第二意见（可选 --second-opinion glm）：Windows 3090 上 GLM-OCR 对全部页（deep）或入包页（quick）
    # 独立重识别，逐页与 MinerU 比对出分歧条。2026-10-20 比测：捕获 MinerU 漏读/误读 94%、误报约 2.5 处/页；
    # 但视觉大模型在看不清的页会编造 → GLM 文本只作分歧提示，绝不进 corr.draft.yaml、不写回。fail-soft。
    # 放在草稿生成之后：GLM 结果在结构上碰不到第三意见合并与草稿。
    so_pages, so_err, so_checked = [], None, 0
    if second_opinion_fn is not None:
        import second_opinion as _so
        text_of = dict(pages)
        targets = sorted(text_of) if deep else sorted({e["page"] for e in suspect_entries if e["page"] in text_of})
        try:
            so_texts = second_opinion_fn(pdf_path, targets)
            so_checked = len(so_texts)
            by_page = {e["page"]: e for e in suspect_entries}
            for p in targets:
                if p not in so_texts:
                    continue
                flags = _so.diff_flags(text_of[p], so_texts[p])
                e = by_page.get(p)
                if e is None:
                    if not flags:
                        continue
                    png = pdf_render.render_page_png(pdf_path, p - 1, review_dir)
                    chars = sum(len(f["mineru"]) + len(f["glm"]) for f in flags)
                    e = {"page": p,
                         "reason": {"heuristic": False, "second_opinion_only": True,
                                    "score": round(min(chars / 200, 1.0), 3)},
                         "png": str(png.relative_to(pdf_path.parent)),
                         "mineru_text": text_of[p]}
                    suspect_entries.append(e)
                    by_page[p] = e
                e["glm_text"] = so_texts[p]
                e["glm_flags"] = flags
                e["reason"]["glm_flag_count"] = len(flags)
                if flags:
                    so_pages.append(p)
        except Exception as ex:          # fail-soft：第二意见是增益层，Windows 不可用时本次仅 Vision
            so_err = f"{type(ex).__name__}: {str(ex)[:200]}"

    # 按可疑度分数降序——最该先看的排最前（复核包）；CLI 摘要页码升序，便于阅读
    suspect_entries.sort(key=lambda e: e["reason"].get("score", 0), reverse=True)
    suspect_nums = sorted(e["page"] for e in suspect_entries)
    packet = {"source": pdf_path.name, "reviewed_coverage": coverage,
              "suspect_pages": suspect_entries}
    if third_err:
        packet["third_opinion_error"] = third_err
    if second_opinion_fn is not None:
        packet["second_opinion"] = {"engine": "glm-ocr", "pages_checked": so_checked,
                                    "flagged_pages": sorted(so_pages)}
        if so_err:
            packet["second_opinion_error"] = so_err
    packet_path = review_dir / "packet.yaml"
    packet_path.write_text(yaml.safe_dump(packet, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")
    agrees = sorted(e["page"] for e in suspect_entries
                    if e["reason"].get("vision_agrees"))
    return {"packet_path": packet_path, "suspect_pages": suspect_nums,
            "reviewed_coverage": coverage, "missing_pages": missing_pages,
            "vision_agrees_pages": agrees, "advanced_recovered_pages": recovered,
            "drafted_pages": drafted, "draft_path": draft_path, "third_opinion_error": third_err,
            "second_opinion_pages": sorted(so_pages), "second_opinion_error": so_err,
            "second_opinion_checked": so_checked}
