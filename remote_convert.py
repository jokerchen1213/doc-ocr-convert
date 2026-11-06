"""remote_convert.py — `cc-convert <文件或目录> --remote`：在 Windows 3090 上原地转换。

案卷目录（cases）与学习资料目录（library）经群晖 Drive 双向同步，Windows 已有同一份源文件
（D:\\SyncDrive\\cases\\… / D:\\SyncDrive\\library\\…），
所以不传文件：等 Windows 侧同步到与 Mac 同样大小 → ssh 让 Windows 用自己的 cc-convert（经 C:\\cc 英文路径，
中文路径会让 llama.cpp 报错）原地转换 → 产物（_md/）经 Drive 同步回 Mac。
2026-10-20 实测：Windows MinerU standard 3.6 s/页，Mac 14.8 s/页。

前置：Windows 在线、有人登录、Drive 客户端在跑（同步依赖桌面会话）。只支持 SYNC_ROOTS 登记的同步目录下的文件。
2026-10-31 起（律师裁定）：不加参数时 cc_convert 先调 dispatch_ready，通过才派 Windows，不通过自动改在 Mac 本机转换；
`--remote` 强制只用 Windows（不满足即报错退出）；`--local` 强制本机。派活之后再出错一律报错，不在本机重做。
"""
from __future__ import annotations

import time
from pathlib import Path

import second_opinion as _so            # 复用 ssh/PowerShell 通道

CASES_MAC = Path.home() / "Documents" / "cases"
CASES_WIN = r"D:\SyncDrive\cases"
LIBRARY_MAC = Path.home() / "Documents" / "library"
LIBRARY_WIN = r"D:\SyncDrive\library"
# Mac 目录 → Windows 目录（均经群晖 Drive 同步）。新增同步目录在此登记。
SYNC_ROOTS = [(CASES_MAC, CASES_WIN), (LIBRARY_MAC, LIBRARY_WIN)]
CC_WIN_PY = r"C:\cc\cc-convert\.venv\Scripts\python.exe"
CC_WIN_MAIN = r"C:\cc\cc-convert\cc_convert.py"


class RemoteConvertError(RuntimeError):
    pass


def _q(s: str) -> str:                   # PowerShell 单引号字面量
    return "'" + s.replace("'", "''") + "'"


def win_path(local: Path) -> str:
    lp = local.resolve()
    for mac, win in SYNC_ROOTS:
        try:
            rel = lp.relative_to(mac.resolve())
        except ValueError:
            continue
        return win + ("\\" + str(rel).replace("/", "\\") if str(rel) != "." else "")
    names = " / ".join(m.name for m, _ in SYNC_ROOTS)
    raise RemoteConvertError(f"--remote 只支持 {names} 目录下的文件（经 Drive 同步）：{local}")


def preflight() -> None:
    try:
        info = _so._kv(_so._ps(
            "'user=' + (Get-CimInstance Win32_ComputerSystem).UserName\n"
            "'drive=' + ((Get-Process cloud-drive-daemon -ErrorAction SilentlyContinue | Measure-Object).Count)", timeout=40))
    except Exception as e:
        raise RemoteConvertError(f"Windows 不在线或 ssh 不通：{e}")
    if not info.get("user", "").strip():
        raise RemoteConvertError("Windows 无人登录（重启后未登录桌面，Drive 不同步）")
    if info.get("drive", "0").strip() in ("", "0"):
        raise RemoteConvertError("Windows 上群晖 Drive 客户端未运行，源文件与产物无法同步")


def _wait_synced(local: Path, wp: str, wait_s: int) -> None:
    """单文件：等 Windows 侧大小与 Mac 一致（Drive 同步完成）。目录：只确认存在。"""
    want = local.stat().st_size if local.is_file() else None
    deadline = time.time() + wait_s
    while True:
        out = _so._kv(_so._ps(f"$i = Get-Item -LiteralPath {_q(wp)} -ErrorAction SilentlyContinue\n"
                              "'exists=' + [bool]$i\n'size=' + $(if ($i -and -not $i.PSIsContainer) { $i.Length } else { -1 })",
                              timeout=40))
        ok = out.get("exists", "").strip().lower() == "true" and (want is None or out.get("size", "").strip() == str(want))
        if ok:
            return
        if time.time() > deadline:
            raise RemoteConvertError(f"等待 {wait_s}s 后 Windows 侧仍未同步到该文件：{wp}")
        time.sleep(5)


def dispatch_ready(target: Path, sync_wait: int = 180) -> str:
    """派活前的全部检查（同步目录 + Windows 预检 + 等 Drive 同步到位），通过返回 Windows 路径。
    任何一步不满足都抛 RemoteConvertError，此时尚未向 Windows 派活，调用方可安全改在本机转换。"""
    target = Path(target).expanduser().resolve()
    wp = win_path(target)
    preflight()
    _wait_synced(target, wp, sync_wait)
    return wp


def run(target: Path, passthrough: list[str], sync_wait: int = 180, timeout: int = 7200) -> int:
    target = Path(target).expanduser().resolve()
    wp = dispatch_ready(target, sync_wait)
    return run_dispatched(target, wp, passthrough, sync_wait, timeout)


def run_dispatched(target: Path, wp: str, passthrough: list[str], sync_wait: int = 180, timeout: int = 7200) -> int:
    """已过 dispatch_ready：让 Windows 原地转换并等产物同步回 Mac。此后出错不在本机重做（避免两端同写 _md/）。"""
    target = Path(target).expanduser().resolve()
    md_local = target.parent / "_md" / (target.stem + ".md") if target.is_file() else None
    before = md_local.stat().st_mtime if md_local and md_local.exists() else 0.0
    args = " ".join(_q(a) for a in passthrough)
    t0 = time.time()
    out = _so._ps("$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONIOENCODING='utf-8'\n"
                  f"& {_q(CC_WIN_PY)} {_q(CC_WIN_MAIN)} {_q(wp)} {args} 2>&1 | "
                  "% { \"$_\" } | ? { $_ -notmatch 'Warning|warn|it/s\\]|RemoteException|^\\s*$' } | Select-Object -Last 15", timeout=timeout)
    print(f"[remote] Windows 转换用时 {time.time() - t0:.0f}s：")
    for line in out.splitlines():
        print(f"  {line}")
    if md_local is None:
        print("[remote] 目录转换：产物经 Drive 同步回 Mac，通常有数十秒延迟")
        return 0
    deadline = time.time() + sync_wait
    while time.time() < deadline:
        if md_local.exists() and md_local.stat().st_mtime > before:
            s1 = md_local.stat().st_size; time.sleep(3)
            if md_local.stat().st_size == s1:
                print(f"✅ [remote] 产物已同步回 Mac：{md_local}")
                return 0
        time.sleep(5)
    print(f"⚠️ [remote] {sync_wait}s 内产物未同步回 Mac（或本次未重转：已转文件需 --force）：{md_local}")
    return 1
