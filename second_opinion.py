"""second_opinion.py — 复核层「第二意见」：Windows 3090 上的 GLM-OCR 独立重识别 + 逐页分歧比对。

定位（2026-10-20 比测，报告 ~/claude-skills/_analysis/2026-10-20-OCR第二引擎比测.md）：
- GLM-OCR 对 MinerU 漏读/误读的捕获率 94%（剔除低置信页 69%），每页误报约 2.5 处，优于 Apple Vision；
- 但视觉大模型在看不清的页会编造通顺假文字 → 本模块**只产出分歧条供人工裁断**，
  GLM 文本绝不进 corr.draft.yaml、绝不写回 MD。

两部分：
1. diff_flags(mineru_text, glm_text)：纯函数，归一化后逐字对齐，滤掉「整段在对方别处出现」的移位
   （表格阅读顺序不同），输出分歧条。
2. RemoteGLM：经 ssh/scp 把页面图送到 Windows 作业目录（Drive 外），确保服务在跑，取回结果，
   finally 删除远端图片。Windows 不在线 / 未登录 / 显存不足 → 抛 SecondOpinionUnavailable，
   上层 fail-soft 退回仅 Vision。
"""
from __future__ import annotations

import base64
import difflib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path

# ── 1. 分歧比对 ─────────────────────────────────────────────────────────────
_TAG = re.compile(r"<[^>]+>")
_IMG = re.compile(r"!\[[^\]]*\]\((?:[^()]|\([^()]*\))*\)")        # 图片链接（路径里可能有「(1)」这类括号）
_MD = re.compile(r"(^|\n)\s*#{1,6}\s*|[*`|>$\\]")
_KEEP = re.compile(r"[0-9A-Za-z㐀-鿿豈-﫿]")
_MIN_INS = 4          # 单边多出的片段 ≥ 4 字才算（短的多为标点/页码噪声）
_MIN_MINERU_ONLY = 40 # MinerU 独有片段只报「长且高度重复」的（复读/循环输出，如 167168169…）：
_REPEAT_RATIO = 0.3   # 不同字符数 / 长度 < 0.3 视为复读。GLM 按设计不出页眉页脚，正常的 MinerU 独有内容不报
_MAX_FLAGS = 40


def normalize(text: str) -> str:
    """去 HTML/Markdown 标记、NFKC、只留汉字/字母/数字、小写。标点与空白差异不算分歧。"""
    t = html.unescape(_TAG.sub(" ", text or ""))
    t = _IMG.sub(" ", t)
    t = _MD.sub(" ", t)
    t = unicodedata.normalize("NFKC", t)
    return "".join(ch for ch in t if _KEEP.match(ch)).lower()


def diff_flags(mineru_text: str, glm_text: str, max_flags: int = _MAX_FLAGS) -> list[dict]:
    """逐页分歧条。kind：glm_only（GLM 读到、MinerU 没有 → MinerU 可能漏读）、
    differ（同一位置读法不同 → 至少一方认错）、mineru_only（MinerU 独有的长段高度重复内容 → 可能复读/循环输出）。
    GLM 无输出（认读失败）时返回空列表，不把整页当分歧。"""
    a, b = normalize(mineru_text), normalize(glm_text)
    if not b:
        return []
    flags = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        ma, gb = a[i1:i2], b[j1:j2]
        if tag == "insert" and (len(gb) < _MIN_INS or gb in a):
            continue
        if tag == "delete" and (len(ma) < _MIN_MINERU_ONLY or ma in b or len(set(ma)) / len(ma) >= _REPEAT_RATIO):
            continue
        if tag == "replace" and ma and gb and ma in b and gb in a:
            continue                                    # 双方都在对方别处出现：纯移位
        kind = {"insert": "glm_only", "delete": "mineru_only", "replace": "differ"}[tag]
        flags.append({"kind": kind, "mineru": ma[:120], "glm": gb[:120],
                      "context": f"{a[max(0, i1 - 10):i1]}【】{a[i2:i2 + 10]}"})
    return flags[:max_flags]


# ── 2. 远程执行（Windows 3090） ─────────────────────────────────────────────
class SecondOpinionUnavailable(RuntimeError):
    """Windows 不可用（离线/未登录/显存不足/服务起不来）——上层应退回仅 Vision。"""


HOST = os.environ.get("CC_SO_HOST", "win")
JOB_ROOT_WIN = os.environ.get("CC_SO_JOB_ROOT_WIN", r"D:\ocr-lab\so")       # Drive 外
JOB_ROOT_WSL = os.environ.get("CC_SO_JOB_ROOT_WSL", "/mnt/d/ocr-lab/so")
SCRIPT_DIR_WSL = os.environ.get("CC_SO_SCRIPT_DIR_WSL", "/mnt/c/cc/cc-convert/remote/win")
PY_WSL = "/opt/ocr/glm/bin/python"
TASK = "ocr-glm-server"
RENDER_SCALE = 200 / 72          # 与比测一致：200 DPI
MIN_FREE_MB = 9000               # GLM 服务约占 11 GB（gpu-memory-utilization 0.45），预留余量


def _ps(script: str, timeout: int = 120) -> str:
    """在 Windows 上跑一段 PowerShell（EncodedCommand，UTF-8 输出），返回 stdout 文本。"""
    full = '[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference="SilentlyContinue"\n' + script
    b64 = base64.b64encode(full.encode("utf-16-le")).decode()
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", HOST,
                        f"powershell -NoProfile -EncodedCommand {b64}"],
                       capture_output=True, timeout=timeout)
    out = r.stdout.decode("utf-8", "replace").replace("\x00", "")
    if r.returncode not in (0, 1):
        raise SecondOpinionUnavailable(f"ssh 失败（rc={r.returncode}）：{r.stderr.decode('utf-8', 'replace')[:200]}")
    return "\n".join(l for l in out.splitlines() if l.strip() and not l.startswith(("#< CLIXML", "<Objs")))


def _kv(text: str) -> dict:
    return dict(l.split("=", 1) for l in text.splitlines() if "=" in l)


def preflight() -> dict:
    """派活前检查：在线、有人登录（计划任务与 Drive 依赖桌面会话）、显存余量、服务是否已在跑。"""
    try:
        info = _kv(_ps(
            "'user=' + (Get-CimInstance Win32_ComputerSystem).UserName\n"
            "'free_mb=' + (nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits)\n"
            "'health=' + $(try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8080/health).StatusCode } catch { 0 })\n"
            f"'task=' + [bool](Get-ScheduledTask -TaskName {TASK} -ErrorAction SilentlyContinue)", timeout=40))
    except (subprocess.TimeoutExpired, OSError) as e:
        raise SecondOpinionUnavailable(f"Windows 不在线或 ssh 不通：{e}")
    if not info.get("user", "").strip():
        raise SecondOpinionUnavailable("Windows 无人登录（重启后未登录桌面）")
    if info.get("task", "").strip().lower() != "true":
        raise SecondOpinionUnavailable(f"Windows 未注册计划任务 {TASK}（见 remote/README.md）")
    running = info.get("health", "0").strip() == "200"
    free = int(re.sub(r"\D", "", info.get("free_mb", "0")) or 0)
    if not running and free < MIN_FREE_MB:
        raise SecondOpinionUnavailable(f"显存余量 {free} MB 不足 {MIN_FREE_MB} MB（显卡被占用）")
    return {"running": running, "free_mb": free}


def _ensure_server(wait_s: int = 240) -> None:
    _ps(f"Start-ScheduledTask -TaskName {TASK}", timeout=40)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        h = _kv(_ps("'health=' + $(try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "
                    "http://127.0.0.1:8080/health).StatusCode } catch { 0 })", timeout=30))
        if h.get("health", "").strip() == "200":
            return
        time.sleep(8)
    raise SecondOpinionUnavailable(f"GLM 服务 {wait_s}s 内未就绪（看 WSL /opt/ocr/vllm_glm.log）")


def run_remote(pdf_path, pages: list[int], timeout: int = 1800) -> dict[int, str]:
    """把 pdf_path 的指定页（1-based）渲染成 200 DPI PNG，送 Windows 跑 GLM-OCR，返回 {页码: 文本}。
    远端作业目录在 Drive 外，结束后（无论成败）删除。"""
    import pdf_render
    if not pages:
        return {}
    pre = preflight()
    job = f"so-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    tmp = Path(tempfile.mkdtemp(prefix="cc-so-"))
    try:
        for p in pages:
            src = pdf_render.render_page_png(pdf_path, p - 1, tmp, scale=RENDER_SCALE)
            src = Path(src)
            if src.name != f"page-{p}.png":
                src.rename(tmp / f"page-{p}.png")
        _ps(f"New-Item -ItemType Directory -Force '{JOB_ROOT_WIN}\\{job}' | Out-Null", timeout=40)
        remote = JOB_ROOT_WIN.replace("\\", "/") + "/" + job + "/"
        r = subprocess.run(["scp", "-q", *[str(f) for f in sorted(tmp.glob("page-*.png"))], f"{HOST}:{remote}"],
                           capture_output=True, timeout=600)
        if r.returncode != 0:
            raise SecondOpinionUnavailable(f"图片上传失败：{r.stderr.decode('utf-8', 'replace')[:200]}")
        if not pre["running"]:
            _ensure_server()
        out = _ps(f"$r = wsl.exe -d Ubuntu -u root -- {PY_WSL} {SCRIPT_DIR_WSL}/glm_pages.py {JOB_ROOT_WSL}/{job} 2>&1 | Out-String\n"
                  "($r -replace \"`0\",\"\").Trim()", timeout=timeout)
        r = subprocess.run(["scp", "-q", f"{HOST}:{remote}out.json", str(tmp / "out.json")],
                           capture_output=True, timeout=120)
        if r.returncode != 0 or not (tmp / "out.json").exists():
            raise RuntimeError(f"GLM 作业无结果：{out[-300:]}")
        data = json.loads((tmp / "out.json").read_text(encoding="utf-8"))
        return {int(k): v for k, v in data.get("pages", {}).items()}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            _ps(f"Remove-Item -Recurse -Force '{JOB_ROOT_WIN}\\{job}' -ErrorAction SilentlyContinue", timeout=40)
        except Exception:
            pass
