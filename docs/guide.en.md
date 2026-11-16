# Piece user guide

[Back to README](../README.en.md) · [简体中文](guide.md) | **English**

The README explains what Piece is for and how to get started. This guide covers additional integrations, command-line operations, and the limits of data exchange and maintenance.

## Connecting your AI

### MCP: provide capabilities to your AI client

Open **Tools & integrations → MCP Configuration**, choose your client and the services you need, save the generated configuration in the client, and reload it. Keep the Piece service running during use.

Start with `piece-kb` only. Confirm that AI can retrieve documents, then add services for the tasks you need:

| Service | Purpose |
| --- | --- |
| `piece-kb` | Read-only search, card and source retrieval, exact quote extraction |
| `piece-index` | Import documents, write notes, add or edit knowledge cards |
| `piece-wiki` | Read, compile, and maintain Markdown knowledge pages and source evidence |
| `piece-graph` | Read and maintain graph entities, semantic relationships, and source evidence |
| `piece-zotero` | Read your local Zotero library, import attachments, and perform authorized changes |

Clients support different connection methods; follow the configuration page and your client's documentation. Searching does not require opening every write capability.

### Skills: workflows for terminal AI

If your AI can run local commands, choose workflows under **Tools & integrations → Skill Export** and export them to a skill directory supported by the client. Exports include the CLI invocation prefix for the current Piece instance.

| Skill | Purpose |
| --- | --- |
| `piece-search` | Read-only search, text retrieval, and source verification |
| `piece-index` | Import and organize documents, create notes, maintain knowledge cards |
| `piece-wiki` | Compile, revise, and check knowledge pages |
| `piece-graph` | Maintain entities, relationships, and graph evidence |
| `piece-zotero` | Read and import Zotero material as needed, perform authorized changes |

Load the exports according to your client's instructions. Re-export after changes to the instance path, port, or version to avoid stale invocation prefixes.

## Running from source

Piece requires Python 3.12+. Managing the environment with [uv](https://docs.astral.sh/uv/getting-started/installation/) is recommended:

```bash
git clone https://github.com/ultraplan-bit/Piece.git
cd Piece
uv sync --python 3.12 --locked --extra desktop
uv run --extra desktop piece serve --open
```

Windows can use the desktop window; macOS and Linux can use the browser interface, with real-hardware compatibility still being refined.

For command help, run `uv run --extra desktop piece --help`. The Windows portable build uses `piece-cli.exe` in the terminal.

## Command line

In the commands below, replace `piece` with `uv run --no-sync piece` in a source environment, or `piece-cli.exe` in the portable build. File, retrieval, and knowledge management commands require a running Piece service.

```bash
piece config init --interactive   # Configure the embedding model; hide the key and save only after confirmation
piece start                       # Start the core service in the background and return once ready
piece config test embedding       # Reach the configured model service; check connectivity and dimensions
piece file import "document.md" --wait
piece file list --name "document" # File-name substring match, not full-text search or wildcards
piece stats                       # Imported/indexed files, chunk count, and total original size
piece status
piece stop                        # Request a stop; the service finishes in-flight work
```

### Configuration and startup

- Without `--interactive`, `config init` is non-interactive; scripts can follow it with `config update --offline --input patch.json`.
- The wizard never goes online automatically and leaves existing admin/MCP keys unchanged. Offline saves are refused while the service is running. An existing index cannot switch embedding model, endpoint, or dimensions directly.
- `start` enables neither GUI, MCP, nor tray by default. Use `start --gui --mcp` or `start --open --mcp` when needed.
- Background startup waits up to 60 seconds by default, adjustable with `--timeout`. A timeout or Ctrl+C stops waiting without killing the background process.
- With a custom knowledge base, keep the same `--data-dir` and `--port` for every command. `--data-dir` is the configuration directory; actual storage is governed by the configured `data_path`.
- If files are already under `files/originals/`, preview with `piece file scan --dry-run`, then register and index unregistered files with `piece file scan --wait`.

### Machine-readable help

When an AI client needs structured parameters, add `--json` to `--help`:

```bash
piece file list --json --help
```

This returns parameter names, value shapes, choices, and defaults. Drill down from `piece` → `piece file` → `piece file list` rather than parsing prose help or guessing arguments through trial and error.

## Importing and organizing documents

### Check extraction first

Text PDFs can use local parsing. Scans, complex formulas, or tables need a suitable service configured in the PDF parsing section of Settings. Options include PaddleOCR, MinerU, or a custom multimodal model; the service's deployment determines whether the documents leave your machine.

After import, check the card text, then try a question under **Tools & integrations → Recall Test** and inspect the candidates. Correct card titles and text when needed; files with an original-page view can also be checked side by side.

### Collections, migration, and export

- Use hierarchical collections to scope documents, such as "Papers → Retrieval-Augmented Generation". A parent includes files in child collections by default, and one file can belong to several collections.
- Obsidian and Zotero imports are one-time copies, not two-way sync, and leave the original library unchanged. Zotero can also be read without importing; see below.
- Text fetched by your AI client, such as web pages, articles, or subtitles, can be imported whole: `import_markdown` on the index MCP service, or `piece file import-markdown` for skills. This is not automatic web scraping by Piece.
- Markdown exports preserve card edits; exports with resources include images too.
- **Reindexing from originals overwrites manual edits.** Export or back up first.

## Wiki and knowledge graph

Wiki and graph are separate features: Wiki stores topic pages, while graph stores entities and relationships. Each can link to source evidence; creating a Wiki page does not require creating a graph first.

Importing documents does not automatically compile pages or generate relationships. When asking AI to organize knowledge, specify what should be saved; ordinary lookups and answers do not need writes.

### Wiki storage and external editing

Wiki text and metadata are stored as Markdown files under `wiki/` in the configured `data_path`. SQLite holds a rebuildable page index and a separate audit trail. In-page links use `[Title](piece://wiki/PAGE_UUID)`, so changing the title or filename does not change identity.

After editing externally, explicitly run:

```bash
piece wiki rebuild-index
```

This refreshes the index without rewriting Markdown or calling a model. The page index is rebuildable; this does not make the entire SQLite database disposable. Backups still need both the database and `wiki/`.

### Write a page through the CLI

One command writes a page, reads it back by default, and creates no temporary file. Replace `CHUNK_ID` with a real card ID you have retrieved, and use line numbers from that card's text:

```bash
piece wiki page add --kind concept --title "Incremental maintenance" \
  --summary "Only change what clearly changed" \
  --evidence CHUNK_ID:2-3
```

- `--evidence CHUNK_ID:LOCATOR` can repeat. LOCATOR is a line number `N` / `N-M`, `grep=substring`, or `re=regex`; the service extracts the exact quote and returns its hash.
- One `--stance` applies to all evidence; multiple instances map in order.
- Update a page with `wiki page update UUID --expected-revision N --expected-content-hash HASH`. Use `graph object add/update` for entities and `graph relation add/update` for relationships.
- Each write generates a request key returned as `data.request_key`. Add `--save-request` to keep a copy under `requests/` in the data directory, or use `--request-file PATH`. An existing file may be reused only if both target and content match.
- `--dry-run` only pre-checks. After confirmation, remove it and rerun the same command to submit.

Page evidence must quote source text verbatim. An agent can use `piece chunk extract` to extract an exact substring by line number or match; the retrieval MCP's `extract-quote` provides the same capability. CLI `--out` writes an evidence file. If a match is not unique, narrow it with `--lines` rather than guessing the first hit.

### Write through MCP

Prefer intent tools with flat arguments:

- `piece-wiki`: `wiki-page-add` / `wiki-page-update`.
- `piece-graph`: `knowledge-object-add` / `knowledge-object-update` / `knowledge-relation-add` / `knowledge-relation-update`.

Inline evidence as `CHUNK_ID:line` or `CHUNK_ID:grep=substring`; `request_key` is generated and returned automatically. These tools share assembly logic with CLI commands such as `wiki page add` and `graph object add`.

Use the batch tools `wiki-apply` / `knowledge-apply` when submitting multiple records at once or when you need field-level control.

See the built-in [Wiki skill](../skills/piece-wiki/SKILL.md) and [graph skill](../skills/piece-graph/SKILL.md) for detailed operations and checking workflows. Those instructions are in Chinese.

## Graph data exchange

### Choose a format

| Format | Best for | Information retained |
| --- | --- | --- |
| JSON | Piece data exchange; choose it for lossless exchange | Piece graph data, including evidence locations and related metadata |
| GraphML | Standard graph structures for general-purpose graph tools | Omits evidence, aliases, revisions, and timestamps; not a lossless round trip |

GraphML is a standard general-purpose graph projection. Ordinary properties do not use Piece-specific JSON encoding.

### GraphML import limits

- Only single-level directed graphs with the agreed properties are supported.
- Node `label` is the title; edge `label` must be a supported Piece relationship name. Unknown business relationships are rejected.
- Symmetric relationships use the standard boolean property `symmetric`.
- Non-UUID node/edge IDs are mapped to internal UUIDs.
- Legacy Piece GraphML imports and arbitrary third-party business-semantic mappings are not supported.

Import performs full validation followed by a rolled-back preview by default. Writing requires confirmation and no conflicts. Cross-library JSON imports retain original evidence locations and hashes, but sources that have not been connected remain unresolved. Keeping an evidence record does not mean the destination library has the source text.

## Working with Zotero

Query your local personal Zotero library directly for bibliographic data, notes, PDF annotations, and available full text without importing into Piece. Import selected attachments when closer verification is needed; identical content reuses an existing file and retains multiple sources.

### Connect and read

Zotero 7+ must be running with **Settings → Advanced → Allow other applications on this computer to communicate with Zotero** enabled. The Piece entry is **Tools & integrations → Zotero Library**.

```bash
piece zotero status --json
piece zotero search "keyword" --collection-key COLLECTION_KEY --limit 20 --json
piece zotero children ITEM_KEY --item-type attachment --json
piece zotero preview --attachment-key ATTACHMENT_KEY --collection-mode none --server-id SERVER_ID --json
piece zotero import --attachment-key ATTACHMENT_KEY --collection-mode none --server-id SERVER_ID --wait --json
```

Replace the keys and `SERVER_ID` with real values returned by queries. Importing is a one-time copy and leaves the original library unchanged.

### Modify Zotero

The separate `piece-zotero` MCP service provides lookups, `zotero_import`, authorization, and change preview/execution. Other services register no Zotero tools.

Modifying Zotero requires explicitly enabling `zotero_write_enabled`, which defaults to `false`; authorize in a Zotero popup, preview the diff, then execute by `plan_id`. See the [Zotero instructions](../skills/piece-zotero/SKILL.md), in Chinese, for details.

## Data and maintenance

### Local storage and external services

Originals, cards, the database, and Wiki files are stored locally at the configured `data_path`. The Windows portable build defaults to `data/` beside the program.

Local storage does not mean every processing step is local:

- Cloud embedding models receive card text for indexing and query text for semantic search.
- Cloud OCR, MinerU, or multimodal parsing services may receive original documents, page images, or related content.
- Your AI client may pass retrieved results, original text, or images to its model provider.
- Enabling WebDAV sends the files being synchronized to your configured cloud storage.

Before processing sensitive content, check each service's deployment location, access scope, and privacy policy.

### Fully quit and back up

Closing the Windows window does not stop the background service; choose **Quit** from the system tray to exit completely. For CLI-started services, request a stop with `piece stop` and wait for shutdown to complete.

Before upgrading or moving, fully exit and back up the entire data directory, including SQLite and Wiki Markdown files. Do not copy only the database, and do not omit other database data just because the Wiki index can be rebuilt.

**v0.1.3 cannot open databases from v0.1.2 or earlier and provides no automatic migration.** Keep the previous application and a complete data backup. To try the new version, extract it to a new location and use an empty data directory; do not copy the old `data/` beside the new application or overwrite the previous installation.

### WebDAV sync

Connect a WebDAV service such as Nutstore under **Cloud Sync**, but note that it **syncs files only, not a complete knowledge base**:

- Other devices need to rebuild their indexes.
- Later syncs favor the local side and may overwrite cloud files.
- One-shot Obsidian / Zotero imports are not continuous sync either.

Back up before syncing, reindexing, or moving.

## FAQ

**An import failed. What should I check?**

Check the model connection and file processing status. For scans, check the parsing service too. Specific errors appear under **Tools & integrations → Logs**.

**Search results are poor.**

Check that the card text is correct and the file has finished indexing, then check whether the query is restricted by file or collection. Make titles specific: "API auth: token refresh flow" is easier to recognize than "Chapter 3".

**Piece can find the documents, but my AI cannot.**

Check that the Piece service is running and the client has loaded the current instance's MCP configuration or skills. Verify the connection with read-only retrieval first; enabling writes is unnecessary.

## Technical notes

Piece combines BM25 and vector retrieval with RRF ranking. Local storage uses SQLite, FTS5, and sqlite-vec; the management interface uses NiceGUI. These choices do not change the data flows through external embedding services, parsers, or your AI client. Privacy boundaries still depend on the configuration described above.
