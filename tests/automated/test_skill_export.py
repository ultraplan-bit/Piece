"""Skill 资源定位与导出合同。

覆盖 app/skills.py：清单解析、读取边界、CLI 前缀推导、渲染注入、
目录形式导出（目标目录自动创建、默认不覆盖、显式覆盖、缺失与非法 ID）。
"""

import sys
from pathlib import Path

import pytest

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app import skills as skill_resources
from app.skills import (
    cli_prefix,
    export_skills,
    list_skills,
    read_skill,
    render_skill,
    skills_dir,
    split_skill_meta,
)

PREFIX = '"C:/Apps/Piece/piece-cli.exe" --data-dir "C:/Apps/Piece/data" --port 8689'


@pytest.fixture
def skills_root(tmp_path: Path, monkeypatch) -> Path:
    """构造两个内置 Skill 的临时 skills 目录并重定向定位。"""
    root = tmp_path / "skills"
    (root / "piece-index").mkdir(parents=True)
    (root / "piece-index" / "SKILL.md").write_text(
        "---\nname: piece-index\ndescription: 建库与维护\n---\n\n"
        "运行 `{{PIECE_CLI}} status --json` 确认目标。\n",
        encoding="utf-8",
    )
    (root / "piece-search").mkdir(parents=True)
    (root / "piece-search" / "SKILL.md").write_text(
        "---\nname: piece-search\ndescription: 检索工作流\n---\n\n"
        "先 `{{PIECE_CLI}} --version`，再 `{{PIECE_CLI}} search --help`。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skill_resources, "skills_dir", lambda: root)
    return root


# ---------- 清单与读取 ----------


def test_list_skills_parses_frontmatter(skills_root: Path):
    result = list_skills()

    assert [s["id"] for s in result] == ["piece-index", "piece-search"]
    by_id = {s["id"]: s for s in result}
    assert by_id["piece-index"]["name"] == "piece-index"
    assert by_id["piece-index"]["description"] == "建库与维护"
    assert by_id["piece-search"]["description"] == "检索工作流"


def test_list_skills_tolerates_missing_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(skill_resources, "skills_dir", lambda: tmp_path / "absent")
    assert list_skills() == []


def test_split_skill_meta_without_frontmatter():
    meta, body = split_skill_meta("# 纯正文")
    assert meta == {}
    assert body == "# 纯正文"


def test_read_skill_rejects_invalid_ids(skills_root: Path):
    assert read_skill("../outside") is None
    assert read_skill("a/b") is None
    assert read_skill("") is None
    assert read_skill("nonexistent") is None
    content = read_skill("piece-index")
    assert content is not None
    assert content.startswith("---")


def test_skills_dir_falls_back_to_repo_layout():
    """无 _MEIPASS、无 builtin_skills 时回退到 app/ 上级的 skills/（源码树布局）。"""
    assert skills_dir() == Path(__file__).resolve().parent.parent.parent / "skills"


def test_skills_dir_prefers_packaged_builtin_skills(tmp_path: Path, monkeypatch):
    """wheel 安装形态：app/builtin_skills 优先于源码树 skills/。"""
    package = tmp_path / "site-packages" / "app"
    builtin = package / "builtin_skills" / "demo"
    builtin.mkdir(parents=True)
    (builtin / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    (tmp_path / "repo" / "skills").mkdir(parents=True)

    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(skill_resources, "__file__", str(package / "skills.py"))

    assert skills_dir() == package / "builtin_skills"


# ---------- CLI 前缀推导 ----------


def _console_name(*names: str) -> str:
    return names[0] if sys.platform == "win32" else names[1]


def test_cli_prefix_frozen_prefers_console_exe(tmp_path: Path, monkeypatch):
    install = tmp_path / "Piece"
    install.mkdir()
    console = install / _console_name("piece-cli.exe", "piece-cli")
    console.write_bytes(b"")
    (install / "Piece.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / "Piece.exe"))

    prefix = cli_prefix(tmp_path / "data", 8689)

    assert prefix == (
        f'"{console.as_posix()}" --data-dir "{(tmp_path / "data").as_posix()}" --port 8689'
    )


def test_cli_prefix_frozen_falls_back_to_self(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Piece.exe"))

    prefix = cli_prefix(tmp_path / "data", 8690)

    assert prefix == (
        f'"{(tmp_path / "Piece.exe").as_posix()}"'
        f' --data-dir "{(tmp_path / "data").as_posix()}" --port 8690'
    )


def test_cli_prefix_prefers_console_script(tmp_path: Path, monkeypatch):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    console = scripts / _console_name("piece.exe", "piece")
    console.write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))

    prefix = cli_prefix(tmp_path / "data", 8689)

    assert prefix.startswith(f'"{console.as_posix()}"')
    assert "--data-dir" in prefix and "--port 8689" in prefix
    assert "\\" not in prefix  # 路径统一正斜杠


def test_cli_prefix_pythonw_swaps_to_python(tmp_path: Path, monkeypatch):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "pythonw.exe").write_bytes(b"")
    (scripts / "python.exe").write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(scripts / "pythonw.exe"))

    prefix = cli_prefix(tmp_path / "data", 8689)

    # 没有 piece.exe 控制台脚本时退回 "<python>" -m app.cli，且 pythonw 已换成 python
    assert prefix.startswith(f'"{(scripts / "python.exe").as_posix()}" -m app.cli')
    assert "--data-dir" in prefix and "--port 8689" in prefix


def test_cli_prefix_module_fallback(tmp_path: Path, monkeypatch):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))

    prefix = cli_prefix(tmp_path / "data", 8689)

    assert prefix == (
        f'"{(scripts / "python.exe").as_posix()}" -m app.cli'
        f' --data-dir "{(tmp_path / "data").as_posix()}" --port 8689'
    )


# ---------- 渲染 ----------


def test_render_skill_injects_prefix_and_header(skills_root: Path):
    rendered = render_skill("piece-index", PREFIX, version="0.1.0")

    assert rendered is not None
    assert "{{PIECE_CLI}}" not in rendered  # 不得残留占位符
    assert rendered.startswith("---")  # frontmatter 原样保留
    assert f"> {PREFIX}" in rendered  # 头部嵌入前缀，含数据目录与端口
    assert "0.1.0" in rendered
    assert f"`{PREFIX} status --json`" in rendered  # 正文命令已替换
    assert "PowerShell" in rendered  # 引号前缀生成 PowerShell 提示
    assert "\r" not in rendered  # LF 输出


def test_render_skill_bare_prefix_omits_powershell_hint(skills_root: Path):
    rendered = render_skill("piece-index", "piece", version="0.1.0")

    assert rendered is not None
    assert "PowerShell" not in rendered


def test_render_skill_normalizes_crlf(tmp_path: Path, monkeypatch):
    root = tmp_path / "skills"
    (root / "crlf").mkdir(parents=True)
    (root / "crlf" / "SKILL.md").write_bytes(
        b"---\r\nname: crlf\r\n---\r\n\r\n`{{PIECE_CLI}} status --json`\r\n"
    )
    monkeypatch.setattr(skill_resources, "skills_dir", lambda: root)

    rendered = render_skill("crlf", PREFIX, version="0.1.0")

    assert rendered is not None
    assert "\r" not in rendered
    assert f"`{PREFIX} status --json`" in rendered


def test_render_skill_rejects_invalid_ids(skills_root: Path):
    assert render_skill("ghost", PREFIX, version="0.1.0") is None
    assert render_skill("", PREFIX, version="0.1.0") is None


# ---------- 导出 ----------


def test_export_creates_target_and_writes_rendered(skills_root: Path, tmp_path: Path):
    target = tmp_path / "export" / "skills"  # 目标不存在，应自动创建

    result = export_skills(["piece-index", "piece-search"], target,
                           prefix=PREFIX, version="0.1.0")

    assert result["exported"] == [
        str(target / "piece-index" / "SKILL.md"),
        str(target / "piece-search" / "SKILL.md"),
    ]
    assert result["exists"] == []
    assert result["missing"] == []
    # 导出内容与渲染结果一致，UTF-8、LF
    expected = render_skill("piece-index", PREFIX, version="0.1.0")
    raw = (target / "piece-index" / "SKILL.md").read_bytes()
    assert raw.decode("utf-8") == expected
    assert b"\r" not in raw
    assert b"{{PIECE_CLI}}" not in raw


def test_export_refuses_overwrite_until_confirmed(skills_root: Path, tmp_path: Path):
    target = tmp_path / "out"
    export_skills(["piece-index"], target, prefix=PREFIX, version="0.1.0")

    # 默认不覆盖：目标已存在时报告 exists，原有内容保持不变
    existing = target / "piece-index" / "SKILL.md"
    existing.write_text("---\nname: changed\n---\n", encoding="utf-8")
    result = export_skills(["piece-index"], target, prefix=PREFIX, version="0.1.0")
    assert result["exported"] == []
    assert result["exists"] == ["piece-index"]
    assert existing.read_text(encoding="utf-8") == "---\nname: changed\n---\n"

    # 显式覆盖后内容与渲染结果一致
    result = export_skills(["piece-index"], target, prefix=PREFIX,
                           version="0.1.0", overwrite=True)
    assert result["exported"] == [str(existing)]
    assert existing.read_text(encoding="utf-8") == render_skill(
        "piece-index", PREFIX, version="0.1.0"
    )


def test_export_reports_missing_and_expands_home(skills_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expanduser 读 USERPROFILE

    result = export_skills(["piece-index", "ghost"], "~/skills-target",
                           prefix=PREFIX, version="0.1.0")

    assert result["missing"] == ["ghost"]
    assert result["exists"] == []
    assert result["exported"] == [str(tmp_path / "skills-target" / "piece-index" / "SKILL.md")]
