#!/usr/bin/env python3
"""scan_pending.py — OCR 巡检：列出案件区未转换的源文档。只读，不写任何文件。

SessionStart hook 调用，扫描范围由 CWD 决定：
  CWD 不在 cases 之下 → 静默退出（零输出，对其他项目无感）
  CWD = cases 根      → 全区扫描（目录级 mtime 过滤，默认近 30 天）
  CWD 在某案件子树内     → 仅扫该案件（不做 mtime 过滤，全量判定）
未转判定复用 cc_convert.converted_state 三闸（单一判定源，与转换工具永不分裂）。
"""
import argparse, json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cc_convert as cc

CASES_ROOT = Path(os.environ.get("CC_CASES_ROOT") or "/Users/user/Documents/cases")  # Windows 端（DSH）经环境变量覆盖
# 扫描排除区（前缀匹配；≠禁止转换——用户手动转换这些目录内文件仍照常）
EXCLUDE_PREFIXES = ("1.签约信息", "2.事务资料", "4.录音资料", "1.零散文件")
SELF_MADE_PREFIXES = ("CC-", "CDX-", "DSH-", "TC-")   # 自产文书（Claude / Codex / DSH 产物、律师自拟），不提醒（用户决策 2026-06-11；CDX/DSH 2026-10-21 加）
EXCLUDE_FILENAMES = ("案件进度信息记录表",)   # 历史遗留统计文件（CLAUDE.md：不主动读取），不提醒
MAX_CASE_LINES = 10         # 提醒最多列出的案件行数，超出截断
DEFAULT_DAYS = 30           # 全区扫描的目录 mtime 过滤窗口（天）


def _dir_excluded(name: str) -> bool:
    """单个目录名是否属扫描排除区（os.walk 剪枝用）。"""
    if name.startswith(EXCLUDE_PREFIXES):
        return True
    if name == cc.PURE_DIR:                          # 真·纯净区
        return True
    if name.startswith(("_", ".")):                  # _md/_工具/.git 等
        return True
    if name.endswith(".assets"):
        return True
    if ".pdf-" in name and name.count("-") >= 4:     # MinerU UUID 目录
        return True
    return False


def find_pending(scan_root: Path, days: int = 0, skip_root_files: bool = False):
    """遍历 scan_root，返回未转源文件列表（排序稳定）。

    days>0 时启用目录级 mtime 过滤：仅对自身 mtime 在窗口内的目录做
    文件级三闸判定，但所有目录都继续下钻（新文件只更新其直接父目录的
    mtime，按案件根 mtime 过滤会漏报——spec §3.2 ⚠️）。
    skip_root_files=True 时跳过 scan_root 直挂的文件——全区扫描场景：
    根级散件（统计表/格式模板）非案件资料，不提醒（用户决策 2026-06-11）。
    """
    cutoff = (time.time() - days * 86400) if days > 0 else None
    pending = []
    for cur, dirnames, filenames in os.walk(scan_root):
        dirnames[:] = sorted(d for d in dirnames if not _dir_excluded(d))
        if skip_root_files and Path(cur) == scan_root:
            continue
        try:
            if cutoff is not None and os.stat(cur).st_mtime < cutoff:
                continue                             # 本目录无近期改动→跳过文件判定，仍下钻
        except OSError:
            continue                                 # 目录消失/权限异常→跳过，不中断扫描
        d = Path(cur)
        for fn in sorted(filenames):
            if fn.startswith(("~$", ".")):           # Office 锁文件/隐藏文件
                continue
            if fn.startswith(SELF_MADE_PREFIXES + EXCLUDE_FILENAMES):    # 自产文书/遗留统计件不提醒（手动转换不受限）
                continue
            f = d / fn
            if f.suffix.lower() not in cc.SOURCE_EXTS:
                continue
            if cc.converted_state(f) == "new":       # done/legacy 均视为已转
                pending.append(f)
    return pending


def resolve_scope(cwd: Path, root: Path):
    """返回 (scan_root, use_mtime_filter)。cwd 不在 root 之下 → (None, False)。
    案件目录 = cwd 相对 root 的第一级子目录（spec §3.2）。"""
    try:
        rel = cwd.resolve().relative_to(root.resolve())
    except ValueError:
        return None, False
    if rel == Path("."):
        return root, True
    if _dir_excluded(rel.parts[0]):
        return None, False                           # CWD 在排除区（如 _工具）→ 静默
    case_dir = root / rel.parts[0]
    if case_dir.is_dir():
        return case_dir, False                       # 单案扫描：全量判定，不过滤
    return root, True                                # 兜底：按全区+过滤处理


def _case_of(rel_dir: str) -> str:
    """目录相对路径 → 所属案件名；根级（"."）归入「（根目录）」（防御分支）。"""
    parts = Path(rel_dir).parts
    return parts[0] if parts else "（根目录）"


def format_report(pending, root: Path, max_lines: int = MAX_CASE_LINES) -> str:
    """按案件聚合的提醒文本；无未转 → 空串（hook 零注入）。"""
    if not pending:
        return ""
    by_case = {}
    for f in pending:
        case = _case_of(str(f.parent.relative_to(root)))
        by_case.setdefault(case, []).append(f.name)
    lines = [f"📋 OCR 巡检：发现 {len(by_case)} 个案件共 {len(pending)} 份未转换资料"]
    cases = sorted(by_case)
    for i, k in enumerate(cases):
        if i >= max_lines:
            rest_files = sum(len(by_case[x]) for x in cases[i:])
            lines.append(f"- ……另有 {len(cases) - i} 个案件共 {rest_files} 份（scan_pending.py --all 可列全）")
            break
        names = by_case[k]
        suffix = " 等" if len(names) > 1 else ""
        lines.append(f"- {k}：{len(names)} 份（{names[0]}{suffix}）")
    lines.append('提示：对我说"转换某案件资料"即可处理（单文件自动转，批量会先确认）。')
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="OCR 巡检：列出案件区未转换源文档（只读）")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"全区扫描的目录 mtime 过滤窗口（天；0=不过滤；默认 {DEFAULT_DAYS}）")
    ap.add_argument("--all", action="store_true", help="不截断，列出全部案件")
    ap.add_argument("--hook", action="store_true",
                    help="SessionStart hook 模式：输出 JSON（systemMessage 用户可见 + additionalContext 进上下文）")
    ap.add_argument("--root", default=str(CASES_ROOT), help=argparse.SUPPRESS)  # 测试注入用
    ap.add_argument("--cwd", default=None, help=argparse.SUPPRESS)                 # 测试注入用
    args = ap.parse_args(argv)
    root = Path(args.root)
    cwd = Path(args.cwd) if args.cwd else Path.cwd()
    scan_root, use_filter = resolve_scope(cwd, root)
    if scan_root is None:
        return 0                                     # 路径守卫：静默退出
    pending = find_pending(scan_root,
                           days=args.days if use_filter else 0,
                           skip_root_files=use_filter)   # 全区扫描不报根级散件
    report = format_report(pending, root,
                           max_lines=10**9 if args.all else MAX_CASE_LINES)
    if not report:
        return 0
    if args.hook:
        print(json.dumps({
            "systemMessage": report,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": report,
            },
        }, ensure_ascii=False))
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
