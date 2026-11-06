"""默认路由：先试 Windows，不可用回退本机（律师 2026-10-31 裁定）。全部 mock，不真连 ssh。"""
import sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import cc_convert
import remote_convert as rc


@pytest.fixture
def synced(tmp_path, monkeypatch):
    """把 tmp_path 登记成 Drive 同步目录，并假装跑在 Mac 上。"""
    monkeypatch.setattr(rc, "SYNC_ROOTS", [(tmp_path, r"D:\fake")])
    monkeypatch.setattr(cc_convert.sys, "platform", "darwin")
    monkeypatch.delenv("CC_CONVERT_ROUTE", raising=False)
    return tmp_path


def _args(target, *extra):
    return cc_convert.build_parser().parse_args([str(target), *extra])


def _pdf(d, name="a.pdf"):
    p = d / name
    p.write_bytes(b"%PDF-1.4 fake")
    return p


def test_wanted_for_new_pdf_in_synced_dir(synced):
    pdf = _pdf(synced)
    assert cc_convert.auto_remote_wanted(_args(pdf), pdf) is True
    assert cc_convert.auto_remote_wanted(_args(synced), synced) is True     # 目录含待转 PDF


@pytest.mark.parametrize("extra", [["--local"], ["--scan"], ["--review"], ["--to", "docx"], ["--migrate"]])
def test_not_wanted_for_flags(synced, extra):
    pdf = _pdf(synced)
    assert cc_convert.auto_remote_wanted(_args(pdf, *extra), pdf) is False


def test_not_wanted_env_off(synced, monkeypatch):
    pdf = _pdf(synced)
    monkeypatch.setenv("CC_CONVERT_ROUTE", "local")
    assert cc_convert.auto_remote_wanted(_args(pdf), pdf) is False


def test_not_wanted_on_windows(synced, monkeypatch):
    pdf = _pdf(synced)
    monkeypatch.setattr(cc_convert.sys, "platform", "win32")    # Windows 端自身运行时不能 ssh 自己
    assert cc_convert.auto_remote_wanted(_args(pdf), pdf) is False


def test_not_wanted_outside_sync_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "SYNC_ROOTS", [(tmp_path / "elsewhere", r"D:\fake")])
    monkeypatch.setattr(cc_convert.sys, "platform", "darwin")
    pdf = _pdf(tmp_path)
    assert cc_convert.auto_remote_wanted(_args(pdf), pdf) is False


def test_not_wanted_for_native_office_only(synced):
    (synced / "a.docx").write_bytes(b"PK fake")
    assert cc_convert.auto_remote_wanted(_args(synced), synced) is False


def test_not_wanted_when_already_converted(synced, monkeypatch):
    pdf = _pdf(synced)
    monkeypatch.setattr(cc_convert, "converted_state", lambda s: "done")
    assert cc_convert.auto_remote_wanted(_args(pdf), pdf) is False
    assert cc_convert.auto_remote_wanted(_args(pdf, "--force"), pdf) is True


@pytest.fixture
def fake_mineru(monkeypatch):
    m = types.ModuleType("mineru"); mp = types.ModuleType("mineru.parser"); mp.parse = None
    monkeypatch.setitem(sys.modules, "mineru", m)
    monkeypatch.setitem(sys.modules, "mineru.parser", mp)


def test_main_falls_back_locally_when_not_ready(synced, monkeypatch, capsys, fake_mineru):
    pdf = _pdf(synced)
    def not_ready(t, *a, **k):
        raise rc.RemoteConvertError("Windows 不在线或 ssh 不通：timeout")
    monkeypatch.setattr(rc, "dispatch_ready", not_ready)
    monkeypatch.setattr(rc, "run_dispatched", lambda *a, **k: pytest.fail("不应派活"))
    calls = iter([[pdf], []])          # 第一次供路由判定；第二次本机转换时返回空，截断后续真转换
    monkeypatch.setattr(cc_convert, "discover", lambda *a, **k: next(calls))
    rcode = cc_convert.main([str(pdf)])
    out = capsys.readouterr().out
    assert "本次在 Mac 本机转换" in out and "timeout" in out
    assert rcode == 1                  # 本机路径继续走到"未发现可转源文件"


def test_main_dispatches_when_ready(synced, monkeypatch):
    pdf = _pdf(synced)
    seen = {}
    monkeypatch.setattr(rc, "dispatch_ready", lambda t, *a, **k: r"D:\fake\a.pdf")
    def fake_run(t, wp, passthrough, *a, **k):
        seen.update(wp=wp, passthrough=passthrough); return 0
    monkeypatch.setattr(rc, "run_dispatched", fake_run)
    assert cc_convert.main([str(pdf), "--force", "-t", "advanced"]) == 0
    assert seen["wp"] == r"D:\fake\a.pdf"
    assert seen["passthrough"] == ["--force", "-t", "advanced"]


def test_main_no_local_redo_after_dispatch_error(synced, monkeypatch, capsys):
    pdf = _pdf(synced)
    monkeypatch.setattr(rc, "dispatch_ready", lambda t, *a, **k: r"D:\fake\a.pdf")
    def boom(*a, **k):
        raise RuntimeError("ssh 中途断开")
    monkeypatch.setattr(rc, "run_dispatched", boom)
    monkeypatch.setattr(cc_convert, "discover", lambda *a, **k: [pdf])
    assert cc_convert.main([str(pdf)]) == 2
    assert "未在本机重做" in capsys.readouterr().err


def test_remote_and_local_conflict(synced):
    pdf = _pdf(synced)
    assert cc_convert.main([str(pdf), "--remote", "--local"]) == 2


def test_remote_run_keeps_interface(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(rc, "dispatch_ready", lambda t, s=180: order.append("ready") or "WP")
    monkeypatch.setattr(rc, "run_dispatched", lambda t, wp, p, s=180, to=7200: order.append(("run", wp, p)) or 0)
    assert rc.run(tmp_path / "x.pdf", ["--force"]) == 0
    assert order == ["ready", ("run", "WP", ["--force"])]
