# Graph Failure Recovery and Request Management

Read this when a write fails, a receipt includes an error code, or the submission result is uncertain. It covers read-back fields, request keys, local archives, and index rebuilds.

Every `--json` response has `success/message/data/error`; check `success` before reading `data`.

## Handling failures

If a failure response includes `next_command` (shell-escaped for bash) and `next_argv` (for other invocation methods), they are complete prefixes for **read-only** recovery commands. Use the recovery command once. If it still fails, stop and report the issue accurately; do not blindly replay writes. If no recovery command is provided, follow the error message without guessing a command.

- **Unknown submission result:** Prefer the suggested `<PIECE> graph request KEY --read-back` (`KEY` is `data.request_key` from the receipt; when a request file exists, use `graph request --input ORIGINAL_REQUEST_FILE --read-back` and the CLI will read the key). Not finding a result yet does not mean the write failed. If a retry is needed, rerun the exact same command with the same key (`graph apply --input ORIGINAL_REQUEST_FILE --read-back` for a request file); do not change the key or rebuild the batch.
- `READ_BACK_INCOMPLETE`: `data.committed=true` means the write succeeded, but read-back was truncated, failed, or observed a changed state. **Do not submit again.** Use `next_command` to read more and compare.
- `VERSION_CONFLICT`: Read the current version and prepare an incremental update. **Do not overwrite automatically** or merely change the request key.
- `KNOWLEDGE_CONFLICT`: A relation with the same endpoints, predicate, and conditions already exists, but its content differs. `data.existing` contains its `id/revision/display/description/basis`; `next_command` is a prefix for `graph relation update EXISTING_UUID --expected-revision N`. Add the fields to change if you want to revise it; otherwise do not write again. Do not bypass the conflict by changing `--qualifier` to create a duplicate.
- `REQUEST_CONFLICT`: The key was already used for different content. Inspect the original request with `graph request`; retry the original content with its original key. Use a new key only for a different operation.
- `IMPACT_CONFLICT`: A dependency changed after the preview. Preview again and obtain confirmation; do not automatically expand the deletion scope.
- `SOURCE_CHANGED` / `QUOTE_MISMATCH` / `INVALID_SOURCE`: Reread the source and its ownership. Do not invent quotations, page numbers, or hashes.
- `CONFIRMATION_REQUIRED`: Stop and obtain user confirmation. Do not add `--yes` yourself.

## Read-back fields

- High-level commands read back by default; `graph apply --read-back` can do so too. `read_back` includes `complete/records/total/limit/truncated/semantic_review_required`; objects and relations also report `submitted_revision/revision_matches`. At most 20 affected records are read back automatically. If this limit is exceeded, a read fails, or the version or local source changes, the result is `READ_BACK_INCOMPLETE`; `data.committed` is still true. **Do not submit again.** Use `next_command` for additional read-only checks.
- Object and relation receipts' `submitted_fields` lists fields explicitly submitted. It does not mean values changed or prove semantic correctness. Do not guess when older requests lack this field. A relation's `record.display` shows current endpoint titles and predicate; compare it with actual `source_id/predicate/target_id`, not only the revision number.
- Report only what actually completed, including ambiguity and unresolved issues.

## Request keys and local archives

- The four intent commands (`object add` / `object update` / `relation add` / `relation update`) do not save a request file by default. The CLI generates a request key and returns it as `data.request_key`; if the result is uncertain, query with `graph request KEY --read-back`. Objects accept optional `--summary`, repeated `--alias`, and `--status`. A new relation requires `--description` and `--basis explicit|synthesis|inference|user_statement`. Use `--reason` to record the reason for a change.
- For a local archive, add `--save-request` (automatically saves under `requests/` in the data directory and returns the path) or `--request-file PATH`; they are mutually exclusive. A request file has the form `{target_id, request:{request_key, reason, objects/relations/evidence...}}`. If the path exists, it may be reused only when both target and content match; the same command with the same file can be retried exactly.
- Request files are **sensitive local material**. Deleting an online record does not delete them; keep or remove them as the user directs.
- **Use `--help` to explore parameters and `--dry-run` to validate a write.** Without `--dry-run`, the command submits. An object with a title and no summary is valid. `--dry-run` does not write online data; after reviewing its result, remove `--dry-run` and rerun the command unchanged to submit.
- For multi-object batches, evidence-only additions, or external sources, use `graph apply --input -` to read a batch from stdin (a heredoc is fine; no file is required). A path or the wrapped request-file format is also accepted. See [apply.en.md](apply.en.md).

## Rebuild the graph index

`graph rebuild-index` rebuilds only the derived full-text (FTS) index for graph objects. It does not modify objects, relations, evidence, or history and does not call a model. Rebuilding requires write access. Use it when search results are abnormal or the index is missing:

```text
<PIECE> graph rebuild-index --json
```
