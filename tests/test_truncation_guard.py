"""截断守卫：VLM 输出在 n_predict 上限处被截断、恰好切在多字节汉字中间时，mineru_llama_cpp 的 C 扩展
严格解码抛 UnicodeDecodeError，原先整本书失败（2026-09-30《公司法司法解释（四）理解与适用》两次各 3.7 小时
后在 p.471–480 同一处失败）。守卫后只把该文字块置空（finish_reason=length），整本继续。"""
import pytest
import engine

mlc = pytest.importorskip("mineru_llama_cpp")


class _FakeCore:
    def __init__(self, exc=None, d=None):
        self.exc, self.d = exc, d

    def generate(self, body):
        if self.exc:
            raise self.exc
        return self.d


class _FakeEngine:
    def __init__(self, core):
        self._core = core

    def _build_body(self, messages, sampling_params, stream):
        return "{}"


def _broken_utf8():
    return UnicodeDecodeError("utf-8", b"\xe6", 0, 1, "unexpected end of data")


def test_guard_turns_decode_error_into_empty_truncated_result():
    assert engine.guard_truncated_utf8() is True
    fake = _FakeEngine(_FakeCore(exc=_broken_utf8()))
    r = mlc.engine.Engine.generate(fake, [{"role": "user", "content": "x"}])
    assert r.content == ""
    assert r.finish_reason == "length"


def test_guard_passes_normal_results_through():
    engine.guard_truncated_utf8()
    d = {"content": "原告：张三", "finish_reason": "stop", "tokens_evaluated": 3,
         "tokens_predicted": 5, "timings": None}
    r = mlc.engine.Engine.generate(_FakeEngine(_FakeCore(d=d)), [])
    assert r.content == "原告：张三" and r.finish_reason == "stop"


def test_guard_does_not_swallow_other_errors():
    engine.guard_truncated_utf8()
    fake = _FakeEngine(_FakeCore(exc=RuntimeError("engine died")))
    with pytest.raises(RuntimeError):
        mlc.engine.Engine.generate(fake, [])


def test_guard_is_idempotent():
    engine.guard_truncated_utf8()
    first = mlc.engine.Engine.generate
    engine.guard_truncated_utf8()
    assert mlc.engine.Engine.generate is first
