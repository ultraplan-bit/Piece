# Zotero 查阅、导入与写入契约

要查阅 Zotero 文献或全文、导出引用、导入 PDF，或准备 `changes` 写入计划时读本文。

## 只读查阅

- **范围**：`<PIECE> zotero collections --limit 50 --offset 0 --server-id ID --json`、`<PIECE> zotero tags --server-id ID --json`。按 `next_offset` 续取；标签可用 `--collection-key KEY` 限定该集合的直接条目及其附件、笔记，不含子集合。
- **搜索**：`<PIECE> zotero search "关键词" --collection-key KEY --limit 20 --server-id ID --json`；可加 `--tag`、`--item-type`。默认只匹配标题、作者和年份；加 `--scope everything` 用 Zotero 的“所有字段与标签”快速搜索，额外覆盖摘要、笔记、批注和已建全文索引的 PDF 正文，响应 `fulltext_searched/notes_searched/annotations_searched` 如实标明本次范围。全文命中依赖 Zotero 自己的索引，未索引的附件不会命中。指定集合不递归子集合，空结果不等于全库无相关内容。
- **条目**：`<PIECE> zotero item ITEM_KEY --server-id ID --json` 取完整书目或笔记正文，没有 PDF 也可读取。列表出现 `truncated_fields` 时按 key 取全，不把截断内容当完整内容。
- **笔记与附件**：`<PIECE> zotero children ITEM_KEY --item-type note --server-id ID --json`，或 `--item-type attachment`。保持文献、附件、笔记的父子关系。
- **批注**：`<PIECE> zotero children ATTACHMENT_KEY --item-type annotation --server-id ID --json`；父项必须是附件 key。保留高亮、评论、页码标签的区别，用户评论不是论文结论。
- **全文**：`<PIECE> zotero fulltext ATTACHMENT_KEY --offset 0 --limit 12000 --server-id ID --json`。偏移按字符计，检查 `coverage`、`complete` 和 `next_offset`；只代表 Zotero 已索引文本。缺失或不完整就说明，不能用摘要冒充全文；公式、表格和原页需实际 PDF 核对。返回 `ZOTERO_FULLTEXT_UNAVAILABLE` 时，`data.next_command` 给出按同一附件 `zotero preview --attachment-key KEY` 的替代路径：预览不写入，用户确认后用相同范围 `zotero import` 让 Piece 解析实际 PDF，索引完成后再读正文。
- **引用**：`<PIECE> zotero citations ITEM_KEY --format bib --style apa --server-id ID --json`。`bib` 是 HTML 书目，另有 `citation`、`bibtex`、`ris`、`csljson`；传文献 key，不传附件/笔记 key。响应带 `warning` 时条目数与请求不符，逐项核对。引用格式正确不代表内容已核验。
- **Piece 关联**：`<PIECE> zotero links --item-key ITEM_KEY --server-id ID --json`，或 `--attachment-key`。返回 `file_id`、状态和来源版本，按分页续取。旧版无稳定实例身份时，不按旧 key 猜关联，可用导入预览按实际附件内容查重。

查阅到此即可结束，不自动导入、创建知识页、请求写授权或保存回答。作答区分书目摘要、论文原文、用户批注与综合判断，并引用实际取得的 key、附件和可用页码。

## 选定 PDF 导入 Piece

1. 从查询结果明确选择文献或附件，优先精确附件范围。预览：
   `<PIECE> zotero preview --attachment-key ATTACHMENT_KEY --collection-mode none --server-id ID --json`。
2. 核对附件可用性、重复内容、已有 `file_id` 与 `file_status`。确认后以相同选择导入：
   `<PIECE> zotero import --attachment-key ATTACHMENT_KEY --collection-mode none --server-id ID --wait --timeout 600 --json`。
3. 也可用 `--item-key KEY` 或 `--zotero-collection KEY`（包含子集合）。三类选择器互斥，各可重复。`--collection-mode path|top|none` 控制集合映射；`path` 生成完整路径名称，不创建对应的 Piece 目录树。`--collection NAME` 追加已存在的 Piece 资料集，可先 `collection tree --json` 核对。
   每批最多 50 个 PDF 附件；集合展开后超过上限返回 `IMPORT_LIMIT_EXCEEDED`，说明范围本身有效只是太大，改选子集合或按条目/附件分批，不要退回整库。
4. 保留 `task_ids`、逐项失败和跳过项。需要续等时用 `<PIECE> task wait ID... --timeout 600 --json`，完成后 `file get FILE_ID --json` 读回。不把受理、`all_done` 或内容重复当成索引成功；只有所有任务 `completed` / `all_succeeded` 才是成功。无新任务的重复文件也可能仍在处理或已有错误。

CLI 未指定范围会处理整库，只能在用户明确要求并预览后使用。旧版无 `server_id` 时省略它，按附件内容去重。导入只复制附件并补来源关联，不接管 Zotero 原件，不覆盖 Piece 用户属性，不自动写 Wiki、图谱或 Zotero 笔记。

## 写入契约

要求 **Zotero 10.0.5+**。`zotero_write_enabled` 默认关闭，不能为绕过拒绝自行开启。只有用户明确要求时，才用 `config update --input 配置补丁.json --json` 提交 `{"zotero_write_enabled": true}`。

1. 把具体目标和变更写入 JSON 文件，用 `<PIECE> zotero preview-changes --input changes.json --server-id ID --json` 展示 `before` / `after`、本地版本和目标 key。预览不写入，`plan_id` 在当前 Piece 进程保留 15 分钟。
2. 仅在用户明确要求授权时运行 `<PIECE> zotero authorize --server-id ID --json`，等待 Zotero 弹窗决定。拒绝后不自动重试。授权细节见 [safety.md](safety.md)。
3. 用户确认具体预览后，`<PIECE> zotero apply-changes PLAN_ID --yes --json`。只接受计划 ID，不重新传另一份变更；不擅自补 `--yes`。

```json
{"changes":[
  {"action":"update_item","item_key":"ABCD2345","add_tags":["已读"]},
  {"action":"create_note","parent_item_key":"ABCD2345","content":"阅读总结与待核查问题","source_item_keys":["ABCD2345"]}
]}
```

示例 key 必须换成实际结果。允许的 action 与字段：
- `update_item`：`item_key`、`fields`、`add_tags/remove_tags`、`add_collections/remove_collections`。同条目的变更合为一项。
- `create_item`：`item_type`（如 `journalArticle`、`book`），可选 `fields`、`creators`、`tags`、`collection_keys`。字段按该类型白名单校验，集合须存在；新条目预分配 key，`before` 为空。
- `create_note`：纯文本 `content`，可选 `parent_item_key`，或独立笔记的 `collection_keys`（不能同时指定）；`source_item_keys` 生成出处链接。
- `update_note`：`item_key`、纯文本 `content`、可选 `source_item_keys`；会替换指定笔记正文，优先新建总结而非覆盖用户笔记。
- `delete_item`：`item_key`。只把条目移入 Zotero 回收站（`deleted: true`），不物理删除；已在回收站的条目拒绝。
- `create_collection`：`name`，可选 `parent_key`（须存在）。`rename_collection`：`collection_key`、`name`。`delete_collection`：`collection_key`，集合进回收站，其中条目不删、只解除归属。集合与条目走不同端点，同一计划内分别写入、分别读回核对。

写入边界：删除只进回收站，不物理删除；不覆盖附件，不创建精确高亮，不改变条目类型或现有对象父项。
