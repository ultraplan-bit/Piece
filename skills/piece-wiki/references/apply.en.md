# Wiki Raw Batch Writes (`apply`)

For the normal workflow, use the intent commands `wiki page add` or `wiki page update`; they use the same per-page submission logic described here. Multi-page batches are not atomic. Use this page to submit multiple pages/evidence in one batch or when field-level control is needed.

## Write contract

- `reason` must be non-empty. A final submission must include `request_key` (in the JSON or injected with `--request-id`; if both are provided, they must match). `--dry-run` validates only; new page IDs and `content_hash` values in its preview are temporary and may differ from the final submission.
- For a new page, provide `ref/kind/title`; `summary/body/aliases/status` are optional. `kind` must be `concept/entity/topic/synthesis/source_summary`; `status` must be `active/disputed/outdated`. A page with only a title and no summary or body is valid.
- For an update, provide `id/expected_revision/expected_content_hash` and the fields to change. Omitted fields keep their current values; `""` / `[]` clear a field, and `null` is not allowed. Do not send derived fields from a read-back response (such as `path` or `index_status`) or unknown fields. Use the current `content_hash` returned by `wiki get page` for `expected_content_hash`.
- Each `evidence` item must use `page` to reference a new `ref` or updated `id` **in this batch**: `{"page":{"ref":"name of page created in this batch"}}` or `{"page":{"id":"UUID of page updated in this batch"}}`. Evidence does not create pages automatically and is not mapped to graph entities.
- When adding evidence to an **existing page** without changing its content fields, `pages` must still include `{"id":..., "expected_revision":..., "expected_content_hash":...}` as concurrency conditions. Evidence is written to the page file and advances its `revision`; use the newest `revision` and `content_hash` from read-back.
- Evidence `source_kind` must be `piece/external/user`; `quote` is required; `stance` must be `supports/contradicts/context` and defaults to `supports`. A quote from this library must appear verbatim in the current card body, with line endings normalized only; fuzzy matching is not used. The service computes `content_hash/heading_path/page_number`; do not supply them. You may provide `expected_content_hash` to reject an outdated body. Never invent a hash if you cannot calculate it reliably.
- Each batch supports up to **20 pages, 200 evidence items, and 512 KiB for the entire JSON**. Split larger requests; they are not converted to background tasks. `apply` returns synchronously; there is **no `--wait` or `task_id`**.
- `apply` commits pages independently and returns `committed/partial/index_status/errors`. `committed=true` means at least one page was actually written. `partial=true` or `index_status!=current` (including a single-page index failure) means the batch did not fully complete; in that case `success=false`, but **do not resend the whole batch**. Receipts for committed pages remain in `data`; use `errors` to handle the issue and `wiki rebuild-index` for an index problem. If nothing was written (`committed=false`), `error.code` is the actual first error code, not `PARTIAL_FAILURE`; reread and handle that code before submitting again.

## Minimal create

The following shows structure only. Replace it with content the user actually asked to save; do not treat the example as something the user said:

```json
{
  "reason": "Record a page explanation the user explicitly asked to preserve",
  "pages": [
    {"ref": "concept", "kind": "concept", "title": "Incremental maintenance", "summary": "Change only what has explicitly changed", "aliases": ["incremental revision"]}
  ],
  "evidence": [
    {"page": {"ref": "concept"}, "source_kind": "user", "stance": "context", "quote": "This project aims to preserve unchanged content and submit only necessary changes."}
  ]
}
```

Pass the batch on stdin; no file is needed. If `--request-id` is omitted, the CLI generates a key and returns it as `data.request_key`:

```bash
<PIECE> wiki apply --input - --read-back --json <<'JSON'
{ ...the batch above... }
JSON
```

To validate first, add `--dry-run`; after checking the preview, remove it and rerun the command unchanged. To keep a local record, use a file path instead: `--input new.json --request-id UNIQUE_NEW_KEY`. Then verify with `wiki get page RETURNED_UUID --json`.

Save `data.refs.concept.id/revision`, `data.pages[].content_hash`, and `data.evidence[].id`. The final response contains `committed`, each record's `id/action/revision`, and `counts.created/updated/reused`. Pages also include `submitted_fields`, which lists explicitly submitted field names only and does not mean values changed or prove semantic correctness. Do not infer this field for older requests that lack it. Do not guess which record was created by searching for a similar title.

## Incremental update

Read the page first, then replace the example with its actual UUID, current `revision`, and current `content_hash`. Save it as `update.json` and leave other fields unchanged:

```json
{
  "reason": "Add scope while preserving the existing body",
  "pages": [
    {"id": "00000000-0000-0000-0000-000000000001", "expected_revision": 1,
     "expected_content_hash": "0000000000000000000000000000000000000000000000000000000000000000",
     "summary": "Applies to page maintenance within an explicitly defined scope"}
  ]
}
```

```text
<PIECE> wiki get page OBJECT_UUID --json
<PIECE> wiki apply --input update.json --dry-run --json
<PIECE> wiki apply --input update.json --request-id UNIQUE_UPDATE_KEY --json
<PIECE> wiki history page OBJECT_UUID --json
```

An update preserves the replaced page file as a local recovery copy `.wiki-recovery-<operation_id>.bak` (it is not scanned and is not another source of truth). Receipts/history provide the recovery path. Back it up; do not claim that the old content was permanently erased.

## Create a page with Piece evidence

Read the source card first and confirm its file/chunk ownership. Replace the example with this library's `library_id`, actual file/chunk IDs, and a verbatim quote. Save as `page-evidence.json`:

```json
{
  "reason": "Save a page and source the material that was read",
  "pages": [
    {"ref": "p", "kind": "concept", "title": "Stable identity", "body": "Page identity does not change with its title."}
  ],
  "evidence": [
    {
      "page": {"ref": "p"}, "source_kind": "piece", "stance": "supports",
      "source_library_id": "00000000-0000-0000-0000-000000000003",
      "source_file_id": 1, "source_chunk_id": 1, "quote": "Page identity does not change with its title."
    }
  ]
}
```

Follow the preview/final-submission steps above, then use `wiki get page RETURNED_PAGE_UUID` to check evidence and `links/backlinks`.

## Add evidence only

Read the page first and get its current `revision` and `content_hash` with `wiki get page`. The batch must contain both `pages` (concurrency conditions only) and `evidence`; the evidence `page` points to the existing page UUID. Do not change the summary merely to force an update.

The following shows ownership structure only. Replace library UUID, file/card IDs, and quote with the actual source. Copy Piece evidence source fields and `expected_content_hash` directly from `chunk extract`; do not write a quote or hash by hand. Save as `evidence-only.json`:

```json
{
  "reason": "Add a source to an existing page without changing its body",
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
      "quote": "Page identity does not change with its title."
    }
  ]
}
```

```text
<PIECE> wiki apply --input evidence-only.json --dry-run --json
<PIECE> wiki apply --input evidence-only.json --request-id UNIQUE_EVIDENCE_KEY --read-back --json
```

An evidence-only batch has no content fields, but writing evidence changes the page file and advances its `revision`; read back by page UUID and verify ownership plus the new `revision`/`content_hash`. Matching owner, source, `stance`, and `quote` returns `reused`, not missing evidence. Use a new request key when changing a submitted batch; do not reuse the old key.
