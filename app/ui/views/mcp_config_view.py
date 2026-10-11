"""
MCP 配置视图

职责:
- 渲染 MCP 配置中栏（客户端列表 + 搜索）
- 渲染 MCP 配置右栏（服务选择、配置入口、配置内容与真实复制反馈）
"""

import inspect
import json
import shlex
from collections.abc import Callable, Iterable
from functools import partial

from nicegui import ui

from app.i18n import t
from app.ui.components import write_clipboard as _write_clipboard
from indexing.mcp.config import get_mcp_port
from indexing.settings import get_settings, is_mcp_auth_enabled


# 独立服务端点；key 用于取翻译文案。
MCP_SERVICES = (
    {"name": "piece-kb", "key": "service_kb"},
    {"name": "piece-index", "key": "service_index"},
    {"name": "piece-zotero", "key": "service_zotero"},
)


# MCP 客户端配置模板
# 使用 {port} 占位符，运行时替换为实际端口
# 协议：Streamable HTTP，端点：/mcp
# 各客户端的顶层键（mcpServers / servers / mcp / context_servers）和 URL 字段
# （url / serverUrl / httpUrl）互不兼容，type 取值也不统一，修改时请以官方文档为准。
# icon 只能取内置 Material Icons 字体里已有的字形（不含 Material Symbols 新增名，
# 例如 code_blocks 会显示为空白），新增前请先确认名字存在。
MCP_CLIENTS = [
    # ==================== 代码编辑器 / IDE ====================
    {
        "id": "cursor",
        "name": "Cursor",
        "icon": "code",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "vscode",
        "name": "VS Code",
        "icon": "laptop_mac",
        "config": {
            "servers": {
                "piece-kb": {
                    "type": "http",
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "type": "http",
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "visual_studio",
        "name": "Visual Studio 2022",
        "icon": "desktop_windows",
        "config": {
            "servers": {
                "piece-kb": {
                    "type": "http",
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "type": "http",
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        # Windsurf 于 2026-06 更名 Devin Desktop，配置字段仍为 serverUrl
        "id": "devin_desktop",
        "name": "Devin Desktop (ex-Windsurf)",
        "icon": "surfing",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "serverUrl": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "serverUrl": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "zed",
        "name": "Zed",
        "icon": "electric_bolt",
        "config": {
            "context_servers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "trae",
        "name": "Trae",
        "icon": "route",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "qoder",
        "name": "Qoder",
        "icon": "travel_explore",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "codebuddy",
        "name": "CodeBuddy",
        "icon": "handshake",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "type": "http",
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "type": "http",
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        # 未收录 JetBrains AI Assistant：官方未提供自定义鉴权 header 入口（YouTrack LLM-25012）
        "id": "jetbrains_junie",
        "name": "JetBrains Junie",
        "icon": "assistant",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "google_antigravity",
        "name": "Google Antigravity",
        "icon": "public",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "serverUrl": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "serverUrl": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    # ==================== AI 编程助手插件 ====================
    {
        "id": "cline",
        "name": "Cline",
        "icon": "terminal",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp",
                    "type": "streamableHttp"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp",
                    "type": "streamableHttp"
                }
            }
        }
    },
    {
        "id": "kilo_code",
        "name": "Kilo Code",
        "icon": "speed",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "type": "streamable-http",
                    "url": "http://localhost:{port}/mcp",
                    "disabled": False
                },
                "piece-index": {
                    "type": "streamable-http",
                    "url": "http://localhost:{index_port}/mcp",
                    "disabled": False
                }
            }
        }
    },
    {
        # 原 Qodo Gen，官方已更名为 Qodo IDE
        "id": "qodo_ide",
        "name": "Qodo IDE",
        "icon": "auto_fix_high",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "augment_code",
        "name": "Augment Code",
        "icon": "add_circle",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "type": "http",
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "type": "http",
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        # 云端 agent 在 GitHub 网页端配置，每个服务必须声明 tools
        "id": "copilot_coding_agent",
        "name": "GitHub Copilot Coding Agent",
        "icon": "precision_manufacturing",
        "setup": "setup_copilot_agent",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "type": "http",
                    "url": "http://localhost:{port}/mcp",
                    "tools": ["*"]
                },
                "piece-index": {
                    "type": "http",
                    "url": "http://localhost:{index_port}/mcp",
                    "tools": ["*"]
                }
            }
        }
    },
    {
        "id": "zencoder",
        "name": "Zencoder",
        "icon": "self_improvement",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    # ==================== 终端 / CLI Agent ====================
    {
        "id": "claude_code",
        "name": "Claude Code",
        "icon": "data_object",
        "config": {},
        "format": "bash",
    },
    {
        "id": "openai_codex",
        "name": "OpenAI Codex CLI",
        "icon": "memory",
        "config": {
            "[mcp_servers.piece-kb]": {
                "url": "http://localhost:{port}/mcp"
            },
            "[mcp_servers.piece-index]": {
                "url": "http://localhost:{index_port}/mcp"
            }
        },
        "format": "toml",
    },
    {
        "id": "copilot_cli",
        "name": "Copilot CLI",
        "icon": "terminal",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "type": "http",
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "type": "http",
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        # 原 Qwen Coder，官方名称为 Qwen Code，与 Gemini CLI 一致使用 httpUrl
        "id": "qwen_code",
        "name": "Qwen Code",
        "icon": "psychology",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "httpUrl": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "httpUrl": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "opencode",
        "name": "Opencode",
        "icon": "open_in_new",
        "config": {
            "mcp": {
                "piece-kb": {
                    "type": "remote",
                    "url": "http://localhost:{port}/mcp",
                    "enabled": True
                },
                "piece-index": {
                    "type": "remote",
                    "url": "http://localhost:{index_port}/mcp",
                    "enabled": True
                }
            }
        }
    },
    {
        "id": "crush",
        "name": "Crush",
        "icon": "favorite",
        "config": {
            "mcp": {
                "piece-kb": {
                    "type": "http",
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "type": "http",
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "factory",
        "name": "Factory (droid)",
        "icon": "factory",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "type": "http",
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "type": "http",
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        # Amp 写在 VS Code settings.json 的 amp.mcpServers 键下
        "id": "amp",
        "name": "Amp",
        "icon": "bolt",
        "setup": "setup_amp",
        "config": {
            "amp.mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "kiro",
        "name": "Kiro",
        "icon": "lightbulb",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp",
                    "disabled": False,
                    "autoApprove": []
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp",
                    "disabled": False,
                    "autoApprove": []
                }
            }
        }
    },
    {
        "id": "rovo_dev",
        "name": "Rovo Dev CLI",
        "icon": "developer_mode",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp",
                    "transport": "http"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp",
                    "transport": "http"
                }
            }
        }
    },
    {
        "id": "warp",
        "name": "Warp",
        "icon": "rocket_launch",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp",
                    "start_on_launch": True
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp",
                    "start_on_launch": True
                }
            }
        }
    },
    {
        "id": "devin_cli",
        "name": "Devin CLI",
        "icon": "smart_toy",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp",
                    "transport": "http"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp",
                    "transport": "http"
                }
            }
        }
    },
    {
        "id": "kimi_code",
        "name": "Kimi Code CLI",
        "icon": "nightlight",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    # ==================== 桌面客户端 ====================
    {
        "id": "workbuddy",
        "name": "WorkBuddy",
        "icon": "workspaces",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "type": "streamable-http",
                    "url": "http://localhost:{port}/mcp",
                    "disabled": False
                },
                "piece-index": {
                    "type": "streamable-http",
                    "url": "http://localhost:{index_port}/mcp",
                    "disabled": False
                }
            }
        }
    },
    {
        "id": "cherrystudio",
        "name": "Cherry Studio",
        "icon": "chat",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "isActive": True,
                    "name": "piece-kb",
                    "type": "streamableHttp",
                    "url": "http://localhost:{port}/mcp",
                    "baseUrl": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "isActive": True,
                    "name": "piece-index",
                    "type": "streamableHttp",
                    "url": "http://localhost:{index_port}/mcp",
                    "baseUrl": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
    {
        "id": "lm_studio",
        "name": "LM Studio",
        "icon": "science",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "url": "http://localhost:{index_port}/mcp"
                }
            }
        }
    },
]


# 官方 MCP 配置文档入口（客户端 id → URL）。
# 只收录厂商自有文档域名上、经核实的页面；未收录的客户端不猜路径，仅显示通用配置模板说明。
# 厂商换域名时 URL 会随之变化（如 cursor/windsurf/openai 近期迁移），维护前请重新以官方文档为准。
MCP_CLIENT_DOCS = {
    "cursor": "https://cursor.com/docs/mcp",
    "vscode": "https://code.visualstudio.com/docs/copilot/customization/mcp-servers",
    "devin_desktop": "https://docs.devin.ai/desktop/cascade/mcp",
    "zed": "https://zed.dev/docs/ai/mcp",
    "trae": "https://docs.trae.ai/ide/add-mcp-servers",
    "qoder": "https://docs.qoder.com/cli/mcp-servers",
    "codebuddy": "https://www.codebuddy.ai/docs/ide/User-guide/MCP",
    "jetbrains_junie": "https://junie.jetbrains.com/docs/junie-cli-mcp-configuration.html",
    "cline": "https://docs.cline.bot/mcp/adding-and-configuring-servers",
    "kilo_code": "https://kilo.ai/docs/automate/mcp/using-in-kilo-code",
    "augment_code": "https://docs.augmentcode.com/setup-augment/mcp",
    "claude_code": "https://code.claude.com/docs/en/mcp",
    "openai_codex": "https://learn.chatgpt.com/docs/extend/mcp",
    "copilot_cli": (
        "https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/"
        "add-mcp-servers"
    ),
    "qwen_code": "https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/",
    "opencode": "https://opencode.ai/docs/mcp-servers/",
    "factory": "https://docs.factory.com/cli/configuration/mcp",
    "amp": "https://ampcode.com/docs/customize/mcp",
    "kiro": "https://kiro.dev/docs/mcp/",
    "warp": "https://docs.warp.dev/agent-platform/capabilities/mcp/",
    "cherrystudio": "https://docs.cherryai.com.cn/docs/en-us/advanced-basic/extensions/mcp",
    "lm_studio": "https://lmstudio.ai/docs/app/mcp",
}


def _client_docs(client: dict) -> str | None:
    """取客户端的官方配置文档 URL；模板自带 docs 优先，否则查收录表。"""
    return client.get("docs") or MCP_CLIENT_DOCS.get(client["id"])


def _get_config_json(client: dict, services: Iterable[str] | None = None) -> str:
    """生成客户端配置，按服务选择端口和密钥；未启用认证时不导出密钥。

    services 为要写入配置的服务名集合，None 保留原有检索与索引服务。
    """
    settings = get_settings()
    ports = {"piece-kb": settings.mcp.port, "piece-index": get_mcp_port(), "piece-zotero": settings.mcp.zotero_port}
    kept = ["piece-kb", "piece-index"] if services is None else [name for name in ports if name in services]
    api_keys = {name: settings.mcp.get_api_key(service)
                for name, service in (("piece-kb", "retrieval"), ("piece-index", "index"), ("piece-zotero", "zotero"))
                if is_mcp_auth_enabled(service)}

    if client.get("format") == "bash":
        commands = []
        for name in kept:
            port = ports[name]
            lines = ["claude mcp add"]
            if api_keys.get(name):
                header = shlex.quote(f"Authorization: Bearer {api_keys[name]}")
                lines.append(f"  --header {header}")
            lines.extend(["  --transport http", f"  {name} http://localhost:{port}/mcp"])
            commands.append(" \\\n".join(lines))
        return "\n\n".join(commands)

    if client.get("format") == "toml":
        sections = []
        for name in kept:
            port = ports[name]
            section = f'[mcp_servers.{name}]\nurl = "http://localhost:{port}/mcp"'
            if api_keys.get(name):
                header = json.dumps(f"Bearer {api_keys[name]}", ensure_ascii=False)
                section += f"\nhttp_headers = {{ Authorization = {header} }}"
            sections.append(section)
        return "\n\n".join(sections)

    config = json.loads(json.dumps(client["config"]))
    if "piece-zotero" in kept:
        _add_zotero_server(config)
    # 替换端口后再注入密钥，避免自定义密钥中的占位符被误替换。
    config_str = json.dumps(config)
    config_str = config_str.replace("{port}", str(ports["piece-kb"]))
    config_str = config_str.replace("{index_port}", str(ports["piece-index"]))
    config_str = config_str.replace("{zotero_port}", str(ports["piece-zotero"]))
    config = json.loads(config_str)
    for name in ports:
        if name not in kept:
            _remove_server(config, name)
    _add_auth_headers(config, {name: key for name, key in api_keys.items() if name in kept})
    return json.dumps(config, indent=2, ensure_ascii=False)


def _add_zotero_server(node) -> bool:
    """复用客户端的索引端点模板，保留它的字段和容器格式。"""
    def clone(value):
        return json.loads(json.dumps(value).replace("piece-index", "piece-zotero").replace("{index_port}", "{zotero_port}"))

    if isinstance(node, dict):
        if isinstance(node.get("piece-index"), dict):
            node["piece-zotero"] = clone(node["piece-index"])
            return True
        for child in node.values():
            if _add_zotero_server(child):
                return True
    elif isinstance(node, list):
        for child in node:
            if isinstance(child, dict) and child.get("name") == "piece-index":
                node.append(clone(child))
                return True
            if _add_zotero_server(child):
                return True
    return False


def _add_auth_headers(config, api_keys: dict[str, str]):
    """递归查找服务节点注入密钥，避免为每个客户端的模板层级单独写判断。"""
    for name, api_key in api_keys.items():
        server = _find_server(config, name)
        if server is not None and api_key:
            server.setdefault("headers", {})["Authorization"] = f"Bearer {api_key}"


def _find_server(node, name: str) -> dict | None:
    """服务节点可能是对象键、数组元素的 name 字段，或直接位于根级别。"""
    if isinstance(node, dict):
        if node.get("name") == name:
            return node
        value = node.get(name)
        if isinstance(value, dict):
            return value
        for child in node.values():
            found = _find_server(child, name)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_server(child, name)
            if found is not None:
                return found
    return None


def _remove_server(node, name: str) -> bool:
    """移除服务节点，支持对象键、数组元素 name 字段两种形态；找不到返回 False。"""
    if isinstance(node, dict):
        if isinstance(node.get(name), dict):
            del node[name]
            return True
        for key, value in list(node.items()):
            if isinstance(value, dict) and value.get("name") == name:
                del node[key]
                return True
            if _remove_server(value, name):
                return True
    elif isinstance(node, list):
        for index, child in enumerate(node):
            if isinstance(child, dict) and child.get("name") == name:
                node.pop(index)
                return True
            if _remove_server(child, name):
                return True
    return False


def _get_config_language(client: dict) -> str:
    """获取配置文件的语言类型"""
    return client.get("format", "json")


def render_mcp_config_middle(
    selected_client: dict,
    ui_refs: dict,
    on_select_client: Callable[[str], None],
):
    """
    渲染 MCP 配置中栏（客户端列表 + 搜索）

    Args:
        selected_client: 选中的客户端 {"value": str | None}
        ui_refs: UI 组件引用字典
        on_select_client: 选择客户端的回调
    """
    # 搜索状态放进 ui_refs，跨重建/跨视图保留
    search_state = ui_refs.setdefault("client_search", {"value": ""})

    def matches(client: dict) -> bool:
        keyword = search_state["value"].strip().lower()
        return not keyword or keyword in client["name"].lower() or keyword in client["id"].lower()

    with ui.column().classes(
        "w-full h-full flex flex-col overflow-hidden theme-panel gap-0"
    ).style("border-right: 1px solid var(--border-color)"):
        # 顶部标题栏
        with ui.row().classes(
            "w-full px-3 items-center justify-between"
        ).style("border-bottom: 1px solid var(--border-color); height: 44px"):
            ui.label(t("mcp_config.title")).classes("text-sm font-medium theme-text")
            ui.label(t("mcp_config.client_count", count=len(MCP_CLIENTS))).classes(
                "text-xs theme-text-muted"
            )

        # 客户端搜索：客户端较多，按名称或 id 过滤
        with ui.column().classes("w-full px-2 pt-2 shrink-0"):
            search = ui.input(
                value=search_state["value"],
                placeholder=t("mcp_config.search_placeholder"),
            ).props("dense outlined clearable").classes("w-full workspace-search")
            search.on_value_change(
                lambda e: (search_state.update({"value": e.value or ""}), client_list.refresh())
            )

        # 客户端列表（每项为可键盘操作的按钮；选中仅用背景色区分，不加色块图标）
        with ui.scroll_area().classes("flex-1 scroll-flush"):
            @ui.refreshable
            def client_list():
                clients = [client for client in MCP_CLIENTS if matches(client)]
                with ui.column().classes("w-full gap-0.5 px-2 py-2"):
                    if not clients:
                        with ui.column().classes("w-full items-center py-6"):
                            ui.icon("search_off", size="lg").classes("theme-text-muted")
                            ui.label(t("mcp_config.no_match")).classes(
                                "text-xs mt-2 theme-text-muted"
                            )
                    for client in clients:
                        is_selected = selected_client["value"] == client["id"]
                        row_classes = "w-full px-3 py-2 rounded-md " + (
                            "theme-selected" if is_selected else "theme-hover"
                        )
                        with ui.button(
                            on_click=partial(on_select_client, client["id"])
                        ).props("flat no-caps align=left no-ripple").classes(row_classes):
                            with ui.row().classes("w-full items-center gap-2"):
                                ui.icon(client["icon"], size="xs").classes("theme-text-muted")
                                ui.label(client["name"]).classes("text-sm theme-text")

            ui_refs["client_list"] = client_list
            client_list()


def _selected_client(selected_client: dict) -> dict | None:
    return next(
        (c for c in MCP_CLIENTS if c["id"] == selected_client.get("value")),
        None,
    )


def _settings_handler(callback):
    """包装「MCP 服务设置」回调：兼容同步与异步实现。"""
    async def open_settings():
        result = callback()
        if inspect.isawaitable(result):
            await result

    return open_settings


async def _copy_config_text(text: str) -> None:
    ok = await _write_clipboard(text)
    ui.notify(
        t("mcp_config.copied") if ok else t("mcp_config.copy_failed"),
        type="positive" if ok else "negative",
    )


def _entry_key(client: dict) -> str:
    """配置入口说明：bash/toml/JSON 三种模板落点不同。"""
    fmt = client.get("format")
    if fmt == "bash":
        return "mcp_config.entry_bash"
    if fmt == "toml":
        return "mcp_config.entry_toml"
    return "mcp_config.entry_json"


def _render_client_config(client: dict, selected_client: dict) -> None:
    """单个客户端的配置区：服务选择 → 配置入口 → 配置内容 → 三者区分说明。"""
    settings = get_settings()
    ports = {"piece-kb": settings.mcp.port, "piece-index": get_mcp_port(), "piece-zotero": settings.mcp.zotero_port}
    # 先提供只读检索；其他服务由用户明确勾选，选择跨客户端和页面保留。
    kept = {"value": selected_client.setdefault("services", {"piece-kb"})}

    @ui.refreshable
    def config_block():
        has_services = bool(kept["value"])
        config_text = _get_config_json(client, kept["value"]) if has_services else ""

        def copy_config():
            if has_services:
                return _copy_config_text(config_text)

        with ui.row().classes("w-full items-center justify-between"):
            ui.label(t("mcp_config.config_heading")).classes(
                "text-xs font-semibold theme-text-muted"
            )
            button = ui.button(
                t("mcp_config.copy_btn"), icon="content_copy", on_click=copy_config,
            ).props("flat dense no-caps size=sm").classes("theme-text-accent")
            if not has_services:
                button.props("disable")
        if not has_services:
            ui.label(t("mcp_config.no_service_selected")).classes("text-xs theme-text-muted")
        code = ui.code(config_text, language=_get_config_language(client)).classes("w-full")
        # 内置复制入口会无条件显示成功；本页只使用上方有结果反馈的按钮。
        code.copy_button.classes("hidden").props("aria-hidden=true tabindex=-1")

    def toggle(name: str, checked: bool) -> None:
        if checked:
            kept["value"].add(name)
        else:
            kept["value"].discard(name)
        config_block.refresh()

    with ui.column().classes("w-full p-5 gap-5"):
        # 服务选择与用途：显示的端点与认证是真实服务状态，不代表客户端已连接
        with ui.column().classes("w-full gap-2"):
            ui.label(t("mcp_config.services_heading")).classes(
                "text-xs font-semibold theme-text-muted"
            )
            authentication = {"piece-kb": is_mcp_auth_enabled("retrieval"), "piece-index": is_mcp_auth_enabled("index"),
                              "piece-zotero": is_mcp_auth_enabled("zotero")}
            for service in MCP_SERVICES:
                name = service["name"]
                purpose = t(f"mcp_config.{service['key']}_purpose")
                with ui.row().classes("w-full items-start gap-2 flex-nowrap"):
                    ui.checkbox(
                        t(f"mcp_config.{service['key']}"),
                        value=name in kept["value"],
                        on_change=lambda e, n=name: toggle(n, e.value),
                    ).props("dense").classes("text-sm theme-text shrink-0")
                    auth_state = t("mcp_config.auth_on" if authentication[name] else "mcp_config.auth_off")
                    ui.label(f"{purpose} · {auth_state} · http://localhost:{ports[name]}/mcp").classes(
                        "text-xs theme-text-muted pt-1 min-w-0"
                    )
            if not settings.mcp.zotero_enabled:
                ui.label(t("zotero.mcp_off")).classes("text-xs theme-text-muted")
            # 配置基于当前运行进程的有效设置；改端口/密钥需重启服务再生效
            ui.label(t("mcp_config.settings_restart_hint")).classes("text-xs theme-text-muted")

        # 配置入口：有官方文档就给链接，没有就只给通用模板说明，不猜客户端路径
        with ui.column().classes("w-full gap-1"):
            ui.label(t("mcp_config.entry_heading")).classes("text-xs font-semibold theme-text-muted")
            ui.label(t(_entry_key(client))).classes("text-xs theme-text-muted")
            if client.get("setup"):
                ui.label(t(f"mcp_config.{client['setup']}")).classes("text-xs theme-text-muted")
            docs = _client_docs(client)
            if docs:
                ui.link(t("mcp_config.docs_btn"), docs, new_tab=True).classes(
                    "text-xs theme-text-accent self-start"
                )

        config_block()
        ui.label(t("mcp_config.notice")).classes("text-xs theme-text-muted")


def render_mcp_config_right(
    selected_client: dict,
    on_open_settings: Callable[[], object] | None = None,
):
    """
    渲染 MCP 配置右栏（服务选择、配置入口、配置内容）

    Args:
        selected_client: 选中的客户端 {"value": str | None}
        on_open_settings: 「MCP 服务设置」回调（同步或异步）；None 时不显示入口
    """
    with ui.column().classes("flex-1 h-full flex flex-col theme-content gap-0"):
        client = _selected_client(selected_client)
        if client:
            icon, title = client["icon"], client["name"]
        else:
            icon, title = "settings_input_component", t("mcp_config.title")

        with ui.row().classes(
            "w-full px-5 items-center justify-between theme-sidebar"
        ).style("border-bottom: 1px solid var(--border-color); height: 44px"):
            with ui.row().classes("items-center gap-2 min-w-0"):
                ui.icon(icon, size="xs").classes("theme-text-accent")
                ui.label(title).classes("text-sm font-medium theme-text truncate")
            if on_open_settings is not None:
                ui.button(
                    t("mcp_config.open_settings"),
                    icon="settings",
                    on_click=_settings_handler(on_open_settings),
                ).props("flat dense no-caps size=sm").classes("theme-text-accent shrink-0")

        with ui.scroll_area().classes("flex-1"):
            if client is None:
                with ui.column().classes("w-full h-full items-center justify-center"):
                    ui.icon("settings_input_component", size="lg").classes("theme-text-muted")
                    ui.label(t("mcp_config.select_client")).classes("text-sm mt-2 theme-text-muted")
            else:
                _render_client_config(client, selected_client)
