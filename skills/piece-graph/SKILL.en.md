---
name: piece-graph
description: Maintain a knowledge graph with the Piece CLI: objects, evidence-backed semantic relations, object neighborhoods, relation searches, source lookups, and checks for structural issues or stale sources. Use for graph creation, object and relation maintenance, graph queries, and knowledge checks. Do not use it to compile or edit Wiki pages, or treat page links as semantic relations.
---

# Piece Graph: Maintenance and Queries

Use the Piece CLI to maintain graph objects and evidence-backed semantic relations, inspect object neighborhoods, search relations and trace sources, and check for stale sources. Graph objects, relations, and evidence are independent data; a Wiki page is not required. Basic graph operations do not call a model or embedding service.

## Verify the instance first

Run `<PIECE> status --json` once. Check the configuration directory, port, `cli_version`, and—when ready—`library_id`; keep using the invocation prefix from the exported skill header. If the service is stopped (`SERVICE_UNAVAILABLE`), ask the user to start it. If a command is missing, ask the user to update Piece and export the skill again.

## Choose a route

| User intent | Route |
| --- | --- |
| Find objects, relations, or neighborhoods | `graph search` for objects or `graph relation-search` for relations → `graph get` → `graph graph`; read only |
| Build the graph or add objects and semantic relations | Use the minimal commands below |
| Add evidence only to existing objects or relations | `references/apply.en.md#add-evidence-only`; do not change summaries just to force an update |
| Check stale sources and disputes | `graph lint` → read candidate records and sources → report; do not auto-repair |
| Missing index or abnormal query results | `graph rebuild-index` (see `references/recovery.en.md`) |
| Delete | Preview the impact first and proceed only after user confirmation (see `references/safety.en.md`) |

## Minimal commands

One write takes one command and creates no temporary files:

```text
<PIECE> status --json
<PIECE> graph search "keyword" --limit 5 --json
<PIECE> chunk get CHUNK_ID --json
<PIECE> graph object add --kind concept --title "Object" --summary "Summary" --evidence CHUNK_ID:2-3 --json
<PIECE> graph relation add --source SOURCE_UUID --predicate depends_on --target TARGET_UUID --description "Relation description" --basis explicit --evidence CHUNK_ID:grep=key phrase --json
```

Write only when the user asks to save something. For deletion, preview the impact with `--dry-run`; add `--yes` only after user confirmation. If a failure receipt includes `next_command`, follow it once.

## Build a graph from documents

Read one batch at a time and write as you go; do not read an entire document before starting:

1. **Locate the source:** use `<PIECE> file list --name "fragment" --json` to find `file_id`, then page through `<PIECE> chunk list FILE_ID --page 1 --json` in `chunk_index` order (including the text).
2. **Split into batches:** use consecutive `chunk_index` ranges of about 5–10 cards each. Apply the same rule whether or not the document has headings; headings or page numbers are only batch labels. Do not rely on the document outline because many documents have none. Batches must not overlap; overlap can create the same relation twice.
3. **Process each batch:** after reading it, check for duplicates with `<PIECE> graph search "…" --json` (same names do not mean the same record; do not merge by name), then use `graph object add` / `graph relation add` to submit the batch's objects, within-batch relations, and evidence (`--evidence CHUNK_ID:line` or `CHUNK_ID:grep=substring`).
4. **Relations across batches:** if the endpoints fall in different batches, do not force a relation before both are visible. After all objects are saved, make a second pass, find the existing objects with `graph search`, and add the relation. Objects persist, so the second pass does not require rereading the source.
5. **Resume work:** progress is stored in the graph. In a new session, run `<PIECE> graph references LIBRARY_UUID FILE_ID --json` to see which evidence from that file is already saved. Use `source_chunk_id` to find covered cards and continue from the first uncovered one. An empty card with no extractable object produces no evidence and may be read again; this is expected.
6. **Completion criteria:** report completion only after every card in the file has been read and cross-batch relations have been added. Do not say the graph is complete after processing only the first few batches.

## Graph retrieval workflow

Choose steps based on the question. Do not expand to two hops by default:

1. Extract object names or aliases, relation descriptions, and necessary conditions from the question. Search objects with `graph search` and relation descriptions with `graph relation-search`; these searches may run in parallel.
2. For a question about the relation between two objects, search for each object candidate first, then use `graph graph OBJECT_UUID --depth 1` to inspect each object's adjacent edges and match endpoints by UUID. Relation text search is not guaranteed to match endpoint titles; endpoint query results can also be incomplete around highly connected objects or when truncated.
3. Use `graph get` to inspect the type, summary, and aliases of same-name candidates. If ambiguity remains, explain it or ask; never guess a UUID from a title or merge objects.
4. Use `--depth 2` only when the question concerns indirect connections, paths, or upstream/downstream relationships. Check `truncated`; a truncated result cannot support a claim that “no relation exists.”
5. For relations used in an answer, run `graph get RELATION_UUID` and inspect evidence. Distinguish `supports`, `contradicts`, and `context`, and state source status. `current` means only that the source location/content has not changed; it does not verify the fact. State when evidence is insufficient.
6. Stop when the candidate evidence is enough to answer. Read-only retrieval must not call add, update, or delete commands, and must not invent relations absent from the graph based on adjacency.

## Read references as needed

- Relation semantics, adding evidence, choosing predicates / basis / stance, duplicate decisions, and read-only query commands: `references/semantics.en.md`
- Failure codes (`VERSION_CONFLICT`, `READ_BACK_INCOMPLETE`, unknown submission results, etc.), recovery actions, read-back fields, request keys and local archives, and index rebuilds: `references/recovery.en.md`
- Delete previews and confirmation, backup scope, and safety rules: `references/safety.en.md`
- Multi-record submissions, field-level contract, and evidence-only batches: `references/apply.en.md`
