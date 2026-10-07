"""Skill 资源定位与导出合同。

覆盖 app/skills.py：清单解析、读取边界、CLI 前缀与 argv 推导、头部注入、
目录形式导出（目标目录自动创建、默认不覆盖、显式覆盖、references 复制与
用户文件保护、缺失与非法 ID）。
"""

import shlex
import sys
from pathlib import Path

import pytest

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app import skills as skill_resources
from app.skills import (
    cli_argv,
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
        "运行 `<PIECE> status --json` 确认目标。\n",
        encoding="utf-8",
    )
    (root / "piece-search").mkdir(parents=True)
    (root / "piece-search" / "SKILL.md").write_text(
        "---\nname: piece-search\ndescription: 检索工作流\n---\n\n"
        "先 `<PIECE> --version`，再 `<PIECE> search --help`。\n",
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


# ---------- CLI 前缀 / argv 推导 ----------


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


def _console_install(tmp_path: Path) -> Path:
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    (scripts / _console_name("piece.exe", "piece")).write_bytes(b"")
    return scripts


def test_cli_argv_console_script(tmp_path: Path, monkeypatch):
    """argv 与 prefix 同源：路径原样、flag 分离、含 --data-dir/--port。"""
    scripts = _console_install(tmp_path)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))

    argv = cli_argv(tmp_path / "data", 8689)

    assert argv == [
        (scripts / _console_name("piece.exe", "piece")).as_posix(),
        "--data-dir", (tmp_path / "data").as_posix(),
        "--port", "8689",
    ]
    assert "\\" not in " ".join(argv)


def test_cli_argv_module_fallback(tmp_path: Path, monkeypatch):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))

    argv = cli_argv(tmp_path / "data", 8689)

    assert argv == [
        (scripts / "python.exe").as_posix(), "-m", "app.cli",
        "--data-dir", (tmp_path / "data").as_posix(), "--port", "8689",
    ]


def test_cli_argv_frozen_prefers_console_exe(tmp_path: Path, monkeypatch):
    install = tmp_path / "Piece"
    install.mkdir()
    console = install / _console_name("piece-cli.exe", "piece-cli")
    console.write_bytes(b"")
    (install / "Piece.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / "Piece.exe"))

    assert cli_argv(tmp_path / "data", 8689)[0] == console.as_posix()


def test_cli_prefix_reuses_cli_argv_executable(tmp_path: Path, monkeypatch):
    """cli_prefix 的可执行文件 token 与 cli_argv[0] 完全一致（双引号内）。"""
    scripts = _console_install(tmp_path)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))

    argv = cli_argv(tmp_path / "data", 8689)
    prefix = cli_prefix(tmp_path / "data", 8689)

    assert prefix.startswith(f'"{argv[0]}"')
    assert prefix.endswith(f'--data-dir "{argv[2]}" --port {argv[4]}')


def test_cli_argv_shlex_join_is_full_readonly_prefix(tmp_path: Path, monkeypatch):
    """父代理用 shlex.join(cli_argv + 只读命令) 生成可 bash 执行的 next_command。"""
    scripts = _console_install(tmp_path)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))

    command = shlex.join(cli_argv(tmp_path / "data", 8689) + ["status", "--json"])

    assert command.endswith("--data-dir %s --port 8689 status --json"
                            % (tmp_path / "data").as_posix())
    assert shlex.split(command)[-2:] == ["status", "--json"]


# ---------- 渲染 ----------


def test_render_skill_injects_prefix_and_header(skills_root: Path):
    rendered = render_skill("piece-index", PREFIX, version="0.1.0")

    assert rendered is not None
    assert rendered.startswith("---")  # frontmatter 原样保留
    assert f"> `{PREFIX}`" in rendered  # 头部嵌入前缀，含数据目录与端口
    assert rendered.count(PREFIX) == 1  # 绝对路径只在头部出现一次
    assert "0.1.0" in rendered
    assert "`<PIECE> status --json`" in rendered  # 正文保留代称
    assert "PowerShell" in rendered  # 引号前缀生成 PowerShell 提示
    assert "\r" not in rendered  # LF 输出


def test_graph_sample_batches_validate_and_roundtrip(knowledge_base):
    import json
    import re
    from indexing.services import knowledge_service as knowledge, file_service, chunk_service

    # 四个批次示例（含仅补证据）都必须可执行且可读回。
    apply_md = skills_dir() / "piece-graph" / "references" / "apply.md"
    content = apply_md.read_text(encoding="utf-8")
    assert "<PIECE>" in content
    batches = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", content, re.S)]
    assert len(batches) == 4
    first = knowledge.apply({**batches[0], "request_key": "skill-create"})
    oid = first["refs"]["concept"]["id"]
    update = batches[1]
    update["objects"][0]["id"] = oid
    knowledge.apply({**update, "request_key": "skill-update"})
    assert knowledge.get_record(kind="object", id=oid)["record"]["revision"] == 2
    target = knowledge.apply({"reason": "测试示例目标", "request_key": "skill-target", "objects": [
        {"ref": "target", "kind": "concept", "title": "稳定身份"}]})["refs"]["target"]["id"]
    relation = batches[2]
    fid = file_service.create_empty_file("示例来源")["file_id"]
    chunk_service.create_chunk_add_task(fid, "示例来源", relation["evidence"][0]["quote"])
    knowledge_base.drain()
    cid = file_service.get_chunks_by_file_id(fid)[0]["id"]
    relation["relations"][0]["source"]["id"] = oid
    relation["relations"][0]["target"]["id"] = target
    relation["evidence"][0].update(source_library_id=first["library_id"], source_file_id=fid, source_chunk_id=cid)
    result = knowledge.apply({**relation, "request_key": "skill-relation"})
    detail = knowledge.get_record(kind="relation", id=result["refs"]["r"]["id"])
    assert detail["evidence"]["items"][0]["location_status"] == "current"
    preview = knowledge.delete({"kind": "evidence", "id": result["evidence"][0]["id"]})
    assert preview["dry_run"] and not preview["committed"]

    only = batches[3]
    rid = result["refs"]["r"]["id"]
    only["evidence"][0]["object"]["id"] = oid
    only["evidence"][1]["relation"]["id"] = rid
    source = chunk_service.get_chunk_by_id(cid)
    assert source is not None
    for evidence in only["evidence"]:
        evidence.update(source_library_id=first["library_id"], source_file_id=fid, source_chunk_id=cid,
                        expected_content_hash=knowledge.content_hash(source["chunk_text"]))
    added = knowledge.apply({**only, "request_key": "skill-evidence-only"})
    assert added["objects"] == added["relations"] == []
    assert added["evidence"][1]["action"] == "reused"
    assert knowledge.get_record(kind="object", id=oid)["record"]["revision"] == 2
    assert knowledge.get_record(kind="relation", id=rid)["record"]["revision"] == 1
    for evidence in added["evidence"]:
        assert knowledge.get_record(kind="evidence", id=evidence["id"])["record"]["location_status"] == "current"


def test_wiki_sample_batches_validate_and_roundtrip(knowledge_base):
    import json
    import re
    from indexing.services import wiki_service as wiki, file_service, chunk_service
    from indexing.services import knowledge_common

    # 四个批次示例（含仅补证据）都必须可执行且可读回。
    apply_md = skills_dir() / "piece-wiki" / "references" / "apply.md"
    content = apply_md.read_text(encoding="utf-8")
    assert "<PIECE>" in content
    batches = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", content, re.S)]
    assert len(batches) == 4
    first = wiki.apply({**batches[0], "request_key": "wiki-create"})
    pid = first["refs"]["concept"]["id"]
    page = wiki.get_record(kind="page", id=pid)["record"]
    update = batches[1]
    update["pages"][0].update(id=pid, expected_revision=page["revision"],
                              expected_content_hash=page["content_hash"])
    wiki.apply({**update, "request_key": "wiki-update"})
    assert wiki.get_record(kind="page", id=pid)["record"]["revision"] == 2

    # 新建页面并附本库证据。
    source_batch = batches[2]
    fid = file_service.create_empty_file("wiki示例来源")["file_id"]
    chunk_service.create_chunk_add_task(fid, "wiki示例来源", source_batch["evidence"][0]["quote"])
    knowledge_base.drain()
    cid = file_service.get_chunks_by_file_id(fid)[0]["id"]
    source_batch["evidence"][0].update(source_library_id=first["library_id"],
                                       source_file_id=fid, source_chunk_id=cid)
    created = wiki.apply({**source_batch, "request_key": "wiki-page-evidence"})
    new_page = wiki.get_record(kind="page", id=created["refs"]["p"]["id"])
    assert new_page["has_evidence"] and new_page["evidence"]["items"][0]["location_status"] == "current"

    # 仅补证据：pages 只带并发条件，页面 revision 不变。
    only = batches[3]
    page_now = wiki.get_record(kind="page", id=pid)["record"]
    only["pages"][0].update(id=pid, expected_revision=page_now["revision"],
                            expected_content_hash=page_now["content_hash"])
    only["evidence"][0]["page"] = {"id": pid}
    source = chunk_service.get_chunk_by_id(cid)
    assert source is not None
    for evidence in only["evidence"]:
        evidence.update(source_library_id=first["library_id"], source_file_id=fid, source_chunk_id=cid,
                        expected_content_hash=knowledge_common.content_hash(source["chunk_text"]))
    added = wiki.apply({**only, "request_key": "wiki-evidence-only"})
    # 仅补证据：页面条目没有内容改动字段，但证据写入页面文件，revision 会推进。
    assert added["pages"] and added["pages"][0]["submitted_fields"] == []
    assert wiki.get_record(kind="page", id=pid)["record"]["revision"] == page_now["revision"] + 1
    assert wiki.get_record(kind="page", id=pid)["has_evidence"]


def test_wiki_gui_discovery_preview_and_export(tmp_path, monkeypatch):
    pytest.importorskip("nicegui")
    from nicegui import ui
    from app.ui.views import skill_view
    from test_import_dialogs import _buttons, _labels, _run, _click

    monkeypatch.setattr(ui, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_view, "get_default_data_dir", lambda: tmp_path / "配置目录")
    assert skill_view._find_skill("piece-wiki")["description"]
    target = tmp_path / "导出"
    with ui.column() as container:
        refs = {}
        skill_view.render_skill_middle({"value": "piece-wiki"}, refs, lambda value: None)
        skill_view.render_skill_right({"value": "piece-wiki"}, {"export_dir": str(target)}, port=18765)
    try:
        assert "piece-wiki" in _labels(container)
        from app.i18n import t
        _run(_click(_buttons(container)[t("skills.export_current")]))
        output = target / "piece-wiki" / "SKILL.md"
        content = output.read_text(encoding="utf-8")
        assert "--port 18765" in content
        assert content.count(f'--data-dir "{(tmp_path / "配置目录").as_posix()}"') == 1
        # references/ 随 Skill 一起导出，且主文件之外不再重复注入前缀
        assert {p.name for p in output.parent.iterdir()} == {"SKILL.md", "references"}
        reference = output.parent / "references" / "apply.md"
        assert reference.is_file()
        assert (tmp_path / "配置目录").as_posix() not in reference.read_text(encoding="utf-8")
    finally:
        container.delete()


def test_builtin_skills_use_cli_alias():
    """内置 Skill 的命令统一写代称，不残留旧占位符。"""
    for skill_md in skills_dir().glob("*/SKILL.md"):
        content = skill_md.read_text(encoding="utf-8")
        assert "<PIECE>" in content, skill_md
        assert "{{" not in content, skill_md


def test_render_skill_bare_prefix_omits_powershell_hint(skills_root: Path):
    rendered = render_skill("piece-index", "piece", version="0.1.0")

    assert rendered is not None
    assert "PowerShell" not in rendered


def test_render_skill_normalizes_crlf(tmp_path: Path, monkeypatch):
    root = tmp_path / "skills"
    (root / "crlf").mkdir(parents=True)
    (root / "crlf" / "SKILL.md").write_bytes(
        b"---\r\nname: crlf\r\n---\r\n\r\n`<PIECE> status --json`\r\n"
    )
    monkeypatch.setattr(skill_resources, "skills_dir", lambda: root)

    rendered = render_skill("crlf", PREFIX, version="0.1.0")

    assert rendered is not None
    assert "\r" not in rendered
    assert "`<PIECE> status --json`" in rendered


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
    assert result["resources"] == []  # 这两个 Skill 没有 references/
    # 导出内容与渲染结果一致，UTF-8、LF
    expected = render_skill("piece-index", PREFIX, version="0.1.0")
    raw = (target / "piece-index" / "SKILL.md").read_bytes()
    assert raw.decode("utf-8") == expected
    assert b"\r" not in raw


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
    assert result["resources"] == []


def test_export_references_share_explicit_overwrite_confirmation(tmp_path: Path, monkeypatch):
    root = tmp_path / "skills"
    skill = root / "piece-wiki"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: piece-wiki\n---\n\n`<PIECE> status --json`\n", encoding="utf-8"
    )
    (skill / "references" / "apply.md").write_text("# apply\n", encoding="utf-8")
    monkeypatch.setattr(skill_resources, "skills_dir", lambda: root)
    target = tmp_path / "out"

    result = export_skills(["piece-wiki"], target, prefix=PREFIX, version="0.1.0")

    reference = target / "piece-wiki" / "references" / "apply.md"
    assert result["exported"] == [str(target / "piece-wiki" / "SKILL.md")]
    assert result["resources"] == [str(reference)]
    assert reference.read_text(encoding="utf-8") == "# apply\n"
    # references 原样复制，不做前缀注入（前缀只在 SKILL.md 头部出现一次）
    assert PREFIX not in reference.read_text(encoding="utf-8")

    # 默认保留用户文件；显式覆盖须同时更新主文和参考，不能混用新旧契约。
    reference.write_text("user\n", encoding="utf-8")
    result = export_skills(["piece-wiki"], target, prefix=PREFIX, version="0.1.0")
    assert result["exists"] == ["piece-wiki"]
    assert reference.read_text(encoding="utf-8") == "user\n"
    result = export_skills(["piece-wiki"], target, prefix=PREFIX,
                           version="0.1.0", overwrite=True)
    assert result["resources"] == [str(reference)]
    assert reference.read_text(encoding="utf-8") == "# apply\n"


def test_export_real_piece_wiki_ships_references(tmp_path: Path):
    result = export_skills(["piece-wiki"], tmp_path / "out", prefix=PREFIX, version="0.1.0")

    assert any(
        Path(p).parent.name == "references" and Path(p).name == "apply.md"
        for p in result["resources"]
    )
    reference = tmp_path / "out" / "piece-wiki" / "references" / "apply.md"
    assert "```json" in reference.read_text(encoding="utf-8")
