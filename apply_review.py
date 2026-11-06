"""apply_review.py — 把 LM 裁断出的修正清单就地写回 stamped MD。
只此一处修改 stamped MD（守住"_md 由 cc-convert 维护"）：改对应页 + 溯源块 + front-matter。"""
from __future__ import annotations
import re
import mdpages

_FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_PROV_HEADER = "## 复核溯源"
# 追加的溯源块以机器哨兵起头（HTML 注释，OCR 正文绝不会产生该串）；剥离时只认哨兵，
# 从根上杜绝「页正文里恰含 ## 复核溯源（含末页独占一行）」导致的误剥离与静默丢内容。
_PROV_SENTINEL = "<!-- CC-REVIEW-PROVENANCE -->"


def _esc(s):
    """转义 Markdown 表格里的竖线，防 OCR 文本含 | 时把表格行冲乱。"""
    return str(s).replace("|", "\\|")


def _set_front_matter(md_text, updates):
    """在顶部 front-matter 内更新/追加 key: value（保序，新增追加在末尾）。"""
    m = _FM_RE.match(md_text)
    if not m:
        raise ValueError("MD 缺少 front-matter，无法标记复核")
    lines = m.group(1).splitlines()
    keys = {ln.split(":", 1)[0].strip(): i for i, ln in enumerate(lines) if ":" in ln}
    for k, v in updates.items():
        if k in keys:
            lines[keys[k]] = f"{k}: {v}"
        else:
            lines.append(f"{k}: {v}")
    return f"---\n" + "\n".join(lines) + "\n---\n" + md_text[m.end():]


def apply_corrections(md_text, corrections, coverage, date):
    """就地替换被改页正文 + 追加溯源块 + 标 front-matter。返回新 MD 文本。"""
    # 先剥离已有的溯源块以保幂等：只认机器哨兵（正文绝不含），取最后一处（追加块永在文末）。
    _cut = md_text.rfind(_PROV_SENTINEL)
    if _cut != -1:
        md_text = md_text[:_cut].rstrip() + "\n"

    corr_by_page = {c["page"]: c for c in corrections}
    m = _FM_RE.match(md_text)
    if not m:
        raise ValueError("MD 缺少 front-matter，无法写回复核修正")
    fm_text = m.group(0)
    pages = mdpages.split_pages(md_text)

    # 防损坏：解析页数须与 front-matter 声明一致，否则疑似解析异常，中止而非静默产出损坏结果
    declared = mdpages.parse_front_matter(md_text).get("pages")
    if declared and declared.isdigit() and int(declared) != len(pages):
        raise ValueError(f"页数不一致：front-matter 声明 {declared} 页，解析出 {len(pages)} 页，"
                         f"疑似解析异常，已中止以防内容损坏")
    # 校验修正清单未引用不存在的页码
    valid = {p for p, _ in pages}
    bad = sorted({c["page"] for c in corrections if c["page"] not in valid})
    if bad:
        raise ValueError(f"修正清单引用了不存在的页码 {bad}（本文档共 {len(pages)} 页）")

    rebuilt = [fm_text.rstrip()]
    for page_no, text in pages:
        rebuilt.append(f"\n<!-- PAGE {page_no} -->\n")
        c = corr_by_page.get(page_no)
        rebuilt.append(c["corrected_text"] if c else text)

    # 溯源块（按「本次调用的 corrections」重建，非累积——单次全量复核模型）。以哨兵起头供剥离锚定。
    prov = [f"\n\n{_PROV_SENTINEL}\n{_PROV_HEADER}",
            f"复核日期：{date}｜覆盖：{coverage}｜修正 {len(corrections)} 处\n"]
    prov.append("| 页 | 原文(MinerU) | 改为 | 依据 |")
    prov.append("|---|---|---|---|")
    for c in corrections:
        for ch in c.get("changes", []):
            prov.append(f"| {c['page']} | {_esc(ch['old'])} | {_esc(ch['new'])} | {_esc(ch['basis'])} |")

    out = "\n\n".join(x for x in rebuilt) + "\n".join(prov) + "\n"
    out = _set_front_matter(out, {
        "reviewed": date, "reviewed_coverage": coverage, "corrections": len(corrections),
    })
    return out
