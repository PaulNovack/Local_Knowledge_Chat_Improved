# Validation

Validated on Python 3.12 in the build environment.

## Passed

- Seven automated tests using real in-memory Qdrant and mocked Ollama HTTP.
- Repeat import skips embedding and preserves other topics.
- JSONL parsing, long-answer chunking, SQLite URL joins and dimension checks.
- WebSocket source events, streamed content, completion and no-evidence refusal.
- Invalid requests, rejected browser origins, empty model output errors.
- Disconnect closes the active Ollama HTTP stream.
- All 825 original dataset records pass input validation.
- Python compilation, JavaScript syntax and Bash syntax checks.
- Launching the actual ASGI server and fetching HTML, CSS and JavaScript.

## Not verified here

- Live model downloads, embedding quality, generated answer quality and GPU usage.
- Docker Compose startup (Docker is not installed in the build environment).
- Visual browser QA: the browser binary download failed in this environment.
- Dependency compatibility across every allowed version in requirements.txt.

## Installed versions used for tests

- fastapi: 0.143.2
- uvicorn: 0.54.0
- httpx: 0.28.1
- qdrant-client: 1.19.1
- pytest: 9.1.1

One third-party FastAPI/Starlette test-client deprecation warning appeared; all tests passed.
