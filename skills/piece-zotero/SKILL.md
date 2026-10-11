---
name: piece-zotero
description: 使用 Piece CLI 查阅本机 Zotero 文献、集合、标签、笔记、批注和可用全文，导出引用；按需选择 PDF 导入 Piece，并在用户明确授权、预览和确认后修改书目、标签、集合归属、新建条目或集合、保存笔记，或把条目/集合移入回收站。适用于文献比较、阅读整理与 Zotero–Piece 协作。
---

# Zotero 本机协作

用 Piece CLI 查阅本机 Zotero 文献、集合、标签、笔记、批注和可用全文，导出引用，按需把 PDF 导入 Piece；写入 Zotero 需用户明确授权、预览并确认。适用于 Piece 0.1.x、本地 API v1，首版仅个人库。

## 先核对目标

执行 `<PIECE> status --json` 核对 Piece 实例与知识库，再执行 `<PIECE> zotero status --json` 检查 `success`、`data.connected` 与 `data.error`；服务未运行就提示用户运行 `<PIECE> serve`。有 `server_id` 时后续命令始终带 `--server-id ID`。参数不确定时先查子命令帮助，不另写请求、轮询或重试脚本。

## 最小命令

```text
<PIECE> zotero status --json
<PIECE> zotero search "关键词" --collection-key KEY --limit 20 --server-id ID --json
<PIECE> zotero item ITEM_KEY --server-id ID --json
<PIECE> zotero preview --attachment-key ATTACHMENT_KEY --collection-mode none --server-id ID --json
<PIECE> zotero import --attachment-key ATTACHMENT_KEY --collection-mode none --server-id ID --wait --timeout 600 --json
```

只在用户要求保存时写入；删除先预览影响，用户确认后才执行。失败回执带 `next_command` 时照它执行一次。

## 按需读参考

- 只读查阅各子命令（集合 / 标签 / 条目 / 笔记附件 / 批注 / 全文 / 引用 / Piece 关联）、导入选择器与 `changes` 各 action 字段表、写入边界：`references/semantics.md`
- 失败码、授权与超时恢复、计划过期与部分成功：`references/recovery.md`
- 授权模型（单次 / 记住 / 撤销）、删除与回收站、安全红线：`references/safety.md`
