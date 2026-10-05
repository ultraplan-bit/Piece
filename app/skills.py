"""
内置 Skill 资源模块

职责:
- 定位内置 skills 目录（源码树与 PyInstaller 制品两种形态）
- 读取 Skill 清单与内容
- 推导当前实例的 CLI 调用前缀，在导出头部注入前缀说明
- 导出 Skill 到用户指定目录（<目录>/<skill 名>/SKILL.md，附带 references/）

skills/ 下每个子目录是一个 Skill（含 SKILL.md，可选 references/），
frontmatter 的 name/description 用作界面展示，目录名即 Skill ID。
仓库内的 SKILL.md 以 ``<PIECE>`` 代指 CLI 前缀，不是可直接复制使用的文件，
分发物是 GUI/CLI 导出的渲染结果。
"""

import re
import sys
from datetime import date
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from pathlib import Path

# 正文命令里代指调用前缀的写法。绝对路径前缀每次约 30~40 token，只在头部写一次；
# 选尖括号是因为照抄执行时 bash、cmd、PowerShell 都会立即报错，不会误连 PATH 上的其他 piece
CLI_ALIAS = "<PIECE>"

# 与 indexing.services.metadata_service 相同的 frontmatter 形态：
# 文件开头由 --- 包裹的块。这里独立维护一份，避免跨包依赖属性服务的语义。
_FRONTMATTER_PATTERN = re.compile(
    r"^\s*---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL
)


def get_version() -> str:
    """pyproject.toml 中的版本号，经安装元数据读取（PyInstaller 制品由 spec 一并打包）。

    未安装的源码目录没有元数据，返回 unknown，不冒充某个具体版本。
    """
    try:
        return _distribution_version("piece")
    except PackageNotFoundError:
        return "unknown"


def _quoted(path: str | Path) -> str:
    """正斜杠加双引号的路径写法：bash、cmd、PowerShell（配合 & 或管道）都能执行。"""
    return f'"{Path(path).expanduser().as_posix()}"'


def _cli_executable() -> list[str]:
    """当前实例 CLI 入口的 token 列表（可执行文件，必要时附 ``-m app.cli``）。

    路径统一正斜杠；不含 ``--data-dir``/``--port``，由调用方决定如何附加。
    """
    if getattr(sys, "frozen", False):
        # 任务 B 产出的控制台入口；不存在时退回 windowed 的自身
        console = Path(sys.executable).with_name(
            "piece-cli.exe" if sys.platform == "win32" else "piece-cli"
        )
        if console.is_file():
            return [console.as_posix()]
        return [Path(sys.executable).as_posix()]

    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        # 登录自启用 pythonw 启动的服务其 stdout 会被丢弃，换成同目录 python.exe
        python = executable.with_name("python.exe")
        if python.is_file():
            executable = python
    # uv tool 环境、仓库 .venv、普通 venv 的 Scripts/ 或 bin/ 都有控制台脚本
    console = executable.parent / ("piece.exe" if sys.platform == "win32" else "piece")
    if console.is_file():
        return [console.as_posix()]
    return [executable.as_posix(), "-m", "app.cli"]


def cli_argv(config_dir: str | Path, port: int) -> list[str]:
    """当前实例 CLI 前缀的 argv 列表，供 ``shlex.join(cli_argv + 命令)`` 拼装。

    agent 的工作目录、PIECE_DATA_DIR 环境都可能与服务不同，因此
    ``--data-dir``/``--port`` 即使等于默认值也显式附加，配合握手的
    TARGET_MISMATCH 才能保证连对库。返回原样 token，转义交给调用方。
    """
    return [
        *_cli_executable(),
        "--data-dir", Path(config_dir).expanduser().as_posix(),
        "--port", str(port),
    ]


def cli_prefix(config_dir: str | Path, port: int) -> str:
    """推导当前实例的 CLI 调用前缀字符串，供导出 Skill 时注入。

    与 :func:`cli_argv` 共用入口选择逻辑；可执行文件与数据目录路径加双引号，
    标志、模块名与端口不加，保持既有导出格式。
    """
    argv = _cli_executable()
    head = " ".join([_quoted(argv[0]), *argv[1:]])
    return f"{head} --data-dir {_quoted(config_dir)} --port {port}"


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
        f"> 由 Piece {version} 于 {date.today().isoformat()} 导出，对应本机安装；"
        "重装、移动目录、更换端口或数据目录后请重新导出。",
        f"> 下文命令中的 `{CLI_ALIAS}` 不能直接执行，运行时须整体替换为：",
        f"> `{prefix}`",
    ]
    # PowerShell 中以引号开头的命令是语法错误，只能提醒，不能替 agent 执行
    if prefix.startswith('"'):
        lines.append("> PowerShell 中以引号开头的命令前需加 `&`。")
    return "\n".join(lines)


def render_skill(skill_id: str, prefix: str, *, version: str) -> str | None:
    """渲染导出用 SKILL.md：frontmatter 后注入前缀说明，正文保留 ``<PIECE>`` 代称。

    返回 UTF-8、LF 的文本；Skill 不存在或 ID 非法时返回 None。
    """
    content = read_skill(skill_id)
    if content is None:
        return None
    content = content.replace("\r\n", "\n")
    match = _FRONTMATTER_PATTERN.match(content)
    head = content[: match.end()] if match else ""
    body = content[match.end():] if match else content
    return f"{head}{_export_header(prefix, version)}\n\n{body}"


def export_skills(
    skill_ids: list[str],
    target_dir: str | Path,
    *,
    prefix: str,
    version: str,
    overwrite: bool = False,
) -> dict:
    """把 Skill 以 <目标目录>/<skill_id>/SKILL.md 形式导出，附带 references/。

    写入的是渲染结果（头部已注入前缀，UTF-8、LF），不是仓库原文；
    references/ 下的补充资源原样复制，与主文使用同一覆盖确认，避免新旧文档混用。
    目标目录不存在时创建；已存在的 Skill 默认拒绝覆盖，由调用方确认。

    Returns:
        {"exported": [写入的 SKILL.md 路径], "exists": [已存在未覆盖的 ID],
         "missing": [不存在的 ID], "resources": [复制成功的 references 文件路径]}
    """
    target = Path(target_dir).expanduser()
    result = {"exported": [], "exists": [], "missing": [], "resources": []}
    for skill_id in skill_ids:
        content = render_skill(skill_id, prefix, version=version)
        if content is None:
            result["missing"].append(skill_id)
            continue

        destination = target / skill_id / "SKILL.md"
        source = skills_dir() / skill_id
        resources = [(path, destination.parent / path.relative_to(source))
                     for path in sorted((source / "references").rglob("*"))
                     if path.is_file() and not path.is_symlink()]
        outputs = [destination, *(output for _, output in resources)]
        if any(output.exists() for output in outputs) and not overwrite:
            result["exists"].append(skill_id)
            continue
        if any(not output.resolve().is_relative_to(destination.parent.resolve()) for output in outputs):
            raise ValueError("Skill 目标文件不能通过符号链接写入目录之外")

        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w" if overwrite else "x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        result["exported"].append(str(destination))
        for path, output in resources:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("wb" if overwrite else "xb") as handle:
                handle.write(path.read_bytes())
            result["resources"].append(str(output))
    return result
