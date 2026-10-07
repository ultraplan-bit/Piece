# wiki 原始批次写入（apply）

主流程用 `wiki page add` / `wiki page update` 意图命令即可，它们内部和本节使用同一套逐页提交逻辑；多页批次不是原子事务。本文件用于一次提交多条页面/证据、或需要字段级控制的场景。

## 写入契约

- `reason` 非空。正式提交必须有 `request_key`（在 JSON 中提供，或用 `--request-id` 注入；两者同时提供必须一致）。`--dry-run` 只校验，预检返回的 UUID **不是**正式创建的 ID。
- 新页面填 `ref/kind/title`，可选 `summary/body/aliases/status`；`kind` 为 `concept/entity/topic/synthesis/source_summary`，`status` 为 `active/disputed/outdated`。只有标题、没有摘要或正文的页面也合法。
- 更新填 `id/expected_revision/expected_content_hash` 与真正要改的字段；省略保持原值，`""` / `[]` 清空，不能传 `null`。不要发送读回响应中的派生字段（`path`、`index_status` 等）或未知字段。`expected_content_hash` 用 `wiki get page` 返回的当前 `content_hash`。
- `evidence` 必须通过 `page` 引用**本批**新建 ref 或更新 id：`{"page":{"ref":"本批新增页面名"}}` 或 `{"page":{"id":"本批更新页面 UUID"}}`。不自动创建页面，也不映射到图谱实体。
- 给**已有页面**追加证据（不提交内容改动字段）时，`pages` 里仍要带上该页 `{"id":..., "expected_revision":..., "expected_content_hash":...}` 作为并发条件。证据写入页面文件，页面 `revision` 会随之推进；读回以最新 `revision`/`content_hash` 为准。
- `evidence` 的 `source_kind` 为 `piece/external/user`；`quote` 必填，`stance` 为 `supports/contradicts/context`，默认 supports。本库引文须逐字出现在当前卡片正文，只统一换行，不模糊匹配。`content_hash/heading_path/page_number` 由服务计算，不能填入；可提交 `expected_content_hash` 拒绝旧正文，无法可靠计算就不伪造哈希。
- 每批最多 **20 页面、200 证据、整个 JSON 512 KiB**；超限拆批，不转后台任务。`apply` 同步返回，**没有 `--wait` 或 `task_id`**。
- `apply` 按页面独立提交：返回 `committed/partial/index_status/errors`。`committed=true` 表示至少一页真实落盘；`partial=true` 或 `index_status!=current`（含单页索引失败）表示未全部完成，此时 `success=false`，但 **不要重发**，已提交回执保留在 `data`，按 `errors` 处理索引问题用 `wiki rebuild-index`。

## 最小新增

以下仅演示结构，须换成用户真实要求保存的内容，不把示例当成用户说过的话。保存为 `new.json`：

```json
{
  "reason": "记录用户明确要求保留的页面说明",
  "pages": [
    {"ref": "concept", "kind": "concept", "title": "增量维护", "summary": "只修改明确变化的内容", "aliases": ["增量修订"]}
  ],
  "evidence": [
    {"page": {"ref": "concept"}, "source_kind": "user", "stance": "context", "quote": "本项目希望保留未修改的内容，只提交必要增量。"}
  ]
}
```

```text
<PIECE> wiki apply --input new.json --dry-run --json
<PIECE> wiki apply --input new.json --request-id UNIQUE_NEW_KEY --json
<PIECE> wiki get page RETURNED_UUID --json
```

保存 `data.refs.concept.id/revision`、`data.pages[].content_hash` 与 `data.evidence[].id`。正式结果包含 `committed`、各类记录的 `id/action/revision` 和 `counts.created/updated/reused`；页面还带 `submitted_fields`，只列显式提交的字段名，不代表值一定变化或语义正确。旧请求缺少此项时不猜测。不通过搜索相似标题猜本次结果。

## 增量修订

先读回页面，以实际 UUID、当前 `revision` 和 `content_hash` 替换示例值，保存为 `update.json`。其余字段保持不动：

```json
{
  "reason": "补充适用范围，保留已有正文",
  "pages": [
    {"id": "00000000-0000-0000-0000-000000000001", "expected_revision": 1,
     "expected_content_hash": "0000000000000000000000000000000000000000000000000000000000000000",
     "summary": "适用于已明确修改范围的页面维护"}
  ]
}
```

```text
<PIECE> wiki get page OBJECT_UUID --json
<PIECE> wiki apply --input update.json --dry-run --json
<PIECE> wiki apply --input update.json --request-id UNIQUE_UPDATE_KEY --json
<PIECE> wiki history page OBJECT_UUID --json
```

更新会把被替换的页面文件保留为本地恢复副本 `.wiki-recovery-<operation_id>.bak`（不参与扫描，不是另一份真源），回执/历史给出恢复路径；备份须覆盖它，不能宣称旧内容被彻底擦除。

## 新建页面与本库证据

先读取来源卡片，确认 file/chunk 归属，以本库 `library_id`、实际 file/chunk ID 和逐字引用替换示例；保存为 `page-evidence.json`：

```json
{
  "reason": "保存已阅读资料中的页面及来源",
  "pages": [
    {"ref": "p", "kind": "concept", "title": "稳定身份", "body": "页面身份不随标题变化。"}
  ],
  "evidence": [
    {
      "page": {"ref": "p"}, "source_kind": "piece", "stance": "supports",
      "source_library_id": "00000000-0000-0000-0000-000000000003",
      "source_file_id": 1, "source_chunk_id": 1, "quote": "页面身份不随标题变化。"
    }
  ]
}
```

按前述 apply 预检 / 正式提交流程执行，然后 `wiki get page RETURNED_PAGE_UUID` 核对证据与 `links/backlinks`。

## 仅补证据

先读取要补证据的页面，`wiki get page` 拿到当前 `revision` 与 `content_hash`。批次同时写 `pages`（仅并发条件）和 `evidence`；证据 `page` 指向该已有页面 UUID。不要修改摘要来凑更新。

下面只演示归属结构；库 UUID、文件／卡片 ID 和引文须替换为实际来源。Piece 证据的来源字段与 `expected_content_hash` 从 `chunk extract` 原样带入，不手写引文或哈希。保存为 `evidence-only.json`：

```json
{
  "reason": "给已有页面补充来源，不修改页面正文",
  "pages": [
    {"id": "00000000-0000-0000-0000-000000000001", "expected_revision": 1,
     "expected_content_hash": "0000000000000000000000000000000000000000000000000000000000000000"}
  ],
  "evidence": [
    {
      "page": {"id": "00000000-0000-0000-0000-000000000001"},
      "source_kind": "piece", "stance": "supports",
      "source_library_id": "00000000-0000-0000-0000-000000000003",
      "source_file_id": 1, "source_chunk_id": 1,
      "quote": "页面身份不随标题变化。"
    }
  ]
}
```

```text
<PIECE> wiki apply --input evidence-only.json --dry-run --json
<PIECE> wiki apply --input evidence-only.json --request-id UNIQUE_EVIDENCE_KEY --read-back --json
```

仅补证据不提交内容改动字段，但证据写入页面文件会使页面 `revision` 推进；按页面 UUID 读回核对归属与新的 `revision`/`content_hash`。相同归属、来源、stance 和 quote 会返回 `reused`，不是丢失证据。修改已提交批次内容时使用新请求键，不复用旧键。
