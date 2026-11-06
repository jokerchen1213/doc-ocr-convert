#!/usr/bin/env python3
"""cc-convert — 源文档 → 轻量 Markdown 的本地按需流水线。
PDF 走 MinerU OCR + 盖页码；docx/xlsx/pptx 走原生解析 + 章节锚点。
原件只读不动；产物落同目录 _md/；索引登记于同目录 _转换索引.md。"""
import argparse, sys, shutil, datetime, re, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 引擎接入史：
# · 2026-06-06（3.2.3）：CLI 改为 client/server(HTTP)，首启健康检查超时 + 客户端 read=60s 硬编码，
#   长 OCR 必崩 → 改进程内直调离线引擎 mineru.cli.common.do_parse 绕开 HTTP 层（用户已批准）。
# · 2026-09-18（4.0.2）：4.0 重写，do_parse 与 pipeline/vlm/hybrid 后端全部移除；官方改提供无状态
#   Python SDK mineru.parser.parse → ParseResult（进程内、无 HTTP、无 60s 上限）——正是我们当年
#   绕路想要的东西，改为直接用 SDK。质量档位 tier 取代后端名：basic ≈ 旧 pipeline（小模型、无 VLM，
#   Apple Silicon 自动走 Torch/MPS），standard/advanced 加 VLM（llama.cpp GGUF），flash 只读文本层不跑模型。
# · docvortex 渲页仍用 ProcessPool(spawn)，故本文件必须真实落盘 + __main__ 守卫（已具备；stdin 跑会
#   BrokenProcessPool，2026-09-18 实测）。
RENDER_FORMATS = ("docx", "html", "pdf", "epub", "latex")   # 与 engine.RENDER_FORMATS 同步（此处 stdlib-only）
DEFAULT_TIER = "standard"               # 2026-09-18 三样本复测：standard 全面快于 3.4.4 pipeline，扫描件 OCR 与之持平；
                                        # basic 虽更快但 4.0 小模型包只带 PP-OCRv6 tiny/small，扫描小字退一档（见 README）
TIERS = ["basic", "standard", "advanced", "flash"]
# 3.x 后端名 → 4.0 tier（对齐上游 changelog 的迁移映射），老命令行/脚本继续可用
LEGACY_TIER_ALIASES = {
    "pipeline": "basic",
    "hybrid-engine": "standard", "hybrid-auto-engine": "standard",
    "vlm-engine": "advanced", "vlm-auto-engine": "advanced", "vlm-http-client": "advanced",
}


def normalize_tier(value: str) -> str:
    """argparse type：接受 4.0 tier 或 3.x 后端旧名（自动映射），其余报错。"""
    v = (value or "").strip().lower()
    v = LEGACY_TIER_ALIASES.get(v, v)
    if v not in TIERS:
        raise argparse.ArgumentTypeError(
            f"未知 tier '{value}'；可选 {TIERS}，或 3.x 旧名 {sorted(LEGACY_TIER_ALIASES)}")
    return v


try:
    from importlib.metadata import version as _pkg_version
    ENGINE_LABEL = f"MinerU {_pkg_version('mineru')}"
except Exception:
    ENGINE_LABEL = "MinerU"
PDF_EXTS = {".pdf"}
OFFICE_EXTS = {".docx", ".pptx", ".xlsx"}                 # 原生 python 解析（章节锚点，保真）
# 2026-09-18 迭代：4.0 原生多格式——图片证据（微信截图/拍照）、OFD（电子发票/电子送达）、旧二进制 Office、RTF/ODF/EPUB/HTML
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".jp2"}
NATIVE_FLASH_EXTS = {".ofd", ".rtf", ".odt", ".ods", ".odp", ".epub", ".html", ".htm", ".doc", ".xls", ".ppt"}
SOURCE_EXTS = PDF_EXTS | OFFICE_EXTS | NATIVE_FLASH_EXTS   # 图片不在此集合：只在 --image-sources 或单文件指名时收（防证据夹上百张图刷屏 scan_pending）
ENGINE_EXTS = PDF_EXTS | IMAGE_EXTS | NATIVE_FLASH_EXTS     # 走 MinerU 引擎 + 盖页码的所有类型
MD_SUBDIR = "_md"
INDEX_NAME = "_转换索引.md"
PURE_DIR = "4.提交微法院的电子资料存档"   # 纯净区：连派生件也不写


def _review_hint(warnings, text_layer):
    """转换后是否建议 --review：有警告（低提取率/丢块/页数不守恒）或 text_layer
    为 none/partial（拍照扫描件）→ 返回一行动因说明；否则 None。只提示，绝不自动跑 Vision。"""
    if warnings:
        detail = warnings[:40]
        return f"转换存在警告（{detail}…），建议复核"
    if text_layer in ("none", "partial"):
        return f"扫描/拍照件（text_layer: {text_layer}），建议复核"
    return None


def discover(target: Path, recursive: bool, image_sources: bool = False):
    """文件→[该文件]；目录→源文件（默认单层；recursive 才进子目录，且跳过 _md/ 与 UUID 目录）。
    单文件指名图片一律收；目录扫描只有 image_sources=True 才收图片（opt-in，防刷屏）。"""
    exts = SOURCE_EXTS | IMAGE_EXTS if image_sources else SOURCE_EXTS
    if target.is_file():
        return [target] if target.suffix.lower() in (SOURCE_EXTS | IMAGE_EXTS) else []
    if recursive:
        out = []
        for f in target.rglob("*"):
            if f.is_file() and f.suffix.lower() in exts:
                if MD_SUBDIR in f.parts:                 # 不收 _md/ 里的
                    continue
                if any(".pdf-" in part and part.count("-") >= 4 for part in f.parts):  # 跳 UUID 目录
                    continue
                out.append(f)
        return sorted(out)
    return sorted(f for f in target.iterdir()
                  if f.is_file() and f.suffix.lower() in exts and not f.name.startswith("."))


def converted_state(src: Path):
    """三道闸：返回 'done' / 'legacy' / 'new'。"""
    import mdpages
    if mdpages.md_for(src) is not None:                   # 闸1：source 匹配，确属本文件（含跨格式消歧）
        return "done"
    # 闸2：PaddleOCR 旧 sidecar
    if list(src.parent.glob(src.stem + ".pdf_by_*.md")) or list(src.parent.glob(src.stem + "*_by_*.md")):
        return "legacy"
    # 闸3：MinerU UUID 目录 <名>.pdf-<uuid>/（uuid 含 4 个连字符）
    for d in src.parent.glob(src.stem + ".pdf-*-*-*-*-*"):
        if d.is_dir():
            return "legacy"
    return "new"


def _disable_upstream_text_normalization():
    """向后兼容入口；实现已移至 engine.disable_upstream_text_normalization()。"""
    import engine
    return engine.disable_upstream_text_normalization()


def run_mineru(src: Path, tmp: Path, tier: str):
    """进程内调 MinerU 4.0 SDK（封装在 engine.parse_to_dir）。产出落 tmp/<stem>/<tier>/：
    markdown.md + middle_json.json（4.0 正典，可重渲染）+ structured_content.json + images/ +
    <stem>_content_list.json（扁平 V1，含 page_idx，stamp.py 直接消费，3.x 形态零改动）。
    非 PDF/图片（Office/OFD/…）由 engine.tier_for 归一为 flash。"""
    import engine
    engine.parse_to_dir(src, tmp / src.stem / engine.tier_for(src, tier), tier)


def locate_outputs(tmp: Path):
    """优先选含 page_idx 的扁平 v1（content_list.json，非 v2）→ 避开 v2 嵌套坑。"""
    jsons = list(tmp.rglob("*content_list*.json"))
    if not jsons:
        raise FileNotFoundError(f"未在 {tmp} 找到 content_list JSON")
    jsons.sort(key=lambda p: ("_v2" in p.name, len(p.name)))   # v1 优先
    images = [d for d in tmp.rglob("images") if d.is_dir()]
    return jsons[0], (images[0] if images else None)


def _md_path(src: Path, mddir: Path):
    cand = mddir / (src.stem + ".md")
    if cand.exists():
        head = cand.read_text(encoding="utf-8", errors="ignore")[:200]
        if f"source: ../{src.name}" not in head:        # 跨格式重名 → 扩展名消歧
            return mddir / f"{src.stem}.{src.suffix.lstrip('.')}.md"
    return cand


DEFAULT_MIN_IMAGE_KB = 8        # 小于此体积的图片(图标/头像/logo/水印)默认不保留；0=不过滤
_IMG_REF = re.compile(r'!\[\]\([^)]*images/([^)]+)\)')


def _strip_image_refs(md_text, skipped_names):
    """删除 md 中指向已跳过图片的 ![](...images/NAME) 引用。
    整行仅含被跳过引用 → 删行；行内尚有其他内容 → 仅删该引用，保留其余。"""
    if not skipped_names:
        return md_text
    out = []
    for line in md_text.split("\n"):
        names = _IMG_REF.findall(line)
        if names and all(n in skipped_names for n in names):
            rest = _IMG_REF.sub("", line).strip()
            if rest:
                out.append(rest)
            # 整行仅为被跳过图片引用 → 丢弃
        elif names:
            out.append(_IMG_REF.sub(
                lambda m: "" if m.group(1) in skipped_names else m.group(0), line))
        else:
            out.append(line)
    return "\n".join(out)


def place_pdf(src, page_json, images_dir, md_text, full_assets,
              min_image_bytes=0, no_images=False):
    mddir = src.parent / MD_SUBDIR; mddir.mkdir(exist_ok=True)
    md_path = _md_path(src, mddir)
    assets = mddir / (md_path.stem + ".assets"); assets.mkdir(parents=True, exist_ok=True)
    # 图片过滤：--no-images 全跳；min_image_bytes>0 跳过小图(图标/头像/水印)；--full-assets 不过滤
    skipped = set()
    if images_dir and any(images_dir.iterdir()):
        imgs = [f for f in images_dir.iterdir() if f.is_file()]
        if no_images:
            skipped = {f.name for f in imgs}
        elif min_image_bytes > 0:
            skipped = {f.name for f in imgs if f.stat().st_size < min_image_bytes}
        kept = [f for f in imgs if f.name not in skipped]
        if kept:
            (assets / "images").mkdir(parents=True, exist_ok=True)
            for f in kept:
                shutil.copy2(f, assets / "images" / f.name)
    md_text = _strip_image_refs(md_text, skipped)        # 清理失效图链
    md_path.write_text(md_text, encoding="utf-8")
    shutil.copy2(page_json, assets / page_json.name)
    mj = page_json.parent / "middle_json.json"          # 4.0 正典中间表示：可 ParseResult.from_json 重渲染
    if mj.exists():
        shutil.copy2(mj, assets / mj.name)
    if full_assets:
        shutil.copytree(page_json.parent, assets / "_raw", dirs_exist_ok=True)
    return md_path


def place_office(src, md_text):
    mddir = src.parent / MD_SUBDIR; mddir.mkdir(exist_ok=True)
    md_path = _md_path(src, mddir)
    md_path.write_text(md_text, encoding="utf-8")
    return md_path


# ── Phase 6：存量 --migrate 两步走 ──────────────────────────────────────────
def _find_legacy_json(src: Path):
    """存量 src 的现成按页 JSON：PaddleOCR sidecar 或 MinerU UUID 目录里的 content_list。"""
    for j in src.parent.glob(src.stem + ".pdf_by_*.json"):
        return j
    for d in src.parent.glob(src.stem + ".pdf-*-*-*-*-*"):
        cands = sorted(d.glob("*content_list*.json"), key=lambda p: ("_v2" in p.name, len(p.name)))
        if cands:
            return cands[0]
    return None


def migrate_one(src: Path):
    """步骤1：用现有 JSON 盖页码，纯新增 _md/，不动旧文件。无 JSON → 返回 None 作特例。"""
    import stamp, index, mdpages
    j = _find_legacy_json(src)
    if j is None:
        return None
    mddir = src.parent / MD_SUBDIR; mddir.mkdir(exist_ok=True)
    existing = mdpages.md_for(src)
    if existing is not None:
        return existing                                          # 幂等
    md_path = _md_path(src, mddir)
    today = datetime.date.today().isoformat()
    stats = {}
    md_text = stamp.stamp(j, f"../{src.name}", "migrated (legacy JSON)",
                          today, assets_stem=src.stem, stats=stats)
    if stats.get("warnings"):
        print(f"⚠️ [migrate] {src.name}: {stats['warnings']}", file=sys.stderr)
    md_path.write_text(md_text, encoding="utf-8")
    index.upsert(src.parent / INDEX_NAME, src.name, md_text.count("<!-- PAGE "),
                 f"{MD_SUBDIR}/{md_path.name}", "✅已转(迁移)", "migrated", today)
    return md_path


PURGE_REGEN = {"_model.json", "_layout.json", "block_list.json", "layout.json", "model.json"}


def purge_plan(uuid_dir: Path, case_root: Path):
    """步骤2：列去膨胀清单，标每项可删/保留（origin.pdf 查唯一副本）。不实际删除。"""
    plan = []
    for f in uuid_dir.iterdir():
        name = f.name
        if name.endswith("_origin.pdf"):
            # 原件能在案件内定位 → 冗余可删；否则疑似唯一副本，保留
            stem = uuid_dir.name.split(".pdf-")[0]
            found = list(case_root.rglob(stem + ".pdf"))
            plan.append({"file": name, "keep": not found,
                         "reason": "疑似唯一副本，保留" if not found else "原件有副本，可删"})
        elif any(name.endswith(s) for s in PURGE_REGEN):
            plan.append({"file": name, "keep": False, "reason": "中间产物，重跑可再生，可删"})
        elif name in ("content_list.json",) or "content_list" in name or name == "images":
            plan.append({"file": name, "keep": True, "reason": "盖页码用，保留存档"})
        else:
            plan.append({"file": name, "keep": True, "reason": "未知，默认保留待确认"})
    return plan


def build_parser():
    p = argparse.ArgumentParser(prog="cc-convert", description="源文档 → 轻量 Markdown")
    p.add_argument("target", help="单个源文件，或包含源文件的目录")
    p.add_argument("--force", action="store_true", help="已转也重转")
    p.add_argument("--scan", action="store_true", help="只扫描补全索引未转行，不转换")
    p.add_argument("--recursive", action="store_true", help="目录递归进子目录（默认仅单层）")
    p.add_argument("--migrate", action="store_true", help="对存量 legacy 用现有 JSON 盖页码迁移（步骤1）")
    p.add_argument("--full-assets", action="store_true", help="保留全部原始输出（含_raw/，且不过滤图片）")
    p.add_argument("--min-image-kb", type=int, default=DEFAULT_MIN_IMAGE_KB,
                   help=f"小于该体积(KB)的图片不保留，过滤图标/头像/水印（默认 {DEFAULT_MIN_IMAGE_KB}KB；0=不过滤）")
    p.add_argument("--no-images", action="store_true", help="完全不保留任何图片（仅留 OCR 文本 + JSON）")
    p.add_argument("-t", "--tier", "-b", "--backend", dest="tier", default=DEFAULT_TIER,
                   type=normalize_tier, metavar="TIER",
                   help=f"MinerU 4.0 质量档位 {TIERS}（默认 {DEFAULT_TIER}）；"
                        f"兼容 3.x 旧名 pipeline→basic / hybrid-*→standard / vlm-*→advanced")
    p.add_argument("--image-sources", action="store_true",
                   help="目录扫描时把图片（jpg/png/webp…微信截图、拍照证据）也当源文件 OCR；单文件指名图片无需此开关")
    p.add_argument("--to", metavar="FMT", choices=list(RENDER_FORMATS),
                   help=f"不重跑 OCR，从已转产物的 middle_json.json 重渲染为 {RENDER_FORMATS}（落 _md/<名>.<fmt>）")
    p.add_argument("--third-opinion", action="store_true",
                   help="配合 --review：额外用 advanced 档对入包页重识别作第三意见（默认不跑：实测漏行是版面没框到，"
                        "重识别补不回、约 13 s/页；草稿默认由 Vision 补行生成）")
    p.add_argument("--remote", action="store_true",
                   help="强制只在 Windows 3090 上原地转换（文件须在 cases 或 library 下、经 Drive 同步；产物经 Drive 同步回来）。"
                        "Windows 不可用时报错退出，不本地兜底。不加本参数时默认即先试 Windows、不可用回退本机。部署见 remote/README.md")
    p.add_argument("--local", action="store_true",
                   help="强制在 Mac 本机转换，不试 Windows（环境变量 CC_CONVERT_ROUTE=local 同效）")
    p.add_argument("--second-opinion", choices=["glm"],
                   help="配合 --review：另用 Windows 3090 上的 GLM-OCR 独立重识别，逐页与 MinerU 比对出分歧条写进复核包"
                        "（只提示、不进草稿、不写回；Windows 不可用时自动退回仅 Vision）。部署见 remote/README.md")
    p.add_argument("--review", action="store_true",
                   help="OCR 复核：挑存疑页跑 Vision 交叉，产出复核包（仅 PDF，不改主转换流程）")
    p.add_argument("--deep", action="store_true",
                   help="（2026-09-18 起为默认行为，保留兼容）配合 --review：对每页做 Vision 交叉")
    p.add_argument("--quick", action="store_true",
                   help="配合 --review：退回只核启发式存疑页 + 空/短页的旧模式（Vision 实测 40 页 12 s，一般无需）")
    p.add_argument("--apply-review", metavar="YAML",
                   help="把修正清单 YAML 就地写回 stamped MD（改页 + 溯源块 + 标 reviewed；仅单个 PDF）")
    p.add_argument("--force-drop-corrections", action="store_true",
                   help="配合 --force：允许重转清洗掉已核验修正（否则已复核文件 --force 会被拒绝）")
    return p


def remote_passthrough(args) -> list[str]:
    """转给 Windows 端 cc_convert 的参数（--remote 与自动路由共用）。"""
    out = [flag for flag, on in (("--force", args.force), ("--force-drop-corrections", args.force_drop_corrections),
                                 ("--recursive", args.recursive), ("--image-sources", args.image_sources),
                                 ("--full-assets", args.full_assets), ("--no-images", args.no_images)) if on]
    if args.tier != DEFAULT_TIER:
        out += ["-t", args.tier]
    if args.min_image_kb != DEFAULT_MIN_IMAGE_KB:
        out += ["--min-image-kb", str(args.min_image_kb)]
    return out


def auto_remote_wanted(args, target: Path) -> bool:
    """不加 --remote / --local 时是否先试 Windows（律师 2026-10-31 裁定：默认 Windows，不可用回退本机）。
    条件同时满足才试：Mac 上（Windows 端自身运行时不再 ssh 自己）；是转换任务；目标在 Drive 同步目录内；
    含需引擎 OCR 的待转件（PDF / 图片；docx 等原生解析本机几秒即完，不送）。"""
    import os
    if args.local or os.environ.get("CC_CONVERT_ROUTE", "").strip().lower() == "local":
        return False
    if sys.platform != "darwin":
        return False
    if args.scan or args.migrate or args.review or args.apply_review or args.to:
        return False
    import engine, remote_convert as _rc
    try:
        _rc.win_path(target)
    except _rc.RemoteConvertError:
        return False
    if not target.exists():
        return False
    return any(s.suffix.lower() in engine.TIERED_EXTS and (args.force or converted_state(s) == "new")
               for s in discover(target, args.recursive, image_sources=args.image_sources))


def main(argv=None):
    import stamp, index, convert_office
    args = build_parser().parse_args(argv)
    target = Path(args.target).expanduser().resolve()
    today = datetime.date.today().isoformat()
    if args.remote and args.local:
        print("[cc-convert] --remote 与 --local 不能同用", file=sys.stderr)
        return 2
    if args.remote:
        if args.scan or args.migrate or args.review or args.apply_review or args.to:
            print("[remote] --remote 只用于转换，不与 --scan/--migrate/--review/--apply-review/--to 同用", file=sys.stderr)
            return 2
        import remote_convert as _rc
        try:
            return _rc.run(target, remote_passthrough(args))
        except _rc.RemoteConvertError as e:
            print(f"[remote] {e}", file=sys.stderr)
            return 2
    if auto_remote_wanted(args, target):
        import remote_convert as _rc
        try:
            wp = _rc.dispatch_ready(target)
        except Exception as e:                      # 尚未派活：安全改在本机
            print(f"[route] Windows 不可用（{' '.join(str(e).split())}），本次在 Mac 本机转换")
        else:
            print("[route] Windows 可用，交 Windows 3090 转换（--local 可强制本机）")
            try:
                return _rc.run_dispatched(target, wp, remote_passthrough(args))
            except Exception as e:                  # 已派活：不在本机重做，避免两端同写 _md/
                print(f"[route] Windows 转换中途出错：{e}\n"
                      f"        未在本机重做（避免两端同时写 _md/）；确认 Windows 侧已停止后可加 --local 重跑", file=sys.stderr)
                return 2
    if not args.scan and not args.migrate and not args.review and not args.apply_review:
        # --to 也需要 mineru（渲染），一并检查
        try:
            from mineru.parser import parse  # noqa: F401
        except Exception as e:
            print(f"[cc-convert] MinerU 引擎不可用（mineru.parser 无法 import）：{e}\n"
                  f"请先完成 Phase 0（uv pip install 'mineru>=4.0,<5' + mineru-kit models download）。",
                  file=sys.stderr)
            return 2
    sources = discover(target, args.recursive, image_sources=args.image_sources)
    if not sources:
        print(f"[cc-convert] 未发现可转源文件：{target}"); return 1

    if args.to:
        import engine, mdpages as _mp
        rc = 0
        for src in sources:
            if src.suffix.lower() not in (PDF_EXTS | IMAGE_EXTS | NATIVE_FLASH_EXTS):
                print(f"[to] 跳过（原生 Office 解析件无 middle_json，重渲染仅限引擎转换件）：{src.name}"); continue
            md_path = _mp.md_for(src)
            if md_path is None:
                print(f"[to] 无已转 MD，先转换：{src.name}", file=sys.stderr); rc = 1; continue
            assets = md_path.parent / (md_path.stem + ".assets")
            mj = assets / "middle_json.json"
            if not mj.exists():
                print(f"[to] {src.name} 缺 {MD_SUBDIR}/{assets.name}/middle_json.json（3.x 时期转换件），"
                      f"需 --force 重转后再 --to", file=sys.stderr); rc = 1; continue
            out = md_path.parent / f"{md_path.stem}.{args.to if args.to != 'latex' else 'tex'}"
            try:
                data = engine.render_from_middle(mj, args.to, images_dir=assets / "images",
                                                 asset_base_url=f"{assets.name}/", title=src.stem)
            except Exception as e:
                print(f"[to] {src.name} 渲染失败：{e}", file=sys.stderr); rc = 1; continue
            if isinstance(data, bytes):
                out.write_bytes(data)
            else:
                out.write_text(data, encoding="utf-8")
            print(f"✅ [to] {src.name} → {MD_SUBDIR}/{out.name}（{out.stat().st_size // 1024} KB）")
        return rc

    if args.review:
        import review as _review
        for src in sources:
            if src.suffix.lower() not in (PDF_EXTS | IMAGE_EXTS):
                print(f"[review] 跳过非 PDF/图片：{src.name}"); continue
            if PURE_DIR in src.parts:
                print(f"[review] 纯净区跳过：{src.name}"); continue
            try:
                so_fn = None
                if args.second_opinion == "glm":
                    import second_opinion as _so
                    so_fn = _so.run_remote
                r = _review.build_review(src, deep=not args.quick, third_opinion=args.third_opinion,
                                         second_opinion_fn=so_fn)
                if r["suspect_pages"]:
                    print(f"⚠️ [review] {src.name}：存疑页 {r['suspect_pages']}"
                          f"（{r['reviewed_coverage']}）→ 复核包 {r['packet_path']}")
                    if r.get("vision_agrees_pages"):
                        n = len(r["vision_agrees_pages"])
                        print(f"   其中 {n} 页 Vision 交叉一致（疑为排版性启发式误报，"
                              f"已标 vision_agrees、按 score 沉底）：{r['vision_agrees_pages']}")
                    if r.get("drafted_pages"):
                        print(f"   已按 Vision 补行生成草稿 {r['draft_path']}（页 {r['drafted_pages']}；"
                              f"Vision 字对可能有错，对照 page-N.png 核定后 --apply-review）")
                    if r.get("advanced_recovered_pages"):
                        print(f"   advanced 档重识别在 {r['advanced_recovered_pages']} 页与 Vision 一致且更全")
                    if r.get("third_opinion_error"):
                        print(f"   ⚠️ 第三意见未跑成：{r['third_opinion_error']}", file=sys.stderr)
                    if r.get("second_opinion_pages"):
                        print(f"   GLM-OCR 第二意见在 {r['second_opinion_pages']} 页与 MinerU 有分歧"
                              f"（共核 {r['second_opinion_checked']} 页；分歧条见复核包 glm_flags，只提示、须对照原图裁断）")
                else:
                    print(f"✅ [review] {src.name}：未发现存疑页（{r['reviewed_coverage']}）")
                if args.second_opinion and r.get("second_opinion_error"):
                    print(f"⚠️ [review] {src.name}：GLM 第二意见未跑成，本次仅 Vision——{r['second_opinion_error']}",
                          file=sys.stderr)
                elif args.second_opinion and not r.get("second_opinion_pages"):
                    print(f"   GLM-OCR 第二意见：共核 {r.get('second_opinion_checked', 0)} 页，未发现分歧")
                if r.get("missing_pages"):
                    print(f"⚠️ [review] {src.name}：MD 缺 p.{r['missing_pages']} 页标记"
                          f"（早期转换丢页），修复需 --force 重转（corr.yaml 不能补页）", file=sys.stderr)
            except FileNotFoundError as e:
                print(f"[review] {e}", file=sys.stderr)
        return 0

    if args.apply_review:
        import apply_review as _apply, yaml as _yaml
        # 载入修正清单（缺文件/格式错/非映射 → 干净报错，不吐 traceback）
        try:
            spec = _yaml.safe_load(Path(args.apply_review).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, _yaml.YAMLError) as e:
            print(f"[apply-review] 无法读取修正清单 {args.apply_review}：{e}", file=sys.stderr)
            return 2
        if not isinstance(spec, dict):
            print(f"[apply-review] 修正清单格式错误（应为 YAML 映射）：{args.apply_review}", file=sys.stderr)
            return 2
        coverage = spec.get("coverage", "suspect")
        corrections = spec.get("corrections", [])
        # 一份修正清单只对应一份文书：目标须恰好解析为一个 PDF，
        # 杜绝把某文书的「按页」修正误写进目录下其它 PDF（法律文件写坏风险）。
        pdfs = [s for s in sources if s.suffix.lower() in (PDF_EXTS | IMAGE_EXTS) and PURE_DIR not in s.parts]
        if len(pdfs) != 1:
            print(f"[apply-review] 需指向单个 PDF/图片（当前匹配 {len(pdfs)} 个）；"
                  f"修正清单只对应一份文书。", file=sys.stderr)
            return 2
        src = pdfs[0]
        import mdpages as _mp
        md_path = _mp.md_for(src)
        if md_path is None:
            print(f"[apply-review] 无 MD，先转换：{src.name}", file=sys.stderr); return 1
        md_text = md_path.read_text(encoding="utf-8")
        # 审计提示（不阻断，单次全量复核模型）：增量复核会覆盖旧溯源表；改写却空 changes 则该页无审计行。
        # 无论如何，原始 OCR 存档始终留在 _md/<stem>.assets/content_list.json，可回溯。
        if _mp.parse_front_matter(md_text).get("reviewed"):
            print(f"⚠️ [apply-review] {src.name} 已复核过，本次以新清单覆盖溯源表；"
                  f"原始 OCR 存档见 {MD_SUBDIR}/{md_path.stem}.assets/（content_list.json）。", file=sys.stderr)
        _no_audit = [c.get("page") for c in corrections if c.get("corrected_text") and not c.get("changes")]
        if _no_audit:
            print(f"⚠️ [apply-review] 第 {_no_audit} 页有改写但未列 changes，溯源表将缺该页审计行。",
                  file=sys.stderr)
        # 写回失败（如页数不一致/引用不存在页码）→ 干净报错，原 MD 不动
        try:
            new_md = _apply.apply_corrections(md_text, corrections, coverage, today)
        except Exception as e:
            print(f"[apply-review] {src.name} 写回失败（未改动原 MD）：{e}", file=sys.stderr)
            return 1
        md_path.write_text(new_md, encoding="utf-8")
        index.upsert(src.parent / INDEX_NAME, src.name, new_md.count("<!-- PAGE "),
                     f"{MD_SUBDIR}/{md_path.name}", f"✅已复核({coverage})",
                     f"{ENGINE_LABEL}+review", today)
        print(f"✅ [apply-review] {src.name}：{len(corrections)} 处修正已写回（{coverage}）")
        return 0

    if args.migrate:
        for src in sources:
            if PURE_DIR in src.parts: continue
            r = migrate_one(src)
            print(f"[migrate] {src.name} → {r}" if r else f"[migrate] 无JSON特例，需 --force 重转：{src.name}")
        # 步骤2：列去膨胀清单（逐案确认，不自动删）。单文件 target 无 UUID 目录可扫，跳过。
        if target.is_dir():
            uuid_dirs = target.rglob("*.pdf-*-*-*-*-*") if args.recursive else target.glob("*.pdf-*-*-*-*-*")
            for d in uuid_dirs:
                if d.is_dir():
                    print(f"\n[去膨胀清单] {d}")
                    for item in purge_plan(d, case_root=target):
                        print(f"   {'保留' if item['keep'] else '可删'}  {item['file']}  — {item['reason']}")
        print("\n[cc-convert] migrate 步骤1完成。步骤2清单已列，确认后手动删除。")
        return 0

    failed = []
    for src in sources:
        if PURE_DIR in src.parts:                        # 纯净区：连派生件都不写
            print(f"[skip] 纯净区不写派生件：{src.name}"); continue
        idx = src.parent / INDEX_NAME
        state = converted_state(src)
        if args.scan:
            if state == "new":
                index.upsert(idx, src.name, "—", "—", "⬜未转", "—", "—")
            continue
        if state == "done" and args.force and not args.force_drop_corrections:
            import mdpages as _mp
            md_path = _mp.md_for(src)
            if md_path is not None and _mp.parse_front_matter(
                    md_path.read_text(encoding="utf-8")).get("reviewed"):
                print(f"⚠️ [skip] 已复核，--force 将清洗修正；确需重转请加 "
                      f"--force-drop-corrections：{src.name}"); continue
        if state == "done" and not args.force:
            print(f"[skip] 已转：{src.name}"); continue
        if state == "legacy" and not args.force:
            index.upsert(idx, src.name, "—", "(legacy)", "🔵legacy", "—", "—")
            print(f"[legacy] 存量已转，跳过（--migrate 可迁移盖页码）：{src.name}"); continue
        ext = src.suffix.lower()
        try:
            native_ok = False
            if ext in OFFICE_EXTS:                       # ── Office 原生分流（失败则兜底走 4.0 引擎 flash）──
                print(f"⏳ 正在原生解析 {src.name} …")
                try:
                    md_text = convert_office.to_markdown(src, f"../{src.name}", "native-docx", today)
                    native_ok = True
                except Exception as e:
                    print(f"⚠️ 原生解析失败（{type(e).__name__}: {str(e)[:80]}），改走 MinerU flash 兜底：{src.name}",
                          file=sys.stderr)
            if native_ok:
                md_path = place_office(src, md_text)
                sec = md_text.split("sections:")[1].split("\n")[0].strip()
                index.upsert(idx, src.name, f"—（{sec}节）", f"{MD_SUBDIR}/{md_path.name}",
                             "✅已转", "native-docx", today)
                print(f"✅ 已转 → {MD_SUBDIR}/{md_path.name}（{sec} 节）")
            else:                                        # ── 引擎分流：PDF / 图片 / OFD·旧Office·RTF·ODF·EPUB·HTML / Office 兜底 ──
                import engine, pdf_render      # 函数内 lazy import：scan_pending.py 需 cc_convert 保持 stdlib-only
                tier = engine.tier_for(src, args.tier)
                label = f"{ENGINE_LABEL} ({tier}{', fallback' if ext in OFFICE_EXTS else ''})"
                print(f"⏳ 正在 MinerU 转换 {src.name}（tier={tier}）…")
                # Windows 上 "/tmp" 是当前盘根（DSH 桌面版沙箱写不进，普通环境也会往 C:\tmp 落垃圾），改用系统临时目录；Mac 不变
                tmp_root = Path("/tmp/cc-convert") if sys.platform != "win32" else Path(tempfile.gettempdir()) / "cc-convert"
                tmp = tmp_root / src.stem
                if tmp.exists(): shutil.rmtree(tmp)
                run_mineru(src, tmp, tier)
                page_json, images = locate_outputs(tmp)
                if ext in IMAGE_EXTS:
                    expected, tl = 1, "none"             # 图片：恒一页、无文本层（拍照/截图证据默认建议复核）
                elif ext in PDF_EXTS:
                    expected = pdf_render.page_count(src)
                    if expected is None:
                        print(f"[cc-convert] 无法读取源 PDF 页数，跳过页数守恒检查：{src.name}", file=sys.stderr)
                    tl = pdf_render.detect_text_layer(src)
                else:
                    expected, tl = None, None            # OFD/Office 等：页=逻辑页，无守恒基准、无文本层概念
                stats = {}
                md_text = stamp.stamp(page_json, f"../{src.name}", label, today,
                                      assets_stem=src.stem, stats=stats,
                                      expected_pages=expected, text_layer=tl)
                if stats.get("warnings"):
                    print(f"⚠️ {src.name}: {stats['warnings']}", file=sys.stderr)
                min_bytes = 0 if args.full_assets else max(0, args.min_image_kb) * 1024
                md_path = place_pdf(src, page_json, images, md_text, args.full_assets,
                                    min_image_bytes=min_bytes, no_images=args.no_images)
                pages = md_text.count("<!-- PAGE ")
                index.upsert(idx, src.name, pages, f"{MD_SUBDIR}/{md_path.name}", "✅已转", label, today)
                print(f"✅ 已转 → {MD_SUBDIR}/{md_path.name}（{pages} 页）")
                hint = _review_hint(stats.get("warnings", ""), tl)
                if hint:
                    print(f"💡 {hint}：./cc-convert '{src}' --review")
                shutil.rmtree(tmp, ignore_errors=True)
        except Exception as e:
            index.upsert(idx, src.name, "—", "—", "⚠️失败", "—", today)
            print(f"[fail] {src.name}: {e}", file=sys.stderr)
            failed.append(src.name)
    if failed:                                           # 有失败必须非零退出，调用方（脚本 / agent）才能据退出码判定
        print(f"[cc-convert] 结束，{len(failed)} 个失败：{'、'.join(failed)}"); return 1
    print("[cc-convert] 完成。"); return 0


if __name__ == "__main__":
    sys.exit(main())
