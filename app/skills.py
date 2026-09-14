"""
内置 Skill 资源模块

职责:
- 定位内置 skills 目录（源码树与 PyInstaller 制品两种形态）
- 读取 Skill 清单与内容
- 推导当前实例的 CLI 调用前缀，把占位符渲染成可用的 SKILL.md
- 导出 Skill 到用户指定目录（<目录>/<skill 名>/SKILL.md）

skills/ 下每个子目录是一个 Skill（含 SKILL.md），
frontmatter 的 name/description 用作界面展示，目录名即 Skill ID。
仓库内的 SKILL.md 使用 ``{{PIECE_CLI}}`` 占位符，不是可直接复制使用的文件，
分发物是 GUI/CLI 导出的渲染结果。
"""

import re
import sys
from datetime import date
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from pathlib import Path

# 导出头部中的命令占位符；渲染时替换为当前实例的调用前缀
CLI_PLACEHOLDER = "{{PIECE_CLI}}"
_FALLBACK_VERSION = "0.1.0"

# 与 indexing.services.metadata_service 相同的 frontmatter 形态：
# 文件开头由 --- 包裹的块。这里独立维护一份，避免跨包依赖属性服务的语义。
_FRONTMATTER_PATTERN = re.compile(
    r"^\s*---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL
)


def get_version() -> str:
    """导出头部使用的版本号；发行版元数据缺失（如 PyInstaller 制品）时退回内置常量。"""
    try:
        return _distribution_version("piece")
    except PackageNotFoundError:
        return _FALLBACK_VERSION


def _quoted(path: str | Path) -> str:
    """正斜杠加双引号的路径写法：bash、cmd、PowerShell（配合 & 或管道）都能执行。"""
    return f'"{Path(path).expanduser().as_posix()}"'


def cli_prefix(config_dir: str | Path, port: int) -> str:
    """推导当前实例的 CLI 调用前缀，供导出 Skill 时注入。

    agent 的工作目录、PIECE_DATA_DIR 环境都可能与服务不同，因此
    ``--data-dir``/``--port`` 即使等于默认值也显式附加，配合握手的
    TARGET_MISMATCH 才能保证连对库。
    """
    suffix = f"--data-dir {_quoted(config_dir)} --port {port}"
    if getattr(sys, "frozen", False):
        # 任务 B 产出的控制台入口；不存在时退回 windowed 的自身
        console = Path(sys.executable).with_name(
            "piece-cli.exe" if sys.platform == "win32" else "piece-cli"
        )
        if console.is_file():
            return f"{_quoted(console)} {suffix}"
        return f"{_quoted(sys.executable)} {suffix}"

    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        # 登录自启用 pythonw 启动的服务其 stdout 会被丢弃，换成同目录 python.exe
        python = executable.with_name("python.exe")
        if python.is_file():
            executable = python
    # uv tool 环境、仓库 .venv、普通 venv 的 Scripts/ 或 bin/ 都有控制台脚本
    console = executable.parent / ("piece.exe" if sys.platform == "win32" else "piece")
    if console.is_file():
        return f"{_quoted(console)} {suffix}"
    return f'{_quoted(executable)} -m app.cli {suffix}'


def skills_dir() -> Path:
    """定位内置 skills 目录。

    候选顺序：PyInstaller 制品的 _MEIPASS/skills（spec datas）→
    wheel 安装的 app/builtin_skills（pyproject force-include）→
    源码树 app/ 上级的 skills/。
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "skills")
    candidates.append(Path(__file__).resolve().parent / "builtin_skills")
    # app/ 的上级目录：源码树为项目根
    candidates.append(Path(__file__).resolve().parent.parent / "skills")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


def split_skill_meta(content: str) -> tuple[dict, str]:
    """分离 SKILL.md 开头的 frontmatter，返回 (元数据, 正文)。

    frontmatter 是可选的展示增强，格式不符或解析失败时
    返回 ({}, 原文)，不阻断清单加载。
    """
    match = _FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}, content
    try:
        import yaml

        meta = yaml.safe_load(match.group(1))
    except Exception:
        return {}, content
    if not isinstance(meta, dict):
        return {}, content
    plain = {
        str(key): value
        for key, value in meta.items()
        if isinstance(value, (str, int, float))
    }
    return plain, content[match.end():]


def list_skills() -> list[dict]:
    """扫描内置 skills 目录，返回按 ID 排序的清单。

    每项: {"id": 目录名, "name": frontmatter name, "description": 描述}。
    name 缺失时回退为目录名，保证界面始终有可显示的标识。
    """
    result = []
    root = skills_dir()
    if not root.is_dir():
        return result
    for skill_md in sorted(root.glob("*/SKILL.md")):
        try:
            meta, _ = split_skill_meta(skill_md.read_text(encoding="utf-8"))
        except OSError:
            continue
        skill_id = skill_md.parent.name
        result.append(
            {
                "id": skill_id,
                "name": str(meta.get("name") or skill_id),
                "description": str(meta.get("description") or ""),
            }
        )
    return result


def _skill_source(skill_id: str) -> Path | None:
    """定位 Skill 的 SKILL.md 源文件；ID 非法（含路径分隔符）或不存在时返回 None。"""
    if not skill_id or "/" in skill_id or "\\" in skill_id or skill_id in {".", ".."}:
        return None
    path = skills_dir() / skill_id / "SKILL.md"
    return path if path.is_file() else None


def read_skill(skill_id: str) -> str | None:
    """读取指定 Skill 的 SKILL.md 原文，不存在或 ID 非法时返回 None。"""
    source = _skill_source(skill_id)
    if source is None:
        return None
    return source.read_text(encoding="utf-8")


def _export_header(prefix: str, version: str) -> str:
    lines = [
        f"> 由 Piece {version} 于 {date.today().isoformat()} 导出。下文命令前缀",
        f"> {prefix}",
        "> 对应本机安装；重装、移动目录、更换端口或数据目录后请重新导出。",
    ]
    # PowerShell 中以引号开头的命令是语法错误，只能提醒，不能替 agent 执行
    if prefix.startswith('"'):
        lines.append("> PowerShell 中以引号开头的命令前需加 `&`。")
    return "\n".join(lines)


def render_skill(skill_id: str, prefix: str, *, version: str) -> str | None:
    """渲染导出用 SKILL.md：占位符替换为前缀，frontmatter 后注入前缀说明。

    返回 UTF-8、LF 的文本；Skill 不存在或 ID 非法时返回 None。
    """
    content = read_skill(skill_id)
    if content is None:
        return None
    content = content.replace("\r\n", "\n")
    match = _FRONTMATTER_PATTERN.match(content)
    head = content[: match.end()] if match else ""
    body = content[match.end():] if match else content
    rendered = f"{head}{_export_header(prefix, version)}\n\n{body}"
    return rendered.replace(CLI_PLACEHOLDER, prefix)


def export_skills(
    skill_ids: list[str],
    target_dir: str | Path,
    *,
    prefix: str,
    version: str,
    overwrite: bool = False,
) -> dict:
    """把 Skill 以 <目标目录>/<skill_id>/SKILL.md 形式导出。

    写入的是渲染结果（占位符已替换、头部已注入，UTF-8、LF），不是仓库原文；
    目标目录不存在时创建；已存在的 Skill 默认拒绝覆盖，由调用方确认。

    Returns:
        {"exported": [写入的路径], "exists": [已存在未覆盖的 ID], "missing": [不存在的 ID]}
    """
    target = Path(target_dir).expanduser()
    result = {"exported": [], "exists": [], "missing": []}
    for skill_id in skill_ids:
        content = render_skill(skill_id, prefix, version=version)
        if content is None:
            result["missing"].append(skill_id)
            continue

        destination = target / skill_id / "SKILL.md"
        if destination.exists() and not overwrite:
            result["exists"].append(skill_id)
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")
        result["exported"].append(str(destination))
    return result
