---
name: piece-wiki
description: Use the Piece CLI to compile source material into Markdown knowledge pages, add evidence-backed content, preserve synthesized conclusions when the user asks, and check existing pages and stale sources. Use for page compilation, incremental revisions, synthesis pages, and Wiki checks. This is not for importing ordinary source files, maintaining knowledge-graph entities or semantic relationships, or automatically saving every answer.
---

# Piece Wiki: Compile and Review

Use the Piece CLI to compile source material into Markdown knowledge pages, add evidence-backed content, preserve synthesized conclusions when the user asks, and check existing pages and stale sources. Markdown files are the source of truth for Wiki pages. Graph entities and semantic relationships are a separate feature maintained with `piece-graph`. Basic Wiki operations do not call a model or embedding API.

## Verify the Piece instance first

Run `<PIECE> status --json` once. Check the configuration directory, port, `cli_version`, and `library_id` when ready; keep using the invocation prefix from the exported Skill header. If the service is not running (`SERVICE_UNAVAILABLE`), ask the user to start it. If the command is unavailable, ask the user to update Piece and export the Skill again.

## Choose the workflow

| User intent | Workflow |
| --- | --- |
| Find material or answer a question | `wiki search` → `wiki get page` → check sources when needed → answer without writing |
| Save a conclusion, compile a page, or revise a page | Use the smallest command below that fits |
| Add evidence to an existing page only | See `references/apply.en.md#add-evidence-only`; do not change the summary just to force an update |
| Check stale or disputed material | `wiki lint` → read candidate pages and sources → report; do not repair automatically |
| The index is missing or Markdown was edited externally | `wiki rebuild-index` (see `references/recovery.en.md`) |
| Clean up local recovery copies | `wiki list-recovery` → preview with `wiki purge-recovery` → after user confirmation, add `--yes` (see `references/recovery.en.md`) |
| Delete | Preview the impact first; execute only after user confirmation (see `references/safety.en.md`) |

## Minimal commands

A write can be a single command; no temporary files are needed:

```text
<PIECE> status --json
<PIECE> wiki search "keyword" --limit 5 --json
<PIECE> chunk get CHUNK_ID --json
<PIECE> wiki page add --kind concept --title "Title" --summary "Summary" --evidence CHUNK_ID:2-3 --json
<PIECE> wiki get page PAGE_UUID --json
<PIECE> wiki page update PAGE_UUID --expected-revision 3 --expected-content-hash ABC... --summary "New summary" --evidence CHUNK_ID:grep=key sentence --json
```

For a long body, use `--body-file -` to read from stdin (for example, a heredoc); there is no need to create a file first.

Write only when the user asks to save something. Preview deletion with `--dry-run` first; add `--yes` only after the user confirms. If a failure receipt includes `next_command`, follow it once.

## Read references as needed

- Page kinds and links, `index_status`, local recovery copies, evidence fields, and read-only queries: `references/semantics.en.md`
- Error codes (`VERSION_CONFLICT`, `READ_BACK_INCOMPLETE`, partial success, unknown commit result, and others), recovery actions, read-back fields, request keys and local request files, and index rebuild: `references/recovery.en.md`
- Delete previews and confirmation, backup scope, and safety boundaries: `references/safety.en.md`
- Multi-page/evidence submissions, field-level contracts, and evidence-only batches: `references/apply.en.md`
