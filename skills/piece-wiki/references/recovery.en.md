# Wiki Failure Recovery and Request Management

Read this when a write fails, a receipt includes an error code, or the submission result is uncertain. This page covers read-back fields, request keys and local request files, and index rebuilds.

All `--json` responses have `success/message/data/error`. Check `success` before reading `data`.

## Handling failures

If a failure response includes `next_command` (shell-escaped for bash) and `next_argv` (for other invocation methods), keep the full prefix it provides. Read commands only check state; `wiki rebuild-index` writes a derived index but does not change the body. Follow the suggested recovery once. If it still fails, stop and report the result; do not replay blindly. Without a recovery command, follow the error details and do not guess a command.

- **Partial success**: `data.committed=true` and `data.partial=true` mean at least one page was written, but one or more pages failed or the index was not updated. **Do not resubmit the whole batch or change its key automatically**; inspect per-page receipts and `errors` first. Completed pages will not be written again. Retry the same original request only when you have confirmed that unfinished items should proceed. For an index-only issue, use `wiki rebuild-index`.
- **No writes**: If `data.committed=false` and `errors` is non-empty, `error.code` is the actual code of the first error (such as `VERSION_CONFLICT`, `CONTENT_CONFLICT`, or `SOURCE_CHANGED`), not `PARTIAL_FAILURE`. Nothing was written; after handling that code, you may submit again. `PARTIAL_FAILURE` is used only when at least one page was written.
- **Unknown submission result**: Prefer the suggested `wiki request KEY --read-back` (`KEY` is in receipt `data.request_key`; if you have a request file, use `wiki request --input ORIGINAL_REQUEST_FILE --read-back` so the CLI retrieves the key). Not finding a result yet does not mean the submission failed. If you need to retry, rerun the exact same command (with a request file, use `wiki apply --input ORIGINAL_REQUEST_FILE --read-back`).
- **`READ_BACK_INCOMPLETE`**: `data.committed=true` means the write succeeded, but read-back was truncated, failed, or observed a changed state. **Do not resubmit**; use `next_command` to read more and compare.
- **`VERSION_CONFLICT` / `CONTENT_CONFLICT`**: Read the current page and prepare an incremental update. **Do not overwrite automatically** and do not only change the request key or hash. For `SOURCE_CHANGED` / `QUOTE_MISMATCH` / `INVALID_SOURCE`, reread the source and its ownership; never invent a quote, page number, or hash.
- **`REQUEST_CONFLICT`**: The key was already used for different content. Use `wiki request` to inspect the original request, then preserve its key when retrying it exactly. Use a new key only for a different operation.
- **`IMPACT_CONFLICT`**: Dependencies changed after preview. Preview again and obtain confirmation; do not expand the deletion scope automatically.
- **`CONFIRMATION_REQUIRED`**: Stop and obtain user confirmation. Do not add `--yes` on your own.

## Read-back fields

- High-level commands read back by default; `wiki apply --read-back` can also request read-back. `read_back` contains `complete/records/total/limit/truncated/semantic_review_required`; pages also report `submitted_revision/revision_matches/content_hash_matches`. At most 20 affected records are read back automatically. Exceeding that budget, a read failure, a revision change, or a source change in this library returns `READ_BACK_INCOMPLETE`. `data.committed` remains true; **do not resubmit**. Use `next_command` for additional read-only checks.
- A page receipt's `submitted_fields` lists fields explicitly submitted. It does not mean their values changed or prove semantic correctness. Do not infer this field for older requests that lack it.
- Report only what actually completed, including ambiguities and unresolved issues.

## Request keys and local records

- The two intent commands (`page add` and `page update`) do not save request files by default. The CLI generates a request key and returns it as `data.request_key`; use `wiki request KEY --read-back` if the result is uncertain. Pages may use `--summary`, `--body-file BODY.md` (or `-`), repeatable `--alias`, and `--status`. `--reason REASON` records why the page was changed. A page without a body is valid.
- To keep a local record, use `--save-request` (saves under `requests/` in the data directory and returns the path) or `--request-file PATH`; these options are mutually exclusive. A request file has the form `{target_id, request:{request_key, reason, pages/evidence...}}`. If the path already exists, it may be reused only when both the target and content are identical; the same command with the same file can then be retried exactly.
- Request files are **sensitive local material**. Deleting an online record does not delete them. Keep or remove them as the user directs.
- **Use `--help` to explore parameters and `--dry-run` to validate a write.** Without `--dry-run`, the command submits. `--dry-run` does not change online data; after checking its result, remove `--dry-run` and rerun the command unchanged to submit.
- For multi-page batches, evidence-only writes, or external sources, use `wiki apply --input -` to read a batch from stdin (for example, a heredoc); it also accepts a file path or the wrapped request file above. See `apply.en.md` for the format.

## Rebuild the index

`wiki rebuild-index` rebuilds search and page-link indexes from the existing Markdown. It does not regenerate the body or overwrite pages. Write permission is required. Use it after a write returns `partial=true` or `index_status` is not `current`:

```text
<PIECE> wiki rebuild-index --json
```

## Local recovery copies

Every page update or deletion saves the replaced original file as `.wiki-recovery-<operation_id>.bak`; copies are not cleaned up automatically. Manage them with:

```text
<PIECE> wiki list-recovery --json
<PIECE> wiki list-recovery --status releasable --json
<PIECE> wiki purge-recovery --json                       # preview only; deletes nothing
<PIECE> wiki purge-recovery --operation-id OP_UUID --yes --json
```

- `list-recovery` is read-only. Each entry's `status` is `needed` (the operation has not committed, and the copy is the only retained version of the old content; **do not clean it up**), `releasable` (the operation is settled and an audit-table `before` snapshot exists), or `orphan` (no operation record exists). `counts` summarizes each state and the total bytes.
- `purge-recovery` previews by default; add `--yes` only after user confirmation. By default it removes only `releasable` copies. `orphan` copies require explicit `--include-orphans`; `needed` copies are never removed. If `--operation-id` targets any copy that cannot be removed, the entire batch is rejected with `RECOVERY_REQUIRED`; entries are not silently skipped.
- Cleanup deletes only local `.bak` files; it does not change page bodies or the audit table. After deletion, the old replaced content can no longer be restored from a local copy, though its `before` snapshot remains readable through `wiki history`.
