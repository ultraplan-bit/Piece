# Wiki Page Semantics, Evidence, and Read-Only Queries

Read this when creating or updating pages, choosing a kind or link, interpreting `index_status` or local recovery copies, or looking up read-only commands.

Document import and retrieval are separate workflows; they do not create graph entities or relationships.

## Pages and links

- Page `--kind`: use `concept` for concepts or methods, `entity` for people, organizations, or products, `topic` for a topic, `synthesis` for a synthesized conclusion, and `source_summary` for a source summary. Updates must include the `revision` and `content_hash` you read, plus at least one field to change. Fields not supplied keep their current values.
- A page has a stable UUID; renaming it does not change its identity. Display titles may be identical, but pages remain distinct by UUID and are never merged by name. Find existing pages with `wiki search` for titles and aliases, and find related material with `file list` or `search`; inspect each candidate's kind, summary, `content_hash`, and evidence before deciding.
- Write page links as `piece://wiki/<UUID>`. They are for navigation only and do not become graph relationships. Links are derived from Markdown, and `wiki lint` reports broken links. `[[Page title]]` is ordinary text and is not resolved to a page link; never guess a UUID from a title.
- Markdown may be edited outside Piece. `wiki get` and `wiki list` return `content_hash`, `path`, and `index_status`. A page's `content_hash` is the SHA-256 of the raw bytes of the entire Markdown file, including front matter, body, and line endings; it is not a hash of the body alone. If `index_status` is not `current`, run `wiki rebuild-index` first. Do not write stale database content back over the file.
- Updates and deletions preserve the replaced or removed page file as a local recovery copy named `.wiki-recovery-<operation_id>.bak`. It is not scanned and is not another source of truth. Receipts and history provide its path, so do not claim the content was permanently erased. Include it in backups. Copies are not cleaned up automatically; use `wiki list-recovery` to inspect them and preview with `wiki purge-recovery` before cleanup after user confirmation (see `recovery.en.md`).

## Evidence

The service extracts exact substrings from card text. Evidence fields are `source_kind/source_library_id/source_file_id/source_chunk_id/expected_content_hash/quote`. Two equivalent methods are supported: inline `--evidence CHUNK_ID:LOCATOR` on an intent command, or write JSON with `chunk extract --out` and pass it with `--evidence-file`.

- **`--evidence CHUNK_ID:LOCATOR`** may be repeated. LOCATOR can be a 1-based line number `N` or range `N-M`, `grep=substring`, or `re=regex`. The service extracts the exact quote and returns its hash. **Ambiguous matches are rejected** with a recovery command using `chunk get`; read the body and narrow the locator with line numbers instead of guessing the first match.
- A match is accepted only when there is exactly one match and the result is not `truncated`. For multiple matches, read the candidates and narrow with a 1-based line or range `N-M`; never guess the first match. `grep=` uses substring matching by default; `re=` uses a regular expression. `chunk extract` also supports `--context N` to include surrounding lines.
- Substrings and regular expressions locate lines, but the returned quote contains the full line. Different keywords on the same line may therefore produce the same quote. A line locator and `--context` can include whole lines, but cannot select part of a line.
- `reused` is normal deduplication when owner, source, `stance`, and `quote` match. The same quote may be attached to different pages.
- `chunk extract --out` never overwrites an existing file; choose a new path when needed.
- A quote from this library must appear verbatim in the current card body (with line endings normalized only). A `--lines` locator may be broad, but the service returns an exact substring and still checks it verbatim at submission time.
- A locator is not a claim of truth: `current` means only that the location has not changed. For external evidence, use `source_kind=external` and provide a title, a safe `http` or `https` URL, and the quote. For user statements, use `source_kind=user`; do not invent a file or web source.
- When adding evidence to an **existing page**, the batch must still include that page's `{id, expected_revision, expected_content_hash}` concurrency conditions. The evidence `page` must point to a new ref or updated ID in the same batch; evidence does not create a page automatically.
- `--stance supports|contradicts|context`: one value applies to all evidence (default `supports`); multiple values map to evidence in order (`--evidence-file` entries first, then `--evidence` entries). A command may attach both supporting and contradicting evidence.
- The older two-step method remains available: `chunk extract CHUNK_ID --lines 2-3 --out evidence.json`, then `--evidence-file evidence.json`. The two evidence methods can be mixed.
- Writes without evidence are allowed, but read-back reports `has_evidence=false`. Do not claim a page is evidenced or present an inference as supported by a quote when it is not.

## Read-only quick reference

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

The type for `get` is optional: with only a UUID, the service detects whether it is a page or evidence, and the response's `kind` reports the actual type. `history` and `delete` still require an explicit type.

`request --input` and a positional request key are mutually exclusive. Both only query; they do not submit. When querying a delete request, read-back verifies that the record is gone and returns `deleted=true`; it does not return the pre-deletion body.

`get` returns `record`, `evidence.items/total/limit/offset`, `links`, `backlinks`, and `has_evidence`; continue reading if the evidence is paginated. UUID remains the identity. `lint` performs structural checks only; it does not judge semantics.

For source material, use `file list --name`, `search --file-id`, `chunk list/get`, `chunk extract`, and `file page FILE_ID PAGE --output source-page.png`. `search` returns candidates only; retrieve the body by `chunk_id`, and do not guess the original page number. Import originals on demand with `file import` or `import-markdown` and wait for indexing; do not create extra files/chunks just for a Wiki page.
