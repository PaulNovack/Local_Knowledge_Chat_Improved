"""Idempotent, additive JSON/JSONL/SQLite import. Never drops collections."""
import argparse
import logging
from pathlib import Path
import time

import httpx
from .core import Settings, Store, read_records, documents, embedding_input, validate_vectors, topic_name, chunk_size_for_topic


def embed(client, settings, docs):
    for attempt in range(3):
        try:
            r = client.post('/api/embed', json={'model': settings.embed_model,
                'input': [embedding_input(d['question'][:1500] + '\n' + d['text'], settings.embed_model) for d in docs],
                'truncate': False})
            r.raise_for_status()
            return validate_vectors(r.json(), len(docs))
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def import_file(path, topic, store, client, settings, batch_size=16):
    inserted = skipped = 0
    batch = []
    def flush():
        nonlocal inserted, skipped, batch
        # Deduplicate within the batch as well as against Qdrant.
        unique = list({doc['id']: doc for doc in batch}.values())
        exists = store.existing(topic, [d['id'] for d in unique])
        new = [d for d in unique if d['id'] not in exists]
        skipped += len(batch) - len(new)
        if new:
            vectors = embed(client, settings, new)
            store.upsert(topic, new, vectors)
            inserted += len(new)
        batch = []
        logging.info('%s: %d new chunks, %d skipped', topic, inserted, skipped)
    for record in read_records(path):
        for doc in documents(record, chunk_size=chunk_size_for_topic(topic)):
            batch.append(doc)
            if len(batch) >= batch_size:
                flush()
    if batch:
        flush()
    return inserted, skipped


def import_directory(path, store, client, settings, batch_size=16):
    inserted = skipped = 0
    if not path.exists():
        return inserted, skipped
    paths = sorted(x for x in path.rglob('*') if x.is_file() and x.suffix.lower() in
                   ('.json', '.jsonl', '.pdf', '.sqlite', '.sqlite3', '.db'))
    for file_path in paths:
        inserted_delta, skipped_delta = import_file(file_path, file_path.stem, store, client, settings, batch_size)
        inserted += inserted_delta
        skipped += skipped_delta
    return inserted, skipped


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('path', type=Path, help='File or directory of supported data files')
    p.add_argument('--topic', help='Required for SQLite; optional for a single JSON/JSONL file')
    p.add_argument('--batch-size', type=int, default=16)
    args = p.parse_args()
    if not 1 <= args.batch_size <= 64:
        p.error('batch-size must be 1..64')
    if not args.path.exists():
        p.error('Input does not exist')
    paths = sorted(x for x in args.path.rglob('*') if x.is_file() and x.suffix.lower() in
                   ('.json', '.jsonl', '.pdf', '.sqlite', '.sqlite3', '.db')) if args.path.is_dir() else [args.path]
    if args.topic and len(paths) != 1:
        p.error('--topic requires exactly one input file')
    if not paths:
        p.error('No supported files found')
    for path in paths:
        if path.suffix.lower() in ('.sqlite', '.sqlite3', '.db') and not args.topic:
            p.error('Specify --topic for SQLite input')
        topic_name(args.topic or path.stem)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    settings = Settings()
    store = Store(settings)
    try:
        with httpx.Client(base_url=settings.ollama_url, timeout=settings.timeout, trust_env=False) as client:
            for path in paths:
                import_file(path, args.topic or path.stem, store, client, settings, args.batch_size)
    finally:
        store.close()

if __name__ == '__main__':
    main()
