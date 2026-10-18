# Review and migration notes

## Original issues addressed

- `startAll.sh` stopped all Docker containers, removed them and pruned unused
  Docker resources. The replacement starts the web app only; Compose manages
  just this project's services with persistent named volumes.
- `ImportJsonFiles.py` deleted each collection at startup and used a fixed 768
  vector size. The new importer is additive, detects dimensions and skips existing
  content before embedding. Obsolete/conflicting imports have been removed.
- The old importer embedded question and answer separately into duplicate points.
  The new representation embeds question plus each answer chunk once.
- Blocking Ollama generation and Qdrant calls ran in the async WebSocket loop.
  Generation now uses async HTTP streaming and vector calls run in worker threads.
- The wire protocol split user input on `|` and used `~END~`/`~URLS~` markers.
  Requests and events are validated JSON messages.
- All retrieved content was concatenated without a clear relevance threshold or
  context budget. New retrieval applies both and exposes numbered excerpts.
- Backend errors were printed without reliably notifying the browser. Error
  events and health endpoints now provide visible diagnostics.
- The frontend used a hard-coded WebSocket host, a free-text collection name,
  one accumulated text blob and globally replaced source links. It now uses
  same-origin connections, an actual collection list and separate message cards.

## Deliberate scope choices

Ollama and Qdrant remain the core services. React/Create React App were replaced
with a no-build browser frontend, reducing installation steps and dependency
surface. Import is a local CLI rather than an unauthenticated browser upload API.
No fine-tuning, crawling, automated QA generation or persistent chat database is
included. SQLite support reads your existing QA builder output; it does not alter
that database. Existing old-format Qdrant collections are preserved but must be
reimported into the new namespace to appear in the new app.

## Retrieval limitations

This uses dense retrieval, not BM25 or a cross-encoder reranker. It does not verify
source truth or enforce claim-level citation correctness. Prompt grounding reduces
unsupported answers but cannot guarantee their absence. Additive imports preserve
changed answers as new documents, so use a new topic for clean dataset replacement.
