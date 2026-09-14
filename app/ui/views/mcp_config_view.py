"""
MCP 配置视图

职责:
- 渲染 MCP 配置中栏（客户端列表）
- 渲染 MCP 配置右栏（JSON 配置展示）
"""

import json
import shlex
from nicegui import ui

from app.i18n import t
from indexing.mcp.config import get_mcp_port
from indexing.settings import get_settings


# MCP 客户端配置模板
# 使用 {port} 占位符，运行时替换为实际端口
# 协议：Streamable HTTP，端点：/mcp
MCP_CLIENTS = [
    # ==================== 代码编辑器 ====================
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
        "id": "windsurf",
        "name": "Windsurf",
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
        "id": "vscode",
        "name": "VS Code",
        "icon": "laptop_mac",
        "config": {
            "mcp": {
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
    # ==================== AI 助手插件 ====================
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
        "id": "github_copilot",
        "name": "GitHub Copilot",
        "icon": "hub",
        "config": {
            "mcp": {
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
        }
    },
    {
        "id": "copilot_coding_agent",
        "name": "Copilot Coding Agent",
        "icon": "precision_manufacturing",
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
        "id": "augment_code",
        "name": "Augment Code",
        "icon": "add_circle",
        "config": {
            "augment.advanced": {
                "mcpServers": [
                    {
                        "name": "piece-kb",
                        "url": "http://localhost:{port}/mcp"
                    },
                    {
                        "name": "piece-index",
                        "url": "http://localhost:{index_port}/mcp"
                    }
                ]
            }
        }
    },
    {
        "id": "roo_code",
        "name": "Roo Code",
        "icon": "pets",
        "config": {
            "mcpServers": {
                "piece-kb": {
                    "type": "streamable-http",
                    "url": "http://localhost:{port}/mcp"
                },
                "piece-index": {
                    "type": "streamable-http",
                    "url": "http://localhost:{index_port}/mcp"
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
        "id": "qodo_gen",
        "name": "Qodo Gen",
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
    # ==================== Claude 系列 ====================
    {
        "id": "claude_desktop",
        "name": "Claude Desktop",
        "icon": "smart_toy",
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
        "id": "claude_code",
        "name": "Claude Code",
        "icon": "data_object",
        "config": {},
        "format": "bash",
    },
    # ==================== 独立 AI 客户端 ====================
    {
        "id": "cherrystudio",
        "name": "CherryStudio",
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
        "id": "amp",
        "name": "Amp",
        "icon": "bolt",
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
        "id": "rovo_dev",
        "name": "Rovo Dev CLI",
        "icon": "developer_mode",
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
    # ==================== JetBrains ====================
    {
        "id": "jetbrains",
        "name": "JetBrains AI Assistant",
        "icon": "integration_instructions",
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
    # ==================== CLI 工具 ====================
    {
        "id": "gemini_cli",
        "name": "Gemini CLI",
        "icon": "stars",
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
        "id": "qwen_coder",
        "name": "Qwen Coder",
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
        "id": "openai_codex",
        "name": "OpenAI Codex",
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
        "id": "amazon_q",
        "name": "Amazon Q Developer",
        "icon": "cloud",
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
    # ==================== 桌面应用 ====================
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
    {
        "id": "warp",
        "name": "Warp",
        "icon": "rocket_launch",
        "config": {
            "piece-kb": {
                "url": "http://localhost:{port}/mcp",
                "start_on_launch": True
            },
            "piece-index": {
                "url": "http://localhost:{index_port}/mcp",
                "start_on_launch": True
            }
        }
    },
    {
        "id": "perplexity",
        "name": "Perplexity Desktop",
        "icon": "search",
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
        "id": "boltai",
        "name": "BoltAI",
        "icon": "flash_on",
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
]


def _get_config_json(client: dict) -> str:
    """生成客户端配置，按服务选择端口和密钥；未启用认证时不导出密钥。"""
    settings = get_settings()
    ports = {"piece-kb": settings.mcp.port, "piece-index": get_mcp_port()}
    api_keys = {
        "piece-kb": settings.mcp.get_api_key("retrieval"),
        "piece-index": settings.mcp.get_api_key("index"),
    } if settings.mcp.auth_enabled else {}

    if client.get("format") == "bash":
        commands = []
        for name, port in ports.items():
            lines = ["claude mcp add"]
            if api_keys.get(name):
                header = shlex.quote(f"Authorization: Bearer {api_keys[name]}")
                lines.append(f"  --header {header}")
            lines.extend(["  --transport http", f"  {name} http://localhost:{port}/mcp"])
            commands.append(" \\\n".join(lines))
        return "\n\n".join(commands)

    if client.get("format") == "toml":
        sections = []
        for name, port in ports.items():
            section = f'[mcp_servers.{name}]\nurl = "http://localhost:{port}/mcp"'
            if api_keys.get(name):
                header = json.dumps(f"Bearer {api_keys[name]}", ensure_ascii=False)
                section += f"\nhttp_headers = {{ Authorization = {header} }}"
            sections.append(section)
        return "\n\n".join(sections)

    # 替换端口后再注入密钥，避免自定义密钥中的占位符被误替换。
    config_str = json.dumps(client["config"])
    config_str = config_str.replace("{port}", str(ports["piece-kb"]))
    config_str = config_str.replace("{index_port}", str(ports["piece-index"]))
    config = json.loads(config_str)
    _add_auth_headers(config, api_keys)
    return json.dumps(config, indent=2, ensure_ascii=False)


def _add_auth_headers(config: dict, api_keys: dict[str, str]):
    """覆盖现有客户端模板的不同层级，只为对应服务添加自己的密钥。"""
    if "mcpServers" in config:
        servers = config["mcpServers"]
    elif "mcp" in config:
        servers = config["mcp"].get("servers", config["mcp"])
    elif "servers" in config:
        servers = config["servers"]
    elif "augment.advanced" in config:
        servers = {
            server["name"]: server
            for server in config["augment.advanced"].get("mcpServers", [])
        }
    else:
        servers = config  # Warp 把服务放在根级别

    for name, server_config in servers.items():
        api_key = api_keys.get(name)
        if api_key and isinstance(server_config, dict):
            server_config.setdefault("headers", {})["Authorization"] = f"Bearer {api_key}"


def _get_config_language(client: dict) -> str:
    """获取配置文件的语言类型"""
    return client.get("format", "json")


def render_mcp_config_middle(
    selected_client: dict,
    ui_refs: dict,
    on_select_client: callable,
):
    """
    渲染 MCP 配置中栏（客户端列表）

    Args:
        selected_client: 选中的客户端 {"value": str | None}
        ui_refs: UI 组件引用字典
        on_select_client: 选择客户端的回调
    """
    with ui.column().classes(
        "w-64 h-full flex flex-col overflow-hidden theme-panel gap-0"
    ).style("border-right: 1px solid var(--border-color)"):
        # 顶部标题栏
        with ui.row().classes(
            "w-full px-3 items-center justify-between"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            ui.label(t("mcp_config.title")).classes("text-sm font-medium theme-text")

        # 客户端列表（同文件列表：包一层自带 gap 的 column，对齐左栏导航）
        with ui.scroll_area().classes("flex-1 scroll-flush"):
            @ui.refreshable
            def client_list():
                with ui.column().classes("w-full gap-0.5 px-2 pt-2"):
                    for client in MCP_CLIENTS:
                        is_selected = selected_client["value"] == client["id"]
                        container_classes = "w-full px-3 py-2 cursor-pointer transition-colors rounded-md "
                        if is_selected:
                            container_classes += "theme-selected"
                        else:
                            container_classes += "theme-hover"

                        with ui.element("div").classes(container_classes).on(
                            "click", lambda _, c=client: on_select_client(c["id"])
                        ):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon(client["icon"], size="xs").classes(
                                    "theme-text-accent" if is_selected else "theme-text-muted"
                                )
                                ui.label(client["name"]).classes("text-sm theme-text")

            ui_refs["client_list"] = client_list
            client_list()


def render_mcp_config_right(
    selected_client: dict,
):
    """
    渲染 MCP 配置右栏（JSON 配置展示）

    Args:
        selected_client: 选中的客户端 {"value": str | None}
    """
    with ui.column().classes("flex-1 h-full flex flex-col theme-content gap-0"):
        # 查找选中的客户端
        client = None
        if selected_client["value"]:
            client = next(
                (c for c in MCP_CLIENTS if c["id"] == selected_client["value"]),
                None
            )

        # 顶部信息区
        if client:
            icon, title = client["icon"], client["name"]
        else:
            icon, title = "settings_input_component", t("mcp_config.title")

        with ui.row().classes(
            "w-full px-5 items-center justify-between theme-sidebar"
        ).style("border-bottom: 1px solid var(--border-color); height: 49px"):
            with ui.row().classes("items-center gap-2"):
                ui.icon(icon, size="xs").classes("theme-text-accent")
                ui.label(title).classes("text-sm font-medium theme-text")

        # 配置内容区
        with ui.scroll_area().classes("flex-1"):
            if client is None:
                # 未选择客户端
                with ui.column().classes("w-full h-full items-center justify-center"):
                    ui.icon("settings_input_component", size="lg").classes("theme-text-muted")
                    ui.label(t("mcp_config.select_client")).classes("text-sm mt-2 theme-text-muted")
            else:
                # 显示配置
                config_text = _get_config_json(client)
                config_lang = _get_config_language(client)

                with ui.card().tight().classes("w-full theme-card").style(
                    "border: 1px solid var(--border-color)"
                ):
                    with ui.column().classes("w-full gap-3 p-3"):
                        # 说明文字
                        ui.label(t("mcp_config.hint")).classes("text-sm theme-text-muted")

                        # 代码块（支持 JSON 和 TOML）
                        ui.code(config_text, language=config_lang).classes("w-full")

                        # 复制按钮
                        with ui.row().classes("w-full justify-end"):
                            async def copy_config(text=config_text):
                                await ui.run_javascript(
                                    f'navigator.clipboard.writeText({json.dumps(text)})'
                                )
                                ui.notify(t("mcp_config.copied"), type="positive")

                            ui.button(
                                t("mcp_config.copy_btn"),
                                icon="content_copy",
                                on_click=copy_config
                            ).props("flat dense").classes("theme-text-accent")
