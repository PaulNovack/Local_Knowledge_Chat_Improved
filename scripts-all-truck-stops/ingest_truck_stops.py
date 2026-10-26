#!/usr/bin/env python3
"""Import truck-stop OpenStreetMap JSON exports into SQLite.

Examples:
    python scripts-all-truck-stops/ingest_truck_stops.py --states TX
    python scripts-all-truck-stops/ingest_truck_stops.py --source-dir /path/to/state-json-files

OSM data attribution: https://www.openstreetmap.org/copyright
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

DEFAULT_SOURCE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = Path(__file__).resolve().parents[1] / 'database' / 'chat_history.sqlite3'
STATE_SUFFIX = re.compile(r'_([A-Z]{2})$')
COLUMNS = (
    'osm_type', 'osm_id', 'source_state', 'state', 'name', 'brand', 'latitude',
    'longitude', 'address', 'house_number', 'street', 'city', 'postal_code',
    'country', 'phone', 'website', 'amenity', 'highway', 'opening_hours',
    'hgv_access', 'hgv_parking', 'truck_parking', 'fuel_diesel',
    'fuel_hgv_diesel', 'tags_json', 'osm_timestamp', 'source_file', 'imported_at',
)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS truck_stops (
    osm_type TEXT NOT NULL CHECK(osm_type IN ('node', 'way', 'relation')),
    osm_id INTEGER NOT NULL,
    source_state TEXT NOT NULL,
    state TEXT,
    name TEXT,
    brand TEXT,
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
    amenity TEXT,
    highway TEXT,
    opening_hours TEXT,
    hgv_access TEXT,
    hgv_parking TEXT,
    truck_parking TEXT,
    fuel_diesel TEXT,
    fuel_hgv_diesel TEXT,
    tags_json TEXT NOT NULL,
    osm_timestamp TEXT,
    source_file TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (osm_type, osm_id)
);
CREATE INDEX IF NOT EXISTS idx_truck_stops_coordinates
    ON truck_stops(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_truck_stops_state
    ON truck_stops(state);
CREATE INDEX IF NOT EXISTS idx_truck_stops_name
    ON truck_stops(name);
CREATE TABLE IF NOT EXISTS truck_stop_names (
    name TEXT PRIMARY KEY
);
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


def normalize_element(element, source_state, osm_timestamp, source_file, imported_at):
    if not isinstance(element, dict):
        raise ValueError('OSM element must be an object')
    osm_type, osm_id = element.get('type'), element.get('id')
    if osm_type not in ('node', 'way', 'relation') or isinstance(osm_id, bool) or not isinstance(osm_id, int):
        raise ValueError('OSM element needs a supported type and integer id')
    tags = element.get('tags')
    if not isinstance(tags, dict):
        tags = {}

    state = _tag(tags, 'addr:state') or source_state
    house_number = _tag(tags, 'addr:housenumber')
    street = _tag(tags, 'addr:street') or _tag(tags, 'addr:place')
    address = ', '.join(part for part in (
        ' '.join(part for part in (house_number, street) if part),
        _tag(tags, 'addr:city'), state, _tag(tags, 'addr:postcode'),
    ) if part) or None
    latitude, longitude = _coordinates(element)
    return {
        'osm_type': osm_type,
        'osm_id': osm_id,
        'source_state': source_state,
        'state': state,
        'name': _tag(tags, 'name'),
        'brand': _tag(tags, 'brand'),
        'latitude': latitude,
        'longitude': longitude,
        'address': address,
        'house_number': house_number,
        'street': street,
        'city': _tag(tags, 'addr:city'),
        'postal_code': _tag(tags, 'addr:postcode'),
        'country': _tag(tags, 'addr:country'),
        'phone': _tag(tags, 'phone') or _tag(tags, 'contact:phone'),
        'website': _tag(tags, 'website') or _tag(tags, 'contact:website'),
        'amenity': _tag(tags, 'amenity'),
        'highway': _tag(tags, 'highway'),
        'opening_hours': _tag(tags, 'opening_hours'),
        'hgv_access': _tag(tags, 'hgv'),
        'hgv_parking': _tag(tags, 'hgv:parking') or _tag(tags, 'parking:hgv'),
        'truck_parking': _tag(tags, 'capacity:hgv'),
        'fuel_diesel': _tag(tags, 'fuel:diesel'),
        'fuel_hgv_diesel': _tag(tags, 'fuel:hgv_diesel') or _tag(tags, 'fuel:HGV_diesel'),
        'tags_json': json.dumps(tags, ensure_ascii=False, sort_keys=True),
        'osm_timestamp': osm_timestamp,
        'source_file': source_file,
        'imported_at': imported_at,
    }


def discover_source_files(source_dir, states=None):
    source_dir = Path(source_dir)
    selected_states = {state.upper() for state in states} if states else None
    files = []
    for path in sorted(source_dir.rglob('truck_stops_*.json')):
        state_match = STATE_SUFFIX.search(path.stem)
        if not state_match:
            continue
        state = state_match[1]
        if selected_states is not None and state not in selected_states:
            continue
        files.append((path, state))
    if not files:
        requested = f" for states {', '.join(sorted(selected_states))}" if selected_states else ''
        raise ValueError(f'No truck-stop JSON files found{requested} in {source_dir}')
    return files


class ProgressReporter:
    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.interactive = self.stream.isatty()

    def start(self, total, db_path):
        self.total = total
        self.interval = max(1, math.ceil(self.total / 50))
        print(f'Found {self.total} source files. Importing into {db_path}', file=self.stream, flush=True)

    def update(self, index, state, filename, record_count):
        if not self.interactive and index % self.interval and index != self.total:
            return
        percent = round(index * 100 / self.total)
        filled = round(percent / 5)
        bar = '#' * filled + '-' * (20 - filled)
        message = f'[{bar}] {index}/{self.total} ({percent}%) truck_stops/{state}: {record_count} records'
        if self.interactive:
            print('\r' + f'{message} - {filename}'.ljust(120), end='', file=self.stream, flush=True)
            if index == self.total:
                print(file=self.stream, flush=True)
        else:
            print(f'{message} - {filename}', file=self.stream, flush=True)


def ingest_files(source_dir=DEFAULT_SOURCE_DIR, db_path=DEFAULT_DB, states=None, progress=None):
    source_dir = Path(source_dir)
    files = discover_source_files(source_dir, states)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    placeholders = ','.join('?' for _ in COLUMNS)
    updates = ','.join(
        f'{column}=excluded.{column}'
        for column in COLUMNS if column not in ('osm_type', 'osm_id')
    )
    sql = (
        f'INSERT INTO truck_stops ({",".join(COLUMNS)}) '
        f'VALUES ({placeholders}) ON CONFLICT(osm_type,osm_id) DO UPDATE SET {updates}'
    )
    counts = Counter()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        if progress:
            progress.start(len(files), db_path)
        for file_index, (path, source_state) in enumerate(files, 1):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f'Cannot read {path}: {exc}') from exc
            if not isinstance(data, dict) or not isinstance(data.get('elements'), list):
                raise ValueError(f'{path} must contain an elements array')
            osm3s = data.get('osm3s')
            timestamp = osm3s.get('timestamp_osm_base') if isinstance(osm3s, dict) else None
            imported_at = datetime.now(timezone.utc).isoformat()
            source_file = path.relative_to(source_dir).as_posix()
            names = set()
            for element in data['elements']:
                tags = element.get('tags') if isinstance(element, dict) else None
                if isinstance(tags, dict):
                    name = _tag(tags, 'name')
                    if name:
                        names.add(name)
            records = (
                normalize_element(element, source_state, timestamp, source_file, imported_at)
                for element in data['elements']
            )
            with conn:
                conn.executemany(sql, (
                    tuple(record[column] for column in COLUMNS) for record in records
                ))
                conn.executemany(
                    'INSERT OR IGNORE INTO truck_stop_names (name) VALUES (?)',
                    ((name,) for name in names),
                )
            counts[source_state] += len(data['elements'])
            if progress:
                progress.update(file_index, source_state, path.name, len(data['elements']))
        with conn:
            conn.execute('''
                INSERT OR IGNORE INTO truck_stop_names (name)
                SELECT DISTINCT name FROM truck_stops WHERE name IS NOT NULL
            ''')
            conn.execute('''
                DELETE FROM truck_stop_names
                WHERE name NOT IN (SELECT name FROM truck_stops WHERE name IS NOT NULL)
            ''')
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source-dir', type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='SQLite database path')
    parser.add_argument('--states', nargs='+', type=str.upper, help='Limit import to state abbreviations')
    args = parser.parse_args(argv)
    try:
        counts = ingest_files(args.source_dir, args.db, args.states, ProgressReporter())
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    for state, count in sorted(counts.items()):
        print(f'{state}: imported {count} truck stops')
    print(f'Total: imported {sum(counts.values())} truck stops into {args.db}')
    print('Data: OpenStreetMap contributors, https://www.openstreetmap.org/copyright')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())