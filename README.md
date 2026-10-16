<div align="center">

<img src="assets/maple-leaf.png" width="120" alt="Piece">

# Piece

**把电脑里的资料，变成 AI 能直接引用、你随时能核对的本地知识库。**

[![Release](https://img.shields.io/github/v/release/ultraplan-bit/Piece?style=flat-square)](https://github.com/ultraplan-bit/Piece/releases)
[![License](https://img.shields.io/github/license/ultraplan-bit/Piece?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
![Platform](https://img.shields.io/badge/Windows%20%7C%20macOS%20%7C%20Linux-555?style=flat-square)

**简体中文** | [English](README.en.md)

</div>

<a href="assets/一图看懂%20Piece.png"><img src="assets/一图看懂%20Piece.png" alt="Piece 总览：把资料变成 AI 能引用的本地知识库" width="100%"></a>

## ✨ 这是什么

PDF、Word、PPT 越攒越多，想找一句话却只能靠记忆翻文件夹。直接拖给 AI，长文档放不下几份；用在线知识库，资料又得先传到别人服务器上。

Piece 的思路很简单：**资料留在本机，建好索引，再把检索能力通过 MCP 交给你在用的 AI**。对话还是在 Claude、Cursor 里进行，Piece 只负责把相关内容和出处递过去。

<a href="assets/为什么选%20Piece.png"><img src="assets/为什么选%20Piece.png" alt="以前与用 Piece 的对比：翻文件、答案没出处、扫描件读不出、换 AI 要重来、资料传上云，对应按含义搜索、标明出处并可回看原页、接解析服务、MCP 与 Skill 接所有 AI、文件和数据库留在本机" width="100%"></a>

## 🧩 能做什么

- **导入即成卡片**：PDF、Word、PPT、Excel（`.xlsx`）、Markdown、TXT、HTML、EPUB，解析后切成一张张知识卡片。
- **卡片能改、能核对**：标题和正文随手修正，旁边对照 PDF 原页，解析错了一眼看得出。
- **混合检索**：BM25 + 向量，RRF 融合排序。
- **接上你常用的 AI**：MCP 按能力分成几个服务——查资料用只读的 `piece-kb`，想让 AI 写笔记、改卡片再加 `piece-index`，维护知识图谱用 `piece-graph`，编译知识页用 `piece-wiki`；也能导出 Skill 给终端 AI。
- **顺带整理**：层级集合分类、Wiki 知识页、知识图谱，还支持整库导入 Obsidian 与 Zotero。
- **本地优先**：SQLite + FTS5 + sqlite-vec 存储，NiceGUI 界面，服务常驻本机。

## 🖼️ 界面预览

点击图片可查看大图。

<table>
  <tr>
    <td width="50%" align="center" valign="top">
      <a href="assets/原页对比图.jpg"><img src="assets/原页对比图.jpg" alt="知识卡片与 PDF 原页并排对比" width="100%"></a>
      <br><b>原页对比</b> · 对照原文，放心核对
    </td>
    <td width="50%" align="center" valign="top">
      <a href="assets/知识卡片编辑.png"><img src="assets/知识卡片编辑.png" alt="编辑知识卡片的标题和正文" width="100%"></a>
      <br><b>卡片编辑</b> · 内容不准，随手修正
    </td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top">
      <a href="assets/召回测试.png"><img src="assets/召回测试.png" alt="输入问题，查看知识库找到的相关卡片" width="100%"></a>
      <br><b>召回测试</b> · 试着提问，看看能找到什么
    </td>
    <td width="50%" align="center" valign="top">
      <a href="assets/skill导出.png"><img src="assets/skill导出.png" alt="导出 Piece 的检索与建库 Skill" width="100%"></a>
      <br><b>Skill 导出</b> · 让终端 AI 也能使用知识库
    </td>
  </tr>
</table>

<details>
<summary>再看一个例子：PPT 公式与原页对比</summary>

[![PPT 公式与原页对比](assets/ppt原页对比图.jpg)](assets/ppt原页对比图.jpg)

</details>

## 🚀 快速开始

### 1. 下载并打开

前往 [**Releases 下载**](https://github.com/ultraplan-bit/Piece/releases)，完整解压后双击 `Piece.exe` 即可。Windows 版不用装 Python，也别只把 exe 单独拖出来。

### 2. 配置嵌入模型

打开 **设置 → 嵌入模型**，它负责让资料能按含义搜索，第一次使用需要配置。新手用默认的[硅基流动](https://siliconflow.cn/)就行：

1. 在服务商控制台获取 **API Key**；
2. 粘贴进 Piece，默认地址和模型先不用改；
3. 点「测试连接」，成功后保存。

也支持其他兼容服务，外部服务可能收费，额度和价格以服务商为准。

### 3. 导入资料

在「资料库」页面点 **+ → 上传文件**，选好文件等处理完成，就能看到知识卡片。扫描件、复杂公式或表格，先在 **设置 → PDF 解析** 里配好解析服务（PaddleOCR、MinerU 或自定义多模态模型）；普通文字版 PDF 可以直接导入。导入后到「召回测试」问一句，看看能不能找到想要的内容。

### 4. 接上你的 AI

**方式一 · MCP**：打开 **MCP 配置** 页面，选你用的客户端，复制配置、保存并重新加载。

- 只想查资料：选检索服务 `piece-kb`；
- 还要让 AI 写笔记、改卡片：再启用索引服务 `piece-index`；
- 维护知识图谱：启用图谱服务 `piece-graph`；
- 编译 Markdown 知识页：启用 Wiki 服务 `piece-wiki`；
- 查阅 Zotero 文献：单独选 `piece-zotero`。

**方式二 · Skill**：AI 能运行本机命令行的话，到 **Skill 导出** 页面导出工作流——`piece-search` 查资料，`piece-index` 导入和整理资料，`piece-wiki` 编译知识页，`piece-graph` 维护图谱，`piece-zotero` 查阅文献。

配好后，在 AI 客户端里直接提问：

> 帮我找出资料中关于这个问题的说明，并标明出处。

Piece 负责提供资料，聊天和回答仍在你常用的 AI 客户端中进行。

## 🔒 数据与隐私

<a href="assets/安心用.png"><img src="assets/安心用.png" alt="你的资料在哪里：原始文件、知识卡片与数据库、常驻服务都在本机，只有在你选择时才发送给嵌入模型、OCR 或 MinerU 解析服务、WebDAV 云同步" width="100%"></a>

文件和数据库都保存在本机。只有你主动使用云端模型、OCR 或解析服务时，相关文字才会发送给对应服务；AI 客户端也会读取你取回的资料。处理敏感内容前，请确认所用服务的隐私政策。

关闭窗口不会停止后台服务，要完全退出请在系统托盘里选「退出」。数据默认在程序旁的 `data/` 文件夹，升级或搬家前先完全退出再备份；`wiki/` 的 Markdown 和 SQLite 数据库要一起备份，不能只复制数据库。

## ❓ 常见问题

**导入失败或 AI 找不到资料怎么办？**
先检查模型连接、文件处理状态和客户端配置，扫描件还要配好解析服务，具体报错看「日志」。

**能在多台电脑间同步吗？**
可以在「云同步」里接坚果云等支持 WebDAV 的服务。它只同步文件，不是完整知识库备份，其他设备需要重新建索引；后续同步以本地为准，可能覆盖云端文件，请先备份。

<details>
<summary>进阶用法：源码运行、命令行、MCP 写入、Wiki 与图谱、Zotero</summary>

<a href="assets/资料的一生.png"><img src="assets/资料的一生.png" alt="资料在 Piece 中的六个步骤：导入、解析、切卡、整理、检索、引用" width="100%"></a>

### 源码运行

项目要求 Python 3.12+，建议用 [uv](https://docs.astral.sh/uv/getting-started/installation/) 管理环境：

```bash
git clone https://github.com/ultraplan-bit/Piece.git
cd Piece
uv sync --python 3.12 --locked --extra desktop
uv run --extra desktop piece serve --open
```

macOS / Linux 可通过源码运行、使用浏览器界面，实机兼容性仍在完善。命令行帮助可运行 `uv run --extra desktop piece --help`；Windows 便携版在终端中使用 `piece-cli.exe`。

### 命令行管理知识库

下面的 `piece`，在源码环境里换成 `uv run --no-sync piece`，便携版换成 `piece-cli.exe`。业务命令需要 Piece 服务已启动。

```bash
piece config init --interactive   # 在终端中配置嵌入模型，隐藏输入密钥，确认后才保存
piece start                       # 后台启动核心服务，确认就绪后返回
piece config test embedding       # 显式访问配置的模型服务，检查连接与维度
piece file import "资料.md" --wait
piece file list --name "资料"     # 文件名包含匹配，不是正文搜索或通配符
piece stats                       # 入库文件数、已索引文件数、切片数和原件大小总计
piece status
piece stop                        # 请求停止；服务会在退出前收尾在途工作
```

- `config init` 不带 `--interactive` 时仍是非交互初始化，脚本可接着用 `config update --offline --input 配置补丁.json`。向导不自动联网，不改已有管理/MCP 密钥；服务运行时拒绝离线保存，已有索引也不能直接切换嵌入模型、地址或维度。
- `start` 默认不启用 GUI、MCP 或托盘，需要时用 `start --gui --mcp` 或 `start --open --mcp`。后台启动默认最多等待 60 秒（`--timeout` 可调），超时或 Ctrl+C 只停止等待，不强杀后台进程。
- 使用自定义知识库时，所有命令保持相同的 `--data-dir` 和 `--port`；`--data-dir` 指配置目录，实际存储位置仍由配置的 `data_path` 决定。
- 文件已放入 `files/originals/` 时，可先 `piece file scan --dry-run` 预览，再 `piece file scan --wait` 登记并索引尚未入库的文件。

### MCP 写入与 Wiki、图谱

MCP 写入优先用图谱与 Wiki 服务里的意图工具：`knowledge-object-add` / `knowledge-object-update` / `knowledge-relation-add` / `knowledge-relation-update`（`piece-graph`）和 `wiki-page-add` / `wiki-page-update`（`piece-wiki`）。参数扁平，证据用 `CHUNK_ID:行号` 或 `CHUNK_ID:grep=子串` 内联，`request_key` 自动生成并回传。它们与 CLI 的 `graph object add`、`wiki page add` 共用同一套组装逻辑；一次提交多条记录或需要字段级控制时，再用 `knowledge-apply` / `wiki-apply` 批次工具。

Wiki 正文与元数据保存在配置 `data_path` 下的 `wiki/` Markdown 文件，SQLite 只保留可重建的页面索引和独立审计；页面内链用 `[标题](piece://wiki/页面UUID)`，改标题或文件名不改变身份。外部编辑后显式运行 `piece wiki rebuild-index` 刷新索引，不会重写 MD 或调用模型。

知识页的证据要求逐字引用原文。Agent 可用 `piece chunk extract` 从卡片正文按行号或匹配切出精确子串（CLI 与 `piece-kb` 的 `extract-quote` 都提供），加 `--out` 可直接落成可提交的证据文件；匹配不唯一时先用 `--lines` 收窄，不猜第一条。

一条命令写入知识页，默认读回、不写任何临时文件：

```bash
piece wiki page add --kind concept --title "增量维护" \
  --summary "只修改明确变化的内容" \
  --evidence CHUNK_ID:2-3
```

`--evidence CHUNK_ID:LOCATOR` 可重复，LOCATOR 为行号 `N` / `N-M`、`grep=子串` 或 `re=正则`，服务按定位切出精确引文并带回哈希。`--stance` 给一次作用于全部证据，给多次按顺序逐条对应。页面更新用 `wiki page update UUID --expected-revision N --expected-content-hash HASH`，图实体用 `graph object add/update`，关系用 `graph relation add/update`。

每次写入 CLI 自动生成请求键并回传 `data.request_key`；需要本地留档加 `--save-request`（存到数据目录 `requests/`）或 `--request-file 路径`，文件已存在时只有目标与内容都相同才允许复用。`--dry-run` 只预检，确认后去掉它原样重跑即可提交。AI 客户端自己取得的网页、文章、字幕等正文也能整篇导入：MCP 用索引服务的 `import_markdown`，Skill 用 `piece file import-markdown`。

### Zotero 协作

可直接查询本机个人 Zotero 库，读取书目、笔记、PDF 批注和可用全文，不要求先导入 Piece。需要深入核对时再选附件导入，相同内容复用已有文件并保留多个来源。

```bash
piece zotero status --json
piece zotero search "关键词" --collection-key COLLECTION_KEY --limit 20 --json
piece zotero children ITEM_KEY --item-type attachment --json
piece zotero preview --attachment-key ATTACHMENT_KEY --collection-mode none --server-id SERVER_ID --json
piece zotero import --attachment-key ATTACHMENT_KEY --collection-mode none --server-id SERVER_ID --wait --json
```

独立 MCP 服务 `piece-zotero` 提供查阅、`zotero_import`、授权、变更预览与执行，其余服务不注册 Zotero 工具。需要 Zotero 7+ 运行并开启「设置 → 高级 → 允许本机其他应用程序与 Zotero 通信」。前端入口是 **工具 → Zotero 文献库**，导入是一次性复制，不改动原库。修改 Zotero 需要显式启用配置 `zotero_write_enabled`（默认 `false`），先在 Zotero 弹窗授权，再预览差异并按 `plan_id` 执行。详见 [Zotero 操作说明](skills/piece-zotero/SKILL.md)。

### 小技巧

- **搜得不理想**：先检查卡片正文，再把标题改具体，例如「接口鉴权：Token 刷新流程」比「第三章」更容易辨认。
- **资料太多**：用层级集合组织成「论文 → 检索增强」等范围，父集合默认包含子集合中的文件，一份文件可归入多个集合。
- **想带走内容**：导出 Markdown 可保留卡片修改，带资源导出还会一起打包插图。
- **想重新解析**：可以重新索引，但从原件重新索引会覆盖手工修改，请先导出或备份。

</details>

## 💬 社区

本项目认可并链接 [LINUX DO](https://linux.do) 社区。遇到问题或有建议，欢迎 [提交 Issue](https://github.com/ultraplan-bit/Piece/issues)，请附上复现步骤和脱敏截图，不要上传密钥或私人资料。

## 📄 许可证

本项目基于 [MIT 许可证](LICENSE) 开源。
