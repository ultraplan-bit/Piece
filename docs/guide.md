# Piece 使用指南

[返回 README](../README.md) · **简体中文** | [English](guide.en.md)

README 介绍用途和第一次使用的路径。本指南说明更多接入方式、命令行操作，以及数据交换和维护时的限制。

## 接入 AI

### MCP：把能力提供给 AI 客户端

打开 **工具与集成 → MCP 配置**，选择客户端和需要的服务，使用页面生成的配置，保存到客户端并重新加载。使用期间保持 Piece 服务运行。

第一次只启用 `piece-kb`，确认 AI 能查到资料，再根据任务增加其他服务：

| 服务 | 用途 |
| --- | --- |
| `piece-kb` | 只读检索、取回知识卡片与原文、提取精确引文 |
| `piece-index` | 导入文档、写笔记、新增或编辑知识卡片 |
| `piece-wiki` | 查阅、编译和维护 Markdown 知识页及来源证据 |
| `piece-graph` | 查阅和维护图谱实体、语义关系及来源证据 |
| `piece-zotero` | 查阅本机 Zotero 文献，按需导入附件及执行授权后的修改 |

不同客户端支持的连接方式不同，请以配置页面和客户端说明为准。不要因为需要检索就同时开放所有写入能力。

### Skill：让终端 AI 使用工作流

如果 AI 能运行本机命令行，可以在 **工具与集成 → Skill 导出** 中选择工作流，导出到客户端支持的 Skill 目录。导出内容会带上当前 Piece 实例的 CLI 调用前缀。

| Skill | 用途 |
| --- | --- |
| `piece-search` | 查资料、取回正文、核对来源，只读 |
| `piece-index` | 导入和整理资料、创建笔记、维护知识卡片 |
| `piece-wiki` | 编译、修订和核查知识页 |
| `piece-graph` | 维护实体、关系和图谱证据 |
| `piece-zotero` | 查阅和按需导入 Zotero 文献，执行授权后的修改 |

导出后按客户端的说明加载。实例路径、端口或版本变化后，请重新导出，避免沿用过时的调用前缀。

## 源码运行

项目要求 Python 3.12+，建议使用 [uv](https://docs.astral.sh/uv/getting-started/installation/) 管理环境：

```bash
git clone https://github.com/ultraplan-bit/Piece.git
cd Piece
uv sync --python 3.12 --locked --extra desktop
uv run --extra desktop piece serve --open
```

Windows 可使用桌面窗口；macOS / Linux 可使用浏览器界面，实机兼容性仍在完善。

命令行帮助可运行 `uv run --extra desktop piece --help`；Windows 便携版在终端中使用 `piece-cli.exe`。

## 命令行

下面的 `piece`，在源码环境中换成 `uv run --no-sync piece`，便携版换成 `piece-cli.exe`。文件、检索和知识管理等业务命令需要 Piece 服务已启动。

```bash
piece config init --interactive   # 交互配置嵌入模型，隐藏密钥，确认后才保存
piece start                       # 后台启动核心服务，确认就绪后返回
piece config test embedding       # 访问配置的模型服务，检查连接与维度
piece file import "资料.md" --wait
piece file list --name "资料"     # 文件名包含匹配，不是正文搜索或通配符
piece stats                       # 入库与已索引文件数、切片数、原件大小总计
piece status
piece stop                        # 请求停止，服务会收尾在途工作
```

### 配置与服务启动

- `config init` 不带 `--interactive` 时是非交互初始化，脚本可接着用 `config update --offline --input 配置补丁.json`。
- 配置向导不自动联网，不改已有管理/MCP 密钥。服务运行时拒绝离线保存，已有索引不能直接切换嵌入模型、地址或维度。
- `start` 默认不启用 GUI、MCP 或托盘。需要时用 `start --gui --mcp` 或 `start --open --mcp`。
- 后台启动默认最多等待 60 秒，`--timeout` 可调。超时或 Ctrl+C 只停止等待，不强杀后台进程。
- 使用自定义知识库时，所有命令保持相同的 `--data-dir` 和 `--port`。`--data-dir` 指配置目录，实际存储位置仍由配置的 `data_path` 决定。
- 文件已放入 `files/originals/` 时，可先 `piece file scan --dry-run` 预览，再 `piece file scan --wait` 登记并索引尚未入库的文件。

### 机器可读的帮助

AI 客户端需要结构化参数时，在 `--help` 上加 `--json`：

```bash
piece file list --json --help
```

返回该命令的参数名、取值形态、可选值与默认值，可从 `piece` → `piece file` → `piece file list` 逐层下钻，不必解析散文帮助或靠试错猜参数。

## 文档导入与整理

### 先确认解析结果

普通文字版 PDF 可以使用本地解析；扫描件、复杂公式或表格，需要在设置的 PDF 解析分组中配置合适的服务。可选 PaddleOCR、MinerU 或自定义多模态模型等，服务部署方式决定相关文档是否离开本机。

导入后先检查卡片正文，再到 **工具与集成 → 召回测试** 输入问题、查看候选。解析不准时可以修正标题和正文；能查看原页的文件，也可以并排对照。

### 分类、迁入与导出

- 用层级集合组织成「论文 → 检索增强」等范围。父集合默认包含子集合中的文件，一份文件可归入多个集合。
- 从 Obsidian 或 Zotero 导入是一次性复制，不是双向同步，也不会改动原库。Zotero 还支持不导入就直接查阅，见下文。
- AI 客户端自己取得的网页、文章、字幕等正文也能整篇导入：MCP 用索引服务的 `import_markdown`，Skill 用 `piece file import-markdown`。这不是 Piece 自动抓取网页。
- 导出 Markdown 可保留卡片修改，带资源导出还会一起打包插图。
- **从原件重新索引会覆盖手工修改**，请先导出或备份。

## Wiki 与知识图谱

Wiki 和图谱是两套独立功能：Wiki 保存主题页面，图谱保存实体与关系。它们可以各自关联原文证据，不要求先有图谱才能写 Wiki。

导入文档不会自动编译知识页或生成关系。让 AI 整理时，应明确要保存什么；普通查阅和回答不需要写入。

### Wiki 的存储与外部编辑

Wiki 正文与元数据保存在配置 `data_path` 下的 `wiki/` Markdown 文件。SQLite 只保留可重建的页面索引和独立审计；页面内链用 `[标题](piece://wiki/页面UUID)`，改标题或文件名不改变身份。

外部编辑后显式运行：

```bash
piece wiki rebuild-index
```

它只刷新索引，不会重写 Markdown 或调用模型。页面索引可重建，但不代表整个 SQLite 数据库可以丢弃，备份仍需包含数据库与 `wiki/`。

### 用 CLI 写入知识页

一条命令写入，默认读回，不写临时文件。请将 `CHUNK_ID` 换成已经查到的真实卡片 ID，行号也要以该卡片正文为准：

```bash
piece wiki page add --kind concept --title "增量维护" \
  --summary "只修改明确变化的内容" \
  --evidence CHUNK_ID:2-3
```

- `--evidence CHUNK_ID:LOCATOR` 可重复。LOCATOR 为行号 `N` / `N-M`、`grep=子串` 或 `re=正则`；服务按定位切出精确引文并带回哈希。
- `--stance` 给一次作用于全部证据，给多次按顺序逐条对应。
- 页面更新用 `wiki page update UUID --expected-revision N --expected-content-hash HASH`。图实体用 `graph object add/update`，关系用 `graph relation add/update`。
- 每次写入自动生成请求键，回传 `data.request_key`。需要本地留档时加 `--save-request`，存到数据目录的 `requests/`；或用 `--request-file 路径`，已有文件仅在目标与内容都相同时允许复用。
- `--dry-run` 只预检，确认后去掉它原样重跑即可提交。

知识页的证据要求逐字引用原文。Agent 可用 `piece chunk extract` 按行号或匹配切出精确子串；检索 MCP 的 `extract-quote` 也提供这项能力。CLI 加 `--out` 可直接生成证据文件。匹配不唯一时先用 `--lines` 收窄，不猜第一条。

### 用 MCP 写入

优先使用扁平参数的意图工具：

- `piece-wiki`：`wiki-page-add` / `wiki-page-update`。
- `piece-graph`：`knowledge-object-add` / `knowledge-object-update` / `knowledge-relation-add` / `knowledge-relation-update`。

证据用 `CHUNK_ID:行号` 或 `CHUNK_ID:grep=子串` 内联，`request_key` 自动生成并回传。它们与 CLI 的 `wiki page add`、`graph object add` 共用同一套组装逻辑。

一次提交多条记录或需要字段级控制时，再用 `wiki-apply` / `knowledge-apply` 批次工具。

具体操作与核查流程见内置 [Wiki Skill](../skills/piece-wiki/SKILL.md) 和[图谱 Skill](../skills/piece-graph/SKILL.md)。

## 图谱数据交换

### 选择格式

| 格式 | 适合用途 | 信息范围 |
| --- | --- | --- |
| JSON | Piece 数据交换，需要无损交换时选用 | 保留 Piece 图谱数据及证据定位等信息 |
| GraphML | 与通用图谱工具交换标准图结构 | 不含证据、别名、修订号和时间戳，不承诺无损往返 |

GraphML 是标准通用图谱投影，普通属性不使用 Piece 私有 JSON 编码。

### GraphML 导入限制

- 仅支持单层有向图及约定属性。
- 节点 `label` 为标题，边 `label` 为 Piece 支持的关系名；未知业务关系会拒绝。
- 对称关系用标准 boolean 属性 `symmetric` 标记。
- 非 UUID 的节点／边 ID 会映射为内部 UUID。
- 不支持旧版 Piece GraphML 导入，也不承诺任意第三方业务语义映射。

导入默认执行完整校验后回滚预览，确认且无冲突后才写入。跨库 JSON 导入保留证据的原始定位和哈希，但未接入来源仍标记为未解析；保留证据记录不等于目标库已经拥有对应原文。

## Zotero 协作

可直接查询本机个人 Zotero 库，读取书目、笔记、PDF 批注和可用全文，不要求先导入 Piece。需要深入核对时再选附件导入，相同内容复用已有文件并保留多个来源。

### 连接与查阅

需要 Zotero 7+ 运行，并开启 **设置 → 高级 → 允许本机其他应用程序与 Zotero 通信**。Piece 前端入口是 **工具与集成 → Zotero 文献库**。

```bash
piece zotero status --json
piece zotero search "关键词" --collection-key COLLECTION_KEY --limit 20 --json
piece zotero children ITEM_KEY --item-type attachment --json
piece zotero preview --attachment-key ATTACHMENT_KEY --collection-mode none --server-id SERVER_ID --json
piece zotero import --attachment-key ATTACHMENT_KEY --collection-mode none --server-id SERVER_ID --wait --json
```

请将命令中的 key 和 `SERVER_ID` 换成查询得到的真实值。导入是一次性复制，不改动原库。

### 修改 Zotero

独立 MCP 服务 `piece-zotero` 提供查阅、`zotero_import`、授权、变更预览与执行，其余服务不注册 Zotero 工具。

修改 Zotero 需要显式启用配置 `zotero_write_enabled`，默认 `false`；先在 Zotero 弹窗授权，再预览差异并按 `plan_id` 执行。详见 [Zotero 操作说明](../skills/piece-zotero/SKILL.md)。

## 数据与维护

### 本地存储与外部服务

原始文件、知识卡片、数据库与 Wiki 文件保存在本机，实际位置由配置的 `data_path` 决定。Windows 便携版默认使用程序旁的 `data/`。

「本地存储」不代表所有处理都在本地：

- 云端嵌入模型接收用于索引的卡片文本，以及语义检索的查询文本。
- 云端 OCR、MinerU 或多模态解析服务可能接收原始文档、页面图像或相关内容。
- AI 客户端取得检索结果、原文或插图后，可能继续交给其模型服务处理。
- 启用 WebDAV 后，待同步文件会发送到你配置的云存储。

处理敏感内容前，请核对各个服务的部署位置、授权范围与隐私政策。

### 完全退出与备份

Windows 版关闭窗口不会停止后台服务，要完全退出请在系统托盘里选「退出」。CLI 启动的服务可用 `piece stop` 请求停止，并等待退出完成。

升级或迁移前，完全退出后备份整个数据目录，包含 SQLite 数据库和 `wiki/` Markdown。不要只复制数据库，也不要因为 Wiki 索引可重建就省略其他数据库数据。

**v0.1.3 不兼容 v0.1.2 及更早版本的旧数据库，也不提供自动迁移。** 请保留旧版程序和完整数据备份；体验新版请解压到新目录，并使用空数据目录，不要将旧 `data/` 复制到新版程序旁或覆盖旧版安装。

### WebDAV 同步

可以在「云同步」中连接坚果云等支持 WebDAV 的服务，但它**只同步文件，不是完整知识库备份**：

- 其他设备需要重新建索引。
- 首次同步遇到同路径但内容不同的文件会暂停，不会推进同步状态；请先手动重命名或处理冲突，再重试。
- 后续同步以本地为准，可能覆盖或删除云端文件。新旧版本共用云端目录，旧版没有冲突保护；同步前请停止旧版并升级所有设备。
- Obsidian / Zotero 一次性导入也不等于持续同步。

同步、重新索引或迁移前请先备份。

## 常见问题

**导入失败怎么办？**

检查模型连接和文件处理状态。扫描件还要检查解析服务；具体报错看 **工具与集成 → 日志**。

**检索结果不理想怎么办？**

先检查卡片正文是否正确、文件是否完成索引，再确认查询是否被文件或集合范围限制。标题可以改得具体些，例如「接口鉴权：Token 刷新流程」比「第三章」更容易辨认。

**Piece 中能找到资料，AI 却找不到怎么办？**

检查 Piece 服务是否运行，以及客户端是否加载了当前实例的 MCP 配置或 Skill。先用只读检索能力验证连接，不必同时开放写入。

## 技术说明

Piece 使用 BM25 与向量混合检索，通过 RRF 融合排序；本地存储使用 SQLite、FTS5 和 sqlite-vec，管理界面使用 NiceGUI。技术选型不改变外部嵌入、解析服务和 AI 客户端的数据流向，隐私边界仍以上述配置为准。
