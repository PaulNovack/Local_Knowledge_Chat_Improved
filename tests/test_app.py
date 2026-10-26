import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from backend.core import Settings, Store, ensure_chat_history_db, read_records, documents, sync_qdrant_topic_to_sqlite, sync_qdrant_topics_to_sqlite, topic_name
from backend.ingest import import_file
from backend.app import create_app, _is_date_question, _reload_saved_corrections
from backend.following_distance import answer_following_distance, _parse
from backend.routing import resolve_routed_answer


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


def test_fresh_sqlite_database_seeds_router_functions(tmp_path):
    path = ensure_chat_history_db(tmp_path / 'chat_history.sqlite3')
    with sqlite3.connect(path) as db:
        rows = db.execute('SELECT id, name FROM routerFunctions ORDER BY id').fetchall()
        topic_columns = {row[1] for row in db.execute('PRAGMA table_info(topic_data)')}
        history_columns = {row[1] for row in db.execute('PRAGMA table_info(chat_history)')}
        location_columns = {row[1] for row in db.execute('PRAGMA table_info(user_location_events)')}

    assert rows == [
        (1, ''), (2, 'Pre-trip Text'), (3, 'Pre-trip Video'), (4, 'Closest Loves'),
        (5, 'Closest Roadys'), (6, 'Closest McDonalds'), (7, 'Closest Restaurant'),
        (8, 'Supported Restaurants'), (9, 'Open Directions'),
    ]
    assert {'topic', 'id', 'routerFunction', 'key'} <= topic_columns
    assert {'routerFunction', 'key'} <= history_columns
    assert {'session_id', 'recorded_at', 'latitude', 'longitude'} <= location_columns


def test_web_code_assets_are_revalidated(settings, store, tmp_path, monkeypatch):
    database = tmp_path / 'chat_history.sqlite3'
    monkeypatch.setattr('backend.app.ensure_chat_history_db', lambda: ensure_chat_history_db(database))
    ollama = httpx.AsyncClient(base_url='http://ollama')
    with TestClient(create_app(settings, store, ollama)) as client:
        for asset in ('/app.js', '/'):
            response = client.get(asset)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-cache, max-age=0, must-revalidate'


def test_geolocation_endpoint_sets_session_and_records_requests(settings, store, tmp_path, monkeypatch):
    database = tmp_path / 'chat_history.sqlite3'
    monkeypatch.setattr('backend.app.ensure_chat_history_db', lambda: ensure_chat_history_db(database))
    app = create_app(settings, store, httpx.AsyncClient(base_url='http://ollama'))
    with TestClient(app) as client:
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE loves_locations (store_number TEXT, latitude REAL, longitude REAL)')
            db.execute('INSERT INTO loves_locations VALUES (?, ?, ?)', ('1234', 30.2672, -97.7431))
        response = client.post(
            '/api/geolocation',
            json={'latitude': 30.2672, 'longitude': -97.7431},
            headers={'origin': 'http://127.0.0.1:8000'},
        )
        session_id = response.cookies['session_id']
        assert response.status_code == 200
        assert len(session_id) == 32
        assert response.cookies['geolocation'] == '30.2672:-97.7431'
        assert response.cookies['LocationType'] == 'Loves'
        assert response.cookies['StoreNUmber'] == '1234'
        client.get('/api/topics')

    with sqlite3.connect(database) as db:
        events = db.execute(
            'SELECT session_id, latitude, longitude FROM user_location_events ORDER BY id'
        ).fetchall()

    assert events == [
        (session_id, 30.2672, -97.7431),
        (session_id, 30.2672, -97.7431),
    ]


def test_geolocation_endpoint_clears_override_location(settings, store, tmp_path, monkeypatch):
    database = tmp_path / 'chat_history.sqlite3'
    monkeypatch.setattr('backend.app.ensure_chat_history_db', lambda: ensure_chat_history_db(database))
    app = create_app(settings, store, httpx.AsyncClient(base_url='http://ollama'))
    with TestClient(app) as client:
        headers = {'origin': 'http://127.0.0.1:8000'}
        location_response = client.post(
            '/api/geolocation',
            json={'latitude': 40.0, 'longitude': -70.0},
            headers=headers,
        )
        assert location_response.status_code == 200
        assert client.cookies.get('geolocation') == '40.0:-70.0'

        clear_response = client.post(
            '/api/geolocation',
            json={'latitude': None, 'longitude': None},
            headers=headers,
        )

        assert clear_response.status_code == 200
        assert client.cookies.get('geolocation') is None

    with sqlite3.connect(database) as db:
        location = db.execute(
            'SELECT latitude, longitude FROM user_location_events ORDER BY id DESC LIMIT 1'
        ).fetchone()

    assert location == (None, None)


def test_closest_loves_route_uses_recent_heading_and_stationary_fallback(tmp_path):
    from backend.routing import resolve_routed_answer

    database = ensure_chat_history_db(tmp_path / 'chat_history.sqlite3')
    with sqlite3.connect(database) as db:
        db.execute('''CREATE TABLE loves_locations (
            store_number TEXT, latitude REAL, longitude REAL, address TEXT, phone TEXT,
            store_hours TEXT,
            fuel_prices_json TEXT, truck_parking INTEGER, shower_count INTEGER,
            amenities_json TEXT, restaurants_json TEXT, restaurant_hours_json TEXT,
            scraped_at TEXT
        )''')
        db.executemany('INSERT INTO loves_locations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [
            ('AHEAD', 30.2, -97.0, '100 County HWY 35, I-65, Austin, TX 78701', '+1-512-555-0100',
             'Mon: 6 a.m. – 10 p.m. Tue: 6 a.m. – 10 p.m.',
             '{"AUTO DIESEL":{"display":"$6.149","price":6.149},"DIESEL B20":{"display":"$6.149","price":6.149}}', 70, 5,
             '["Private Showers","Truck Parking"]',
             '[{"alt":"restaurant","src":"/chesters.jpg"},{"alt":"restaurant","src":"/mcdonalds.jpg"}]',
             '[{"name":"Chester\'s","hours":"Monday: 10 a.m. - 9 p.m. Tuesday: 10 a.m. - 9 p.m. Wednesday: 10 a.m. - 9 p.m. Thursday: 10 a.m. - 9 p.m. Friday: 10 a.m. - 9 p.m. Saturday: 10 a.m. - 6 p.m. Sunday: 10 a.m. - 6 p.m."}]',
             '2026-10-23T10:00:00+00:00'),
            ('NEARBY', 29.99, -97.0, '200 Nearby Road, Austin, TX 78702', None, None, '{}', 0, 0, '[]', '[]', '[]', ''),
        ])
        db.executemany(
            'INSERT INTO user_location_events(session_id, latitude, longitude) VALUES (?, ?, ?)',
            [('moving-session', 29.95, -97.0), ('moving-session', 30.0, -97.0),
             ('still-session', 30.0, -97.0)],
        )

    moving = resolve_routed_answer(
        'CDLA', 'What is closest loves in direction I am Going', database, 'moving-session',
    )
    still = resolve_routed_answer(
        'CDLA', 'What is closest loves in direction I am Going', database, 'still-session',
    )

    assert moving['routerFunction'] == 'Closest Loves'
    assert moving['key'] == ''
    assert moving['directions'] == {
        'address': '100 County Highway 35, Interstate 65, Austin, Texas',
        'latitude': 30.2,
        'longitude': -97.0,
    }
    assert 'store AHEAD' in moving['text']
    assert 'store #AHEAD' not in moving['text']
    assert 'Address: 100 County Highway 35, Interstate 65, Austin, Texas' in moving['text']
    assert 'Phone: 512 555 0100' in moving['text']
    assert '78701' not in moving['text']
    assert 'Store hours: Monday thru Tuesday: 6 AM to 10 PM' in moving['text']
    assert 'Auto Diesel: $6.149 per gallon.' in moving['text']
    assert 'Diesel B20: $6.149 per gallon.' in moving['text']
    assert 'Diesel prices:\nAuto Diesel: $6.149 per gallon.\nDiesel B20: $6.149 per gallon.' in moving['text']
    assert 'Fuel prices last scraped on October 23, 2026.' in moving['text']
    assert 'Truck parking: Yes (70 spaces listed)' in moving['text']
    assert 'Showers: Yes (5 private showers)' in moving['text']
    assert 'Laundry: Not listed' in moving['text']
    assert "Restaurants: Chester's and McDonald's" in moving['text']
    assert "Restaurant hours:\nChester's: Monday thru Friday: 10 AM to 9 PM; Saturday thru Sunday: 10 AM to 6 PM" in moving['text']
    assert 'Store hours:' not in still['text']
    assert 'Restaurant hours:' not in still['text']
    assert 'store NEARBY' in still['text']


def test_closest_loves_question_is_answered_in_chat_without_embedding(tmp_path, monkeypatch, settings, store):
    seeded_store(store)
    database = tmp_path / 'chat_history.sqlite3'
    monkeypatch.setattr('backend.app.ensure_chat_history_db', lambda: ensure_chat_history_db(database))
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE loves_locations (store_number TEXT, latitude REAL, longitude REAL)')
        db.execute('INSERT INTO loves_locations VALUES (?, ?, ?)', ('1234', 30.01, -97.0))

    def unexpected_request(request):
        raise AssertionError(f'Unexpected Ollama request: {request.url}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(unexpected_request))
    with TestClient(create_app(settings, store, ollama)) as client:
        client.get('/api/topics')
        location_response = client.post(
            '/api/geolocation',
            json={'latitude': 30.0, 'longitude': -97.0},
            headers={'origin': 'http://127.0.0.1:8000'},
        )
        assert location_response.cookies['StoreNUmber'] == '1234'
        with client.websocket_connect('/ws/chat', headers={'origin': 'http://127.0.0.1:8000'}) as ws:
            ws.send_json({'topic': 'PHP', 'question': 'What is closest loves in direction I am Going'})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'done':
                    break

    answer = next(event['text'] for event in events if event['type'] == 'token')
    directions = next(event['sources'][0]['directions'] for event in events if event['type'] == 'sources')
    assert 'store 1234' in answer
    assert 'store #1234' not in answer
    assert 'miles away' in answer
    assert directions == {'address': '', 'latitude': 30.01, 'longitude': -97.0}
    with sqlite3.connect(database) as db:
        route, key = db.execute('SELECT routerFunction, "key" FROM chat_history').fetchone()
    assert (route, key) == ('Closest Loves', '')


def test_closest_mcdonalds_route_uses_recent_heading_and_hours(tmp_path):
    database = ensure_chat_history_db(tmp_path / 'chat_history.sqlite3')
    with sqlite3.connect(database) as db:
        db.execute('''CREATE TABLE restaurants (
            chain TEXT, name TEXT, latitude REAL, longitude REAL, address TEXT,
            street TEXT, city TEXT, state TEXT, postal_code TEXT, phone TEXT,
            website TEXT, opening_hours TEXT, drive_through TEXT
        )''')
        db.executemany('INSERT INTO restaurants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [
            ('mcdonalds', "McDonald's", 30.2, -97.0, '100 Main St, Austin, TX 78701',
             'Main St', 'Austin', 'TX', '78701', '+1-512-555-0100', 'https://example.com',
             'Mo-Su 05:00-23:00', 'yes'),
            ('mcdonalds', "McDonald's", 29.99, -97.0, '200 Nearby Rd, Austin, TX 78702',
             'Nearby Rd', 'Austin', 'TX', '78702', None, None, None, None),
        ])
        db.executemany(
            'INSERT INTO user_location_events(session_id, latitude, longitude) VALUES (?, ?, ?)',
            [('moving-session', 29.95, -97.0), ('moving-session', 30.0, -97.0),
             ('still-session', 30.0, -97.0)],
        )

    question = 'What is the closest McDonalds in the direction I am going?'
    moving = resolve_routed_answer('CDLA', question, database, 'moving-session')
    still = resolve_routed_answer('CDLA', question, database, 'still-session')

    assert moving['routerFunction'] == 'Closest Restaurant'
    assert moving['key'] == 'mcdonalds'
    assert 'in the direction you are heading' in moving['text']
    assert 'Address: 100 Main St, Austin, Texas' in moving['text']
    assert 'Hours:' in moving['text']
    assert 'Drive-through: yes' in moving['text']
    assert 'Phone: 512 555 0100' in moving['text']
    assert 'in the direction' not in still['text']
    assert still['routerFunction'] == 'Closest Restaurant'
    assert still['key'] == 'mcdonalds'
    assert "The closest McDonald's is about" in still['text']


def test_closest_restaurant_route_uses_shared_chain_and_recent_heading(tmp_path):
    database = ensure_chat_history_db(tmp_path / 'chat_history.sqlite3')
    with sqlite3.connect(database) as db:
        db.execute('''CREATE TABLE restaurants (
            chain TEXT, name TEXT, latitude REAL, longitude REAL, address TEXT,
            street TEXT, city TEXT, state TEXT, postal_code TEXT, phone TEXT, website TEXT,
            opening_hours TEXT, drive_through TEXT
        )''')
        db.executemany('INSERT INTO restaurants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [
            ('starbucks', 'Starbucks', 30.2, -97.0, '12345 Main St, Austin, TX 78701', 'Main St', 'Austin', 'TX', '78701',
             '+1-512-555-0100', 'https://example.com', 'Su-Th 05:30-22:00; Fr-Sa 05:00-23:00', None),
            ('starbucks', 'Starbucks', 29.99, -97.0, '200 Nearby Rd', 'Nearby Rd', 'Austin', 'TX', '78702',
             None, None, None, None),
            ('dunkin', "Dunkin'", 30.1, -97.0, '300 Other Rd', 'Other Rd', 'Austin', 'TX', '78703',
             None, None, None, None),
            ('kfc', 'KFC', 30.2, -97.0, '400 Chicken Rd', 'Chicken Rd', 'Austin', 'TX', '78704',
             None, None, None, None),
            ('potbelly', 'Potbelly', 30.2, -97.0, '500 Sandwich Rd', 'Sandwich Rd', 'Austin', 'TX', '78705',
             None, None, None, None),
              ('bubba_gump_shrimp_company', 'Bubba Gump Shrimp Company', 30.2, -97.0,
               '600 Shrimp Rd', 'Shrimp Rd', 'Austin', 'TX', '78706', None, None, None, None),
              ('torchy_s_tacos', "Torchy's Tacos", 30.2, -97.0, '700 Taco Rd', 'Taco Rd', 'Austin', 'TX', '78707',
               None, None, None, None),
                ('checkers', 'Checkers', 30.2, -97.0, '800 Burger Rd', 'Burger Rd', 'Austin', 'TX', '78708',
                 None, None, None, None),
        ])
        db.executemany(
            'INSERT INTO user_location_events(session_id, latitude, longitude) VALUES (?, ?, ?)',
            [('moving-session', 29.95, -97.0), ('moving-session', 30.0, -97.0)],
        )

    result = resolve_routed_answer(
        'CDLA', 'What is the closest Starbucks in the direction I am going?',
        database, 'moving-session',
    )

    assert result['routerFunction'] == 'Closest Restaurant'
    assert result['key'] == 'starbucks'
    assert result['directions'] == {
        'address': '12345 Main St, Austin, Texas',
        'latitude': 30.2,
        'longitude': -97.0,
    }
    assert 'in the direction you are heading' in result['text']
    assert 'The closest Starbucks' in result['text']
    assert 'Name:' not in result['text']
    assert 'Address: 12345 Main St, Austin, Texas' in result['text']
    assert '78701' not in result['text']
    assert 'Hours: Sunday thru Thursday: 5:30 AM to 10 PM; Friday thru Saturday: 5 AM to 11 PM' in result['text']
    assert 'Phone: 512 555 0100' in result['text']
    assert 'Website:' not in result['text']
    assert 'https://example.com' not in result['text']

    kfc_result = resolve_routed_answer(
        'CDLA', 'What is the nearest Kentucky Fried Chicken restaurant in the direction I am traveling?',
        database, 'moving-session',
    )
    assert kfc_result['routerFunction'] == 'Closest Restaurant'
    assert kfc_result['key'] == 'kfc'
    assert 'The closest KFC' in kfc_result['text']

    potbellies_result = resolve_routed_answer(
        'CDLA', 'What is the nearest Potbellies restaurant in my direction of travel?',
        database, 'moving-session',
    )
    assert potbellies_result['routerFunction'] == 'Closest Restaurant'
    assert potbellies_result['key'] == 'potbelly'
    assert 'The closest Potbelly' in potbellies_result['text']

    bubba_gump_result = resolve_routed_answer(
        'CDLA', "What is the nearest Bubba Gump restaurant in the direction I'm headed?",
        database, 'moving-session',
    )
    assert bubba_gump_result['routerFunction'] == 'Closest Restaurant'
    assert bubba_gump_result['key'] == 'bubba_gump_shrimp_company'
    assert 'The closest Bubba Gump Shrimp Company' in bubba_gump_result['text']

    bubblegum_shrimp_result = resolve_routed_answer(
        'CDLA', "What is the nearest bubblegum shrimp restaurant in the direction I'm going?",
        database, 'moving-session',
    )
    assert bubblegum_shrimp_result['routerFunction'] == 'Closest Restaurant'
    assert bubblegum_shrimp_result['key'] == 'bubba_gump_shrimp_company'
    assert 'The closest Bubba Gump Shrimp Company' in bubblegum_shrimp_result['text']

    torchys_result = resolve_routed_answer(
        'CDLA', "What is the nearest torches taco restaurant in the direction I'm going?",
        database, 'moving-session',
    )
    assert torchys_result['routerFunction'] == 'Closest Restaurant'
    assert torchys_result['key'] == 'torchy_s_tacos'
    assert "The closest Torchy's Tacos" in torchys_result['text']

    checkers_result = resolve_routed_answer(
        'CDLA', "What is the newest Checkers in the direction I'm going?",
        database, 'moving-session', [{
            'question': 'What is the nearest Checkers restaurant in my direction of travel?',
            'text': 'Find the nearest Checkers location using the user\'s current location.',
            'routerFunction': 'Closest Restaurant', 'key': 'checkers',
        }],
    )
    assert checkers_result['routerFunction'] == 'Closest Restaurant'
    assert checkers_result['key'] == 'checkers'
    assert 'The closest Checkers' in checkers_result['text']
    assert 'Find the nearest Checkers location' not in checkers_result['text']

    metadata_result = resolve_routed_answer(
        'CDLA', 'Find a Checkers ahead', database, 'moving-session', [{
            'question': 'What is the nearest Checkers restaurant in my direction of travel?',
            'text': 'Find the nearest Checkers location using the user\'s current location.',
            'routerFunction': 'Closest Restaurant', 'key': 'checkers',
        }],
    )
    assert metadata_result['key'] == 'checkers'
    assert 'The closest Checkers' in metadata_result['text']
    assert 'Find the nearest Checkers location' not in metadata_result['text']

    keyed_result = resolve_routed_answer(
        'CDLA', 'What is the nearest torchey taco in my direction of travel?',
        database, 'moving-session',
        [{'routerFunction': 'Closest Restaurant', 'key': 'torchy_s_tacos'}],
    )
    assert keyed_result['routerFunction'] == 'Closest Restaurant'
    assert keyed_result['key'] == 'torchy_s_tacos'
    assert "The closest Torchy's Tacos" in keyed_result['text']
    assert resolve_routed_answer(
        'CDLA', 'What is the nearest pharmacy?', database, 'moving-session',
        [{'routerFunction': 'Closest Restaurant', 'key': 'bubba_gump_shrimp_company'}],
    ) is None


def test_closest_restaurant_unknown_chain_returns_data_apology(tmp_path):
    database = ensure_chat_history_db(tmp_path / 'chat_history.sqlite3')
    with sqlite3.connect(database) as db:
        db.execute('''CREATE TABLE restaurants (
            chain TEXT, name TEXT, latitude REAL, longitude REAL, address TEXT,
            street TEXT, city TEXT, state TEXT, postal_code TEXT, phone TEXT, website TEXT,
            opening_hours TEXT, drive_through TEXT
        )''')

    result = resolve_routed_answer(
        'CDLA', 'What is closest Portillos?', database, 'unknown-restaurant-session',
        [{
            'question': 'What is the nearest El Pollo Loco restaurant in my direction of travel?',
            'routerFunction': 'Closest Restaurant', 'key': 'el_pollo_loco',
        }],
    )

    assert result['text'] == 'Sorry, I do not have data for that restaurant.'
    assert result['key'] == ''


def test_store_search_preserves_router_metadata(settings, store):
    record = {
        'question': 'What is the nearest Bubba Gump Shrimp Company restaurant?',
        'answer': 'Find the nearest Bubba Gump Shrimp Company location.',
        'url': '', 'source': 'user_additions',
        'routerFunction': 'Closest Restaurant', 'key': 'bubba_gump_shrimp_company',
    }
    store.upsert('CDLA', list(documents(record)), [[1.0, 0.0, 0.0]])

    result = store.search('CDLA', [1.0, 0.0, 0.0])[0]

    assert result['routerFunction'] == 'Closest Restaurant'
    assert result['key'] == 'bubba_gump_shrimp_company'


def test_closest_restaurant_notes_when_state_exists_without_street(tmp_path):
    database = ensure_chat_history_db(tmp_path / 'chat_history.sqlite3')
    with sqlite3.connect(database) as db:
        db.execute('''CREATE TABLE restaurants (
            chain TEXT, name TEXT, latitude REAL, longitude REAL, address TEXT,
            street TEXT, city TEXT, state TEXT, postal_code TEXT, phone TEXT,
            website TEXT, opening_hours TEXT, drive_through TEXT
        )''')
        db.execute('''INSERT INTO restaurants VALUES (
            'popeyes', 'Popeyes', 30.2, -97.0, 'TX', NULL, NULL, 'TX', NULL,
            NULL, NULL, NULL, NULL
        )''')
        db.execute(
            'INSERT INTO user_location_events(session_id, latitude, longitude) VALUES (?, ?, ?)',
            ('state-only-session', 30.0, -97.0),
        )

    result = resolve_routed_answer(
        'CDLA', 'What is the closest Popeyes restaurant?', database, 'state-only-session',
    )

    assert 'I do not have enough recent movement sent from your device to determine your direction.' in result['text']
    assert result['text'].endswith(
        'I can not give you the exact address. Our data is provided by overpass it has a latitude and longitude but no street address. I will contact the restaurant to provide a better API or update Overpass with their location data'
    )


def test_closest_restaurant_chat_uses_retrieved_key(settings, store, tmp_path, monkeypatch):
    database = ensure_chat_history_db(tmp_path / 'chat_history.sqlite3')
    monkeypatch.setattr('backend.app.ensure_chat_history_db', lambda: database)
    async def skip_saved_additions_reload(*args):
        return None

    monkeypatch.setattr('backend.app._reload_saved_corrections', skip_saved_additions_reload)
    with sqlite3.connect(database) as db:
        db.execute('''CREATE TABLE restaurants (
            chain TEXT, name TEXT, latitude REAL, longitude REAL, address TEXT,
            street TEXT, city TEXT, state TEXT, postal_code TEXT, phone TEXT, website TEXT,
            opening_hours TEXT, drive_through TEXT
        )''')
        db.execute('''INSERT INTO restaurants (
            chain, name, latitude, longitude, address, street
        ) VALUES (?, ?, ?, ?, ?, ?)''', (
            'torchy_s_tacos', "Torchy's Tacos", 30.2, -97.0, '700 Taco Rd', 'Taco Rd',
        ))
        db.execute(
            'INSERT INTO user_location_events(session_id, latitude, longitude) VALUES (?, ?, ?)',
            ('routing-session', 30.0, -97.0),
        )

    record = {
        'question': 'What is the nearest Torchy\'s Tacos restaurant in my direction of travel?',
        'answer': 'Find the nearest Torchy\'s Tacos location using the user\'s current location.',
        'url': '', 'source': 'user_additions',
        'routerFunction': 'Closest Restaurant', 'key': 'torchy_s_tacos',
    }
    store.upsert('CDLA', list(documents(record)), [[1.0, 0.0, 0.0]])

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        raise AssertionError(f'Unexpected Ollama request: {request.url}')

    ollama = httpx.AsyncClient(
        base_url='http://ollama', transport=httpx.MockTransport(handle),
    )
    with TestClient(create_app(settings, store, ollama)) as client:
        client.cookies.set('session_id', 'routing-session')
        with client.websocket_connect(
            '/ws/chat', headers={'origin': 'http://127.0.0.1:8000'},
        ) as ws:
            ws.send_json({
                'topic': 'CDLA',
                'question': 'What is the nearest torchey taco in my direction of travel?',
            })
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'done':
                    break

    answer = next(event['text'] for event in events if event['type'] == 'token')
    assert "The closest Torchy's Tacos" in answer
    with sqlite3.connect(database) as db:
        route, key = db.execute(
            'SELECT routerFunction, "key" FROM chat_history ORDER BY ID DESC LIMIT 1'
        ).fetchone()
    assert (route, key) == ('Closest Restaurant', 'torchy_s_tacos')


def test_closest_restaurant_chat_rejects_unmatched_retrieved_chain(settings, store, tmp_path, monkeypatch):
    database = ensure_chat_history_db(tmp_path / 'chat_history.sqlite3')
    monkeypatch.setattr('backend.app.ensure_chat_history_db', lambda: database)
    async def skip_saved_additions_reload(*args):
        return None

    monkeypatch.setattr('backend.app._reload_saved_corrections', skip_saved_additions_reload)
    record = {
        'question': 'What is the nearest El Pollo Loco restaurant in my direction of travel?',
        'answer': 'Find the nearest El Pollo Loco location using the user\'s current location.',
        'url': '', 'source': 'user_additions',
        'routerFunction': 'Closest Restaurant', 'key': 'el_pollo_loco',
    }
    store.upsert('CDLA', list(documents(record)), [[1.0, 0.0, 0.0]])

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        raise AssertionError(f'Unexpected Ollama request: {request.url}')

    ollama = httpx.AsyncClient(
        base_url='http://ollama', transport=httpx.MockTransport(handle),
    )
    with TestClient(create_app(settings, store, ollama)) as client:
        with client.websocket_connect(
            '/ws/chat', headers={'origin': 'http://127.0.0.1:8000'},
        ) as ws:
            ws.send_json({'topic': 'CDLA', 'question': 'What is closest Portillos?'})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'done':
                    break

    answer = next(event['text'] for event in events if event['type'] == 'token')
    assert answer == 'Sorry, I do not have data for that restaurant.'


def test_supported_restaurants_route_lists_distinct_database_names(tmp_path):
    database = ensure_chat_history_db(tmp_path / 'chat_history.sqlite3')
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE restaurants (name TEXT)')
        db.executemany('INSERT INTO restaurants VALUES (?)', [
            ('Starbucks',), ('Starbucks',), ('Dunkin',), ('',), (None,),
        ])

    result = resolve_routed_answer(
        'CDLA', 'What Restaurants do you currently support', database,
    )

    assert result['routerFunction'] == 'Supported Restaurants'
    assert result['text'] == (
        'The restaurants currently supported are:\nDunkin\nStarbucks'
    )


def test_supported_restaurants_route_lists_only_primary_names(tmp_path):
    database = ensure_chat_history_db(tmp_path / 'chat_history.sqlite3')
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE restaurant_names (name TEXT PRIMARY KEY, "primary" INTEGER)')
        db.executemany('INSERT INTO restaurant_names VALUES (?, ?)', [
            ('River Bend', 0), ('River Bend Grill', 1), ('Distinct Cafe', 1),
        ])

    result = resolve_routed_answer(
        'CDLA', 'What Restaurants do you currently support', database,
    )

    assert result['text'] == (
        'The restaurants currently supported are:\nDistinct Cafe\nRiver Bend Grill'
    )


def test_chat_history_schema_adds_route_fields_without_backfilling(tmp_path):
    path = tmp_path / 'chat_history.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('''
            CREATE TABLE chat_history (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 1,
                created_date TEXT NOT NULL DEFAULT (CURRENT_DATE),
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        db.execute("INSERT INTO chat_history(topic, question, answer) VALUES ('CDLA', 'Old question?', 'Old answer.')")

    ensure_chat_history_db(path)

    with sqlite3.connect(path) as db:
        row = db.execute('SELECT routerFunction, "key" FROM chat_history WHERE ID = 1').fetchone()
    assert row == (None, None)


def test_qdrant_topics_sync_to_sqlite_preserves_ids_and_fields(tmp_path, store):
    source_path = tmp_path / 'PHP.jsonl'
    source_record = {
        'question': 'What is PHP?',
        'answer': 'PHP is a scripting language.',
        'url': '',
        'source': 'user_additions',
        'routerFunction': 'php_lookup',
        'key': 2,
    }
    source_path.write_text(json.dumps(source_record) + '\n', encoding='utf-8')
    record = list(read_records(source_path))[0]
    assert record['routerFunction'] == 'php_lookup'
    assert record['key'] == 2
    doc = list(documents(record))[0]
    store.upsert('PHP', [doc], [[1.0, 0.0, 0.0]])

    db_path = tmp_path / 'chat_history.sqlite3'
    assert sync_qdrant_topics_to_sqlite(store, db_path) == {'PHP': 1}
    point = store.client.retrieve(store.name('PHP'), [doc['id']], with_payload=True, with_vectors=True)[0]
    with sqlite3.connect(db_path) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(topic_data)')}
        row = db.execute(
            'SELECT topic, id, vector, question, answer, text, url, source, embedding_model, routerFunction, "key" FROM topic_data'
        ).fetchone()

    assert columns == {
        'topic', 'id', 'vector', 'question', 'answer', 'text', 'url', 'source',
        'embedding_model', 'routerFunction', 'key',
    }
    assert row[0] == 'PHP'
    assert row[1] == str(point.id) == doc['id']
    assert json.loads(row[2]) == point.vector
    assert row[3:] == (
        'What is PHP?', 'PHP is a scripting language.', 'PHP is a scripting language.',
        '', 'PHP.jsonl', 'test-embed', 'php_lookup', 2,
    )


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
        ('How many seconds does a 40-foot truck traveling at 50 miles per hour need to stop?', 40, 50, 5),
        ('How long to stop a 70-foot rig at 60 mph?', 70, 60, 8),
        ('How long would it take a 70-foot tractor-trailer going 60 mph to come to a complete stop?', 70, 60, 8),
        ('If my combination vehicle is 65 feet long and I am driving 55 mph, what is my stopping time in seconds?', 65, 55, 7.5),
        ('A 30-foot truck is traveling at 35 mph. How many seconds will it take to stop?', 30, 35, 3),
        ('How quickly can an 80-foot combination stop from 65 miles per hour?', 80, 65, 9),
        ('What is the total stopping time for a 50-foot truck moving at 45 mph?', 50, 45, 6),
        ('How many seconds should a 70 foot rig follow at 40 MPH?', 70, 40, 7),
        ('If I am driving a 40 foot righ going 70 MPH how long will it take me to stop', 40, 70, 5),
        ('What following time for a 70-ft truck at 41 mph?', 70, 41, 8),
        ('How long to stop a 70\u2011foot truck at 60 mph?', 70, 60, 8),
        ('If I am going 70 MPH with a 50 foot truck what is stopping distance?', 50, 70, 6),
        ('What safe following distance for a 70 ft truck at 40 mph?', 70, 40, 7),
        ('How many seconds should a 70-foot rig follow at 40 mi/h?', 70, 40, 7),
        ('What following time does a 70-ft truck need at 41 mph? Include the formula.', 70, 41, 8),
    ]
    for question, length, speed, seconds in cases:
        result = answer_following_distance(question)
        assert result.handled
        assert result.length_feet == length
        assert result.speed_mph == pytest.approx(speed)
        assert result.following_gap_seconds == seconds
        assert result.formula in result.answer
        assert 'not the physical time needed to stop' in result.answer
        assert result.answer.endswith('by 10, then add 1 second if speed is over 40 mph.')

    fallbacks = [
        'What is minimum tire tread depth?',
        'How long does a truck take to stop at 60 mph?',
        'How long to stop a 70-foot truck?',
        'My truck is 70 feet long and travels at 60 mph.',
        'What gear should a 70-foot truck use at 60 mph?',
        'Why does a 70-foot truck take time to stop at 60 mph?',
        'How long to stop a 70-foot truck at 40 mph or 60 mph?',
        'How long to stop a 70-foot truck at 40 or 60 mph?',
        'How long to stop a 70-foot trailer with a 20-foot tractor at 60 mph?',
        'How long to stop a 70-foot truck at 60 mph? What about tires?',
        'How long to stop a 70-foot truck at 60 km/h?',
        'How long to stop a 70-foot truck at 60 mph and what gear should I use?',
        'How long to stop a seventy-foot truck at sixty mph?',
        'How long to stop a -70-foot truck at 60 mph?',
        'How long to stop a 70-foot truck at -60 mph?',
        'How long to stop a 70-foot truck at 0 mph?',
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
            question = 'If I am going 70 MPH with a 50 foot truck what is stopping distance?'
            ws.send_json({'topic': 'PHP', 'question': question})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'done':
                    break

    answer = next(event['text'] for event in events if event['type'] == 'token')
    assert '50 / 10 + 1 = 6 seconds' in answer
    with sqlite3.connect(tmp_path / 'database' / 'chat_history.sqlite3') as db:
        row = db.execute('SELECT question, answer FROM chat_history').fetchone()
    assert row == (question, answer)


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
            'SELECT ID, topic, question, answer, accepted, created_date, created_at, routerFunction, "key" FROM chat_history ORDER BY ID DESC LIMIT 1'
        ).fetchone()

    assert row[0] >= 1
    assert row[1] == 'PHP'
    assert embed_inputs == ['What about PHP?']
    assert row[2] == 'What about PHP?'
    assert row[3] == 'PHP is used for web development [1].'
    assert row[4] == 1
    assert row[5]
    assert row[6]
    assert row[7:] == (None, None)


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
    assert first_data['items'][0]['routerFunction'] is None
    assert first_data['items'][0]['key'] is None
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
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 50000)) as client:
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

        blocked_origin = client.delete(
            '/api/responses/1',
            headers={'host': '127.0.0.1:8000', 'origin': 'https://example.com'},
        )
        blocked_host = client.delete(
            '/api/responses/1',
            headers={'host': 'contributing-campaigns-glad-cookbook.trycloudflare.com',
                     'origin': 'http://127.0.0.1:8000'},
        )
        local_headers = {'host': '127.0.0.1:8000', 'origin': 'http://127.0.0.1:8000'}
        deleted = client.delete('/api/responses/1', headers=local_headers)
        missing = client.delete('/api/responses/1', headers=local_headers)
        deleted_without_matching_correction = client.delete(
            '/api/responses/3', headers=local_headers,
        )
        remaining = client.get('/api/responses')

    assert blocked_origin.status_code == 403
    assert blocked_host.status_code == 403
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
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 8000)) as client:
        options = client.get('/api/router-functions')
        assert options.json()['routerFunctions'] == [
            '', 'Pre-trip Text', 'Pre-trip Video', 'Closest Loves', 'Closest Roadys',
            'Closest McDonalds', 'Closest Restaurant', 'Supported Restaurants', 'Open Directions',
        ]
        response = client.post('/api/train', headers={'origin': 'http://127.0.0.1:8000'}, json={
            'topic': 'PHP',
            'question': 'What is PHP?',
            'answer': 'PHP is a general-purpose scripting language for web development.',
            'routerFunction': 'Supported Restaurants',
            'key': '',
        })
        assert response.status_code == 200
        payload = response.json()
        assert payload['ok'] is True

    saved = tmp_path / 'topic_data' / 'user_additions' / 'PHP.jsonl'
    assert saved.exists()
    assert 'PHP is a general-purpose scripting language for web development.' in saved.read_text(encoding='utf-8')
    saved_record = json.loads(saved.read_text(encoding='utf-8').splitlines()[0])
    assert (saved_record['routerFunction'], saved_record['key']) == ('Supported Restaurants', '')
    doc = list(documents(saved_record))[0]
    point = store.client.retrieve(store.name('PHP'), [doc['id']], with_payload=True)[0]
    assert (point.payload['routerFunction'], point.payload['key']) == ('Supported Restaurants', '')
    sync_qdrant_topic_to_sqlite(store, 'PHP', tmp_path / 'database' / 'chat_history.sqlite3')
    with sqlite3.connect(tmp_path / 'database' / 'chat_history.sqlite3') as db:
        route, key = db.execute(
            'SELECT routerFunction, "key" FROM topic_data WHERE topic = ? AND id = ?',
            ('PHP', doc['id']),
        ).fetchone()
    assert (route, key) == ('Supported Restaurants', '')
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
        'routerFunction': 'Pre-trip Video',
        'key': 2,
    }
    with TestClient(app) as client:
        first = client.post('/api/train', headers={'origin': 'http://127.0.0.1:8000'}, json=payload)
        second = client.post('/api/train', headers={'origin': 'http://127.0.0.1:8000'}, json={
            'topic': 'PHP',
            'question': 'What is PHP? ',
            'answer': 'PHP is a server-side scripting language for web development.',
            'routerFunction': 'Pre-trip Video',
            'key': 2,
        })
        third = client.post('/api/train', headers={'origin': 'http://127.0.0.1:8000'}, json={
            **payload,
            'question': ' WHAT IS PHP? ',
            'answer': 'PHP is a server-side scripting language for web development.',
        })

    assert first.json()['saved'] > 0
    assert second.json()['updated'] is True
    assert third.json()['duplicate'] is True
    saved = tmp_path / 'topic_data' / 'user_additions' / 'PHP.jsonl'
    saved_lines = [line for line in saved.read_text(encoding='utf-8').splitlines() if line.strip()]
    assert len(saved_lines) == 1
    assert 'PHP is a server-side scripting language for web development.' in saved_lines[0]
    saved_record = json.loads(saved_lines[0])
    assert (saved_record['routerFunction'], saved_record['key']) == ('Pre-trip Video', 2)
    results = store.search('PHP', [1.0, 0.0, 0.0])
    assert any(item['text'] == 'PHP is a server-side scripting language for web development.' for item in results)
    assert not any(item['text'] == 'PHP is a general-purpose scripting language for web development.' for item in results)


def test_training_updates_all_questions_with_same_router_key(tmp_path, monkeypatch, settings, store):
    monkeypatch.setattr('backend.core.ROOT', tmp_path)
    monkeypatch.setattr('backend.app.ROOT', tmp_path)
    (tmp_path / 'web').mkdir()
    records = [
        {'question': 'How do I perform step 1 of pre-trip inspection?', 'answer': 'Old brake answer.',
         'url': '', 'source': 'user_additions', 'routerFunction': 'Pre-trip Text', 'key': 1},
        {'question': 'How do I do step one of the pre-trip inspection?', 'answer': 'Old brake answer.',
         'url': '', 'source': 'user_additions', 'routerFunction': 'Pre-trip Text', 'key': 1},
        {'question': 'How do I perform step 2 of pre-trip inspection?', 'answer': 'Keep this answer.',
         'url': '', 'source': 'user_additions', 'routerFunction': 'Pre-trip Text', 'key': 2},
    ]
    additions_path = tmp_path / 'topic_data' / 'user_additions' / 'CDLA.jsonl'
    additions_path.parent.mkdir(parents=True)
    additions_path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
    old_docs = [doc for record in records for doc in documents(record)]
    store.upsert('CDLA', old_docs, [[1.0, 0.0, 0.0]] * len(old_docs))

    def handle(request):
        if request.url.path == '/api/embed':
            count = len(json.loads(request.content)['input'])
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]] * count})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        raise AssertionError(f'unexpected request to {request.url.path}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    new_answer = 'Inspection of: Air or Hydraulic brakes\n\nThe updated brake inspection.'
    with TestClient(create_app(settings, store, ollama)) as client:
        response = client.post('/api/train', headers={'origin': 'http://127.0.0.1:8000'}, json={
            'topic': 'CDLA',
            'question': records[0]['question'],
            'answer': new_answer,
            'routerFunction': 'Pre-trip Text',
            'key': 1,
        })

    assert response.status_code == 200
    routed_answer = resolve_routed_answer(
        'CDLA', records[1]['question'], tmp_path / 'database' / 'chat_history.sqlite3',
    )
    assert routed_answer['text'] == new_answer
    saved_records = [json.loads(line) for line in additions_path.read_text(encoding='utf-8').splitlines()]
    assert [record['answer'] for record in saved_records] == [new_answer, new_answer, 'Keep this answer.']
    old_ids = [doc['id'] for record in records[:2] for doc in documents(record)]
    assert store.existing('CDLA', old_ids) == set()
    updated_docs = [doc for record in saved_records[:2] for doc in documents(record)]
    points = store.client.retrieve(store.name('CDLA'), [doc['id'] for doc in updated_docs], with_payload=True)
    assert len(points) == len(updated_docs)
    assert all(point.payload['answer'] == new_answer for point in points)
    with sqlite3.connect(tmp_path / 'database' / 'chat_history.sqlite3') as db:
        answers = db.execute(
            'SELECT answer FROM topic_data WHERE routerFunction=? AND "key"=1 ORDER BY question',
            ('Pre-trip Text',),
        ).fetchall()
    assert [answer[0] for answer in answers] == [new_answer, new_answer]


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


def test_ask_and_train_page_serves_local_page(settings, store):
    app = create_app(settings, store, httpx.AsyncClient())
    with TestClient(app) as client:
        response = client.get('/ask-and-train/')

    assert response.status_code == 200
    assert '<title>AskAndTrain</title>' in response.text


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


def test_saved_correction_reload_updates_router_function_payload(tmp_path, monkeypatch, settings, store):
    record = {
        'question': 'What is PHP?',
        'answer': 'PHP is a general-purpose scripting language for web development.',
        'url': '',
        'source': 'user_additions',
    }
    doc = list(documents(record))[0]
    store.upsert('PHP', [doc], [[1.0, 0.0, 0.0]])
    additions = tmp_path / 'topic_data' / 'user_additions'
    additions.mkdir(parents=True)
    (additions / 'PHP.jsonl').write_text(
        json.dumps({**record, 'routerFunction': 'php_lookup', 'key': 2}) + '\n',
        encoding='utf-8',
    )
    monkeypatch.setattr('backend.app.ROOT', tmp_path)

    def handle(request):
        raise AssertionError(f'unexpected embedding request to {request.url.path}')

    async def reload():
        async with httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle)) as ollama:
            await _reload_saved_corrections(store, ollama, settings)

    asyncio.run(reload())
    point = store.client.retrieve(store.name('PHP'), [doc['id']], with_payload=True)[0]
    assert point.payload['routerFunction'] == 'php_lookup'
    assert point.payload['key'] == 2


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


@pytest.mark.parametrize(('spoken_step', 'step_number'), [('1', 1), ('two', 2), ('twenty-two', 22)])
def test_pre_trip_text_route_uses_original_question_and_case_insensitive_sqlite(
    tmp_path, monkeypatch, settings, store, spoken_step, step_number):
    seeded_store(store)
    store.upsert('CDLA', list(documents({'question': 'pre-trip placeholder', 'answer': 'placeholder', 'url': ''})), [[1.0, 0.0, 0.0]])
    monkeypatch.setattr('backend.core.ROOT', tmp_path)
    monkeypatch.setattr('backend.app.ROOT', tmp_path)
    database_path = tmp_path / 'database' / 'chat_history.sqlite3'
    (tmp_path / 'web').mkdir()
    ensure_chat_history_db(database_path)
    with sqlite3.connect(database_path) as db:
        db.execute('''
            INSERT INTO topic_data(topic, id, vector, question, answer, text, url, source,
                                   embedding_model, routerFunction, "key")
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'cdla', f'step-{step_number}-stale-point', '[]',
            f'How do I do step {step_number} of the pre-trip inspection?',
            f'Stale step {step_number} answer.', f'Stale step {step_number} answer.',
            '', 'user_additions', 'test-embed', 'pre-trip text', step_number,
        ))
        question = f'How do I perform step {spoken_step} of the pre trip inspection?'
        db.execute('''
            INSERT INTO topic_data(topic, id, vector, question, answer, text, url, source,
                                   embedding_model, routerFunction, "key")
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'cdla', f'step-{step_number}-point', '[]', question,
            f'Inspect the identified step {step_number} item.',
            f'Inspect the identified step {step_number} item.',
            '', 'user_additions', 'test-embed', 'pre-trip text', step_number,
        ))

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        raise AssertionError(f'unexpected Ollama request to {request.url.path}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    with TestClient(create_app(settings, store, ollama)) as client:
        with client.websocket_connect('/ws/chat', headers={'origin': 'http://localhost:8000'}) as ws:
            ws.send_json({'topic': 'CDLA', 'question': question})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'done':
                    break
            history_item = client.get('/api/responses').json()['items'][0]

    assert any(event.get('text') == f'Inspect the identified step {step_number} item.' for event in events)
    assert any(event['type'] == 'sources' and event['sources'][0]['point_id'] == f'step-{step_number}-point' for event in events)
    assert not any(event['type'] == 'error' for event in events)
    with sqlite3.connect(database_path) as db:
        history_route = db.execute('SELECT routerFunction, "key" FROM chat_history').fetchone()
    assert history_route == ('Pre-trip Text', step_number)
    assert history_item['routerFunction'] == 'Pre-trip Text'
    assert history_item['key'] == step_number


def test_pre_trip_text_out_of_range_step_does_not_use_nearest_qdrant_result(tmp_path, monkeypatch, settings, store):
    seeded_store(store)
    step_38 = {
        'question': 'How do I perform Step 38 of the pre-trip inspection?',
        'answer': 'This is the misleading Step 38 answer.',
        'url': '',
    }
    store.upsert('CDLA', list(documents(step_38)), [[1.0, 0.0, 0.0]])
    monkeypatch.setattr('backend.core.ROOT', tmp_path)
    monkeypatch.setattr('backend.app.ROOT', tmp_path)
    (tmp_path / 'web').mkdir()

    def handle(request):
        if request.url.path == '/api/embed':
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0, 0.0]]})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'test'}, {'name': 'test-embed:latest'}]})
        raise AssertionError(f'unexpected Ollama request to {request.url.path}')

    ollama = httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handle))
    expected = 'There is no Step 40 in the pre-trip inspection. Valid steps are 1 through 39.'
    with TestClient(create_app(settings, store, ollama)) as client:
        with client.websocket_connect('/ws/chat', headers={'origin': 'http://localhost:8000'}) as ws:
            ws.send_json({'topic': 'CDLA', 'question': 'How do I perform step 40 of the pre-trip inspection?'})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'done':
                    break

    assert any(event.get('text') == expected for event in events)
    assert not any(event.get('text') == 'This is the misleading Step 38 answer.' for event in events)


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


def test_speak_caches_wav_by_voice_and_playback_rate(tmp_path, monkeypatch, settings, store):
    (tmp_path / 'web').mkdir()
    monkeypatch.setattr('backend.core.ROOT', tmp_path)
    monkeypatch.setattr('backend.app.ROOT', tmp_path)
    piper_calls = []
    piper_config = {'model': 'en_US-ryan-low.onnx', 'output_rate_scale': 0.9}

    def handle(request):
        if request.url.path == '/health':
            return httpx.Response(200, json=piper_config)
        if request.url.path == '/speak':
            piper_calls.append(request)
            audio_marker = f"{piper_config['model']}:{piper_config['output_rate_scale']}"
            return httpx.Response(200, content=audio_marker.encode(), headers={'content-type': 'audio/wav'})
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
        assert first.content == second.content == b'en_US-ryan-low.onnx:0.9'
        assert len(piper_calls) == 1
        piper_config['output_rate_scale'] = 0.85
        changed_rate = client.post('/api/speak', json={'text': 'Hello there'})
        assert changed_rate.content == b'en_US-ryan-low.onnx:0.85'
        assert len(piper_calls) == 2
        piper_config['model'] = 'alternate-test-voice.onnx'
        changed_voice = client.post('/api/speak', json={'text': 'Hello there'})
        assert changed_voice.content == b'alternate-test-voice.onnx:0.85'
        assert len(piper_calls) == 3
        client.post('/api/speak', json={'text': 'Other text'})
        assert len(piper_calls) == 4

    with sqlite3.connect(db_path) as db:
        rows = db.execute('SELECT hash, text FROM tts_cache ORDER BY text').fetchall()
    assert [r[1] for r in rows] == ['Hello there', 'Hello there', 'Hello there', 'Other text']
    assert all((tmp_path / 'database' / 'tts_cache' / f'{r[0]}.wav').is_file() for r in rows)
