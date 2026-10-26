import importlib.util
import json
import sqlite3
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts-all-truck-stops' / 'ingest_truck_stops.py'
SPEC = importlib.util.spec_from_file_location('truck_stops_ingest', MODULE_PATH)
ingest_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ingest_module)
DOWNLOADER_PATH = MODULE_PATH.with_name('download_truck_stops.py')
DOWNLOADER_SPEC = importlib.util.spec_from_file_location('truck_stops_downloader', DOWNLOADER_PATH)
downloader_module = importlib.util.module_from_spec(DOWNLOADER_SPEC)
DOWNLOADER_SPEC.loader.exec_module(downloader_module)


def test_ingest_creates_truck_stop_tables_and_is_idempotent(tmp_path):
    source_dir = tmp_path / 'sources'
    source_dir.mkdir()
    (source_dir / 'truck_stops_TX.json').write_text(json.dumps({
        'osm3s': {'timestamp_osm_base': '2026-10-24T00:00:00Z'},
        'elements': [{
            'type': 'node',
            'id': 42,
            'lat': 31.0,
            'lon': -97.0,
            'tags': {
                'addr:city': 'Austin',
                'amenity': 'fuel',
                'brand': "Love's Travel Stops",
                'fuel:diesel': 'yes',
                'hgv': 'designated',
                'name': "Love's Travel Stop #123",
                'phone': '555-0100',
            },
        }],
    }), encoding='utf-8')
    db_path = tmp_path / 'truck_stops.sqlite3'

    counts = ingest_module.ingest_files(source_dir, db_path, progress=None)
    repeated_counts = ingest_module.ingest_files(source_dir, db_path, progress=None)

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        row = conn.execute('''
            SELECT osm_type, osm_id, state, name, brand, amenity, hgv_access, fuel_diesel
            FROM truck_stops
        ''').fetchone()
        stop_names = conn.execute('SELECT name FROM truck_stop_names').fetchall()
        indexes = {row[1] for row in conn.execute('PRAGMA index_list(truck_stops)')}

    assert counts == {'TX': 1}
    assert repeated_counts == {'TX': 1}
    assert {'truck_stops', 'truck_stop_names'} <= tables
    assert row == ('node', 42, 'TX', "Love's Travel Stop #123", "Love's Travel Stops", 'fuel', 'designated', 'yes')
    assert stop_names == [("Love's Travel Stop #123",)]
    assert 'idx_truck_stops_coordinates' in indexes
    assert 'idx_truck_stops_state' in indexes


def test_scraper_uses_truck_stop_evidence_without_matching_generic_fuel():
    assert downloader_module.is_truck_stop({'amenity': 'truck_stop'})
    assert downloader_module.is_truck_stop({'highway': 'services'})
    assert downloader_module.is_truck_stop({
        'amenity': 'fuel',
        'brand': "Love's",
        'name': "Love's Travel Stop #123",
    })
    assert downloader_module.is_truck_stop({'amenity': 'fuel', 'hgv': 'designated'})
    assert downloader_module.is_truck_stop({'amenity': 'fuel', 'brand': 'TA'})
    assert not downloader_module.is_truck_stop({'amenity': 'fuel', 'name': 'Tangle Fuel'})
    assert not downloader_module.is_truck_stop({'amenity': 'fuel', 'name': 'Generic Fuel'})
