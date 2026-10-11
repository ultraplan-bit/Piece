---
name: piece-wiki
description: 使用 Piece CLI 按需把资料编译成 Markdown 知识页、为页面追加有证据的内容、保存用户要求沉淀的综合结论，以及核查既有页面和失效来源。适用于知识页编译、页面增量修订、综合页与知识核查；不是普通原件导入，不维护知识图谱实体与语义关系，不自动保存所有回答。
---

# Piece Wiki 编译与核查

用 Piece CLI 按需把资料编译成 Markdown 知识页、为页面追加有证据的内容、保存用户要求沉淀的综合结论，并核查既有页面和失效来源。Wiki 页面以 Markdown 文件为正式存储；图谱实体与语义关系是另一套独立功能，用 `piece-graph` 维护。Wiki 基本操作不调用模型或嵌入接口。

## 先核对实例

执行一次 `<PIECE> status --json`，核对配置目录、端口、`cli_version` 与就绪时的 `library_id`，沿用导出头部的前缀。服务未运行（`SERVICE_UNAVAILABLE`）就提示用户启动；命令不存在时要求更新并重新导出 Skill。

## 先选路线

| 用户意图 | 路线 |
| --- | --- |
| 查资料、回答问题 | `wiki search` → `wiki get page` → 必要时核对来源 → 回答，不写入 |
| 保存结论、编译或修改知识页 | 按下文最小命令写入 |
| 只给已有页面补证据 | `references/apply.md#仅补证据`；不改摘要凑更新 |
| 核查失效与争议 | `wiki lint` → 读候选页面与来源 → 报告，不自动修复 |
| 索引缺失或外部改了 MD | `wiki rebuild-index`（见 `references/recovery.md`） |
| 清理本地恢复副本 | `wiki list-recovery` → 预览 `wiki purge-recovery` → 用户确认后加 `--yes`（见 `references/recovery.md`） |
| 删除 | 先预览影响，用户确认后才执行（见 `references/safety.md`） |

## 最小命令

一次写入就是一条命令，不写任何临时文件：

```text
<PIECE> status --json
<PIECE> wiki search "关键词" --limit 5 --json
<PIECE> chunk get CHUNK_ID --json
<PIECE> wiki page add --kind concept --title "标题" --summary "摘要" --evidence CHUNK_ID:2-3 --json
<PIECE> wiki get page PAGE_UUID --json
<PIECE> wiki page update PAGE_UUID --expected-revision 3 --expected-content-hash ABC... --summary "新摘要" --evidence CHUNK_ID:grep=关键句 --json
```

正文较长时用 `--body-file -` 从 stdin 读（heredoc 即可），不必先写文件。

只在用户要求保存时写入；删除先 `--dry-run` 预览影响，用户确认后才加 `--yes`。失败回执带 `next_command` 时照它执行一次。

## 按需读参考

- 页面 kind 与链接规则、`index_status`、本地恢复副本、证据字段与只读查询命令：`references/semantics.md`
- 失败码（`VERSION_CONFLICT`、`READ_BACK_INCOMPLETE`、部分成功、提交结果未知等）与恢复动作、读回字段、请求键与留档、索引重建：`references/recovery.md`
- 删除预览与确认、备份范围、安全红线：`references/safety.md`
- 一次提交多条页面 / 证据、字段级契约、仅补证据批次：`references/apply.md`
