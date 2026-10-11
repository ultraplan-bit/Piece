# Wiki 页面语义、证据与只读查询

要新建或修改页面、判断 kind 与链接、理解 `index_status` 或本地恢复副本，或需要只读查询命令时读本文。

资料导入和检索是另一路径，不建图谱实体关系。

## 页面与链接

- 页面 `--kind`：概念/方法用 `concept`，人物/机构/产品用 `entity`，主题用 `topic`，综合结论用 `synthesis`，来源摘要用 `source_summary`。更新必须带已读到的 `revision` 和 `content_hash` 及至少一个待改字段；未传字段保持原值。
- 页面身份是稳定 UUID，重命名不改变身份；显示标题可以相同，按 UUID 区分，不按名称合并。查已有页面用 `wiki search` 找同名与别名，`file list`、`search` 找相关资料；读取候选的类型、摘要、`content_hash` 与证据后判断。
- 页面内链写 `piece://wiki/<UUID>`，只用于导航，不进入图谱语义关系；链接由 MD 内容派生，`wiki lint` 会报告失效或未登记链接。
- MD 可以在 Piece 之外被外部编辑。`wiki get`/`wiki list` 返回 `content_hash`、`path`、`index_status`；`index_status` 不是 `current` 时先 `wiki rebuild-index`，不要用数据库旧正文反写文件。
- 更新或删除会把被替换/移除的页面文件保留为本地恢复副本 `.wiki-recovery-<operation_id>.bak`（不参与扫描，不是另一份真源），回执与历史会给出恢复路径；因此不要宣称操作后内容被彻底擦除。备份须覆盖它。副本不会自动清理，用 `wiki list-recovery` 查看、`wiki purge-recovery` 预览后经用户确认清理（见 `recovery.md`）。

## 证据

证据由服务从卡片正文切出精确子串，字段为 `source_kind/source_library_id/source_file_id/source_chunk_id/expected_content_hash/quote`。两种给法等价：意图命令的 `--evidence CHUNK_ID:LOCATOR` 内联，或 `chunk extract --out` 落成 JSON 再 `--evidence-file`。

- **`--evidence CHUNK_ID:LOCATOR`** 可重复，LOCATOR 为行号 `N` / `N-M`（1 基）、`grep=子串` 或 `re=正则`。服务按定位切出精确引文并带回哈希；**匹配不唯一就拒绝提交**并给出 `chunk get` 恢复命令，此时先读正文再用行号收窄，不猜第一条。
- **只有恰好 1 个匹配且未 `truncated` 才采用**；多匹配时先读回候选，用行号 `N-M`（1 基）收窄，不猜第一条。`grep=` 默认子串，`re=` 用正则；`chunk extract` 另有 `--context N` 取前后行。
- 子串和正则都是按行定位，但引文都返回整行；同一行的不同关键词可能得到相同引文。行号选整行、`--context` 扩展前后行，都不能截取行内片段。
- 相同归属、来源、`stance` 和 `quote` 判为 `reused` 是正常去重；同一引文可分别挂到不同页面。
- `chunk extract --out` 不覆盖已存在文件；需要时换新路径。
- 本库引文必须逐字出现在当前卡片正文（只统一换行）。`--lines` 定位可以宽松，服务返回的 `quote` 始终是精确子串，仍受提交时的逐字校验。
- 定位不等于断言：`current` 只表示定位未变，不代表内容为真。外部证据用 `source_kind=external` 并自备标题、安全 http/https 地址和引文；用户陈述用 `source_kind=user`，不伪造文件或网页来源。
- 给**已有页面**追加证据时，批次里仍要带该页的 `{id, expected_revision, expected_content_hash}` 并发条件；证据的 `page` 必须指本批新建 ref 或更新 id，不自动创建页面。
- `--stance supports|contradicts|context`：给一次作用于全部证据（默认 supports），给多次按证据顺序逐一对应（`--evidence-file` 在前、`--evidence` 在后）。同一命令里可以同时挂支持与反对证据。
- 仍可用旧的两段式：`chunk extract CHUNK_ID --lines 2-3 --out evidence.json` 再 `--evidence-file evidence.json`，两种证据可混用。
- 允许没有证据的写入，但读回会明确 `has_evidence=false`，不能伪称有证据或把推断说成引文支持。

## 只读速查

```text
<PIECE> wiki list --kind concept --status active --limit 20 --offset 0 --json
<PIECE> wiki get page PAGE_UUID --limit 20 --offset 0 --json
<PIECE> wiki get evidence EVIDENCE_UUID --json
<PIECE> wiki get ANY_UUID --json
<PIECE> wiki references LIBRARY_UUID FILE_ID --limit 20 --offset 0 --json
<PIECE> wiki history page PAGE_UUID --limit 20 --json
<PIECE> wiki lint --page-id PAGE_UUID --limit 20 --json
<PIECE> wiki request --input page-request.json --read-back --json
<PIECE> wiki request REQUEST_KEY --read-back --json
```

`get` 的类型可省略：只给 UUID 时服务按 UUID 探测是页面还是证据，响应 `kind` 报告实际类型；`history`、`delete` 仍必须写明类型。
`request --input` 与位置请求键二选一，只查询不提交；查询删除请求时，读回验证记录已不存在，返回 `deleted=true`，不返回删除前正文。

`get` 返回 `record` 和 `evidence.items/total/limit/offset`、`links`、`backlinks`、`has_evidence`；分页继续读取。身份仍以 UUID 为准。`lint` 只做程序性结构检查，不做语义判断。

资料层命令：`file list --name`、`search --file-id`、`chunk list/get`、`chunk extract`、`file page FILE_ID PAGE --output 原页.png`。`search` 只给候选，再按 `chunk_id` 取正文；不要猜原页页码。原件导入按需用 `file import` / `import-markdown` 并等待索引，不为知识页额外创建 files/chunks。
