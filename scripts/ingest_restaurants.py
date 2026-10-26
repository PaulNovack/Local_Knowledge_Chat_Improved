#!/usr/bin/env python3
"""Import restaurant-chain OpenStreetMap JSON exports into one SQLite table.

Examples:
  python scripts/ingest_restaurants.py --states TX
  python scripts/ingest_restaurants.py --states TX --chains mcdonalds starbucks
  python scripts/ingest_restaurants.py --source-dir /path/to/scripts-all-restaurants

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

DEFAULT_SOURCE_DIR = Path(__file__).resolve().parents[1] / 'scripts-all-restaurants'
DEFAULT_DB = Path(__file__).resolve().parents[1] / 'database' / 'chat_history.sqlite3'
STATE_SUFFIX = re.compile(r'_([A-Z]{2})$')
COLUMNS = (
    'chain', 'osm_type', 'osm_id', 'source_state', 'state', 'name', 'latitude',
    'longitude', 'address', 'house_number', 'street', 'city', 'postal_code',
    'country', 'phone', 'website', 'amenity', 'cuisine', 'opening_hours',
    'drive_through', 'takeaway', 'wheelchair', 'tags_json', 'osm_timestamp',
    'source_file', 'imported_at',
)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS restaurants (
    chain TEXT NOT NULL,
    osm_type TEXT NOT NULL CHECK(osm_type IN ('node', 'way', 'relation')),
    osm_id INTEGER NOT NULL,
    source_state TEXT NOT NULL,
    state TEXT,
    name TEXT,
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
    cuisine TEXT,
    opening_hours TEXT,
    drive_through TEXT,
    takeaway TEXT,
    wheelchair TEXT,
    tags_json TEXT NOT NULL,
    osm_timestamp TEXT,
    source_file TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (chain, osm_type, osm_id)
);
CREATE INDEX IF NOT EXISTS idx_restaurants_chain_coordinates
    ON restaurants(chain, latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_restaurants_coordinates
    ON restaurants(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_restaurants_chain_state
    ON restaurants(chain, state);
CREATE TABLE IF NOT EXISTS restaurant_names (
    name TEXT PRIMARY KEY,
    "primary" INTEGER NOT NULL DEFAULT 1 CHECK("primary" IN (0, 1))
);
'''


def _refresh_primary_restaurant_names(conn):
    names = [row[0] for row in conn.execute('SELECT name FROM restaurant_names')]
    groups = {}
    for chain, name in conn.execute('''
        SELECT DISTINCT chain, name FROM restaurants
        WHERE name IS NOT NULL AND trim(name) != ''
    '''):
        words = re.findall(r'[a-z]+|[0-9]+', name.casefold())
        prefix = ''.join(words[:2])
        if prefix:
            groups.setdefault((chain, prefix), set()).add(name)

    suppressed_names = set()
    primary_names = set()
    for group in groups.values():
        group_names = group.intersection(names)
        if len(group_names) < 2:
            continue
        candidates = [name for name in group_names if not re.search(r'\bclosed\b', name, re.I)]
        winner = min(candidates or group_names,
                     key=lambda name: (-len(name), name.casefold(), name))
        primary_names.add(winner)
        suppressed_names.update(group_names - {winner})

    primary_names.update(set(names) - suppressed_names)
    conn.execute('UPDATE restaurant_names SET "primary" = 0')
    conn.executemany(
        'UPDATE restaurant_names SET "primary" = 1 WHERE name = ?',
        ((name,) for name in primary_names),
    )


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


def normalize_element(element, chain, source_state, osm_timestamp, source_file, imported_at):
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
        'chain': chain,
        'osm_type': osm_type,
        'osm_id': osm_id,
        'source_state': source_state,
        'state': state,
        'name': _tag(tags, 'name'),
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
        'cuisine': _tag(tags, 'cuisine'),
        'opening_hours': _tag(tags, 'opening_hours'),
        'drive_through': _tag(tags, 'drive_through'),
        'takeaway': _tag(tags, 'takeaway'),
        'wheelchair': _tag(tags, 'wheelchair'),
        'tags_json': json.dumps(tags, ensure_ascii=False, sort_keys=True),
        'osm_timestamp': osm_timestamp,
        'source_file': source_file,
        'imported_at': imported_at,
    }


def discover_source_files(source_dir, states=None, chains=None):
    source_dir = Path(source_dir)
    selected_states = {state.upper() for state in states} if states else None
    selected_chains = {chain.casefold().removesuffix('_states') for chain in chains} if chains else None
    files = []
    for path in sorted(source_dir.rglob('*.json')):
        if not path.parent.name.endswith('_states'):
            continue
        state_match = STATE_SUFFIX.search(path.stem)
        if not state_match:
            continue
        state = state_match[1]
        chain = path.parent.name[:-len('_states')].casefold()
        if selected_states is not None and state not in selected_states:
            continue
        if selected_chains is not None and chain not in selected_chains:
            continue
        files.append((path, chain, state))
    if not files:
        requested = []
        if selected_states:
            requested.append(f"states {', '.join(sorted(selected_states))}")
        if selected_chains:
            requested.append(f"chains {', '.join(sorted(selected_chains))}")
        description = ' and '.join(requested) or 'any chains or states'
        raise ValueError(f'No restaurant JSON files found for {description} in {source_dir}')
    return files


class ProgressReporter:
    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.interactive = self.stream.isatty()

    def start(self, total, db_path):
        self.total = total
        self.interval = max(1, math.ceil(self.total / 50))
        print(f'Found {self.total} source files. Importing into {db_path}', file=self.stream, flush=True)

    def update(self, index, chain, state, filename, record_count):
        if not self.interactive and index % self.interval and index != self.total:
            return
        percent = round(index * 100 / self.total)
        filled = round(percent / 5)
        bar = '#' * filled + '-' * (20 - filled)
        message = f'[{bar}] {index}/{self.total} ({percent}%) {chain}/{state}: {record_count} records'
        if self.interactive:
            print('\r' + f'{message} - {filename}'.ljust(120), end='', file=self.stream, flush=True)
            if index == self.total:
                print(file=self.stream, flush=True)
        else:
            print(f'{message} - {filename}', file=self.stream, flush=True)


def ingest_files(source_dir=DEFAULT_SOURCE_DIR, db_path=DEFAULT_DB, states=None, chains=None, progress=None):
    source_dir = Path(source_dir)
    files = discover_source_files(source_dir, states, chains)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    placeholders = ','.join('?' for _ in COLUMNS)
    updates = ','.join(
        f'{column}=excluded.{column}'
        for column in COLUMNS if column not in ('chain', 'osm_type', 'osm_id')
    )
    sql = (
        f'INSERT INTO restaurants ({",".join(COLUMNS)}) '
        f'VALUES ({placeholders}) ON CONFLICT(chain,osm_type,osm_id) DO UPDATE SET {updates}'
    )
    counts = Counter()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        name_columns = {row[1] for row in conn.execute('PRAGMA table_info(restaurant_names)')}
        if 'primary' not in name_columns:
            conn.execute('''
                ALTER TABLE restaurant_names
                ADD COLUMN "primary" INTEGER NOT NULL DEFAULT 1 CHECK("primary" IN (0, 1))
            ''')
        if progress:
            progress.start(len(files), db_path)
        for file_index, (path, chain, source_state) in enumerate(files, 1):
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
                normalize_element(element, chain, source_state, timestamp, source_file, imported_at)
                for element in data['elements']
            )
            with conn:
                conn.executemany(sql, (
                    tuple(record[column] for column in COLUMNS) for record in records
                ))
                conn.executemany(
                    'INSERT OR IGNORE INTO restaurant_names (name) VALUES (?)',
                    ((name,) for name in names),
                )
            counts[(chain, source_state)] += len(data['elements'])
            if progress:
                progress.update(file_index, chain, source_state, path.name, len(data['elements']))
        with conn:
            conn.execute('''
                INSERT OR IGNORE INTO restaurant_names (name)
                SELECT DISTINCT name FROM restaurants WHERE name IS NOT NULL
            ''')
            conn.execute('''
                DELETE FROM restaurant_names
                WHERE name NOT IN (SELECT name FROM restaurants WHERE name IS NOT NULL)
            ''')
            _refresh_primary_restaurant_names(conn)
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source-dir', type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='SQLite database path')
    parser.add_argument('--states', nargs='+', type=str.upper, help='Limit import to state abbreviations')
    parser.add_argument('--chains', nargs='+', type=str.casefold, help='Limit import to chain folder slugs')
    args = parser.parse_args(argv)
    try:
        counts = ingest_files(args.source_dir, args.db, args.states, args.chains, ProgressReporter())
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    for (chain, state), count in sorted(counts.items()):
        print(f'{chain}/{state}: imported {count} restaurants')
    print(f'Total: imported {sum(counts.values())} restaurants into {args.db}')
    print('Data: OpenStreetMap contributors, https://www.openstreetmap.org/copyright')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())