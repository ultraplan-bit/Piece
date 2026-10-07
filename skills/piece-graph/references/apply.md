# graph 原始批次写入（apply）

主流程用 `graph object add` / `graph relation add` 等意图命令即可，它们内部和本节是同一套原子提交。本文件用于一次提交多条记录、或需要字段级控制的场景。

## 写入契约

- `reason` 非空。正式提交必须有 `request_key`（在 JSON 中提供，或用 `--request-id` 注入；两者同时提供必须一致）。`--dry-run` 只校验，预检返回的 UUID **不是**正式创建的 ID。
- 新实体填 `ref/kind/title`，可选 `summary/aliases/status`；`kind` 仅 `concept/entity`，`status` 为 `active/disputed/outdated`。实体不存长篇正文，没有本地文件的实体合法。
- 更新填 `id/expected_revision` 与真正要改的字段；省略保持原值，`""` / `[]` 清空，不能传 `null`。不要发送读回响应中的时间戳、派生字段等未知字段。
- 目标引用只能是 `{"id":"已有 UUID"}` 或 `{"ref":"本批新增实体名"}`。全批 `ref` 唯一、类型必须匹配；不能混填 id/ref，不能按标题推断。
- `relations` 新建必填 `ref/source/predicate/target/description/basis`，可选 `qualifier/status`。
- `evidence` 恰好选 `object` 或 `relation` 作为 owner，`source_kind` 为 `piece/external/user`；`quote` 必填，`stance` 为 `supports/contradicts/context`，默认 supports。本库引文须逐字出现在当前卡片正文，只统一换行，不模糊匹配。`content_hash/heading_path/page_number` 由服务计算，不能填入；可提交 `expected_content_hash` 拒绝旧正文，无法可靠计算就不伪造哈希。
- 每批最多 **20 实体、100 关系、200 证据、整个 JSON 512 KiB**；超限拆批，不转后台任务。全部成功或全部回滚。`apply` 同步返回，**没有 `--wait` 或 `task_id`**。

## 最小新增

以下仅演示结构，须换成用户真实要求保存的内容，不把示例当成用户说过的话。保存为 `new.json`：

```json
{
  "reason": "记录用户明确要求保留的术语实体",
  "objects": [
    {"ref": "concept", "kind": "concept", "title": "增量维护", "summary": "只修改明确变化的内容", "aliases": ["增量修订"]}
  ],
  "evidence": [
    {"object": {"ref": "concept"}, "source_kind": "user", "stance": "context", "quote": "本项目希望保留未修改的内容，只提交必要增量。"}
  ]
}
```

```text
<PIECE> graph apply --input new.json --dry-run --json
<PIECE> graph apply --input new.json --request-id UNIQUE_NEW_KEY --json
<PIECE> graph get object RETURNED_UUID --json
```

保存 `data.refs.concept.id/revision` 与 `data.evidence[].id`。正式结果包含 `committed=true`、各类记录的 `id/action/revision` 和 `counts.created/updated/reused`；实体和关系还带 `submitted_fields`，只列显式提交的字段名，不代表值一定变化或语义正确。旧请求缺少此项时不猜测。不通过搜索相似标题猜本次结果。

## 增量修订

先读回实体，以实际 UUID 替换示例 UUID、以当前版本替换 `expected_revision`，保存为 `update.json`。其余字段保持不动：

```json
{
  "reason": "补充适用范围，保留已有摘要",
  "objects": [
    {"id": "00000000-0000-0000-0000-000000000001", "expected_revision": 1, "summary": "适用于已明确修改范围的图谱维护"}
  ]
}
```

```text
<PIECE> graph apply --input update.json --dry-run --json
<PIECE> graph apply --input update.json --request-id UNIQUE_UPDATE_KEY --json
<PIECE> graph get object OBJECT_UUID --json
<PIECE> graph history object OBJECT_UUID --json
```

## 关系与本库证据

先读取来源卡片，确认 file/chunk 归属，以本库 `library_id`、实际实体 UUID、file/chunk ID 和逐字引用替换示例；保存为 `relation.json`：

```json
{
  "reason": "保存已阅读资料中的关系及来源",
  "relations": [
    {
      "ref": "r", "source": {"id": "00000000-0000-0000-0000-000000000001"},
      "predicate": "depends_on", "target": {"id": "00000000-0000-0000-0000-000000000002"},
      "description": "增量维护依赖稳定身份以定位实体", "qualifier": "本项目图谱维护", "basis": "explicit"
    }
  ],
  "evidence": [
    {
      "relation": {"ref": "r"}, "source_kind": "piece", "stance": "supports",
      "source_library_id": "00000000-0000-0000-0000-000000000003",
      "source_file_id": 1, "source_chunk_id": 1, "quote": "增量维护依赖稳定的实体身份。"
    }
  ]
}
```

按前述 apply 预检 / 正式提交流程执行，然后 `graph get relation RETURNED_RELATION_UUID` 核对证据。

## 仅补证据

先读取要补证据的实体／关系，确认引文支持的具体断言。批次只写 `evidence`，每条选择自己的已有实体或关系 UUID；同一批可以挂到多个归属，不需要 `objects` / `relations`，也不要修改摘要来凑更新。

下面只演示归属结构；库 UUID、文件／卡片 ID 和引文须替换为实际来源。Piece 证据的来源字段与 `expected_content_hash` 从 `chunk extract` 原样带入，不手写引文或哈希。保存为 `evidence-only.json`：

```json
{
  "reason": "给已有实体和关系补充来源，不修改原有内容",
  "evidence": [
    {
      "object": {"id": "00000000-0000-0000-0000-000000000001"},
      "source_kind": "piece", "stance": "supports",
      "source_library_id": "00000000-0000-0000-0000-000000000003",
      "source_file_id": 1, "source_chunk_id": 1,
      "quote": "增量维护依赖稳定的实体身份。"
    },
    {
      "relation": {"id": "00000000-0000-0000-0000-000000000004"},
      "source_kind": "piece", "stance": "supports",
      "source_library_id": "00000000-0000-0000-0000-000000000003",
      "source_file_id": 1, "source_chunk_id": 1,
      "quote": "增量维护依赖稳定的实体身份。"
    }
  ]
}
```

```text
<PIECE> graph apply --input evidence-only.json --dry-run --json
<PIECE> graph apply --input evidence-only.json --request-id UNIQUE_EVIDENCE_KEY --read-back --json
```

仅补证据不会增加实体／关系的 revision；仍按 owner UUID 读回核对归属。相同归属、来源、stance 和 quote 会返回 `reused`，不是丢失证据。修改已提交批次内容时使用新请求键，不复用旧键。
