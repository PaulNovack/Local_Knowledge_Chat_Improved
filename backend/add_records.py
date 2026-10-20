"""Add JSONL/JSON records to topic_data/user_additions/<topic>.jsonl and index them in Qdrant.

Example: python -m backend.add_records new_questions.jsonl --topic=CDLA
"""
import argparse
import json
import logging
from pathlib import Path

import httpx

from .app import _normalize_answer, _normalize_question
from .core import Settings, Store, chunk_size_for_topic, documents, read_records, topic_name, training_records_path
from .ingest import embed


def load_existing(path):
    records = []
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def merge_records(existing, incoming):
    """Append new questions, replace changed answers, skip exact duplicates. Returns (records, added, updated)."""
    by_question = {_normalize_question(str(r.get('question', ''))): r for r in existing}
    added = updated = 0
    for record in incoming:
        key = _normalize_question(record['question'])
        saved = by_question.get(key)
        if saved is None:
            existing.append(record)
            by_question[key] = record
            added += 1
        elif _normalize_answer(str(saved.get('answer', ''))) != _normalize_answer(record['answer']):
            saved.update(record)
            updated += 1
    return existing, added, updated


def index_records(records, topic, store, client, settings, batch_size=16):
    docs = {}
    for record in records:
        for doc in documents(record, chunk_size=chunk_size_for_topic(topic)):
            docs[doc['id']] = doc
    docs = list(docs.values())
    inserted = 0
    for start in range(0, len(docs), batch_size):
        batch = docs[start:start + batch_size]
        exists = store.existing(topic, [d['id'] for d in batch])
        new = [d for d in batch if d['id'] not in exists]
        if new:
            store.upsert(topic, new, embed(client, settings, new))
            inserted += len(new)
    return inserted


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('path', type=Path, help='JSONL or JSON file of question/answer records')
    p.add_argument('--topic', required=True, help='Topic (Qdrant collection and user_additions file name)')
    p.add_argument('--batch-size', type=int, default=16)
    args = p.parse_args()
    if not args.path.is_file() or args.path.suffix.lower() not in ('.jsonl', '.json'):
        p.error('Input must be an existing .jsonl or .json file')
    if not 1 <= args.batch_size <= 64:
        p.error('batch-size must be 1..64')
    topic = topic_name(args.topic)

    incoming = [{**record, 'source': 'user_additions'} for record in read_records(args.path)]
    if not incoming:
        p.error('No records found')

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    additions_path = training_records_path(topic)
    merged, added, updated = merge_records(load_existing(additions_path), incoming)
    additions_path.parent.mkdir(parents=True, exist_ok=True)
    if added or updated:
        additions_path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in merged) + '\n', encoding='utf-8')
    logging.info('%s: %d added, %d updated, %d unchanged', additions_path, added, updated, len(incoming) - added - updated)

    settings = Settings()
    store = Store(settings)
    try:
        with httpx.Client(base_url=settings.ollama_url, timeout=settings.timeout, trust_env=False) as client:
            inserted = index_records(incoming, topic, store, client, settings, args.batch_size)
    finally:
        store.close()
    logging.info('%s: %d new chunks indexed in Qdrant', topic, inserted)


if __name__ == '__main__':
    main()
