# Local Knowledge Chat — improved Ollama + Qdrant edition

Demo video: https://youtu.be/_xjihvMqhKc

A rebuilt version of Paul Novack's Melanie-Martinez-Rag-Chat-Ollama project.
It keeps local retrieval and local model generation, supports arbitrary topics,
and includes the original four topic data files unchanged. No Node.js is required:
FastAPI serves the browser UI and a structured streaming WebSocket API.

## Quick start on Ubuntu

Requirements: Python 3.10+, Docker with the Compose plugin, and a current Ollama
installation. Commands below run from this extracted project's directory.
Internet access is needed for initial dependency/image/model downloads. After
installation and model downloads, chat and import run locally.

```bash
sudo apt update
sudo apt install python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 1. Start Qdrant, ASR, and Piper

```bash
docker compose up -d qdrant asr-service piper-service
```

It uses its own Compose project and persistent volume. It does not stop, remove,
or prune unrelated containers. Port 6333 must be free; an existing Qdrant service
can be used instead by setting `QDRANT_URL` in `.env`. New collection names begin
with `rag_v2_`; old collections are untouched. Qdrant is bound to localhost.

### 2. Prepare Ollama (choose ONE option)

**A. Existing native Ollama installation:**

```bash
ollama pull qwen3.5:4b
ollama pull nomic-embed-text
```

If Ollama is not running, start `ollama serve` in another terminal. Do not start a
second server if the Ollama system service is already running.

**B. Docker Ollama, if you do not already have Ollama on port 11434:**

```bash
docker compose --profile ollama up -d ollama
docker compose exec ollama ollama pull qwen3.5:4b
docker compose exec ollama ollama pull nomic-embed-text
```

The provided Docker configuration works on CPU; it does not configure GPU access.
Native Ollama can use compatible GPUs according to your installation. CPU chat may
be slow. Model downloads and RAM requirements depend on the selected model.

### 3. Import original data

```bash
set -a
source .env
set +a
.venv/bin/python -m backend.ingest topic_data
```

If you prefer using `python` directly, activate the virtual environment first
with `source .venv/bin/activate`. The ingest command needs the environment that
installed `qdrant-client` and the other project dependencies.

Wait until Qdrant and Ollama have started before importing. The importer prints
chunk counts. An interrupted import can be rerun; identical documents are skipped
before embedding. Failed batches cause a nonzero exit and can be retried.

### 4. Start the app

```bash
bash startAll.sh
```

Open **http://127.0.0.1:8000**. Select a collection and ask a question.
`startAll.sh` creates a virtual environment if necessary, installs Python
requirements, loads `.env`, starts the bundled ASR service on port 9000, and
then starts the web app. It does not start Docker, pull models, or reimport
data on every launch.

If you already started the ASR service with Docker Compose, set
`START_LOCAL_ASR=0` in `.env` before running `startAll.sh` so the local launcher
does not try to start a second ASR process on the same port.

Start `piper-service` with Docker Compose before using ChatByVoice so the app can
fetch and play the generated Amy voice WAV files.

The voice page uses the local ASR service. If `faster-whisper` reports missing
audio decoding support, install `ffmpeg` on your system before starting the app.

On startup the backend creates a local SQLite chat history database at
`database/chat_history.sqlite3`. Each completed answer is stored with the topic,
question, answer text, `accepted = true`, and automatic date/timestamp fields.

## Reset and start from scratch

Use this when you want a completely clean local setup and do not need the
current Qdrant data, local Python environment, logs, or chat history database.

```bash
cd /home/pnovack/code-2027/Local_Knowledge_Chat_Improved
docker compose down -v
rm -rf .venv logs database
```

Then recreate the environment and reload the data:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
docker compose up -d qdrant asr-service piper-service
docker compose --profile ollama up -d ollama
source .env
.venv/bin/python -m backend.ingest topic_data
START_LOCAL_ASR=0 bash startAll.sh
```

If you use a native Ollama installation instead of the Docker service, keep
your existing Ollama process running and just pull the required models again if
they are missing.

The ChatByVoice page now speaks the answer back after transcription and chat.
The backend returns a generated WAV file from the Piper service and the browser
plays it automatically after each reply.

To start manually after loading `.env`:

```bash
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --ws-max-size 32768
```

## Import your own data

### JSON array (original repository format)

```json
[
  {
    "question": "What is PHP?",
    "answer": "PHP is a general-purpose scripting language especially suited to web development.",
    "url": "https://www.php.net/manual/en/intro-whatis.php"
  }
]
```

```bash
python -m backend.ingest /path/to/php.json --topic PHP
```

### JSONL

One complete JSON object per line. Escape newlines inside strings as `\n`.
Do not include Markdown fences. `url` or `source_url` is optional.

```bash
python -m backend.ingest /path/to/php_qa.jsonl --topic PHP
```

### Your PHP manual builder's SQLite database

```bash
python -m backend.ingest /path/to/php_manual_qa.sqlite3 --topic PHP
```

This reads `qa_pairs` and obtains original URLs from `chunks.page_url`. A generic
SQLite database containing only `qa_pairs(question, answer)` is supported too.
The database is opened read-only. Collection names accept letters, numbers,
underscores and hyphens, up to 80 characters.

### PDF files

PDF input is supported for text-based documents. The importer extracts text,
stores the file stem as the question, and chunks the combined text with the
same overlapping chunk settings used for the other sources.

```bash
python -m backend.ingest /path/to/manual.pdf --topic Manual
```

Use a topic when the PDF filename is not already a valid collection name.

The importer stores one embedding per answer chunk, including its question in the
embedding input. Long answers have overlapping chunks. The embedding dimension
is obtained from Ollama rather than hard-coded to 768. `nomic-embed-text` uses
its retrieval prefixes; other models receive plain text.

**Imports are additive.** Exact duplicates are skipped; changing an answer adds a
new document and preserves the previous one. To replace a dataset cleanly, import
it under a new topic and select that topic. No automatic deletion is performed.
Changing the embedding model creates a separate collection namespace; reimport
for the new model. Changing only the chat model does not require reimporting.
If you replace a model's weights while keeping the same embedding model name,
use a new topic too; model names do not fingerprint the actual weights.

## What improved

- Non-destructive, resumable imports with deterministic IDs and batch embeddings.
- JSON, JSONL and SQLite input, plus validation with useful JSONL line errors.
- Current Qdrant `query_points` interface and Ollama `/api/embed` endpoint.
- Async Ollama streaming; synchronous Qdrant work is moved off the event loop.
- At most two active retrieval/generation tasks per backend process.
- Bounded retrieved context, duplicate excerpt removal, configurable similarity
  threshold, and an explicit refusal when no source meets the threshold.
- Answers are prompted to cite numbered excerpts. Full excerpts and similarity
  scores are available below each response; displayed sources are retrieval
  results, not proof that every generated sentence is supported.
- Bounded conversation history to help follow-up questions. History is per tab,
  in memory, and cleared when switching topics or starting a new conversation.
- Structured JSON WebSocket events instead of delimiter strings and HTML tokens.
- Stop closes the socket and cancels the server request/stream. Ollama may take
  a short time to finish releasing model resources after cancellation.
- Browser text is rendered with `textContent`, never model-supplied HTML. Code
  formatting/newlines are preserved as plain text. HTTP(S) source links only.
- Responsive interface, topic picker, status messages and JSON chat export.
- Same-origin connection by default; no hard-coded browser backend hostname.
- Small Python dependency list instead of the original broad environment freeze.

## Configuration

Copy `.env.example` to `.env`; values are shell environment assignments. The
launcher loads it, but manual Python commands require the `set -a / source .env /
set +a` sequence above. Quote values containing spaces.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CHAT_MODEL` | `qwen3.5:4b` | Ollama answer model; may be changed to another installed chat model |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model; reimport after changing |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama API |
| `QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant API |
| `TOP_K` | `6` | Maximum retrieved excerpts passed to the model |
| `MIN_SCORE` | `0.35` | Minimum cosine similarity; tune using representative questions |
| `CONTEXT_CHARS` | `12000` | Character budget for retrieved excerpts |
| `OLLAMA_TIMEOUT` | `180` | HTTP operation timeout in seconds; increase for slow CPUs |
| `ASR_URL` | `http://127.0.0.1:9000/transcribe` | Local speech-to-text endpoint used by ChatByVoice |
| `ASR_TIMEOUT` | `60` | HTTP timeout in seconds for the ASR service |
| `ALLOWED_ORIGINS` | localhost/127.0.0.1 port 8000 | Comma-separated allowed browser origins |

Similarity scores are not confidence probabilities. Increase `MIN_SCORE` if
irrelevant matches appear; reduce it if relevant questions return no matches.
The application uses approximate character budgets, not model-specific token
counting. Default context assumes the configured 16K model context. Large
non-English text or code may require reducing `CONTEXT_CHARS`.

The app intentionally defaults to localhost. It has no user authentication or
rate limits beyond the generation semaphore; use a proper authenticated reverse
proxy and service security before making it accessible outside your machine.
No analytics, remote scripts, CDN fonts, or third-party chat APIs are used.
The original example datasets were not fact-checked; their inclusion does not
validate their factual or medical claims. Better source data improves answers.

## Diagnostics

- Open `http://127.0.0.1:8000/api/health` for dependency/model checks.
- No topics: run the importer, then click the refresh button beside the picker.
- Missing model: pull the exact name shown in `.env` into the Ollama server used
  by `OLLAMA_URL` (native and Docker installations have separate model storage).
- Import fails: inspect the traceback for invalid input, missing model, port
  conflict or connection error, fix it, and rerun the same command.
- Old data does not appear: import it again; the new app does not read old-format
  Qdrant collections because the payload/embedding layout has changed.
- Empty chat output: inspect backend and Ollama logs; ensure your Ollama version
  and model support `think: false`. Models requiring thinking may need that
  setting removed in `backend/app.py` and a larger output allowance.
- A model request fails while streaming: the browser keeps any partial text and
  shows a failure status. Failed/partial responses are not added to model history.
- Port 8000 in use: stop the existing app, or change both the uvicorn port and
  `ALLOWED_ORIGINS` to match the browser URL.

To stop only this project's Qdrant container:

```bash
docker compose stop qdrant
```

For Docker Ollama: `docker compose --profile ollama stop ollama`.
Keep the named volumes to retain vectors and downloaded models. Do not use
`down -v` unless you deliberately intend to delete this project's stored data.

## Tests and verification

```bash
pip install pytest
python -m pytest -q
```

Tests use in-memory Qdrant and mocked Ollama HTTP calls. They cover repeat imports,
collection preservation, schema/dimension checks, SQLite URLs, JSONL validation,
streaming sources/completion, origin restrictions, invalid messages and no-match
refusal. They do not require model downloads. See `VALIDATION.md` for what was
verified in the build environment and the remaining local checks.

## Provenance

Original project: https://github.com/PaulNovack/Melanie-Martinez-Rag-Chat-Ollama
Reviewed commit: `a47642565eb38b8cb435337c2b4516326b4293a2`.
The four files in `topic_data/` are copied unchanged from that revision.
The backend, browser app and startup workflow have been rewritten in this edition.
The original repository did not include a license file at the reviewed revision;
this package does not assign a new license to its original content.
