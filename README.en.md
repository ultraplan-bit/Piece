<div align="center">

<img src="assets/maple-leaf.png" width="120" alt="Piece logo">

[简体中文](README.md) | **English**

</div>

<a href="assets/一图看懂%20Piece.png"><img src="assets/一图看懂%20Piece.png" alt="Piece at a glance: turning your documents into a local knowledge base AI can cite" width="100%"></a>

## What Piece can do

- **Find information**: Let AI find relevant passages and cite sources instead of searching through files by hand.
- **Edit knowledge cards**: Your documents are organized into knowledge cards with editable titles and text.
- **Check the originals**: Compare cards with PDF pages or pages converted from Word and PowerPoint to spot parsing errors.
- **Organize notes**: Group files into collections, and let AI help you create notes and add cards.

Before and after Piece, the same tasks look like this:

<a href="assets/为什么选%20Piece.png"><img src="assets/为什么选%20Piece.png" alt="Before versus with Piece: hunting through files, answers with no source, unreadable scans, redoing setup per AI client, and documents sent to the cloud, paired with search by meaning, cited answers that link back to the original page, parsing services, MCP and skills for every AI client, and files and database kept on your machine" width="100%"></a>

## Screenshots

The screenshots show the Chinese interface. Click any image to view it at full size.

<table>
  <tr>
    <td width="50%" align="center" valign="top">
      <a href="assets/原页对比图.jpg"><img src="assets/原页对比图.jpg" alt="Knowledge cards alongside the original PDF page" width="100%"></a>
      <br><b>Source comparison</b> · Check content against the original
    </td>
    <td width="50%" align="center" valign="top">
      <a href="assets/知识卡片编辑.png"><img src="assets/知识卡片编辑.png" alt="Editing a knowledge card's title and text" width="100%"></a>
      <br><b>Card editing</b> · Correct content whenever needed
    </td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top">
      <a href="assets/召回测试.png"><img src="assets/召回测试.png" alt="A search query and the related knowledge cards it finds" width="100%"></a>
      <br><b>Recall Test</b> · Ask a question and see what comes back
    </td>
    <td width="50%" align="center" valign="top">
      <a href="assets/skill导出.png"><img src="assets/skill导出.png" alt="Exporting Piece skills for searching and organizing a knowledge base" width="100%"></a>
      <br><b>Skill Export</b> · Let terminal-based AI use your knowledge base
    </td>
  </tr>
</table>

<details>
<summary>Another example: PowerPoint formulas and source pages</summary>

[![PowerPoint formulas alongside the original page](assets/ppt原页对比图.jpg)](assets/ppt原页对比图.jpg)

</details>

## The life of a document in Piece

<a href="assets/资料的一生.png"><img src="assets/资料的一生.png" alt="Six steps a document goes through in Piece: import, parse, split into cards, organize, retrieve, and cite" width="100%"></a>

## Getting started

### 1. Download and open Piece

Download the ZIP from [**Releases**](https://github.com/ultraplan-bit/Piece/releases), extract the entire archive, and double-click `Piece.exe`. The Windows package does not require a separate Python installation. Keep the executable with the other extracted files.

### 2. Set up the search model

Open **Settings → Embedding Model**. This model helps Piece search by meaning and needs to be configured before first use.

New users can start with the default [SiliconFlow](https://siliconflow.cn/) service:

1. Get your own **API Key** from the provider's dashboard.
2. Paste it into Piece. You can leave the default address and model unchanged.
3. Click **Test Connection**, then save once the test succeeds.

Other compatible services are also supported. External services may charge fees; check your provider's pricing and usage limits.

### 3. Import your documents

On the **Library** page, click **+ → Upload file** and pick the documents to import, then wait for processing to finish. Your knowledge cards will appear. Supported formats include **PDF, Word, PowerPoint, Excel (`.xlsx`), Markdown, TXT, HTML, and EPUB**.

- **Web pages and e-books**: Save a web page as HTML from your browser and import it. Piece keeps only the main content and records the title, author, and source URL as file properties; pages saved with a SingleFile-style extension keep their images too. EPUB e-books import directly, one card per chapter, with the book title and authors attached. DRM-protected e-books cannot be imported.
- **Scans, complex formulas, or tables**: Configure a PaddleOCR service, MinerU, or a custom multimodal model under **Settings → PDF Parsing Settings**. Follow the prompts to enter the service address and key, then test the connection. PDFs with a usable text layer can be imported without this setup.
- **Word / PowerPoint**: Installing Microsoft Office or LibreOffice is recommended. Formats such as `.doc`, `.ppt`, `.rtf`, `.odt`, and `.odp` require a working conversion tool.
- **Existing Obsidian notes or Zotero papers**: In the same menu, **Import local folder** brings in an Obsidian vault (skipping `.obsidian`, templates, and link-only index pages). **Import from Zotero** opens the literature workspace: search and select items, then preview and choose the PDFs to import with their authors, year, DOI, and attachment identities. Zotero 7+ must be running with local communication enabled under Settings → Advanced. Imports make one-time copies without modifying the source library.

After importing, open **Recall Test** and ask a relevant question to check whether Piece finds the content you need.

### 4. Connect your AI client

**Option 1: MCP Config**

MCP is a way for AI to access your knowledge base. Open Piece's **MCP Config** page, choose your AI client, copy the configuration, then save and reload it as required by that client.

- **Only need to search?** Choose the read-only retrieval service, `piece-kb`.
- **Want AI to write notes or edit cards too?** Also enable the indexing service, `piece-index`.
- **Reading or organizing Zotero literature?** Select `piece-zotero` separately. Enable it in **Settings → Zotero Library** and restart Piece with MCP enabled. Its default port is 8688 and its key is independent.

Do not share the access keys in your configuration. After changing keys or ports, fully quit and restart Piece, then update your AI client's configuration.

**Option 2: Skill Export**

If your AI can run local commands, export workflows from **Skill Export**: `piece-search` finds source material, `piece-index` imports and organizes documents, `piece-wiki` compiles and maintains Markdown pages, `piece-graph` maintains entities, semantic relationships, and evidence, and `piece-zotero` reads and works with Zotero literature. Export them again after moving the application or changing the data directory.

**Wiki** provides page search, text, references, page links, and history. **Knowledge Graph** provides entities, a relationship table, bounded neighborhoods, and evidence checks. Use the `piece wiki` and `piece graph` CLI commands or the two existing MCP services. Their identities and lifecycles are separate: page links are not semantic relationships, matching names do not create associations, and search results are not merged across features.

For MCP writes, prefer the intent tools on the indexing service: `knowledge_object_add` / `knowledge_object_update` / `knowledge_relation_add` / `knowledge_relation_update` and `wiki_page_add` / `wiki_page_update`. They take flat parameters, accept inline evidence as `CHUNK_ID:lines` or `CHUNK_ID:grep=text`, and generate and return the `request_key` automatically. They share the same assembly logic as the CLI's `graph object add` and `wiki page add`; fall back to the `knowledge_apply` / `wiki_apply` batch tools only when you need to submit several records at once or control individual fields. When a new relation collides with an existing one on the same endpoints and predicate, the receipt carries the existing relation's summary and revision and spells out the `knowledge_relation_update` call. The retrieval service's `knowledge-get` / `wiki-get` need only a UUID; `kind` is detected when omitted.

Wiki text and metadata are authoritative Markdown files under `wiki/` in the configured `data_path`. SQLite holds rebuildable page indexes and separate audit records. Link pages with `[Title](piece://wiki/PAGE_UUID)`; renaming titles or files does not change identity. After external edits, explicitly run `piece wiki rebuild-index`; it does not rewrite Markdown or call a model.

Uploading documents does not automatically build Wiki pages or a graph. Deleting source files retains pages, entities, and their quote snapshots, with source locations marked missing. A quote matching the current source is not factual verification.

Knowledge evidence requires verbatim quotes, and copying long text with formulas or tables by hand is error-prone. Agents can use `piece chunk extract` to slice an exact substring from a card's text by line range or match, yielding submit-ready evidence directly (available in the CLI and both MCP services: `extract_quote` on the indexing service and `extract-quote` on the retrieval service).

Use `--out` to save the sliced quote straight to a submit-ready evidence file; when the match is not unique, narrow it with `--lines` first instead of guessing the first hit.

Writes to knowledge pages go through intent commands: one command, read back by default, no temporary files:

```bash
piece wiki page add --kind concept --title "Incremental maintenance" \
  --summary "Change only what clearly changed" \
  --evidence CHUNK_ID:2-3
```

`--evidence CHUNK_ID:LOCATOR` is repeatable; LOCATOR is a line number `N` / `N-M`, `grep=substring`, or `re=regex`. The service slices the exact quote and returns its hash; a non-unique match refuses to submit. `--stance` given once applies to all evidence, given several times maps to each in order. The two-step form `chunk extract --out evidence.json` then `--evidence-file evidence.json` still works.

Update pages with `wiki page update UUID --expected-revision N --expected-content-hash HASH`, using the hash from the latest read-back. Do not bypass external-edit conflicts by merely changing the revision. Use `graph object add/update` for entities and `graph relation add/update` for relationships; records are identified by their own UUIDs, never by title.

Graph batches use a SQLite transaction; Wiki batches commit per page and can partially succeed. Check `partial`, `errors`, and `index_status`. If files were saved but indexing failed, keep the files and explicitly rebuild the index rather than regenerating or overwriting the text.

Every write generates a request key and returns it as `data.request_key`; the server keeps the receipt by key. To keep a local copy add `--save-request` (auto-saved under `requests/` in the data directory) or `--request-file PATH`; an existing path is reused only when both target and content match, and is never overwritten. `--dry-run` only pre-checks; rerun without it to submit. Multi-record batches go straight through stdin:

```bash
piece wiki apply --input - --read-back <<'JSON'
{"reason": "...", "pages": [...], "evidence": [...]}
JSON
```

Request files contain sensitive local data; deleting an online record does not delete them. `read_back` includes at most 20 affected records. `READ_BACK_INCOMPLETE` means the write was committed but read-back was incomplete or the state has changed: do not resubmit. Recoverable errors include `next_command` / `next_argv` for read-only checks on the same instance. If the submission outcome is unknown, first query `wiki request KEY --read-back` (or `--input FILE` when a request file exists, so the CLI reads the key).

Content your AI client fetches by itself, such as web pages, WeChat articles, or video transcripts, can go straight into the knowledge base: MCP uses the `import_markdown` tool of the indexing service, and the skill runs `piece file import-markdown` with the content from stdin or a file. Piece keeps the original, splits it into cards automatically, and records the title and source URL as file properties.

Once connected, ask a question in your **AI client**, for example:

> Find the relevant explanations in my documents and cite the sources.

Piece provides the source material; conversations and answers still take place in your usual AI client.

### Working with Zotero

Read the local personal Zotero library directly: bibliographic records, notes, PDF annotations, and available indexed text. No prior Piece import or PDF attachment is required for looking up a reference. Import selected attachments only when deeper analysis is needed; identical content reuses an existing Piece file while retaining multiple Zotero sources.

```bash
piece zotero status --json
piece zotero search "query" --collection-key COLLECTION_KEY --limit 20 --json
piece zotero children ITEM_KEY --item-type attachment --json
piece zotero preview --attachment-key ATTACHMENT_KEY --collection-mode none --server-id SERVER_ID --json
piece zotero import --attachment-key ATTACHMENT_KEY --collection-mode none --server-id SERVER_ID --wait --json
```

Replace placeholders with returned keys and the server ID; omit `--server-id` on older read-only Zotero versions without one. Search covers title, creator, and year by default; `--scope everything` also searches abstracts, notes, annotations, and indexed full text. Collection search does not include descendants. Indexed text reports coverage and is not a substitute for the original PDF.

The dedicated **`piece-zotero` MCP service** provides Zotero reads, selected imports, authorization, change previews and execution. The existing `piece-kb` and `piece-index` services do not register Zotero tools. MCP imports require an explicit scope and allow at most 50 attachments; use `zotero_check_tasks` on the same service to check indexing completion. Run `piece mcp-config --service zotero --include-secrets --json` for client configuration and keep its key private. Export the dedicated **`piece-zotero` skill** from Skill Export or with `piece skill export piece-zotero --dir TARGET_DIRECTORY`.

Open **Tools → Zotero Library** (also reached through “Import from Zotero”) to browse paginated literature records, notes, annotations, indexed text and Piece associations, or export citations. Select literature items, preview and choose individual attachments for import, then refresh indexing status without confusing acceptance or duplicates with completion. For writes, enable them in **Settings → Zotero Library**, preview the changes, request authorization, and confirm the plan. Unresolved results retain the same plan rather than creating content again.

Zotero mutations require **10.0.5+**, an explicitly enabled `zotero_write_enabled` setting (default `false`), and approval in Zotero's authorization dialog. Preview the exact metadata, tag, collection, or note changes before confirming their `plan_id`. Keys stay in process memory and previews expire after 15 minutes. There is no automatic deletion, attachment replacement, full-library synchronization, or saving of answers. Do not expose Zotero's local port externally.

## A few useful tips

- **Search results not quite right?** Check the card text first, then make the title more specific. For example, “Authentication: Token refresh flow” is easier to recognize than “Chapter 3.”
- **Too many documents?** Use hierarchical collections such as “Research → Retrieval.” A parent includes files in its descendants by default; turn off “Include subcollections” to show direct assignments only. A file can belong to multiple collections, and moving a collection does not move original files.
- **Want to take your content elsewhere?** Markdown exports preserve card edits. Exporting with resources also bundles the illustrations.
- **Need to parse a document again?** You can reindex it, but **reindexing from the original file overwrites manual edits**. Export or back up your work first.

## FAQ

<a href="assets/安心用.png"><img src="assets/安心用.png" alt="Where your documents live: originals, knowledge cards, and the database stay on your computer, and content is only sent to the embedding model, OCR or MinerU parsing services, or WebDAV sync when you choose to" width="100%"></a>

**Are my files uploaded?**

Files and the database are stored locally. However, cloud models and parsing services receive the relevant text, queries, or page content, and your AI client also receives the material you retrieve. Check the privacy policies of your chosen services before processing sensitive information.

**Can AI still search after I close the window?**

Yes. Closing the management window does not stop the background service. To fully quit Piece, use the exit option in the system tray menu.

**Where is my data, and how do I back it up?**

The Windows portable package stores data in the `data/` folder beside the application by default. Fully quit Piece before backing up that folder or upgrading. If you changed the storage location, back up that data directory too. Include both the `wiki/` Markdown files and SQLite database (graph data, Wiki audit history, and source metadata); a database-only backup is incomplete. Deleting a Wiki page retains its audit history and local `.wiki-recovery-*.bak` recovery copies; include these in backups too. Online deletion is not secure erasure and does not remove local request files or external backups.

**Can I sync between computers?**

Use **Cloud Sync** to connect a WebDAV-compatible service such as Nutstore. It syncs files, not the complete knowledge base; other devices need to build their own indexes. **After the initial sync, the local files take precedence, which can overwrite or delete remote files. Back up first.**

**What if an import fails or AI cannot find my documents?**

Check the model connection, file processing status, and AI client configuration. Scanned documents also need a parsing service. See **Logs** for specific errors.

<details>
<summary>Run from source and use the command line (optional)</summary>

Python 3.12+ is required. We recommend [uv](https://docs.astral.sh/uv/getting-started/installation/) to manage the environment:

```bash
git clone https://github.com/ultraplan-bit/Piece.git
cd Piece
uv sync --python 3.12 --locked --extra desktop
uv run --extra desktop piece serve --open
```

On macOS and Linux, you can run from source with a browser-based interface; real-device compatibility is still being improved. For command-line help, run `uv run --extra desktop piece --help`. With the Windows portable package, use `piece-cli.exe` in your terminal. Commands that work with your knowledge base require the Piece service to be running.

</details>

---

Found a problem or have a suggestion? [Open an issue](https://github.com/ultraplan-bit/Piece/issues) with reproduction steps and redacted screenshots. Do not upload access keys or private documents.

## Community

This project recognizes and links to the [LINUX DO](https://linux.do) community.

## License

This project is open source under the [MIT License](LICENSE).
