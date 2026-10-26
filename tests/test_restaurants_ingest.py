import io
import json
import sqlite3

from scripts.ingest_restaurants import ProgressReporter, ingest_files, normalize_element


def test_normalize_element_supports_node_and_center_coordinates():
    common = ('mcdonalds', 'TX', '2026-10-24T04:53:31Z', 'mcdonalds_states/mcdonalds_TX.json', 'now')
    node = normalize_element({
        'type': 'node',
        'id': 10,
        'lat': 37.5,
        'lon': -122.2,
        'tags': {
            'addr:city': 'Sample City',
            'addr:housenumber': '1',
            'addr:postcode': '12345',
            'addr:street': 'Main St',
            'amenity': 'fast_food',
            'opening_hours': 'Mo-Su 06:00-23:00',
            'phone': '555-0100',
            'name': "McDonald's",
        },
    }, *common)
    way = normalize_element({
        'type': 'way',
        'id': 20,
        'center': {'lat': 35.0, 'lon': -90.0},
        'tags': {'addr:city': 'Other City'},
    }, *common)
    incomplete = normalize_element({'type': 'node', 'id': 30}, *common)

    assert node['chain'] == 'mcdonalds'
    assert node['address'] == '1 Main St, Sample City, TX, 12345'
    assert (node['latitude'], node['longitude']) == (37.5, -122.2)
    assert node['opening_hours'] == 'Mo-Su 06:00-23:00'
    assert node['phone'] == '555-0100'
    assert (way['latitude'], way['longitude']) == (35.0, -90.0)
    assert incomplete['tags_json'] == '{}'
    assert incomplete['opening_hours'] is None
    assert (incomplete['latitude'], incomplete['longitude']) == (None, None)


def test_ingest_files_keeps_chains_separate_and_upserts_with_filters(tmp_path):
    source_dir = tmp_path / 'sources'
    mcdonalds_dir = source_dir / 'mcdonalds_states'
    starbucks_dir = source_dir / 'starbucks_states'
    mcdonalds_dir.mkdir(parents=True)
    starbucks_dir.mkdir(parents=True)
    shared_node = {
        'type': 'node',
        'id': 42,
        'lat': 31.0,
        'lon': -97.0,
        'tags': {'amenity': 'fast_food', 'name': 'Example', 'brand': 'Example', 'addr:city': 'Austin'},
    }
    (mcdonalds_dir / 'mcdonalds_TX.json').write_text(json.dumps({
        'osm3s': {'timestamp_osm_base': '2026-10-24T04:53:31Z'},
        'elements': [shared_node, {
            'type': 'way',
            'id': 43,
            'center': {'lat': 30.0, 'lon': -98.0},
            'tags': {'amenity': 'restaurant', 'name': "McDonald's"},
        }],
    }), encoding='utf-8')
    (starbucks_dir / 'starbucks_TX.json').write_text(json.dumps({
        'elements': [shared_node],
    }), encoding='utf-8')
    db_path = tmp_path / 'restaurants.sqlite3'

    progress_output = io.StringIO()
    counts = ingest_files(source_dir, db_path, progress=ProgressReporter(progress_output))
    ingest_files(source_dir, db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute('''
            SELECT chain, osm_type, osm_id, latitude, longitude, city, osm_timestamp
            FROM restaurants ORDER BY chain, osm_type, osm_id
        ''').fetchall()
        indexes = {row[1] for row in conn.execute('PRAGMA index_list(restaurants)')}

    assert counts == {('mcdonalds', 'TX'): 2, ('starbucks', 'TX'): 1}
    assert 'Found 2 source files' in progress_output.getvalue()
    assert '50%' in progress_output.getvalue()
    assert '100%' in progress_output.getvalue()
    assert '1/2 (50%)' in progress_output.getvalue()
    assert '2/2 (100%)' in progress_output.getvalue()
    assert len(rows) == 3
    assert rows[0] == ('mcdonalds', 'node', 42, 31.0, -97.0, 'Austin', '2026-10-24T04:53:31Z')
    assert rows[1][:5] == ('mcdonalds', 'way', 43, 30.0, -98.0)
    assert rows[2][:5] == ('starbucks', 'node', 42, 31.0, -97.0)
    assert 'idx_restaurants_chain_coordinates' in indexes
    assert conn.execute('SELECT name FROM restaurant_names ORDER BY name').fetchall() == [
        ('Example',), ("McDonald's",),
    ]

    filtered_counts = ingest_files(source_dir, db_path, states=['TX'], chains=['STARBUCKS'])
    assert filtered_counts == {('starbucks', 'TX'): 1}


def test_ingest_migrates_existing_names_and_marks_longest_chain_name_primary(tmp_path):
    source_dir = tmp_path / 'sources'
    records = {
        '7_brew': ['7 Brew', '7-Brew Coffee', '7Brew', '7Brew Coffee'],
        'a_and_w': ['A&W (USA)', 'A&W Seafood Restaurant', 'A & W'],
        'arby_s': ["Arby's", "Arby's (closed)", 'Arby’s', 'Arbys'],
        'schlotzsky_s': ["Arby's"],
    }
    for chain, names in records.items():
        chain_dir = source_dir / f'{chain}_states'
        chain_dir.mkdir(parents=True)
        (chain_dir / f'{chain}_TX.json').write_text(json.dumps({
            'elements': [
                {'type': 'node', 'id': index, 'tags': {'name': name}}
                for index, name in enumerate(names, 1)
            ],
        }), encoding='utf-8')
    db_path = tmp_path / 'restaurants.sqlite3'
    with sqlite3.connect(db_path) as conn:
        conn.execute('CREATE TABLE restaurant_names (name TEXT PRIMARY KEY)')

    ingest_files(source_dir, db_path)

    with sqlite3.connect(db_path) as conn:
        assert dict(conn.execute(
            'SELECT name, "primary" FROM restaurant_names'
        ).fetchall()) == {
            '7 Brew': 0,
            '7-Brew Coffee': 1,
            '7Brew': 0,
            '7Brew Coffee': 0,
            'A & W': 0,
            'A&W (USA)': 0,
            'A&W Seafood Restaurant': 1,
            "Arby's": 1,
            "Arby's (closed)": 0,
            'Arby’s': 0,
            'Arbys': 0,
        }