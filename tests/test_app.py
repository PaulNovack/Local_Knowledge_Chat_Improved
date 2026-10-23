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
from backend.following_distance import answer_following_distance, _parse


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


def test_following_distance_calculations_and_fallbacks():
    cases = [
        ('How long to stop a 70-foot rig at 60 mph?', 70, 60, 8),
        ('What safe following distance for a 70 ft truck at 40 mph?', 70, 40, 7),
        ('How many seconds should a 70-foot rig follow at 64.37376 km/h?', 70, 40, 7),
    ]
    for question, length, speed, seconds in cases:
        result = answer_following_distance(question)
        assert result.handled
        assert result.length_feet == length
        assert result.speed_mph == pytest.approx(speed)
        assert result.following_gap_seconds == seconds
        assert result.formula in result.answer
        assert 'not the physical time needed to stop' in result.answer

    fallbacks = [
        'How long to stop a 70-foot truck?',
        'How long to stop a 70-foot truck at 40 or 60 mph?',
        'How long to stop a 70-foot truck at 60 mph and what gear should I use?',
        'How long does a truck take to stop at 60 mph?',
    ]
    assert all(_parse(question) is None for question in fallbacks)


def test_following_distance_answer_is_saved_without_calling_llm(tmp_path, monkeypatch, settings, store):
    seeded_store(store)
    (tmp_path / 'web').mkdir()
    monkeypatch.setattr('backend.core.ROOT', tmp_path)
    monkeypatch.setattr('backend.app.ROOT', tmp_path)

    def unexpected_request(request):
        raise AssertionError(f'following-distance route called Ollama: {request.url.path}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(unexpected_request))
    with TestClient(create_app(settings, store, ollama)) as client:
        with client.websocket_connect('/ws/chat', headers={'origin': 'http://localhost:8000'}) as ws:
            ws.send_json({'topic': 'PHP', 'question': 'How long to stop a 70-foot rig at 60 mph?'})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'done':
                    break

    answer = next(event['text'] for event in events if event['type'] == 'token')
    assert '70 / 10 + 1 = 8 seconds' in answer
    with sqlite3.connect(tmp_path / 'database' / 'chat_history.sqlite3') as db:
        row = db.execute('SELECT question, answer FROM chat_history').fetchone()
    assert row == ('How long to stop a 70-foot rig at 60 mph?', answer)


def test_chat_history_is_created_and_saved(tmp_path, monkeypatch, settings, store):
    seeded_store(store)

    (tmp_path / 'web').mkdir()
    monkeypatch.setattr('backend.core.ROOT', tmp_path)
    monkeypatch.setattr('backend.app.ROOT', tmp_path)

    embed_inputs = []

    def handle(request):
        if request.url.path == '/api/embed':
            embed_inputs.append(json.loads(request.content)['input'])
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        payload = json.loads(request.content)
        assert len(payload['messages']) == 2
        assert payload['messages'][-1]['content'].endswith('Question: What about PHP?')
        return httpx.Response(200, text='\n'.join(json.dumps(x) for x in [
            {'message': {'content': 'PHP is used for web development [1].'}}, {'done': True}]))

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        db_path = tmp_path / 'database' / 'chat_history.sqlite3'
        assert db_path.exists()
        with client.websocket_connect('/ws/chat', headers={'origin': 'http://localhost:8000'}) as ws:
            ws.send_json({
                'topic': 'PHP',
                'question': 'What about PHP?',
                'history': [{'role': 'user', 'content': 'What was the prior question?'}],
            })
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
    assert embed_inputs == ['What about PHP?']
    assert row[2] == 'What about PHP?'
    assert row[3] == 'PHP is used for web development [1].'
    assert row[4] == 1
    assert row[5]
    assert row[6]


def test_step_video_route_serves_numbered_mp4_and_rejects_missing_steps(tmp_path, monkeypatch, settings, store):
    videos = tmp_path / 'Videos'
    videos.mkdir()
    (videos / 'Step_2_CDL_Parking_Brake_Check_Amy.mp4').write_bytes(b'test-video')
    (videos / 'CDL_Fifth_Wheel_Coupling_Walkthrough_Amy.mp4').write_bytes(b'coupling-video')
    (videos / 'CDL_Fifth_Wheel_Uncoupling_Walkthrough_Amy.mp4').write_bytes(b'uncoupling-video')
    (videos / 'CDL_Lane_Change_Check_Amy.mp4').write_bytes(b'lane-change-video')
    (videos / 'CDL_Right_Turn_Tractor_Trailer_Amy.mp4').write_bytes(b'right-turn-video')
    (tmp_path / 'web').mkdir()
    monkeypatch.setattr('backend.app.ROOT', tmp_path)
    monkeypatch.setattr('backend.core.ROOT', tmp_path)

    ollama = httpx.AsyncClient(base_url='http://ollama')
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        response = client.get('/api/videos/step/2')
        named_response = client.get('/api/videos/named/fifth-wheel-coupling')
        uncoupling_response = client.get('/api/videos/named/fifth-wheel-uncoupling')
        lane_change_response = client.get('/api/videos/named/lane-change')
        right_turn_response = client.get('/api/videos/named/right-turn')
        recorded = client.post('/api/videos/step/2/view', headers={
            'origin': 'https://chat.example.com', 'host': 'chat.example.com', 'x-forwarded-proto': 'https',
        },
                               json={'question': 'What is step 2 of the pre-trip inspection?'})
        named_recorded = client.post('/api/videos/named/fifth-wheel-coupling/view', headers={
            'origin': 'https://chat.example.com', 'host': 'chat.example.com', 'x-forwarded-proto': 'https',
        }, json={'question': 'Show me how to hook up a trailer'})
        uncoupling_recorded = client.post('/api/videos/named/fifth-wheel-uncoupling/view', headers={
            'origin': 'https://chat.example.com', 'host': 'chat.example.com', 'x-forwarded-proto': 'https',
        }, json={'question': 'Show me how to disconnect a trailer'})
        history = client.get('/api/responses')
        lane_change_recorded = client.post('/api/videos/named/lane-change/view', headers={
            'origin': 'https://chat.example.com', 'host': 'chat.example.com', 'x-forwarded-proto': 'https',
        }, json={'question': 'Show me how to change lanes'})
        lane_change_history = client.get('/api/responses')
        right_turn_recorded = client.post('/api/videos/named/right-turn/view', headers={
            'origin': 'https://chat.example.com', 'host': 'chat.example.com', 'x-forwarded-proto': 'https',
        }, json={'question': 'Show me making a right turn.'})
        right_turn_history = client.get('/api/responses')
        rejected = client.post('/api/videos/step/2/view', headers={'origin': 'https://untrusted.example'},
                       json={'question': 'Untrusted video request'})
        missing = client.get('/api/videos/step/3')
        missing_view = client.post('/api/videos/step/3/view', headers={'origin': 'http://127.0.0.1:8000'},
                                   json={'question': 'What is step 3 of the pre-trip inspection?'})

    assert response.status_code == 200
    assert response.headers['content-type'] == 'video/mp4'
    assert response.headers['content-disposition'].startswith('inline;')
    assert response.content == b'test-video'
    assert named_response.status_code == 200
    assert named_response.headers['content-type'] == 'video/mp4'
    assert named_response.content == b'coupling-video'
    assert uncoupling_response.status_code == 200
    assert uncoupling_response.headers['content-type'] == 'video/mp4'
    assert uncoupling_response.content == b'uncoupling-video'
    assert lane_change_response.status_code == 200
    assert lane_change_response.headers['content-type'] == 'video/mp4'
    assert lane_change_response.content == b'lane-change-video'
    assert right_turn_response.status_code == 200
    assert right_turn_response.headers['content-type'] == 'video/mp4'
    assert right_turn_response.content == b'right-turn-video'
    assert missing.status_code == 404
    assert recorded.status_code == 200
    assert recorded.json() == {'ok': True, 'filename': 'Step_2_CDL_Parking_Brake_Check_Amy.mp4'}
    assert history.json()['items'][0]['topic'] == 'CDLA'
    assert named_recorded.status_code == 200
    assert named_recorded.json() == {
        'ok': True, 'filename': 'CDL_Fifth_Wheel_Coupling_Walkthrough_Amy.mp4',
    }
    assert history.json()['items'][1]['question'] == 'Show me how to hook up a trailer'
    assert history.json()['items'][1]['answer'] == 'CDL_Fifth_Wheel_Coupling_Walkthrough_Amy.mp4'
    assert uncoupling_recorded.status_code == 200
    assert uncoupling_recorded.json() == {
        'ok': True, 'filename': 'CDL_Fifth_Wheel_Uncoupling_Walkthrough_Amy.mp4',
    }
    assert history.json()['items'][0]['question'] == 'Show me how to disconnect a trailer'
    assert history.json()['items'][0]['answer'] == 'CDL_Fifth_Wheel_Uncoupling_Walkthrough_Amy.mp4'
    assert history.json()['items'][1]['question'] == 'Show me how to hook up a trailer'
    assert history.json()['items'][1]['answer'] == 'CDL_Fifth_Wheel_Coupling_Walkthrough_Amy.mp4'
    assert history.json()['items'][2]['question'] == 'What is step 2 of the pre-trip inspection?'
    assert history.json()['items'][2]['answer'] == 'Step_2_CDL_Parking_Brake_Check_Amy.mp4'
    assert lane_change_recorded.status_code == 200
    assert lane_change_recorded.json() == {'ok': True, 'filename': 'CDL_Lane_Change_Check_Amy.mp4'}
    assert lane_change_history.json()['items'][0]['question'] == 'Show me how to change lanes'
    assert lane_change_history.json()['items'][0]['answer'] == 'CDL_Lane_Change_Check_Amy.mp4'
    assert right_turn_recorded.status_code == 200
    assert right_turn_recorded.json() == {'ok': True, 'filename': 'CDL_Right_Turn_Tractor_Trailer_Amy.mp4'}
    assert right_turn_history.json()['items'][0]['question'] == 'Show me making a right turn.'
    assert right_turn_history.json()['items'][0]['answer'] == 'CDL_Right_Turn_Tractor_Trailer_Amy.mp4'
    assert rejected.status_code == 403
    assert missing_view.status_code == 404


def test_complete_pretrip_video_view_records_all_steps_in_order(tmp_path, monkeypatch, settings, store):
    videos = tmp_path / 'Videos'
    videos.mkdir()
    filenames = []
    for step in range(1, 40):
        filename = f'Step_{step}_Inspection.mp4'
        (videos / filename).write_bytes(b'test-video')
        filenames.append(filename)
    (tmp_path / 'web').mkdir()
    monkeypatch.setattr('backend.app.ROOT', tmp_path)
    monkeypatch.setattr('backend.core.ROOT', tmp_path)

    ollama = httpx.AsyncClient(base_url='http://ollama')
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        response = client.post('/api/videos/pre-trip/complete/view',
            headers={'origin': 'http://127.0.0.1:8000'},
            json={'question': 'Show me the complete pre-trip inspection'})
        history = client.get('/api/responses?page=1&page_size=1')

    assert response.status_code == 200
    assert response.json() == {'ok': True, 'filenames': filenames}
    assert history.json()['items'][0]['topic'] == 'CDLA'
    assert history.json()['items'][0]['question'] == 'Show me the complete pre-trip inspection'
    assert history.json()['items'][0]['answer'].splitlines() == filenames


def test_view_responses_api_paginates_newest_first(tmp_path, monkeypatch, settings, store):
    monkeypatch.setattr('backend.core.ROOT', tmp_path)
    monkeypatch.setattr('backend.app.ROOT', tmp_path)
    (tmp_path / 'web').mkdir()

    def handle(request):
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        db_path = tmp_path / 'database' / 'chat_history.sqlite3'
        with sqlite3.connect(db_path) as db:
            db.executemany(
                '''
                INSERT INTO chat_history(topic, question, answer, accepted, created_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                [
                    ('PHP', 'Oldest question?', 'Oldest answer.', 1, '2026-10-19', '2099-10-19 00:00:03'),
                    ('CDLA', 'Middle question?', 'Middle answer.', 1, '2026-10-19', '2000-10-19 00:00:01'),
                    ('Dementia', 'Newest question?', 'Newest answer.', 1, '2026-10-19', '2026-10-19 00:00:02'),
                ],
            )

        first = client.get('/api/responses?page=1&page_size=2')
        second = client.get('/api/responses?page=2&page_size=2')

    assert first.status_code == 200
    assert second.status_code == 200
    first_data = first.json()
    second_data = second.json()
    assert first_data['total'] == 3
    assert first_data['total_pages'] == 2
    assert first_data['page'] == 1
    assert [item['ID'] for item in first_data['items']] == [1, 3]
    assert [item['ID'] for item in second_data['items']] == [2]


def test_view_responses_api_filters_by_keyword(tmp_path, monkeypatch, settings, store):
    monkeypatch.setattr('backend.core.ROOT', tmp_path)
    monkeypatch.setattr('backend.app.ROOT', tmp_path)
    (tmp_path / 'web').mkdir()

    def handle(request):
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        db_path = tmp_path / 'database' / 'chat_history.sqlite3'
        with sqlite3.connect(db_path) as db:
            db.executemany(
                '''
                INSERT INTO chat_history(topic, question, answer, accepted, created_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                [
                    ('PHP', 'How do I add context?', 'Use context from the chat history.', 1, '2026-10-19', '2026-10-19 00:00:01'),
                    ('CDLA', 'How do corrections work?', 'Save the corrected answer.', 1, '2026-10-19', '2026-10-19 00:00:02'),
                    ('Dementia', 'What is a dolly?', 'A dolly helps move heavy objects.', 1, '2026-10-19', '2026-10-19 00:00:03'),
                ],
            )

        response = client.get('/api/responses?q=corrections&page=1&page_size=10')

    assert response.status_code == 200
    data = response.json()
    assert data['total'] == 1
    assert data['keyword'] == 'corrections'
    assert [item['question'] for item in data['items']] == ['How do corrections work?']


def test_view_responses_delete_removes_only_selected_history_row(tmp_path, monkeypatch, settings, store):
    monkeypatch.setattr('backend.core.ROOT', tmp_path)
    monkeypatch.setattr('backend.app.ROOT', tmp_path)
    (tmp_path / 'web').mkdir()
    additions_dir = tmp_path / 'topic_data' / 'user_additions'
    additions_dir.mkdir(parents=True)
    additions = [
        {'question': 'Wrong answer?', 'answer': 'Incorrect.', 'url': '', 'source': 'user_additions'},
        {'question': 'Keep correction?', 'answer': 'Still correct.', 'url': '', 'source': 'user_additions'},
        {'question': 'Different saved correction?', 'answer': 'Actual corrected answer.', 'url': '', 'source': 'user_additions'},
    ]
    additions_path = additions_dir / 'CDLA.jsonl'
    additions_path.write_text(''.join(json.dumps(record) + '\n' for record in additions), encoding='utf-8')
    docs_by_question = {
        record['question']: list(documents(record))
        for record in additions
    }
    for docs in docs_by_question.values():
        store.upsert('CDLA', docs, [[1.0, 0.0, 0.0]] * len(docs))

    ollama = httpx.AsyncClient(base_url='http://ollama')
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        db_path = tmp_path / 'database' / 'chat_history.sqlite3'
        with sqlite3.connect(db_path) as db:
            db.executemany(
                'INSERT INTO chat_history(topic, question, answer) VALUES (?, ?, ?)',
                [
                    ('CDLA', 'Wrong answer?', 'Incorrect.'),
                    ('CDLA', 'Keep this?', 'Correct.'),
                    ('CDLA', 'Different saved correction?', 'Wrong generated answer.'),
                ],
            )

        blocked = client.delete('/api/responses/1', headers={'origin': 'https://example.com'})
        deleted = client.delete('/api/responses/1', headers={'origin': 'http://127.0.0.1:8000'})
        missing = client.delete('/api/responses/1', headers={'origin': 'http://127.0.0.1:8000'})
        deleted_without_matching_correction = client.delete(
            '/api/responses/3', headers={'origin': 'http://127.0.0.1:8000'},
        )
        remaining = client.get('/api/responses')

    assert blocked.status_code == 403
    assert deleted.status_code == 200
    assert deleted.json() == {'ok': True, 'id': 1, 'correction_removed': True}
    assert missing.status_code == 404
    assert deleted_without_matching_correction.status_code == 200
    assert deleted_without_matching_correction.json()['correction_removed'] is False
    assert [item['question'] for item in remaining.json()['items']] == ['Keep this?']
    saved_records = [json.loads(line) for line in additions_path.read_text(encoding='utf-8').splitlines()]
    assert [record['question'] for record in saved_records] == ['Keep correction?', 'Different saved correction?']
    assert store.existing('CDLA', [doc['id'] for doc in docs_by_question['Wrong answer?']]) == set()
    assert store.existing('CDLA', [doc['id'] for doc in docs_by_question['Keep correction?']])
    assert store.existing('CDLA', [doc['id'] for doc in docs_by_question['Different saved correction?']])


def test_view_responses_page_is_served(tmp_path, monkeypatch, settings, store):
    def handle(request):
        return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        response = client.get('/view-responses/')

    assert response.status_code == 200
    assert 'View Responses' in response.text


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
    results = store.search('PHP', [1.0, 0.0, 0.0])
    assert any(item['text'] == 'PHP is a server-side scripting language for web development.' for item in results)
    assert not any(item['text'] == 'PHP is a general-purpose scripting language for web development.' for item in results)


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
    old_record = {
        'question': 'What is PHP?',
        'answer': 'Outdated answer.',
        'url': '',
        'source': 'user_additions',
    }
    store.upsert('PHP', list(documents(old_record)), [[1.0, 0.0, 0.0]])
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
    assert not any(item['text'] == 'Outdated answer.' for item in results)


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


def test_speak_caches_wav_by_text_hash(tmp_path, monkeypatch, settings, store):
    (tmp_path / 'web').mkdir()
    monkeypatch.setattr('backend.core.ROOT', tmp_path)
    monkeypatch.setattr('backend.app.ROOT', tmp_path)
    piper_calls = []

    def handle(request):
        if request.url.path == '/speak':
            piper_calls.append(request)
            return httpx.Response(200, content=b'RIFFwav', headers={'content-type': 'audio/wav'})
        return httpx.Response(200, json={'models': []})

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    app = create_app(settings, store, ollama)
    with TestClient(app) as client:
        app.state.piper = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        db_path = tmp_path / 'database' / 'chat_history.sqlite3'
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT name FROM sqlite_master WHERE name='tts_cache'").fetchone()
        first = client.post('/api/speak', json={'text': 'Hello there'})
        second = client.post('/api/speak', json={'text': 'Hello there'})
        assert first.content == second.content == b'RIFFwav'
        assert len(piper_calls) == 1
        client.post('/api/speak', json={'text': 'Other text'})
        assert len(piper_calls) == 2

    with sqlite3.connect(db_path) as db:
        rows = db.execute('SELECT hash, text FROM tts_cache ORDER BY text').fetchall()
    assert [r[1] for r in rows] == ['Hello there', 'Other text']
    assert all((tmp_path / 'database' / 'tts_cache' / f'{r[0]}.wav').is_file() for r in rows)
