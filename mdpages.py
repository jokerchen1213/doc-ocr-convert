"""mdpages.py — stamped MD 的页级解析（review 与 apply_review 共用）。"""
from __future__ import annotations
import re
from pathlib import Path

# 只认「独占一行」的页码标记（stamp.py 恒把标记单独成行输出）；
# 锚定行首行尾可避免页正文里恰含 inline `<!-- PAGE n -->` 时被误切出幻影页。
_PAGE_RE = re.compile(r"(?m)^<!--\s*PAGE\s+(\d+)\s*-->[ \t]*$")


def split_pages(md_text):
    """按 <!-- PAGE n --> 切成 [(页码1based, 正文), ...]。front-matter 及首个 marker 前的内容忽略。"""
    parts = _PAGE_RE.split(md_text)
    # parts = [前言, '1', 页1正文, '2', 页2正文, ...]
    out = []
    for i in range(1, len(parts), 2):
        out.append((int(parts[i]), parts[i + 1].strip()))
    return out


def parse_front_matter(md_text):
    """取顶部 ---...--- 内的简单 key: value；无则 {}。"""
    if not md_text.startswith("---"):
        return {}
    end = md_text.find("\n---", 3)
    if end == -1:
        return {}
    fm = {}
    for line in md_text[3:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def md_for(src) -> "Path | None":
    """以 front-matter source: ../<原名> 为权威键解析 src 对应的 stamped MD。
    候选顺序：_md/<stem>.md → _md/<stem>.<ext>.md（镜像 cc_convert._md_path 消歧规则）。
    读每个候选前 300 字节，严格匹配 f"source: ../{src.name}"；无匹配返回 None
    （不做宽容回退——读错文书比读不到更危险）。"""
    src = Path(src)
    candidates = [
        src.parent / "_md" / (src.stem + ".md"),
        src.parent / "_md" / f"{src.stem}.{src.suffix.lstrip('.')}.md",
    ]
    needle = f"source: ../{src.name}"
    for cand in candidates:
        if cand.exists():
            head = cand.read_text(encoding="utf-8", errors="ignore")[:300]
            if needle in head:
                return cand
    return None
