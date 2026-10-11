# 图谱语义判断与只读查询

要新建或修改实体、关系、证据，判断谓词方向 / basis / stance 与是否合并，或需要只读查询命令时读本文。

图谱实体、证据与 Wiki 页面相互独立、不同步，Wiki 页面用 `piece-wiki` 维护；资料导入和检索是另一路径。

## 实体

- 实体 `--kind`：概念/方法用 `concept`，人物/机构/产品用 `entity`。实体不存长篇正文（`--body-file` 已移除）；用 `--summary` 和证据承载内容。更新必须带已读到的 `revision` 和至少一个待改字段；未传字段保持原值。
- 查已有图谱：`graph search` 找同名与别名，`file list`、`search` 找相关资料。读取候选的类型、摘要与证据后判断，**同名实体不自动合并，共现不自动视为语义关系**，不按名称合并。
- 页面内链属于 Wiki 导航，不是图谱边。
- 允许没有证据的写入，但读回会明确 `has_evidence=false`，不能伪称有证据或把推断说成引文支持。

## 关系

先把 `A → B` 读成一句话，与原文对照，再选谓词和两端顺序：

| 含义 | 关系 |
| --- | --- |
| A 是 B 的一种 | `A is_a B` |
| A 是 B 的组成部分 | `A part_of B` |
| A 要运行或成立，需要 B | `A depends_on B` |
| A 这条规则／方法适用于 B | `A applies_to B` |

- `supports` 也有方向；`contradicts` / `related_to` 对称归一，不人为赋予方向。拿不准时不要仅凭共现强行建立组成、依赖等关系。
- **修改描述不改变结构关系。** 改 `--description` 不会改谓词或端点；纠正谓词传 `--predicate`，纠正方向传相应 `--source` / `--target`，其他字段不用重传。
- **关系的证据独立于实体。** 证明 A 或 B 的引文不自动证明 A 与 B 的关系。边的 `evidence_count` 只统计该关系自己的证据，包含支持、反对和背景，不是支持票数。
- `--basis explicit|synthesis|inference|user_statement` 区分来源性质，不用浮点“可信度”。`related_to` 需说明原因，不把共现推成因果。
- 相同端点、谓词和条件的关系只建一条，增加支持或反对证据；适用条件不同可分别建边。`contradicts` 用 `--basis` 与 `--stance` 如实标注，冲突先保留双方证据与条件。
- 再建一条端点、谓词、`--qualifier` 都相同的关系：内容一致返回 `reused`；内容不同返回 `KNOWLEDGE_CONFLICT`，`data.existing` 给出已有关系的 `id/revision/display/description/basis`，`next_command` 是对它的 `relation update` 前缀。读 `existing` 判断是复用还是修订，不要换 `--qualifier` 绕过去再建一条。
- `--stance supports|contradicts|context`：给一次作用于全部证据（默认 supports），给多次按证据顺序逐一对应（`--evidence-file` 在前、`--evidence` 在后）。同一命令里可以同时挂支持与反对证据。

## 证据

证据由服务从卡片正文切出精确子串，字段为 `source_kind/source_library_id/source_file_id/source_chunk_id/expected_content_hash/quote`。两种给法等价：意图命令的 `--evidence CHUNK_ID:LOCATOR` 内联，或 `chunk extract --out` 落成 JSON 再 `--evidence-file`。

- **`--evidence CHUNK_ID:LOCATOR`** 可重复，LOCATOR 为行号 `N` / `N-M`（1 基）、`grep=子串` 或 `re=正则`。服务按定位切出精确引文并带回哈希；**匹配不唯一就拒绝提交**并给出 `chunk get` 恢复命令，此时先读正文再用行号收窄，不猜第一条。
- **只有恰好 1 个匹配且未 `truncated` 才采用**；多匹配时先读回候选，用行号 `N-M`（1 基）收窄，不猜第一条。`grep=` 默认子串，`re=` 用正则；`chunk extract` 另有 `--context N` 取前后行。
- 子串和正则都是按行定位，但引文都返回整行；同一行的不同关键词可能得到相同引文。行号选整行、`--context` 扩展前后行，都不能截取行内片段。
- 相同归属、来源、`stance` 和 `quote` 判为 `reused` 是正常去重；同一引文可分别挂到不同实体或关系。
- `chunk extract --out` 不覆盖已存在文件；需要时换新路径。
- 本库引文必须逐字出现在当前卡片正文（只统一换行）。`--lines` 定位可以宽松，服务返回的 `quote` 始终是精确子串，仍受提交时的逐字校验。
- 定位不等于断言：`current` 只表示定位未变，不代表内容为真。外部证据用 `source_kind=external` 并自备标题、安全 http/https 地址和引文；用户陈述用 `source_kind=user`，不伪造文件或网页来源。
- 仍可用旧的两段式：`chunk extract CHUNK_ID --lines 2-3 --out evidence.json` 再 `--evidence-file evidence.json`，两种证据可混用。

## 只读速查

```text
<PIECE> graph list --kind concept --status active --limit 20 --offset 0 --json
<PIECE> graph get object OBJECT_UUID --limit 20 --offset 0 --json
<PIECE> graph get relation RELATION_UUID --json
<PIECE> graph get evidence EVIDENCE_UUID --json
<PIECE> graph get ANY_UUID --json
<PIECE> graph graph OBJECT_UUID --depth 1 --edge-type relation --max-nodes 30 --max-edges 60 --json
<PIECE> graph references LIBRARY_UUID FILE_ID --limit 20 --offset 0 --json
<PIECE> graph history object OBJECT_UUID --limit 20 --json
<PIECE> graph lint --object-id OBJECT_UUID --limit 20 --json
<PIECE> graph rebuild-index --json
<PIECE> graph request --input object-request.json --read-back --json
<PIECE> graph request REQUEST_KEY --read-back --json
```

`get` 的类型可省略：只给 UUID 时服务按 UUID 探测是实体、关系还是证据，响应 `kind` 报告实际类型；`history`、`delete` 仍必须写明类型。
`request --input` 与位置请求键二选一，只查询不提交；查询删除请求时，读回验证记录已不存在，返回 `deleted=true`，不返回删除前正文。

`get` 返回 `record` 和 `evidence.items/total/limit/offset`、`has_evidence`；分页继续读取。关系 `record.display` 和 graph 关系边的 `display` 是只读展示（如 `A —depends_on→ B`，对称关系不带箭头），按当前端点标题生成；身份仍以 UUID 为准，不把 display 写回批次。`graph` 默认 depth=1、最大 2，`--edge-type` 只接受 `relation`，检查 `truncated`，返回计数不是全图总数。`lint` 只做程序性结构检查，不做语义判断。

资料层命令：`file list --name`、`search --file-id`、`chunk list/get`、`chunk extract`、`file page FILE_ID PAGE --output 原页.png`。`search` 只给候选，再按 `chunk_id` 取正文；不要猜原页页码。原件导入按需用 `file import` / `import-markdown` 并等待索引，不为图谱实体额外创建 files/chunks。
