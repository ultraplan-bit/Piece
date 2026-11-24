# Raw Graph Batch Writes (`apply`)

Use intent commands such as `graph object add` / `graph relation add` for the main workflow; they use the same atomic submission logic as this section. This file is for submitting multiple records at once or when field-level control is needed.

## Write contract

- `reason` must be non-empty. A committed submission must have a `request_key` (in JSON or injected with `--request-id`; if both are supplied, they must match). `--dry-run` validates only; UUIDs returned for the preview are temporary and are not guaranteed to match IDs created by a real submission.
- A new object requires `ref/kind/title`; `summary/aliases/status` are optional. `kind` is only `concept/entity`; `status` is `active/disputed/outdated`. Objects do not store long-form bodies. An object without a local file is valid.
- An update requires `id/expected_revision` and fields that should actually change. Omitted fields retain their values; `""` / `[]` clear a field, and `null` is not allowed. Do not send timestamps, derived fields, or other unknown fields from read-back responses.
- References to targets must be either `{"id":"existing UUID"}` or `{"ref":"name of a new object in this batch"}`. Each `ref` must be unique across the batch and its kind must match. Do not mix `id` and `ref` in a target or infer an ID from a title.
- A new item in `relations` requires `ref/source/predicate/target/description/basis`; `qualifier/status` are optional.
- Each `evidence` item must choose exactly one owner, `object` or `relation`. `source_kind` is `piece/external/user`; `quote` is required; `stance` is `supports/contradicts/context` and defaults to `supports`. Piece quotations must appear verbatim in the current card text (only line endings are normalized); no fuzzy matching is used. The service computes `content_hash/heading_path/page_number`; do not provide them. You may provide `expected_content_hash` to reject stale source text. Never invent a hash when it cannot be calculated reliably.
- Each batch is limited to **20 objects, 100 relations, 200 evidence records, and 512 KiB for the complete JSON**. Split larger batches; they are not moved to a background task. The write is all-or-nothing. `apply` returns synchronously and has **no `--wait` or `task_id`**.

## Minimal add

The following shows structure only. Replace it with content the user actually asked to save; do not treat the example as the user's statement:

```json
{
  "reason": "Record a term the user explicitly asked to keep",
  "objects": [
    {"ref": "concept", "kind": "concept", "title": "Incremental maintenance", "summary": "Change only what has explicitly changed", "aliases": ["Incremental revision"]}
  ],
  "evidence": [
    {"object": {"ref": "concept"}, "source_kind": "user", "stance": "context", "quote": "This project should preserve unchanged content and submit only necessary increments."}
  ]
}
```

Pass a batch directly through stdin; if `--request-id` is omitted, the CLI generates and returns `data.request_key`:

```bash
<PIECE> graph apply --input - --read-back --json <<'JSON'
{ ...the batch above... }
JSON
```

To validate first, add `--dry-run`; after reviewing the result, remove it and rerun the command unchanged. To keep a local request file, use a file path instead: `--input new.json --request-id UNIQUE_NEW_KEY`. Then verify with `graph get object RETURNED_UUID --json`.

Save `data.refs.concept.id/revision` and `data.evidence[].id`. A successful result includes `committed=true`, each record's `id/action/revision`, and `counts.created/updated/reused`. Objects and relations also include `submitted_fields`, which lists only explicitly submitted field names; it does not mean values changed or prove semantic correctness. Do not guess this field when an older request lacks it. Do not search for a similar title to guess which record was created.

## Incremental revision

Read the object first. Replace the example with its actual UUID and current `expected_revision`, then save as `update.json`. Leave all other fields unchanged:

```json
{
  "reason": "Add the scope while preserving the existing summary",
  "objects": [
    {"id": "00000000-0000-0000-0000-000000000001", "expected_revision": 1, "summary": "Applies to graph maintenance with an explicitly defined change scope"}
  ]
}
```

```text
<PIECE> graph apply --input update.json --dry-run --json
<PIECE> graph apply --input update.json --request-id UNIQUE_UPDATE_KEY --json
<PIECE> graph get object OBJECT_UUID --json
<PIECE> graph history object OBJECT_UUID --json
```

## Relations and Piece evidence

Read the source card first and confirm the file/chunk ownership. Replace the example with this library's `library_id`, actual object UUIDs, file/chunk IDs, and verbatim quotation; save as `relation.json`:

```json
{
  "reason": "Save a relation found in a read source, with its evidence",
  "relations": [
    {
      "ref": "r", "source": {"id": "00000000-0000-0000-0000-000000000001"},
      "predicate": "depends_on", "target": {"id": "00000000-0000-0000-0000-000000000002"},
      "description": "Incremental maintenance depends on stable identity to locate objects", "qualifier": "Project graph maintenance", "basis": "explicit"
    }
  ],
  "evidence": [
    {
      "relation": {"ref": "r"}, "source_kind": "piece", "stance": "supports",
      "source_library_id": "00000000-0000-0000-0000-000000000003",
      "source_file_id": 1, "source_chunk_id": 1, "quote": "Incremental maintenance depends on stable object identities."
    }
  ]
}
```

Use the preview / commit workflow above, then verify the evidence with `graph get relation RETURNED_RELATION_UUID`.

## Add evidence only

Read the object or relation first and confirm the specific claim supported by the quotation. Put only `evidence` in the batch and choose the existing object or relation UUID for each item. One batch may attach evidence to multiple owners. `objects` / `relations` are not required; do not alter summaries just to force an update.

The example shows only the owner structure. Replace library UUID, file/chunk IDs, and quotation with values from the real source. For Piece evidence, copy source fields and `expected_content_hash` directly from `chunk extract`; do not write a quotation or hash by hand. Save as `evidence-only.json`:

```json
{
  "reason": "Add sources to existing objects and relations without changing their content",
  "evidence": [
    {
      "object": {"id": "00000000-0000-0000-0000-000000000001"},
      "source_kind": "piece", "stance": "supports",
      "source_library_id": "00000000-0000-0000-0000-000000000003",
      "source_file_id": 1, "source_chunk_id": 1,
      "quote": "Incremental maintenance depends on stable object identities."
    },
    {
      "relation": {"id": "00000000-0000-0000-0000-000000000004"},
      "source_kind": "piece", "stance": "supports",
      "source_library_id": "00000000-0000-0000-0000-000000000003",
      "source_file_id": 1, "source_chunk_id": 1,
      "quote": "Incremental maintenance depends on stable object identities."
    }
  ]
}
```

```text
<PIECE> graph apply --input evidence-only.json --dry-run --json
<PIECE> graph apply --input evidence-only.json --request-id UNIQUE_EVIDENCE_KEY --read-back --json
```

Adding evidence only does not increment an object's or relation's revision; verify the owner UUID by reading the record back. Matching owner, source, `stance`, and `quote` returns `reused` and does not mean evidence was lost. Use a new request key when changing the content of a submitted batch; never reuse the old key.
