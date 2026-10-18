"""Shared configuration, validated input, vector storage and Ollama access."""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid

import httpx
from qdrant_client import QdrantClient, models

ROOT = Path(__file__).resolve().parents[1]
DATABASE_DIR = ROOT / 'database'
CHAT_HISTORY_DB = DATABASE_DIR / 'chat_history.sqlite3'
DOC_CHUNK_SIZE = int(os.getenv('DOC_CHUNK_SIZE', '2400'))
DOC_CHUNK_OVERLAP = int(os.getenv('DOC_CHUNK_OVERLAP', '180'))
CDLA_DOC_CHUNK_SIZE = int(os.getenv('CDLA_DOC_CHUNK_SIZE', '3600'))

@dataclass
class Settings:
    ollama_url: str = os.getenv('OLLAMA_URL', 'http://127.0.0.1:11434')
    qdrant_url: str = os.getenv('QDRANT_URL', 'http://127.0.0.1:6333')
    asr_url: str = os.getenv('ASR_URL', 'http://127.0.0.1:9000/transcribe')
    chat_model: str = os.getenv('CHAT_MODEL', 'qwen3.5:4b')
    embed_model: str = os.getenv('EMBED_MODEL', 'nomic-embed-text')
    top_k: int = int(os.getenv('TOP_K', '6'))
    min_score: float = float(os.getenv('MIN_SCORE', '0.35'))
    context_chars: int = int(os.getenv('CONTEXT_CHARS', '12000'))
    timeout: float = float(os.getenv('OLLAMA_TIMEOUT', '180'))
    asr_timeout: float = float(os.getenv('ASR_TIMEOUT', '60'))
    origins: str = os.getenv('ALLOWED_ORIGINS', 'http://127.0.0.1:8000,http://localhost:8000')

    def __post_init__(self):
        if not 1 <= self.top_k <= 30 or not -1 <= self.min_score <= 1:
            raise ValueError('TOP_K must be 1..30 and MIN_SCORE -1..1')
        if self.context_chars < 2000 or self.timeout <= 0:
            raise ValueError('CONTEXT_CHARS >= 2000 and OLLAMA_TIMEOUT > 0 required')
        if self.asr_timeout <= 0:
            raise ValueError('ASR_TIMEOUT > 0 required')


def topic_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise ValueError('Topic must contain 1–80 letters, numbers, underscores or hyphens')
    return value


def collection_prefix(settings):
    return 'rag_v2_' + hashlib.sha256(settings.embed_model.encode()).hexdigest()[:10] + '_'


def clean_record(row, source):
    if not isinstance(row, dict):
        raise ValueError('Each record must be an object')
    q, a = row.get('question'), row.get('answer')
    if not isinstance(q, str) or not q.strip() or not isinstance(a, str) or not a.strip():
        raise ValueError('Each record requires nonempty question and answer strings')
    url = row.get('url') or row.get('source_url') or ''
    if not isinstance(url, str):
        raise ValueError('url must be a string')
    if url and not url.startswith(('https://', 'http://')):
        url = ''
    return {'question': q.strip(), 'answer': a.strip(), 'url': url, 'source': source}


def read_records(path):
    path = Path(path).resolve()
    if path.suffix.lower() in ('.sqlite', '.sqlite3', '.db'):
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            # Supports the PHP manual builder's schema; generic qa_pairs also works.
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'qa_pairs' not in tables:
                raise ValueError('SQLite file must contain qa_pairs(question, answer)')
            if {'chunks', 'pages'} <= tables:
                rows = db.execute('SELECT q.question,q.answer,c.page_url AS url FROM qa_pairs q JOIN chunks c ON q.chunk_id=c.id')
            else:
                rows = db.execute('SELECT question,answer FROM qa_pairs')
            names = [c[0] for c in rows.description]
            for row in rows:
                yield clean_record(dict(zip(names, row)), path.name)
    elif path.suffix.lower() == '.pdf':
        from pypdf import PdfReader

        reader = PdfReader(path)
        if reader.is_encrypted:
            try:
                reader.decrypt('')
            except Exception as exc:
                raise ValueError(f'{path.name}: encrypted PDF files are not supported') from exc
        pages = []
        for page_number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            if text.strip():
                record = clean_record({
                    'question': path.stem,
                    'answer': text.strip(),
                    'url': '',
                    'source': path.name,
                }, path.name)
                record['page'] = page_number
                section = _pdf_section_title(text)
                if section:
                    record['section'] = section
                pages.append(record)
        if not pages:
            raise ValueError(f'{path.name}: no extractable text found')
        for record in pages:
            yield record
    elif path.suffix.lower() == '.jsonl':
        with path.open(encoding='utf-8-sig') as f:
            for i, line in enumerate(f, 1):
                if line.strip():
                    try:
                        yield clean_record(json.loads(line), path.name)
                    except (ValueError, TypeError) as exc:
                        raise ValueError(f'{path.name}:{i}: {exc}') from exc
    elif path.suffix.lower() == '.json':
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, list):
            raise ValueError('JSON input must be an array')
        for row in data:
            yield clean_record(row, path.name)
    else:
        raise ValueError('Supported files: .json, .jsonl, .sqlite, .sqlite3, .db')


def documents(record, chunk_size=DOC_CHUNK_SIZE, overlap=DOC_CHUNK_OVERLAP):
    answer = record['answer']
    for start in range(0, len(answer), chunk_size - overlap):
        text = answer[start:start + chunk_size]
        doc = {**record, 'answer': text, 'text': text}
        identity = json.dumps([record['question'], text, record['url']], ensure_ascii=False)
        doc['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
        yield doc
        if start + chunk_size >= len(answer):
            break


def chunk_size_for_topic(topic):
    if topic_name(topic).upper() == 'CDLA':
        return CDLA_DOC_CHUNK_SIZE
    return DOC_CHUNK_SIZE


def training_records_path(topic):
    return ROOT / 'topic_data' / 'user_additions' / f'{topic_name(topic)}.jsonl'


def _pdf_section_title(text):
    for line in (part.strip() for part in text.splitlines()):
        if re.match(r'^(Section|Chapter|Part)\b', line):
            return line[:120]
    return ''


def embedding_input(text, model, query=False):
    # Nomic's retrieval prefixes are model-specific, not applied to other models.
    if model.split(':')[0] == 'nomic-embed-text':
        return ('search_query: ' if query else 'search_document: ') + text
    return text


def validate_vectors(data, expected):
    vectors = data.get('embeddings')
    if not isinstance(vectors, list) or len(vectors) != expected or not vectors:
        raise ValueError('Ollama returned the wrong embedding count')
    dimension = len(vectors[0])
    if not dimension or any(len(v) != dimension or any(
        not isinstance(n, (float, int)) or not math.isfinite(n) for n in v) for v in vectors):
        raise ValueError('Ollama returned invalid embedding vectors')
    return vectors


def ensure_chat_history_db(path=None):
    path = Path(path) if path is not None else ROOT / 'database' / 'chat_history.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        existing = {row[1] for row in db.execute("PRAGMA table_info(chat_history)")}
        if existing and 'ID' not in existing:
            db.execute('ALTER TABLE chat_history RENAME TO chat_history_legacy')
            db.execute('''
                CREATE TABLE chat_history (
                    ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    accepted INTEGER NOT NULL DEFAULT 1 CHECK (accepted IN (0, 1)),
                    created_date TEXT NOT NULL DEFAULT (CURRENT_DATE),
                    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
                )
            ''')
            legacy_columns = {row[1] for row in db.execute("PRAGMA table_info(chat_history_legacy)")}
            if {'topic', 'question', 'answer'} <= legacy_columns:
                accepted_expr = 'accepted' if 'accepted' in legacy_columns else '1'
                date_expr = 'created_date' if 'created_date' in legacy_columns else 'CURRENT_DATE'
                time_expr = 'created_at' if 'created_at' in legacy_columns else 'CURRENT_TIMESTAMP'
                db.execute(f'''
                    INSERT INTO chat_history(topic, question, answer, accepted, created_date, created_at)
                    SELECT topic, question, answer, {accepted_expr}, {date_expr}, {time_expr}
                    FROM chat_history_legacy
                ''')
            db.execute('DROP TABLE chat_history_legacy')
        db.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 1 CHECK (accepted IN (0, 1)),
                created_date TEXT NOT NULL DEFAULT (CURRENT_DATE),
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        db.execute('''
            CREATE INDEX IF NOT EXISTS idx_chat_history_topic_created_at
            ON chat_history(topic, created_at DESC)
        ''')
    return path


def save_chat_history(topic, question, answer, accepted=True, path=None):
    path = ensure_chat_history_db(path)
    with sqlite3.connect(path) as db:
        db.execute(
            'INSERT INTO chat_history(topic, question, answer, accepted) VALUES (?, ?, ?, ?)',
            (topic_name(topic), str(question).strip(), str(answer).strip(), 1 if accepted else 0),
        )


class Store:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client if client is not None else QdrantClient(url=settings.qdrant_url, timeout=30, trust_env=False)
        self.prefix = collection_prefix(settings)

    def name(self, topic):
        return self.prefix + topic_name(topic)

    def topics(self):
        return sorted(c.name[len(self.prefix):] for c in self.client.get_collections().collections
                      if c.name.startswith(self.prefix))

    def ensure(self, topic, dimension):
        name = self.name(topic)
        if not self.client.collection_exists(name):
            self.client.create_collection(name, vectors_config=models.VectorParams(
                size=dimension, distance=models.Distance.COSINE))
        config = self.client.get_collection(name).config.params.vectors
        if not isinstance(config, models.VectorParams) or config.size != dimension:
            raise ValueError('Embedding dimension mismatch. Use a new topic/model collection.')

    def existing(self, topic, ids):
        if not self.client.collection_exists(self.name(topic)):
            return set()
        return {str(p.id) for p in self.client.retrieve(self.name(topic), ids, with_payload=False)}

    def upsert(self, topic, docs, vectors):
        self.ensure(topic, len(vectors[0]))
        self.client.upsert(self.name(topic), points=[models.PointStruct(
            id=doc['id'], vector=vector, payload={**doc, 'embedding_model': self.settings.embed_model})
            for doc, vector in zip(docs, vectors)], wait=True)

    def search(self, topic, vector):
        name = self.name(topic)
        if hasattr(self.client, 'query_points'):
            result = self.client.query_points(name, query=vector,
                limit=self.settings.top_k * 3, score_threshold=self.settings.min_score,
                with_payload=True)
            hits = result.points if hasattr(result, 'points') else result
        elif hasattr(self.client, 'search'):
            hits = self.client.search(name, query_vector=vector,
                limit=self.settings.top_k * 3, score_threshold=self.settings.min_score,
                with_payload=True)
        else:
            hits = self.client.query(name, query_vector=vector,
                limit=self.settings.top_k * 3, score_threshold=self.settings.min_score,
                with_payload=True)
        chosen, seen, used = [], set(), 0
        for hit in hits:
            payload = hit.payload or {}
            text = payload.get('text', '')
            if not isinstance(text, str) or not text or text in seen:
                continue
            question = str(payload.get('question', ''))
            cost = len(text) + len(question) + 100
            if used + cost > self.settings.context_chars:
                continue
            seen.add(text)
            used += cost
            chosen.append({'id': len(chosen) + 1, 'question': question, 'text': text,
                           'url': payload.get('url', ''), 'source': payload.get('source', ''),
                           'score': round(hit.score, 4)})
            if len(chosen) >= self.settings.top_k:
                break
        return chosen

    def close(self):
        self.client.close()
