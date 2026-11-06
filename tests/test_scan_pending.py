import json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scan_pending as sp


def make_case(root, name="20260101测试案件"):
    d = root / name / "3.诉讼资料" / "2.对方诉讼资料"
    d.mkdir(parents=True)
    return d


def test_detects_unconverted_pdf(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    d = make_case(root)
    (d / "质证意见.pdf").write_bytes(b"%PDF")
    pending = sp.find_pending(root, days=0)
    assert [f.name for f in pending] == ["质证意见.pdf"]


def test_converted_and_legacy_not_reported(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    d = make_case(root)
    # 闸1：已有 _md 且 source 匹配
    (d / "判决书.pdf").write_bytes(b"%PDF")
    md = d / "_md"; md.mkdir()
    (md / "判决书.md").write_text("---\nsource: ../判决书.pdf\n---\n", encoding="utf-8")
    # 闸2：legacy PaddleOCR sidecar
    (d / "答辩状.pdf").write_bytes(b"%PDF")
    (d / "答辩状.pdf_by_PaddleOCR-VL-1.5.md").write_text("x", encoding="utf-8")
    assert sp.find_pending(root, days=0) == []


def test_excluded_zones_skipped(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    case = root / "20260101测试案件"
    for sub in ("1.签约信息（报价、方案、法律服务合同、发票等）",
                "2.事务资料（授权委托、所函、当事人身份信息等）",
                "4.录音资料",
                "3.诉讼资料/4.提交微法院的电子资料存档"):
        d = case / sub; d.mkdir(parents=True)
        (d / "x.pdf").write_bytes(b"%PDF")
    assert sp.find_pending(root, days=0) == []


def test_underscore_and_hidden_dirs_skipped(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    for sub in ("_工具/cc-convert", ".git/objects"):
        d = root / sub; d.mkdir(parents=True)
        (d / "x.pdf").write_bytes(b"%PDF")
    assert sp.find_pending(root, days=0) == []


def test_office_lock_files_skipped(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    d = make_case(root)
    (d / "~$合同.docx").write_bytes(b"x")
    assert sp.find_pending(root, days=0) == []


def test_resolve_scope_three_states(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    case = root / "20260101甲案"; case.mkdir()
    sub = case / "3.诉讼资料"; sub.mkdir()
    other = tmp_path / "elsewhere"; other.mkdir()
    assert sp.resolve_scope(other, root) == (None, False)          # 守卫：静默
    assert sp.resolve_scope(root, root) == (root, True)            # 全区 + 过滤
    assert sp.resolve_scope(sub, root) == (case, False)            # 单案 + 全量
    assert sp.resolve_scope(case, root) == (case, False)


def test_mtime_filter_skips_old_dirs(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    d = make_case(root)
    (d / "old.pdf").write_bytes(b"%PDF")
    old = time.time() - 90 * 86400
    os.utime(d, (old, old))                                        # 仅做旧直接父目录
    assert sp.find_pending(root, days=30) == []                    # 过滤生效
    assert len(sp.find_pending(root, days=0)) == 1                 # 关过滤仍可见


def test_format_report_groups_and_hints(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    d = make_case(root, "20260101甲案")
    (d / "a.pdf").write_bytes(b"%PDF")
    (d / "b.pdf").write_bytes(b"%PDF")
    out = sp.format_report(sp.find_pending(root, days=0), root)
    assert "1 个案件" in out and "2 份" in out
    assert "- 20260101甲案：2 份（a.pdf 等）" in out
    assert "转换" in out                                            # 行动提示行


def test_format_report_empty_when_no_pending():
    assert sp.format_report([], Path("/tmp")) == ""


def test_format_report_truncates(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    for i in range(12):
        d = root / f"2026{i:04d}案" / "3.诉讼资料"
        d.mkdir(parents=True)
        (d / f"f{i}.pdf").write_bytes(b"%PDF")
    out = sp.format_report(sp.find_pending(root, days=0), root)
    assert "另有 2 个案件共 2 份" in out


def test_uuid_dir_pruned(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    d = make_case(root)
    uuid_dir = d / "判决书.pdf-12345678-1234-1234-1234-123456789abc"
    uuid_dir.mkdir()
    (uuid_dir / "origin.pdf").write_bytes(b"%PDF")                 # UUID 目录内的 PDF 不应上报
    assert sp.find_pending(root, days=0) == []


def test_detects_unconverted_office(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    d = make_case(root)
    (d / "服务合同.docx").write_bytes(b"PK")
    assert [f.name for f in sp.find_pending(root, days=0)] == ["服务合同.docx"]


def test_full_scan_skips_root_level_files(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    (root / "工作日志.xlsx").write_bytes(b"PK")                    # 根级散件：不报
    d = make_case(root)
    (d / "证据.pdf").write_bytes(b"%PDF")
    names = [f.name for f in sp.find_pending(root, days=0, skip_root_files=True)]
    assert names == ["证据.pdf"]


def test_case_root_files_still_reported(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    case = root / "20260101甲案"; case.mkdir()
    (case / "判决书.pdf").write_bytes(b"%PDF")                     # 案件根直挂：要报
    assert [f.name for f in sp.find_pending(case, days=0)] == ["判决书.pdf"]


def test_format_report_root_level_defensive(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    f = root / "散件.xlsx"; f.write_bytes(b"PK")
    out = sp.format_report([f], root)                              # 防御分支：不崩溃
    assert "1 个案件" in out and "散件.xlsx" in out and "（根目录）" in out


def test_resolve_scope_excluded_first_level(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    tool = root / "_工具" / "cc-convert"; tool.mkdir(parents=True)
    assert sp.resolve_scope(tool, root) == (None, False)


def test_resolve_scope_symlink_paths(tmp_path):
    real_root = tmp_path / "cases"; real_root.mkdir()
    case = real_root / "20260101甲案"; case.mkdir()
    link = tmp_path / "link"
    os.symlink(real_root, link)
    assert sp.resolve_scope(link / "20260101甲案", real_root) == (case, False)


def test_main_outside_root_silent(tmp_path, capsys):
    root = tmp_path / "cases"; root.mkdir()
    other = tmp_path / "elsewhere"; other.mkdir()
    rc = sp.main(["--root", str(root), "--cwd", str(other)])
    assert rc == 0 and capsys.readouterr().out == ""


def test_main_full_scan_prints_report(tmp_path, capsys):
    root = tmp_path / "cases"; root.mkdir()
    d = make_case(root)
    (d / "新证据.pdf").write_bytes(b"%PDF")
    rc = sp.main(["--root", str(root), "--cwd", str(root)])
    out = capsys.readouterr().out
    assert rc == 0 and "新证据.pdf" in out


def test_main_case_scope_only_current(tmp_path, capsys):
    root = tmp_path / "cases"; root.mkdir()
    d1 = make_case(root, "20260101甲案"); (d1 / "a.pdf").write_bytes(b"%PDF")
    d2 = make_case(root, "20260202乙案"); (d2 / "b.pdf").write_bytes(b"%PDF")
    sp.main(["--root", str(root), "--cwd", str(d1)])
    out = capsys.readouterr().out
    assert "a.pdf" in out and "b.pdf" not in out


def test_format_report_aggregates_by_case(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    case = root / "20260101甲案"
    d1 = case / "3.诉讼资料" / "2.对方诉讼资料"; d1.mkdir(parents=True)
    d2 = case / "3.诉讼资料" / "5.证据材料搜集"; d2.mkdir(parents=True)
    (d1 / "a.pdf").write_bytes(b"%PDF")
    (d2 / "b.pdf").write_bytes(b"%PDF")
    out = sp.format_report(sp.find_pending(root, days=0), root)
    assert out.count("20260101甲案") == 1                          # 跨子目录合并为一行
    assert "2 份" in out


def test_lingsan_dir_excluded(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    d = root / "1.零散文件" / "03.投标文件"; d.mkdir(parents=True)
    (d / "报价函.pdf").write_bytes(b"%PDF")
    assert sp.find_pending(root, days=0) == []


def test_cc_tc_prefixed_files_excluded(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    d = make_case(root)
    (d / "CC-质证意见-某案-20260601.pdf").write_bytes(b"%PDF")
    (d / "TC-答辩状模板.docx").write_bytes(b"PK")
    (d / "对方证据.pdf").write_bytes(b"%PDF")
    assert [f.name for f in sp.find_pending(root, days=0)] == ["对方证据.pdf"]


def test_main_full_scan_skips_root_files(tmp_path, capsys):
    root = tmp_path / "cases"; root.mkdir()
    (root / "散件.xlsx").write_bytes(b"PK")
    d = make_case(root)
    (d / "新证据.pdf").write_bytes(b"%PDF")
    sp.main(["--root", str(root), "--cwd", str(root)])
    out = capsys.readouterr().out
    assert "新证据.pdf" in out and "散件.xlsx" not in out


def test_main_all_flag_no_truncation(tmp_path, capsys):
    root = tmp_path / "cases"; root.mkdir()
    for i in range(12):
        d = root / f"2026{i:04d}案" / "3.诉讼资料"
        d.mkdir(parents=True)
        (d / f"f{i}.pdf").write_bytes(b"%PDF")
    sp.main(["--root", str(root), "--cwd", str(root), "--all"])
    out = capsys.readouterr().out
    assert "另有" not in out and out.count("1 份") == 12


def test_progress_sheet_excluded(tmp_path):
    root = tmp_path / "cases"; root.mkdir()
    case = root / "20260101甲案"; case.mkdir()
    (case / "案件进度信息记录表.docx").write_bytes(b"PK")
    assert sp.find_pending(case, days=0) == []


def test_main_hook_json_output(tmp_path, capsys):
    root = tmp_path / "cases"; root.mkdir()
    d = make_case(root)
    (d / "新证据.pdf").write_bytes(b"%PDF")
    sp.main(["--root", str(root), "--cwd", str(root), "--hook"])
    payload = json.loads(capsys.readouterr().out)
    assert "新证据.pdf" in payload["systemMessage"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "新证据.pdf" in payload["hookSpecificOutput"]["additionalContext"]


def test_main_hook_silent_when_clean(tmp_path, capsys):
    root = tmp_path / "cases"; root.mkdir()
    make_case(root)
    rc = sp.main(["--root", str(root), "--cwd", str(root), "--hook"])
    assert rc == 0 and capsys.readouterr().out == ""
