# Graph Semantics and Read-Only Queries

Read this when creating or editing objects, relations, or evidence; choosing predicate direction, `basis`, or `stance`; deciding whether records should be merged; or using read-only query commands.

Graph objects, evidence, and Wiki pages are independent and are not synchronized. Maintain Wiki pages with `piece-wiki`; document import and retrieval are separate workflows.

## Objects

- Object `--kind`: use `concept` for concepts or methods, and `entity` for people, organizations, or products. Objects do not store long-form bodies (`--body-file` was removed); use `--summary` and evidence. Updates must include the `revision` you read and at least one field to change. Omitted fields retain their current values.
- To inspect an existing graph, use `graph search` for names and aliases, and `file list` / `search` for relevant sources. Review each candidate's type, summary, and evidence before deciding. **Same-name objects are not merged automatically, and co-occurrence is not a semantic relation.** Never merge by name.
- Wiki page links are navigation, not graph edges.
- Writes without evidence are allowed, but read-back will report `has_evidence=false`. Do not claim that a record has evidence or that an inference is supported by a quotation when it is not.

## Relations

Read `A → B` as a sentence and compare it with the source before choosing the predicate and endpoint order:

| Meaning | Relation |
| --- | --- |
| A is a kind of B | `A is_a B` |
| A is part of B | `A part_of B` |
| A needs B to run or hold | `A depends_on B` |
| Rule or method A applies to B | `A applies_to B` |

- `supports` is directional too. `contradicts` and `related_to` are normalized as symmetric; do not assign them an artificial direction. If uncertain, do not infer composition or dependency from co-occurrence alone.
- **Changing a description does not change relation structure.** Changing `--description` does not change the predicate or endpoints. To correct the predicate, pass `--predicate`; to correct direction, pass the appropriate `--source` / `--target`. Other fields need not be resubmitted.
- **Relation evidence is independent of object evidence.** A quotation supporting A or B does not automatically support the relation between A and B. An edge's `evidence_count` counts only evidence attached to that relation, including supporting, contradicting, and contextual evidence; it is not a support-vote count.
- `--basis explicit|synthesis|inference|user_statement` describes the nature of the source; do not use a floating-point “confidence” score. Explain `related_to`; do not turn co-occurrence into causation.
- Create only one relation for the same endpoints, predicate, and conditions; add supporting or contradicting evidence to it. Different qualifying conditions may have separate edges. Represent a contradiction truthfully with `--basis` and `--stance`, and preserve both sides' evidence and conditions.
- If another relation has the same endpoints, predicate, and `--qualifier`, identical content returns `reused`; different content returns `KNOWLEDGE_CONFLICT`. `data.existing` gives the existing `id/revision/display/description/basis`, and `next_command` is a prefix for updating that relation. Inspect `existing` and decide whether to reuse or revise it; do not bypass the conflict by changing `--qualifier` to create a duplicate.
- `--stance supports|contradicts|context`: pass it once to apply to all evidence (default `supports`), or pass it multiple times to match evidence in order (`--evidence-file` first, then `--evidence`). A command may attach both supporting and contradicting evidence.

## Evidence

The service extracts an exact substring from the card text. Fields are `source_kind/source_library_id/source_file_id/source_chunk_id/expected_content_hash/quote`. Two equivalent forms are available: inline `--evidence CHUNK_ID:LOCATOR` on an intent command, or JSON created by `chunk extract --out` and then passed with `--evidence-file`.

- `--evidence CHUNK_ID:LOCATOR` may be repeated. `LOCATOR` is a 1-based line number `N` / range `N-M`, `grep=substring`, or `re=regex`. The service extracts the exact quotation and returns its hash. **Ambiguous matches are rejected** with a recovery command using `chunk get`; read the text and narrow it by line numbers. Never guess the first match.
- Use a match only when it is unique and not `truncated`. For multiple matches, read the candidates and narrow with a 1-based line range `N-M`; do not guess the first. `grep=` means substring by default; `re=` uses a regular expression. `chunk extract` also supports `--context N` to include surrounding lines.
- Substrings and regexes locate lines, and the returned quotation contains the whole line. Different keywords on one line may return the same quotation. A line number selects whole lines; `--context` adds surrounding lines. Neither can extract only part of a line.
- Matching owner, source, `stance`, and `quote` is normally deduplicated as `reused`; the same quotation may be attached to different objects or relations.
- `chunk extract --out` does not overwrite an existing file; choose a new path when needed.
- Piece quotations must appear verbatim in the current card text (only line endings are normalized). `--lines` may locate loosely, but the returned `quote` is always an exact substring and is still checked verbatim at submission.
- A source location is not a claim: `current` means only that the location has not changed, not that the content is true. For external evidence, set `source_kind=external` and provide a title, safe `http` / `https` URL, and quotation. For user statements, use `source_kind=user`; do not fabricate a file or web source.
- When adding evidence to an **existing object**, a batch must still include that object's `{id, expected_revision}` concurrency condition. The evidence `object` must reference an object created in this batch or the updated object ID. No object is created automatically.
- `chunk extract CHUNK_ID --lines 2-3 --out evidence.json` followed by `--evidence-file evidence.json` remains supported; the two evidence forms can be mixed.

## Read-only command quick reference

```text
<PIECE> graph list --kind concept --status active --limit 20 --offset 0 --json
<PIECE> graph search "keyword" --limit 5 --json
<PIECE> graph relation-search "relation description keywords" --limit 5 --json
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

The type for `get` is optional: when given only a UUID, the service detects whether it is an object, relation, or evidence; the response's `kind` reports the actual type. `history` and `delete` still require an explicit type.

`request --input` and a positional request key are mutually exclusive. This command only checks status and does not submit. When checking a delete request, read-back verifies that the record is absent and returns `deleted=true`; it does not return the pre-delete body.

`search` matches object titles, aliases, and summaries. `relation-search` matches a relation's own description and conditions; after a hit, use its `id` with `get relation` to read evidence. Both return candidates only. Endpoint titles are read live, are not copied into the index, and are not semantically merged.

`get` returns `record` and `evidence.items/total/limit/offset`, plus `has_evidence`; continue reading pages as needed. Relation `record.display` and graph edge `display` are read-only views (for example, `A —depends_on→ B`; symmetric relations have no arrow) generated from current endpoint titles. UUIDs remain the identity; never write `display` back in a batch. `graph` defaults to depth 1, with a maximum of 2; `--edge-type` accepts only `relation`. Check `truncated`; returned counts are not whole-graph totals. `lint` performs structural checks only, not semantic review.

Data-layer commands include `file list --name`, `search --file-id`, `chunk list/get`, `chunk extract`, and `file page FILE_ID PAGE --output source-page.png`. `search` returns candidates only; then read the text by `chunk_id`. Do not guess a source page number. Import originals on demand with `file import` / `import-markdown` and wait for indexing; do not create files or chunks just to add graph objects.
