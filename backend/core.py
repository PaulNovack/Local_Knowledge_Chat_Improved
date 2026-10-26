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
    piper_url: str = os.getenv('PIPER_URL', 'http://127.0.0.1:8001/speak')
    piper_health_url: str = os.getenv('PIPER_HEALTH_URL', 'http://127.0.0.1:8001/health')
    chat_model: str = os.getenv('CHAT_MODEL', 'qwen3.5:4b')
    embed_model: str = os.getenv('EMBED_MODEL', 'nomic-embed-text')
    top_k: int = int(os.getenv('TOP_K', '6'))
    min_score: float = float(os.getenv('MIN_SCORE', '0.35'))
    context_chars: int = int(os.getenv('CONTEXT_CHARS', '12000'))
    timeout: float = float(os.getenv('OLLAMA_TIMEOUT', '180'))
    asr_timeout: float = float(os.getenv('ASR_TIMEOUT', '60'))
    piper_timeout: float = float(os.getenv('PIPER_TIMEOUT', '60'))
    origins: str = os.getenv('ALLOWED_ORIGINS', 'http://127.0.0.1:8000,http://localhost:8000')

    def __post_init__(self):
        if not 1 <= self.top_k <= 30 or not -1 <= self.min_score <= 1:
            raise ValueError('TOP_K must be 1..30 and MIN_SCORE -1..1')
        if self.context_chars < 2000 or self.timeout <= 0:
            raise ValueError('CONTEXT_CHARS >= 2000 and OLLAMA_TIMEOUT > 0 required')
        if self.asr_timeout <= 0:
            raise ValueError('ASR_TIMEOUT > 0 required')
        if self.piper_timeout <= 0:
            raise ValueError('PIPER_TIMEOUT > 0 required')


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
    router_function = row.get('routerFunction', '')
    if not isinstance(router_function, str):
        raise ValueError('routerFunction must be a string')
    key = row.get('key', '')
    if not isinstance(key, str) and (not isinstance(key, int) or isinstance(key, bool)):
        raise ValueError('key must be a string or integer')
    return {
        'question': q.strip(), 'answer': a.strip(), 'url': url,
        'source': source, 'routerFunction': router_function, 'key': key,
    }


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


def ensure_topic_data_table(path=None):
    path = Path(path) if path is not None else ROOT / 'database' / 'chat_history.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS topic_data (
                topic TEXT NOT NULL,
                id TEXT NOT NULL,
                vector TEXT NOT NULL DEFAULT '',
                question TEXT NOT NULL DEFAULT '',
                answer TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                embedding_model TEXT NOT NULL DEFAULT '',
                routerFunction TEXT NOT NULL DEFAULT '',
                key INTEGER NOT NULL DEFAULT '',
                PRIMARY KEY (topic, id)
            )
        ''')
        db.execute('''
            CREATE INDEX IF NOT EXISTS idx_topic_data_topic_key
            ON topic_data(topic, key)
        ''')
    return path


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
                    routerFunction TEXT,
                    "key" INTEGER,
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
                routerFunction TEXT,
                "key" INTEGER,
                accepted INTEGER NOT NULL DEFAULT 1 CHECK (accepted IN (0, 1)),
                created_date TEXT NOT NULL DEFAULT (CURRENT_DATE),
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        chat_history_columns = {row[1] for row in db.execute('PRAGMA table_info(chat_history)')}
        if 'routerFunction' not in chat_history_columns:
            db.execute('ALTER TABLE chat_history ADD COLUMN routerFunction TEXT')
        if 'key' not in chat_history_columns:
            db.execute('ALTER TABLE chat_history ADD COLUMN "key" INTEGER')
        db.execute('''
            CREATE INDEX IF NOT EXISTS idx_chat_history_topic_created_at
            ON chat_history(topic, created_at DESC)
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS routerFunctions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS user_location_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                latitude REAL,
                longitude REAL
            )
        ''')
        db.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_location_events_session_time
            ON user_location_events(session_id, recorded_at)
        ''')
        db.executemany(
            'INSERT OR IGNORE INTO routerFunctions(name) VALUES (?)',
                [('',), ('Pre-trip Text',), ('Pre-trip Video',), ('Closest Loves',), ('Closest Roadys',), ('Closest McDonalds',),
               ('Closest Restaurant',), ('Supported Restaurants',), ('Open Directions',),
               ('Close Directions',)],
        )
    ensure_topic_data_table(path)
    with sqlite3.connect(path) as db:
        db.execute('''
            INSERT OR IGNORE INTO topic_data(
                topic, id, vector, question, answer, text, url, source,
                embedding_model, routerFunction, key
            ) VALUES (
                '__system__', 'closest-loves', '',
                'What is closest loves in direction I am Going',
                'Find the nearest Love''s travel center using the current location and travel direction.',
                '', '', '', '', 'Closest Loves', ''
            )
        ''')
        db.execute('''
            INSERT OR IGNORE INTO topic_data(
                topic, id, vector, question, answer, text, url, source,
                embedding_model, routerFunction, key
            ) VALUES (
                '__system__', 'closest-mcdonalds', '',
                'What is the closest McDonalds in the direction I am going?',
                'Find the nearest McDonald''s using the current location and recent direction of travel. If direction cannot be determined, return the nearest overall.',
                '', '', '', '', 'Closest McDonalds', ''
            )
        ''')
    return path


def read_router_function_names(path=None):
    path = ensure_chat_history_db(path)
    with sqlite3.connect(path) as db:
        return [row[0] for row in db.execute('SELECT name FROM routerFunctions ORDER BY id')]


def ensure_tts_cache_db(path=None):
    path = Path(path) if path is not None else ROOT / 'database' / 'chat_history.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS tts_cache (
                hash TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
    return path


def sync_qdrant_topic_to_sqlite(store, topic, path=None):
    topic = topic_name(topic)
    path = ensure_topic_data_table(path)
    columns = ('topic', 'id', 'vector', 'question', 'answer', 'text', 'url',
               'source', 'embedding_model', 'routerFunction', 'key')
    placeholders = ', '.join('?' for _ in columns)
    column_list = ', '.join(f'"{name}"' for name in columns)
    count = 0
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM topic_data WHERE topic = ?', (topic,))
        offset = None
        while True:
            points, offset = store.client.scroll(
                store.name(topic), limit=256, offset=offset,
                with_payload=True, with_vectors=True,
            )
            rows = []
            for point in points:
                payload = point.payload or {}
                rows.append((
                    topic,
                    str(point.id),
                    json.dumps(point.vector, ensure_ascii=False, separators=(',', ':')),
                    str(payload.get('question') or ''),
                    str(payload.get('answer') or ''),
                    str(payload.get('text') or ''),
                    str(payload.get('url') or ''),
                    str(payload.get('source') or ''),
                    str(payload.get('embedding_model') or ''),
                    str(payload.get('routerFunction') or ''),
                    payload.get('key', ''),
                ))
            if rows:
                db.executemany(
                    f'INSERT INTO topic_data ({column_list}) VALUES ({placeholders})',
                    rows,
                )
                count += len(rows)
            if offset is None:
                break
    return count


def sync_qdrant_topics_to_sqlite(store, path=None):
    return {
        topic: sync_qdrant_topic_to_sqlite(store, topic, path)
        for topic in store.topics()
    }


def save_tts_cache_entry(text_hash, text, path=None):
    path = ensure_tts_cache_db(path)
    with sqlite3.connect(path) as db:
        db.execute(
            '''
            INSERT INTO tts_cache(hash, text)
            VALUES (?, ?)
            ON CONFLICT(hash) DO UPDATE SET text=excluded.text
            ''',
            (str(text_hash), str(text).strip()),
        )


def save_chat_history(topic, question, answer, accepted=True, path=None, router_function=None, key=None):
    path = ensure_chat_history_db(path)
    with sqlite3.connect(path) as db:
        db.execute(
            'INSERT INTO chat_history(topic, question, answer, accepted, routerFunction, "key") VALUES (?, ?, ?, ?, ?, ?)',
            (topic_name(topic), str(question).strip(), str(answer).strip(), 1 if accepted else 0,
             router_function, key),
        )


def delete_chat_history(response_id, path=None):
    path = ensure_chat_history_db(path)
    with sqlite3.connect(path) as db:
        cursor = db.execute('DELETE FROM chat_history WHERE ID = ?', (int(response_id),))
        return cursor.rowcount > 0


def get_chat_history_entry(response_id, path=None):
    path = ensure_chat_history_db(path)
    with sqlite3.connect(path) as db:
        row = db.execute(
            'SELECT ID, topic, question, answer FROM chat_history WHERE ID = ?',
            (int(response_id),),
        ).fetchone()
    if row is None:
        return None
    return dict(zip(('ID', 'topic', 'question', 'answer'), row))


def _chat_history_search_clause(keyword):
    term = str(keyword or '').strip().lower()
    if not term:
        return '', []
    escaped = term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    pattern = f'%{escaped}%'
    clause = "WHERE lower(topic || ' ' || question || ' ' || answer) LIKE ? ESCAPE '\\'"
    return clause, [pattern]


def read_chat_history(page=1, page_size=20, keyword='', path=None):
    page = int(page)
    page_size = min(int(page_size), 50)
    if page < 1:
        raise ValueError('page must be at least 1')
    if page_size < 1:
        raise ValueError('page_size must be at least 1')

    path = ensure_chat_history_db(path)
    offset = (page - 1) * page_size
    term = str(keyword or '').strip().lower()
    clause, params = _chat_history_search_clause(keyword)
    with sqlite3.connect(path) as db:
        total = db.execute(f'SELECT COUNT(*) FROM chat_history {clause}', params).fetchone()[0]
        rows = db.execute(
            '''
            SELECT ID, topic, question, answer, accepted, created_date, created_at, routerFunction, "key"
            FROM chat_history
            '''
            + clause + '''
            ORDER BY datetime(created_at) DESC, ID DESC
            LIMIT ? OFFSET ?
            ''',
            [*params, page_size, offset],
        ).fetchall()
    items = [dict(zip(('ID', 'topic', 'question', 'answer', 'accepted', 'created_date', 'created_at', 'routerFunction', 'key'), row)) for row in rows]
    total_pages = math.ceil(total / page_size) if total else 0
    return {
        'items': items,
        'page': page,
        'page_size': page_size,
        'keyword': term,
        'total': total,
        'total_pages': total_pages,
        'has_prev': page > 1 and total > 0,
        'has_next': page < total_pages,
    }


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

    def set_payload(self, topic, payload, point_ids):
        for start in range(0, len(point_ids), 256):
            self.client.set_payload(
                self.name(topic), payload=payload,
                points=point_ids[start:start + 256], wait=True,
            )

    def upsert(self, topic, docs, vectors):
        self.ensure(topic, len(vectors[0]))
        self.client.upsert(self.name(topic), points=[models.PointStruct(
            id=doc['id'], vector=vector, payload={**doc, 'embedding_model': self.settings.embed_model})
            for doc, vector in zip(docs, vectors)], wait=True)

    def delete_user_additions_for_question(self, topic, question, keep_ids=()):
        name = self.name(topic)
        if not self.client.collection_exists(name):
            return
        target = re.sub(r'\W+', ' ', str(question)).strip().lower()
        keep_ids = {str(point_id) for point_id in keep_ids}
        source_filter = models.Filter(must=[models.FieldCondition(
            key='source', match=models.MatchValue(value='user_additions'))])
        point_ids = []
        offset = None
        while True:
            points, offset = self.client.scroll(
                name, scroll_filter=source_filter, limit=256, offset=offset,
                with_payload=['question'], with_vectors=False)
            point_ids.extend(
                point.id for point in points
                if str(point.id) not in keep_ids
                and re.sub(r'\W+', ' ', str((point.payload or {}).get('question', ''))).strip().lower() == target
            )
            if offset is None:
                break
        if point_ids:
            self.client.delete(name, points_selector=point_ids, wait=True)

    def delete_user_additions_for_route_key(self, topic, router_function, key):
        name = self.name(topic)
        if not self.client.collection_exists(name):
            return
        target_route = str(router_function).strip().casefold()
        target_key = str(key).strip()
        source_filter = models.Filter(must=[models.FieldCondition(
            key='source', match=models.MatchValue(value='user_additions'))])
        point_ids = []
        offset = None
        while True:
            points, offset = self.client.scroll(
                name, scroll_filter=source_filter, limit=256, offset=offset,
                with_payload=['routerFunction', 'key'], with_vectors=False)
            point_ids.extend(
                point.id for point in points
                if str((point.payload or {}).get('routerFunction', '')).strip().casefold() == target_route
                and str((point.payload or {}).get('key', '')).strip() == target_key
            )
            if offset is None:
                break
        if point_ids:
            self.client.delete(name, points_selector=point_ids, wait=True)

    def delete_stale_user_additions(self, topic, keep_ids):
        name = self.name(topic)
        if not self.client.collection_exists(name):
            return
        keep_ids = {str(point_id) for point_id in keep_ids}
        source_filter = models.Filter(must=[models.FieldCondition(
            key='source', match=models.MatchValue(value='user_additions'))])
        stale_ids = []
        offset = None
        while True:
            points, offset = self.client.scroll(
                name, scroll_filter=source_filter, limit=256, offset=offset,
                with_payload=False, with_vectors=False)
            stale_ids.extend(point.id for point in points if str(point.id) not in keep_ids)
            if offset is None:
                break
        if stale_ids:
            self.client.delete(name, points_selector=stale_ids, wait=True)

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
                           'routerFunction': payload.get('routerFunction', ''),
                           'key': payload.get('key', ''),
                           'score': round(hit.score, 4)})
            if len(chosen) >= self.settings.top_k:
                break
        return chosen

    def close(self):
        self.client.close()
