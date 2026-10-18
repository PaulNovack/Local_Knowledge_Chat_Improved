import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from backend.core import Settings, Store, read_records, documents, topic_name
from backend.ingest import import_file
from backend.app import create_app, _is_date_question


def write_simple_pdf(path: Path, text: str):
    escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    content = f'BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n'.encode('latin-1')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
        b'<< /Length ' + str(len(content)).encode('ascii') + b' >>\nstream\n' + content + b'endstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    pdf = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f'{index} 0 obj\n'.encode('ascii'))
        pdf.extend(obj)
        pdf.extend(b'\nendobj\n')
    xref_offset = len(pdf)
    pdf.extend(f'xref\n0 {len(objects) + 1}\n'.encode('ascii'))
    pdf.extend(b'0000000000 65535 f \n')
    for offset in offsets[1:]:
        pdf.extend(f'{offset:010d} 00000 n \n'.encode('ascii'))
    pdf.extend(b'trailer\n')
    pdf.extend(f'<< /Size {len(objects) + 1} /Root 1 0 R >>\n'.encode('ascii'))
    pdf.extend(b'startxref\n')
    pdf.extend(f'{xref_offset}\n'.encode('ascii'))
    pdf.extend(b'%%EOF\n')
    path.write_bytes(pdf)

@pytest.fixture
def settings():
    return Settings(chat_model='test', embed_model='test-embed', min_score=0.1)

@pytest.fixture
def store(settings):
    s = Store(settings, QdrantClient(':memory:'))
    yield s
    s.close()


def test_input_validation_and_chunk_coverage(tmp_path):
    file = tmp_path / 'test.jsonl'
    pair = {'question': 'What is PHP?', 'answer': 'A language. ' * 500, 'url': 'javascript:alert(1)'}
    file.write_text(json.dumps(pair) + '\n', encoding='utf-8')
    record = list(read_records(file))[0]
    assert record['url'] == ''
    chunks = list(documents(record))
    assert len(chunks) > 1
    assert chunks[-1]['text'].endswith(record['answer'][-50:])
    assert list(documents(record)) == chunks
    with pytest.raises(ValueError):
        topic_name('../invalid')
    file.write_text('{bad json}\n')
    with pytest.raises(ValueError, match=':1:'):
        list(read_records(file))


def test_sqlite_php_schema(tmp_path):
    path = tmp_path / 'php.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE pages(url TEXT); CREATE TABLE chunks(id INTEGER,page_url TEXT); '
                         'CREATE TABLE qa_pairs(question TEXT,answer TEXT,chunk_id INTEGER); '
                         "INSERT INTO chunks VALUES(1,'https://php.net/manual/en/'); "
                         "INSERT INTO qa_pairs VALUES('What is PHP?','A language.',1);")
    assert list(read_records(path))[0]['url'] == 'https://php.net/manual/en/'


def test_pdf_import_uses_overlapping_chunks(tmp_path, settings, store):
    path = tmp_path / 'manual.pdf'
    text = ' '.join(f'chunk{i:03d}' for i in range(500))
    write_simple_pdf(path, text)
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]] * len(json.loads(request.content)['input'])})
        raise AssertionError(f'unexpected request to {request.url.path}')

    with httpx.Client(base_url='http://ollama', transport=httpx.MockTransport(handle)) as client:
        inserted, skipped = import_file(path, 'PDF', store, client, settings, batch_size=64)

    assert inserted > 1
    assert skipped == 0
    assert len(calls) == 1
    chunks = list(documents(list(read_records(path))[0]))
    assert len(chunks) > 1
    assert len(chunks[0]['text']) > 1600
    assert chunks[0]['text'][-180:] == chunks[1]['text'][:180]
    retrieved = store.client.retrieve(store.name('PDF'), [chunk['id'] for chunk in chunks], with_payload=True)
    payloads = {str(point.id): point.payload for point in retrieved}
    assert payloads[chunks[0]['id']]['text'] == chunks[0]['text']
    assert payloads[chunks[1]['id']]['text'] == chunks[1]['text']
    assert chunks[0]['page'] == 1


def test_documents_preserve_pdf_metadata():
    record = {
        'question': 'manual.pdf',
        'answer': 'Alpha beta gamma delta epsilon',
        'url': '',
        'source': 'manual.pdf',
        'page': 7,
        'section': 'Section 3 - Brakes',
    }
    doc = list(documents(record, chunk_size=12, overlap=4))[0]
    assert doc['page'] == 7
    assert doc['section'] == 'Section 3 - Brakes'


def test_cdla_uses_larger_chunks(tmp_path, settings, store):
    path = tmp_path / 'cdla.pdf'
    text = ' '.join(f'chunk{i:03d}' for i in range(700))
    write_simple_pdf(path, text)
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]] * len(json.loads(request.content)['input'])})
        raise AssertionError(f'unexpected request to {request.url.path}')

    with httpx.Client(base_url='http://ollama', transport=httpx.MockTransport(handle)) as client:
        inserted, skipped = import_file(path, 'CDLA', store, client, settings, batch_size=64)

    assert inserted > 0
    assert skipped == 0
    assert len(calls) == 1
    docs = list(documents(list(read_records(path))[0], chunk_size=3600))
    assert len(docs) == 1 or len(docs[0]['text']) > 2400


def test_repeat_import_skips_embedding_and_preserves_topics(tmp_path, settings, store):
    path = tmp_path / 'data.json'
    path.write_text(json.dumps([{'question': 'What is PHP?', 'answer': 'A programming language.'}]))
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'embeddings': [[1.0,0.0,0.0]]})
    with httpx.Client(base_url='http://ollama', transport=httpx.MockTransport(handle)) as client:
        assert import_file(path, 'PHP', store, client, settings) == (1, 0)
        assert import_file(path, 'PHP', store, client, settings) == (0, 1)
        assert len(calls) == 1
        assert import_file(path, 'Other', store, client, settings) == (1, 0)
    assert store.topics() == ['Other', 'PHP']
    assert store.search('PHP', [1.0,0.0,0.0])[0]['text'] == 'A programming language.'
    with pytest.raises(ValueError, match='dimension'):
        store.ensure('PHP', 2)


def seeded_store(store):
    record = {'question': 'What is PHP?', 'answer': 'A programming language.', 'url': 'https://www.php.net/', 'source': 'test.json'}
    docs = list(documents(record))
    store.upsert('PHP', docs, [[1.0,0.0,0.0]])
    return store


def test_stream_sources_and_completion(settings, store):
    seeded_store(store)
    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200,json={'embeddings':[[1.0,0.0,0.0]]})
        if request.url.path == '/api/tags':
            return httpx.Response(200,json={'models':[{'name':'test'},{'name':'test-embed:latest'}]})
        payload=json.loads(request.content)
        assert 'A programming language.' in payload['messages'][-1]['content']
        assert len(payload['messages']) == 2
        return httpx.Response(200, text='\n'.join(json.dumps(x) for x in [
            {'message':{'content':'PHP is a language [1].'}}, {'done':True}]))
    ollama=httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app=create_app(settings,store,ollama)
    with TestClient(app) as client:
        assert client.get('/').status_code==200
        health = client.get('/api/health')
        assert health.status_code == 200
        assert health.json()['ok']
        with client.websocket_connect('/ws/chat', headers={'origin':'http://localhost:8000'}) as ws:
            ws.send_json({'topic':'PHP','question':'What is PHP?'})
            events=[]
            while True:
                data=ws.receive_json();events.append(data)
                if data['type']=='done':break
        assert any(e['type']=='sources' and len(e['sources'])==1 for e in events)
        assert any(e.get('text')=='PHP is a language [1].' for e in events)


def test_chat_history_is_created_and_saved(tmp_path, monkeypatch, settings, store):
    seeded_store(store)

    (tmp_path / 'web').mkdir()
    monkeypatch.setattr('backend.core.ROOT', tmp_path)
    monkeypatch.setattr('backend.app.ROOT', tmp_path)

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        payload = json.loads(request.content)
        assert payload['messages'][-1]['content'].endswith('Question: What is PHP used for?')
        return httpx.Response(200, text='\n'.join(json.dumps(x) for x in [
            {'message': {'content': 'PHP is used for web development [1].'}}, {'done': True}]))

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        db_path = tmp_path / 'database' / 'chat_history.sqlite3'
        assert db_path.exists()
        with client.websocket_connect('/ws/chat', headers={'origin': 'http://localhost:8000'}) as ws:
            ws.send_json({'topic': 'PHP', 'question': 'What is PHP used for?'})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'done':
                    break

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            'SELECT ID, topic, question, answer, accepted, created_date, created_at FROM chat_history ORDER BY ID DESC LIMIT 1'
        ).fetchone()

    assert row[0] >= 1
    assert row[1] == 'PHP'
    assert row[2] == 'What is PHP used for?'
    assert row[3] == 'PHP is used for web development [1].'
    assert row[4] == 1
    assert row[5]
    assert row[6]


def test_date_questions_get_strict_date_rule(settings, store):
    assert _is_date_question('When did Melanie Martinez release her first album?')
    assert _is_date_question('What date was Cry Baby released?')
    assert not _is_date_question('What is PHP?')


def test_date_questions_add_secondary_retrieval(settings):
    seen = {'embed_inputs': [], 'search_calls': 0, 'prompt': ''}

    class FakeStore:
        def topics(self):
            return ['Melanie_Martinez']

        def search(self, topic, vector):
            seen['search_calls'] += 1
            if seen['search_calls'] == 1:
                return [{
                    'id': 1,
                    'question': "What is the name of Melanie Martinez's debut album?",
                    'text': "The name of Melanie Martinez's debut album is 'Cry Baby'.",
                }]
            return [{
                'id': 2,
                'question': "When was 'Cry Baby' released?",
                'text': "'Cry Baby' was released on August 14, 2015.",
            }]

        def close(self):
            pass

    def handle(request):
        if request.url.path == '/api/embed':
            payload = json.loads(request.content)
            seen['embed_inputs'].append(payload['input'])
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        payload = json.loads(request.content)
        seen['prompt'] = payload['messages'][-1]['content']
        assert 'August 14, 2015' in seen['prompt']
        return httpx.Response(200, text='\n'.join(json.dumps(x) for x in [
            {'message': {'content': 'Cry Baby was released on August 14, 2015 [1].'}}, {'done': True}]))

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, FakeStore(), ollama)
    with TestClient(app) as client:
        with client.websocket_connect('/ws/chat', headers={'origin': 'http://localhost:8000'}) as ws:
            ws.send_json({'topic': 'Melanie_Martinez', 'question': 'When did Melanie Martinez release her first album?'})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'done':
                    break

    assert seen['search_calls'] == 2
    assert len(seen['embed_inputs']) == 2
    assert seen['embed_inputs'][1] == 'When was Cry Baby released?'
    assert any('Cry Baby was released on August 14, 2015 [1].' in event.get('text', '') for event in events)


def test_train_endpoint_persists_and_upserts(tmp_path, monkeypatch, settings, store):
    seeded_store(store)

    monkeypatch.setattr('backend.core.ROOT', tmp_path)

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        raise AssertionError(f'unexpected request to {request.url.path}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        response = client.post('/api/train', headers={'origin': 'http://127.0.0.1:8000'}, json={
            'topic': 'PHP',
            'question': 'What is PHP?',
            'answer': 'PHP is a general-purpose scripting language for web development.',
        })
        assert response.status_code == 200
        payload = response.json()
        assert payload['ok'] is True

    saved = tmp_path / 'topic_data' / 'user_additions' / 'PHP.jsonl'
    assert saved.exists()
    assert 'PHP is a general-purpose scripting language for web development.' in saved.read_text(encoding='utf-8')
    results = store.search('PHP', [1.0, 0.0, 0.0])
    assert any(item['text'] == 'PHP is a general-purpose scripting language for web development.' for item in results)


def test_train_endpoint_skips_duplicate_corrections(tmp_path, monkeypatch, settings, store):
    seeded_store(store)

    monkeypatch.setattr('backend.core.ROOT', tmp_path)

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        raise AssertionError(f'unexpected request to {request.url.path}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, store, ollama)
    payload = {
        'topic': 'PHP',
        'question': 'What is PHP?',
        'answer': 'PHP is a general-purpose scripting language for web development.',
    }
    with TestClient(app) as client:
        first = client.post('/api/train', headers={'origin': 'http://127.0.0.1:8000'}, json=payload)
        second = client.post('/api/train', headers={'origin': 'http://127.0.0.1:8000'}, json={
            'topic': 'PHP',
            'question': 'What is PHP? ',
            'answer': 'PHP is a server-side scripting language for web development.',
        })

    assert first.json()['saved'] > 0
    assert second.json()['updated'] is True
    saved = tmp_path / 'topic_data' / 'user_additions' / 'PHP.jsonl'
    saved_lines = [line for line in saved.read_text(encoding='utf-8').splitlines() if line.strip()]
    assert len(saved_lines) == 1
    assert 'PHP is a server-side scripting language for web development.' in saved_lines[0]


def test_train_endpoint_rejects_non_local_origin(tmp_path, monkeypatch, settings, store):
    seeded_store(store)

    monkeypatch.setattr('backend.core.ROOT', tmp_path)

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        raise AssertionError(f'unexpected request to {request.url.path}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        response = client.post('/api/train', headers={'origin': 'http://example.com'}, json={
            'topic': 'PHP',
            'question': 'What is PHP?',
            'answer': 'PHP is a general-purpose scripting language for web development.',
        })

    assert response.status_code == 403
    assert response.json()['detail'] == 'This endpoint is only available from the local web app.'


def test_ask_and_train_page_rejects_non_local_host(tmp_path, monkeypatch, settings, store):
    seeded_store(store)

    monkeypatch.setattr('backend.core.ROOT', tmp_path)

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        raise AssertionError(f'unexpected request to {request.url.path}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        response = client.get('/ask-and-train/', headers={'host': 'example.com'})

    assert response.status_code == 403
    assert response.json()['detail'] == 'This page is only available from the local web app.'


def test_saved_corrections_reload_on_startup(tmp_path, monkeypatch, settings, store):
    additions = tmp_path / 'topic_data' / 'user_additions'
    additions.mkdir(parents=True)
    (tmp_path / 'web').mkdir()
    (additions / 'PHP.jsonl').write_text(
        json.dumps({'question': 'What is PHP?', 'answer': 'PHP is a general-purpose scripting language for web development.', 'url': '', 'source': 'user_additions'}) + '\n',
        encoding='utf-8',
    )
    monkeypatch.setattr('backend.app.ROOT', tmp_path)

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]}, request=request)
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        raise AssertionError(f'unexpected request to {request.url.path}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, store, ollama)
    with TestClient(app):
        pass

    assert 'PHP' in store.topics()
    results = store.search('PHP', [1.0, 0.0, 0.0])
    assert any(item['text'] == 'PHP is a general-purpose scripting language for web development.' for item in results)


def test_exact_saved_correction_short_circuits_llm(settings, store):
    record = {
        'question': 'During an emergency braking situation, skidding tires means?',
        'answer': 'you no longer have control of your vehicle',
        'url': '',
        'source': 'user_additions',
    }
    doc = list(documents(record))[0]
    store.upsert('CDLA', [doc], [[1.0, 0.0, 0.0]])

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        raise AssertionError(f'unexpected request to {request.url.path}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    with TestClient(create_app(settings, store, ollama)) as client:
        with client.websocket_connect('/ws/chat', headers={'origin': 'http://localhost:8000'}) as ws:
            ws.send_json({'topic': 'CDLA', 'question': 'During an emergency braking situation, skidding tires means?'})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'done':
                    break

    assert any(event.get('text') == 'you no longer have control of your vehicle' for event in events)
    assert not any(event['type'] == 'error' for event in events)


def test_top_saved_correction_short_circuits_llm_for_paraphrase(settings, store):
    def search(topic, vector):
        return [
            {
                'id': 1,
                'question': 'When your vehicle hydroplanes you should',
                'text': 'take your foot off the gas pedal and push the clutch to allow the wheels to spin',
                'url': '',
                'source': 'user_additions',
                'score': 0.96,
            },
            {
                'id': 2,
                'question': 'DL-7C',
                'text': 'Hydroplaning occurs when a layer of water gets between the tires and the road.',
                'url': '',
                'source': 'DL-7C.pdf',
                'score': 0.81,
            },
        ]

    class FakeStore:
        def topics(self):
            return ['CDLA']

        def search(self, topic, vector):
            return search(topic, vector)

        def close(self):
            pass

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]}, request=request)
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        raise AssertionError(f'unexpected request to {request.url.path}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    with TestClient(create_app(settings, FakeStore(), ollama)) as client:
        with client.websocket_connect('/ws/chat', headers={'origin': 'http://localhost:8000'}) as ws:
            ws.send_json({'topic': 'CDLA', 'question': 'what should I do if i start to hydroplane'})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'done':
                    break

    assert any(event.get('text') == 'take your foot off the gas pedal and push the clutch to allow the wheels to spin' for event in events)
    assert not any(event['type'] == 'error' for event in events)


def test_invalid_request_origin_and_no_evidence(settings,store):
    seeded_store(store)
    def handle(request):
        assert request.url.path=='/api/embed' # No chat call when retrieval has no matches.
        return httpx.Response(200,json={'embeddings':[[0.0,1.0,0.0]]})
    ollama=httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    with TestClient(create_app(settings,store,ollama)) as client:
        with pytest.raises(Exception):
            with client.websocket_connect('/ws/chat',headers={'origin':'https://untrusted.example'}):pass
        with client.websocket_connect('/ws/chat',headers={'origin':'http://localhost:8000'}) as ws:
            ws.send_text('not json')
            assert ws.receive_json()['type']=='error'
        with client.websocket_connect('/ws/chat',headers={'origin':'http://localhost:8000'}) as ws:
            ws.send_json({'topic':'PHP','question':'Who won the race?'})
            events=[]
            while True:
                event=ws.receive_json(); events.append(event)
                if event['type']=='done': break
            assert any('enough information' in e.get('text','') for e in events)


def test_ollama_failure_is_visible(settings,store):
    seeded_store(store)
    def handle(request):
        if request.url.path=='/api/embed':
            return httpx.Response(200,json={'embeddings':[[1.0,0.0,0.0]]})
        return httpx.Response(200,text=json.dumps({'done':True,'message':{'content':''}}))
    ollama=httpx.AsyncClient(base_url='http://ollama',transport=httpx.MockTransport(handle))
    with TestClient(create_app(settings,store,ollama)) as client:
        with client.websocket_connect('/ws/chat',headers={'origin':'http://localhost:8000'}) as ws:
            ws.send_json({'topic':'PHP','question':'What is PHP used for?'})
            events=[]
            while True:
                event=ws.receive_json();events.append(event)
                if event['type']=='error':break
            assert 'empty answer' in events[-1]['message']
            assert not any(e['type']=='done' for e in events)


def test_disconnect_cancels_ollama_stream(settings,store):
    import asyncio
    import threading
    seeded_store(store)
    closed = threading.Event()
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"message":{"content":"First token"}}\n'
            await asyncio.Event().wait()
        async def aclose(self):
            closed.set()
    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200,json={'embeddings':[[1.0,0.0,0.0]]})
        return httpx.Response(200,stream=SlowStream())
    ollama=httpx.AsyncClient(base_url='http://ollama',transport=httpx.MockTransport(handle))
    with TestClient(create_app(settings,store,ollama)) as client:
        with client.websocket_connect('/ws/chat',headers={'origin':'http://localhost:8000'}) as ws:
            ws.send_json({'topic':'PHP','question':'What is PHP?'})
            while ws.receive_json()['type'] != 'token':pass
        assert closed.wait(3), 'Disconnect should close the model HTTP stream'
