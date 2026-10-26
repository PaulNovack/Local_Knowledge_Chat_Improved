#!/usr/bin/env python3
"""Import downloaded McDonald's OpenStreetMap state JSON into SQLite.

OSM data is available under the ODbL. Attribution:
https://www.openstreetmap.org/copyright

Examples:
  python scripts/ingest_mcdonalds.py
  python scripts/ingest_mcdonalds.py --states TX LA OK
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3

DEFAULT_SOURCE_DIR = Path(__file__).resolve().parent / 'mcdonalds_states'
DEFAULT_DB = Path(__file__).resolve().parents[1] / 'database' / 'chat_history.sqlite3'
STATE_FILE = re.compile(r'^mcdonalds_([A-Z]{2})\.json$')
COLUMNS = (
    'osm_type', 'osm_id', 'source_state', 'state', 'name', 'latitude', 'longitude',
    'address', 'house_number', 'street', 'city', 'postal_code', 'country', 'phone',
    'website', 'opening_hours', 'drive_through_hours', 'drive_through', 'takeaway',
    'wheelchair', 'cuisine', 'tags_json', 'osm_timestamp', 'imported_at',
)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS mcdonalds_locations (
    osm_type TEXT NOT NULL CHECK(osm_type IN ('node', 'way', 'relation')),
    osm_id INTEGER NOT NULL,
    source_state TEXT NOT NULL,
    state TEXT,
    name TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    address TEXT,
    house_number TEXT,
    street TEXT,
    city TEXT,
    postal_code TEXT,
    country TEXT,
    phone TEXT,
    website TEXT,
    opening_hours TEXT,
    drive_through_hours TEXT,
    drive_through TEXT,
    takeaway TEXT,
    wheelchair TEXT,
    cuisine TEXT,
    tags_json TEXT NOT NULL,
    osm_timestamp TEXT,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (osm_type, osm_id)
);
CREATE INDEX IF NOT EXISTS idx_mcdonalds_locations_state
    ON mcdonalds_locations(state);
CREATE INDEX IF NOT EXISTS idx_mcdonalds_locations_coordinates
    ON mcdonalds_locations(latitude, longitude);
'''


def _tag(tags, key):
    value = tags.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _coordinates(element):
    point = element
    if element.get('lat') is None or element.get('lon') is None:
        point = element.get('center') or {}
    latitude, longitude = point.get('lat'), point.get('lon')
    if latitude is None or longitude is None:
        return None, None
    latitude, longitude = float(latitude), float(longitude)
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError('latitude must be between -90 and 90')
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError('longitude must be between -180 and 180')
    return latitude, longitude


def normalize_element(element, source_state, osm_timestamp, imported_at):
    if not isinstance(element, dict):
        raise ValueError('OSM element must be an object')
    osm_type, osm_id = element.get('type'), element.get('id')
    if osm_type not in ('node', 'way', 'relation') or isinstance(osm_id, bool) or not isinstance(osm_id, int):
        raise ValueError('OSM element needs a supported type and integer id')
    tags = element.get('tags')
    if not isinstance(tags, dict):
        raise ValueError(f'{osm_type} {osm_id} has no tags object')

    state = _tag(tags, 'addr:state') or source_state
    house_number = _tag(tags, 'addr:housenumber')
    street = _tag(tags, 'addr:street')
    city = _tag(tags, 'addr:city')
    postal_code = _tag(tags, 'addr:postcode')
    address_parts = [
        ' '.join(part for part in (house_number, street) if part),
        city, state, postal_code,
    ]
    address = ', '.join(part for part in address_parts if part) or None
    latitude, longitude = _coordinates(element)
    return {
        'osm_type': osm_type,
        'osm_id': osm_id,
        'source_state': source_state,
        'state': state,
        'name': _tag(tags, 'name') or "McDonald's",
        'latitude': latitude,
        'longitude': longitude,
        'address': address,
        'house_number': house_number,
        'street': street,
        'city': city,
        'postal_code': postal_code,
        'country': _tag(tags, 'addr:country'),
        'phone': _tag(tags, 'phone') or _tag(tags, 'contact:phone'),
        'website': _tag(tags, 'website') or _tag(tags, 'contact:website'),
        'opening_hours': _tag(tags, 'opening_hours'),
        'drive_through_hours': _tag(tags, 'opening_hours:drive_through'),
        'drive_through': _tag(tags, 'drive_through'),
        'takeaway': _tag(tags, 'takeaway'),
        'wheelchair': _tag(tags, 'wheelchair'),
        'cuisine': _tag(tags, 'cuisine'),
        'tags_json': json.dumps(tags, ensure_ascii=False, sort_keys=True),
        'osm_timestamp': osm_timestamp,
        'imported_at': imported_at,
    }


def read_source_files(source_dir=DEFAULT_SOURCE_DIR, states=None):
    selected_states = {state.upper() for state in states} if states else None
    files = []
    for path in sorted(Path(source_dir).glob('mcdonalds_*.json')):
        match = STATE_FILE.fullmatch(path.name)
        if match and (selected_states is None or match[1] in selected_states):
            files.append((path, match[1]))
    if not files:
        requested = ', '.join(sorted(selected_states)) if selected_states else 'any states'
        raise ValueError(f"No McDonald's JSON files found for {requested} in {source_dir}")

    imported_at = datetime.now(timezone.utc).isoformat()
    records, counts = [], Counter()
    for path, source_state in files:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f'Cannot read {path}: {exc}') from exc
        if not isinstance(data, dict) or not isinstance(data.get('elements'), list):
            raise ValueError(f'{path} must contain an elements array')
        osm3s = data.get('osm3s')
        timestamp = osm3s.get('timestamp_osm_base') if isinstance(osm3s, dict) else None
        for element in data['elements']:
            try:
                record = normalize_element(element, source_state, timestamp, imported_at)
            except (TypeError, ValueError) as exc:
                raise ValueError(f'Invalid element in {path}: {exc}') from exc
            records.append(record)
            counts[source_state] += 1
    return records, counts


def ingest_files(source_dir=DEFAULT_SOURCE_DIR, db_path=DEFAULT_DB, states=None):
    records, counts = read_source_files(source_dir, states)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    placeholders = ','.join('?' for _ in COLUMNS)
    updates = ','.join(
        f'{column}=excluded.{column}'
        for column in COLUMNS if column not in ('osm_type', 'osm_id')
    )
    sql = (
        f'INSERT INTO mcdonalds_locations ({",".join(COLUMNS)}) '
        f'VALUES ({placeholders}) ON CONFLICT(osm_type,osm_id) DO UPDATE SET {updates}'
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        with conn:
            conn.executemany(sql, [tuple(record[column] for column in COLUMNS) for record in records])
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source-dir', type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='SQLite database path')
    parser.add_argument('--states', nargs='+', type=str.upper, help='Limit import to state abbreviations')
    args = parser.parse_args(argv)
    try:
        counts = ingest_files(args.source_dir, args.db, args.states)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    for state, count in sorted(counts.items()):
        print(f'{state}: imported {count} locations')
    print(f'Total: imported {sum(counts.values())} locations into {args.db}')
    print('Data: OpenStreetMap contributors, https://www.openstreetmap.org/copyright')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())