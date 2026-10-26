import json
import sqlite3

from scripts.ingest_mcdonalds import ingest_files, normalize_element


def test_normalize_element_supports_node_and_center_coordinates():
    timestamp = '2026-10-24T04:53:31Z'
    imported_at = '2026-10-24T05:00:00+00:00'
    node = normalize_element({
        'type': 'node',
        'id': 10,
        'lat': 37.5,
        'lon': -122.2,
        'tags': {
            'addr:housenumber': '1',
            'addr:street': 'Main St',
            'addr:city': 'Sample City',
            'addr:postcode': '12345',
            'addr:state': 'CA',
            'opening_hours': 'Mo-Su 06:00-23:00',
            'opening_hours:drive_through': '24/7',
            'name': "McDonald's",
        },
    }, 'CA', timestamp, imported_at)
    way = normalize_element({
        'type': 'way',
        'id': 20,
        'center': {'lat': 35.0, 'lon': -90.0},
        'tags': {'addr:city': 'Other City'},
    }, 'AR', timestamp, imported_at)

    assert node['address'] == '1 Main St, Sample City, CA, 12345'
    assert (node['latitude'], node['longitude']) == (37.5, -122.2)
    assert node['opening_hours'] == 'Mo-Su 06:00-23:00'
    assert node['drive_through_hours'] == '24/7'
    assert (way['latitude'], way['longitude']) == (35.0, -90.0)
    assert way['state'] == 'AR'


def test_ingest_files_upserts_records_with_osm_metadata(tmp_path):
    source_dir = tmp_path / 'states'
    source_dir.mkdir()
    source = source_dir / 'mcdonalds_TX.json'
    source.write_text(json.dumps({
        'osm3s': {'timestamp_osm_base': '2026-10-24T04:53:31Z'},
        'elements': [{
            'type': 'way',
            'id': 42,
            'center': {'lat': 31.0, 'lon': -97.0},
            'tags': {'addr:city': 'Austin', 'opening_hours': 'Mo-Su 05:00-23:00'},
        }],
    }), encoding='utf-8')
    db_path = tmp_path / 'chat_history.sqlite3'

    counts = ingest_files(source_dir, db_path)
    ingest_files(source_dir, db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute('''
            SELECT osm_type, osm_id, source_state, state, city, latitude, longitude,
                   opening_hours, osm_timestamp, tags_json
            FROM mcdonalds_locations
        ''').fetchall()
    assert counts == {'TX': 1}
    assert len(rows) == 1
    assert rows[0][:8] == ('way', 42, 'TX', 'TX', 'Austin', 31.0, -97.0, 'Mo-Su 05:00-23:00')
    assert rows[0][8] == '2026-10-24T04:53:31Z'
    assert json.loads(rows[0][9])['addr:city'] == 'Austin'