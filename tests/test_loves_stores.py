from urllib.parse import parse_qs
import sqlite3

import httpx

from scripts.loves_stores_test import acquire_access_token, fetch_all_stores
from scripts.enrich_loves_locations import migrate, parse_detail, store_detail, structured_hours
from scripts.scrape_loves import SCHEMA, ensure_schema, parse_card, save


def test_loves_scraper_expands_amenities_into_columns_and_lookup_table(tmp_path):
    html = '''
    <article>
      <a href="/locations/123">Store #123</a>
      <div data-row="1"><ul><li>Exit 10</li><li>Austin, TX</li><li>I-35</li></ul></div>
      <a href="https://www.google.com/maps?query=30.1%2C-97.2">123 Main St 78701</a>
      <div data-row="2"></div><div data-row="3"></div>
    <div><img alt="amenity" src="/icons/shower.png"><span>Private Showers</span></div>
    <div><img alt="amenity" data-src="/icons/wifi.png"><span>Free Wi-Fi</span></div>
    </article>
    '''
    record = parse_card(html, 'TX', 'https://www.loves.com/search')
    database = tmp_path / 'chat_history.sqlite3'
    with sqlite3.connect(database) as conn:
        conn.executescript(SCHEMA)
        conn.execute('''
            INSERT INTO loves_locations (
                store_number, state, fuel_prices_json, amenities_json, restaurants_json,
                hours_json, payment_types_json, station_codes_json, sections_json,
                images_json, raw_text, raw_html, source_url, scraped_at
            ) VALUES ('old-store', 'TX', '{}', '["Private Showers"]', '[]', '{}',
                      '[]', '[]', '{}', '[]', '', '', '', '')
        ''')
        ensure_schema(conn)
        save(conn, record)
        migrated_flags = conn.execute(
            'SELECT has_private_showers, has_free_wifi FROM loves_locations WHERE store_number=?',
            ('old-store',),
        ).fetchone()
        flags = conn.execute(
            'SELECT has_private_showers, has_free_wifi, has_atm FROM loves_locations WHERE store_number=?',
            ('123',),
        ).fetchone()
        amenities = conn.execute(
            'SELECT amenity, icon_url FROM loves_location_amenities WHERE store_number=? ORDER BY amenity',
            ('123',),
        ).fetchall()

    assert migrated_flags == (1, 0)
    assert flags == (1, 1, 0)
    assert amenities == [
        ('Free Wi-Fi', 'https://www.loves.com/icons/wifi.png'),
        ('Private Showers', 'https://www.loves.com/icons/shower.png'),
    ]


def test_client_credentials_fetches_all_stores():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path == '/oauth/token':
            assert request.headers['authorization'].startswith('Basic ')
            assert parse_qs(request.content.decode()) == {'grant_type': ['client_credentials']}
            return httpx.Response(200, json={'access_token': 'test-token'})
        assert request.url == 'https://api.example.test/locations/api/stores'
        assert request.headers['authorization'] == 'Bearer test-token'
        return httpx.Response(200, json=[{'storeNumber': 123}, {'storeNumber': 456}])

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        token = acquire_access_token(
            client, 'https://api.example.test/oauth/token', 'client-id', 'client-secret',
        )
        stores = fetch_all_stores(client, 'https://api.example.test/locations/api/', token)

    assert len(seen) == 2
    assert stores == [{'storeNumber': 123}, {'storeNumber': 456}]


def test_detail_parser_extracts_facility_schedules_and_full_weekday_ranges():
        html = '''
        <main><h1>Love's Travel Stop</h1><article>
            <div class="store-number">Store #123</div>
            <div data-row="1"><ul><li>Exit 10</li><li>Austin, TX</li><li>I-35</li></ul></div>
            <a href="https://www.google.com/maps?query=30.1%2C-97.2">123 Main St 78701</a>
            <div data-row="3">
                <div><label>Store</label><ul>
                    <li>Monday-Friday: 8:00 AM - 8:00 PM</li><li>Saturday: Closed</li>
                </ul></div>
            </div>
        </article></main>
        '''
        item = {'number': '123', 'state': 'TX', 'url': 'https://www.loves.com/locations/tx/austin/store-123'}

        record, extra, facilities, restaurants = parse_detail(html, item)
        parsed = structured_hours(facilities[0])

        assert record['store_number'] == '123'
        assert facilities[0]['schedule'] == [
                'Monday-Friday: 8:00 AM - 8:00 PM', 'Saturday: Closed',
        ]
        assert [(row['weekday'], row['opens_at'], row['closes_at'], row['is_closed'])
                        for row in parsed if row['parse_status'] == 'parsed'] == [
                (0, '08:00', '20:00', 0), (1, '08:00', '20:00', 0),
                (2, '08:00', '20:00', 0), (3, '08:00', '20:00', 0),
                (4, '08:00', '20:00', 0), (5, None, None, 1),
        ]
        assert extra['store_hours'] == facilities[0]['hours']
        assert restaurants == []


def test_nameless_restaurant_block_does_not_fail_store_parse(caplog):
        html = '''
        <main><h1>Love's Travel Stop</h1><article>
            <div class="store-number">Store #466</div>
            <div data-row="1"><ul><li>Exit 10</li><li>Austin, TX</li><li>I-35</li></ul></div>
            <a href="https://www.google.com/maps?query=30.1%2C-97.2">123 Main St 78701</a>
            <div data-row="3">
                <div><label>Store</label><ul><li>Monday-Sunday: Open 24 hours</li></ul></div>
                <div class="restaurant-hours"><div>Hours are temporarily unavailable</div></div>
            </div>
        </article></main>
        '''
        item = {'number': '466', 'state': 'TX', 'url': 'https://www.loves.com/locations/tx/austin/store-466'}

        record, extra, facilities, restaurants = parse_detail(html, item)

        assert record['store_number'] == '466'
        assert extra['store_hours'] == 'Monday-Sunday: Open 24 hours'
        assert facilities[0]['schedule'] == ['Monday-Sunday: Open 24 hours']
        assert restaurants == []
        assert 'Skipping unnamed restaurant-hours block for store 466' in caplog.text


def test_missing_detail_hours_preserve_previous_normalized_rows(tmp_path):
    item = {'number': '123', 'state': 'TX', 'url': 'https://www.loves.com/locations/tx/austin/store-123'}
    initial_html = '''
    <main><h1>Love's Travel Stop</h1><article>
      <div class="store-number">Store #123</div>
      <div data-row="1"><ul><li>Exit 10</li><li>Austin, TX</li><li>I-35</li></ul></div>
      <a href="https://www.google.com/maps?query=30.1%2C-97.2">123 Main St 78701</a>
      <div data-row="3"><div><label>Store</label><ul>
        <li>Monday-Friday: 8:00 AM - 8:00 PM</li>
      </ul></div></div>
    </article></main>
    '''
    missing_hours_html = initial_html.replace(
        '<div data-row="3"><div><label>Store</label><ul>\n'
        '        <li>Monday-Friday: 8:00 AM - 8:00 PM</li>\n'
        '      </ul></div></div>',
        '',
    )
    with sqlite3.connect(tmp_path / 'stores.sqlite3') as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        migrate(conn)
        store_detail(conn, *parse_detail(initial_html, item))
        prior_rows = conn.execute(
            'SELECT facility_type, facility_name, hours_text, schedule_json '
            'FROM loves_location_hours WHERE store_number=?', ('123',),
        ).fetchall()
        prior_intervals = conn.execute(
            '''SELECT d.weekday, h.opens_at, h.closes_at
               FROM loves_facilities f
               JOIN loves_facility_hours h ON h.facility_id=f.id
               JOIN loves_facility_hour_days d ON d.hours_id=h.id
               WHERE f.store_number=? ORDER BY d.weekday''', ('123',),
        ).fetchall()

        store_detail(conn, *parse_detail(missing_hours_html, item))

        assert conn.execute(
            'SELECT facility_type, facility_name, hours_text, schedule_json '
            'FROM loves_location_hours WHERE store_number=?', ('123',),
        ).fetchall() == prior_rows
        assert conn.execute(
            '''SELECT d.weekday, h.opens_at, h.closes_at
               FROM loves_facilities f
               JOIN loves_facility_hours h ON h.facility_id=f.id
               JOIN loves_facility_hour_days d ON d.hours_id=h.id
               WHERE f.store_number=? ORDER BY d.weekday''', ('123',),
        ).fetchall() == prior_intervals