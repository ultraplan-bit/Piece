<div align="center">

<img src="assets/maple-leaf.png" width="120" alt="Piece 图标">

**简体中文** | [English](README.en.md)

</div>

## 能帮你做什么

- **查资料**：不用反复翻文件，让 AI 帮你寻找相关内容并标明出处。
- **改卡片**：资料会整理成一张张知识卡片，标题和正文都能自己修改。
- **看原文**：对照 PDF 或转换后的 Word、PPT 原页，检查有没有解析错误。
- **整理笔记**：用集合给资料分类，也可以让 AI 帮你新建笔记、补充卡片。

## 界面预览

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

## 快速上手

### 1. 下载并打开

前往 [**Releases 下载**](https://github.com/ultraplan-bit/Piece/releases)，完整解压 ZIP，双击 `Piece.exe` 即可。Windows 版不需要额外安装 Python，也不要只把 exe 单独移出来。

### 2. 配置搜索用的模型

打开 **设置 → 嵌入模型**。它负责让资料能够按含义搜索，第一次使用需要配置。

新手可以使用默认的[硅基流动](https://siliconflow.cn/)服务：

1. 在服务商控制台获取自己的 **API Key**。
2. 粘贴到 Piece 中，默认地址和模型先不用改。
3. 点击「测试连接」，成功后保存。

也支持其他兼容服务。外部服务可能收费，额度和价格以服务商为准。

### 3. 导入资料

在「文件库」页面点击 **+ → 上传文件**，选择要导入的文件，等待处理完成，就能看到知识卡片。支持 **PDF、Word、PPT、Excel（`.xlsx`）、Markdown 和 TXT**。

- **扫描件、复杂公式或表格**：在「设置 → PDF 解析」中配置 PaddleOCR 服务、MinerU 或自定义多模态模型，按页面提示填写地址、密钥并测试连接。普通文字版 PDF 可以先直接导入。
- **Word / PPT**：建议安装 Microsoft Office 或 LibreOffice；`.doc`、`.ppt`、`.rtf`、`.odt`、`.odp` 等格式必须有可用的转换工具。
- **已有的 Obsidian 笔记或 Zotero 文献**：同一菜单里的 **导入本机目录** 可整库导入 Obsidian vault（自动跳过 `.obsidian`、模板和纯链接的索引页）；**从 Zotero 导入** 会把带 PDF 的条目连同作者、年份、DOI 等属性一并导入，需要 Zotero 7 或更新版本正在运行，并在 Zotero 的「设置 → 高级」中勾选「允许本机其他应用程序与 Zotero 通信」。两者都是一次性复制，不会改动原库。

导入后可以打开「召回测试」，输入一个相关问题，看看能否找到想要的内容。

### 4. 接上你常用的 AI

**方式一：MCP 配置**

MCP 可以理解为 AI 访问知识库的连接方式。打开 Piece 的 **MCP 配置** 页面，选择你使用的客户端，复制配置，按客户端要求保存并重新加载。

- **只想查资料**：选择检索服务 `piece-kb`。
- **还想让 AI 写笔记、改卡片**：再启用索引服务 `piece-index`。

不要分享配置里的访问密钥。修改密钥或端口后，需完全退出并重启 Piece，再更新客户端配置。

**方式二：Skill 导出**

如果你的 AI 能运行本机命令行，也可以在 **Skill 导出** 页面导出并安装工作流：`piece-search` 用于查资料，`piece-index` 用于导入和整理资料。移动程序或更换数据目录后，请重新导出。

连接完成后，在你的 **AI 客户端**中提问，例如：

> 帮我找出资料中关于这个问题的说明，并标明出处。

Piece 负责提供资料，聊天和回答仍在你常用的 AI 客户端中进行。

## 用顺手的几个小技巧

- **搜得不理想**：先检查卡片正文，再把标题改具体，例如「接口鉴权：Token 刷新流程」，比「第三章」更容易辨认。
- **资料太多**：用集合分成「论文」「工作」「学习」等范围，查找更方便。
- **想带走内容**：导出 Markdown 可保留卡片修改；带资源导出还会一起打包插图。
- **想重新解析**：可以重新索引，但**从原件重新索引会覆盖手工修改**，请先导出或备份。

## 常见问题

**文件会被上传吗？**

文件和数据库保存在本地。但使用云端模型或解析服务时，文字、问题或页面内容会发送给对应服务；AI 客户端也会读取你取回的资料。处理敏感内容前，请确认所用服务的隐私政策。

**关掉窗口后，AI 还能查吗？**

可以，关闭管理窗口不会停止后台服务。要完全退出，请在系统托盘中选择「退出」。

**数据在哪里，怎么备份？**

Windows 解压版默认保存在程序旁的 `data/` 文件夹。升级或搬家前，先完全退出 Piece，再备份该文件夹；如果改过存储位置，也要备份对应的数据目录。

**能在多台电脑间同步吗？**

可在「云同步」连接坚果云等支持 WebDAV 的服务。它只同步文件，不是完整知识库备份；其他设备需要重新建立索引。**后续同步以本地为准，可能覆盖或删除云端文件，请先备份。**

**导入失败或 AI 找不到资料怎么办？**

先检查模型连接、文件处理状态，以及 AI 客户端配置是否正确。扫描件还需要配置解析服务；具体错误可在「日志」中查看。

<details>
<summary>源码运行与命令行（可选）</summary>

项目要求 Python 3.12+，建议使用 [uv](https://docs.astral.sh/uv/getting-started/installation/) 管理环境：

```bash
git clone https://github.com/ultraplan-bit/Piece.git
cd Piece
uv sync --python 3.12 --locked --extra desktop
uv run --extra desktop piece serve --open
```

macOS / Linux 可通过源码运行，使用浏览器界面，实机兼容性仍在完善。命令行帮助可运行 `uv run --extra desktop piece --help`；Windows 便携版在终端中使用 `piece-cli.exe`。业务命令需要 Piece 服务已启动。

</details>

---

遇到问题或有建议，欢迎 [提交 Issue](https://github.com/ultraplan-bit/Piece/issues)。请附上复现步骤和脱敏截图，不要上传密钥或私人资料。

## 社区

本项目认可并链接 [LINUX DO](https://linux.do) 社区。

## 许可证

本项目基于 [MIT 许可证](LICENSE) 开源。
