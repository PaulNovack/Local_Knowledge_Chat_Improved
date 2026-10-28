#!/usr/bin/env python3
"""Download Geofabrik state extracts and save major department stores to SQLite.
Python 3.9+, curl, and osmium>=4.0 are required. No Overpass servers are used.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time

REGIONS = dict(zip(
    'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split(),
    'alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota mississippi missouri montana nebraska nevada new-hampshire new-jersey new-mexico new-york north-carolina north-dakota ohio oklahoma oregon pennsylvania rhode-island south-carolina south-dakota tennessee texas utah vermont virginia washington west-virginia wisconsin wyoming district-of-columbia'.split()))
SHOP_TYPES = {'department_store', 'clothes', 'discount', 'variety_store', 'outlet', 'supermarket', 'general'}
DEFAULT_DB = Path(__file__).resolve().parents[1] / 'database' / 'chat_history.sqlite3'


def load_brands():
    path = Path(__file__).resolve().with_name('department_brands.json')
    try:
        definitions = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'Cannot load {path}: {exc}') from exc
    if not isinstance(definitions, list) or not definitions:
        raise ValueError('department_brands.json must contain a nonempty array')
    brands, aliases = {}, {}
    for item in definitions:
        if not isinstance(item, dict):
            raise ValueError('Each department-store brand must be an object')
        slug, names = item.get('slug'), item.get('names')
        if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9_]+', slug):
            raise ValueError(f'Invalid department-store brand slug: {slug!r}')
        if slug in brands:
            raise ValueError(f'Duplicate department-store brand slug: {slug}')
        if not isinstance(names, list) or not names or not all(isinstance(name, str) and name.strip() for name in names):
            raise ValueError(f'Invalid names for {slug}')
        names = sorted(set(names), key=lambda name: (-len(name), name.casefold()))
        brands[slug] = re.compile(r'^(?:' + '|'.join(re.escape(name) for name in names)
                                  + r')(?=$|[\s,/#()&-])', re.I)
        aliases[slug] = names
    return brands, aliases


try:
    PATTERNS, BRAND_ALIASES = load_brands()
except (RuntimeError, ValueError) as exc:
    print(f'Configuration error: {exc}', file=sys.stderr)
    raise SystemExit(2)

CONFIG_HASH = hashlib.sha256(json.dumps(BRAND_ALIASES, sort_keys=True).encode()).hexdigest()[:16]
SCHEMA = 'geofabrik-departments-v1-' + CONFIG_HASH


def log(text):
    print(text, flush=True)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError('Another department-store downloader is running in this cache folder') from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError('Another department-store downloader is running in this cache folder') from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def md5_file(path):
    digest = hashlib.md5()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def curl_command(args):
    return ['curl', '-4', '--location', '--fail', '--show-error', '--retry', str(args.retries),
            '--retry-all-errors', '--retry-delay', '5', '--connect-timeout', '20',
            '--speed-time', '60', '--speed-limit', '1024', '--user-agent', 'DepartmentExtractDownloader/1.0']


def download_extract(state, args):
    region = REGIONS[state]
    url = args.base_url.rstrip('/') + '/' + region + '-latest.osm.pbf'
    pbf = args.cache / (state + '.osm.pbf')
    partial = args.cache / (state + '.osm.pbf.part')
    expected_path = args.cache / (state + '.download.json')
    for attempt in range(3):
        log(f'{state}: checking Geofabrik source checksum')
        result = subprocess.run(curl_command(args) + ['--silent', '--max-time', '120', url + '.md5'],
                                stdout=subprocess.PIPE, check=True, text=True)
        match = re.match(r'^\s*([0-9a-fA-F]{32})\b', result.stdout)
        if not match:
            raise RuntimeError('Provider did not return a valid MD5 checksum')
        expected = match.group(1).lower()
        old = read_json(expected_path)
        if not isinstance(old, dict) or old.get('md5') != expected or old.get('url') != url:
            partial.unlink(missing_ok=True)
            atomic_json(expected_path, {'url': url, 'md5': expected})
        if pbf.exists():
            log(f'{state}: validating cached extract')
            if md5_file(pbf) == expected:
                return pbf, expected, url
            pbf.unlink()
        log(f'{state}: downloading extract with resume enabled')
        command = curl_command(args) + ['--continue-at', '-', '--output', str(partial), url]
        result = subprocess.run(command)
        if result.returncode in (33, 36):
            partial.unlink(missing_ok=True)
            result = subprocess.run(command)
        if result.returncode:
            raise RuntimeError(f'Download failed (curl {result.returncode}); partial file retained')
        if md5_file(partial) == expected:
            os.replace(partial, pbf)
            return pbf, expected, url
        partial.unlink(missing_ok=True)
        log(f'{state}: checksum changed during download; retrying ({attempt + 1}/3)')
    raise RuntimeError('Checksum mismatch after three attempts; no department-store rows were replaced')


def normalized_name(value):
    return re.sub(r"[\s’'._-]+", '', value.casefold())


BRANDS_BY_NAME = {}
for brand, names in BRAND_ALIASES.items():
    for name in names:
        BRANDS_BY_NAME.setdefault(normalized_name(name), set()).add(brand)


def matching_brands(tags):
    shop = tags.get('shop', '').casefold()
    if shop not in SHOP_TYPES:
        return []
    values = [tags.get('brand', ''), tags.get('name', '')]
    exact = set()
    for value in values:
        exact.update(BRANDS_BY_NAME.get(normalized_name(value), ()))
    if exact:
        return sorted(exact)
    return [brand for brand, pattern in PATTERNS.items()
            if any(pattern.search(value) for value in values if value)]


def extract_departments(pbf):
    import osmium
    records, ways, relations, points = {}, {}, {}, {}
    log('Pass 1: finding configured department stores in the local state extract')
    processor = osmium.FileProcessor(str(pbf)).with_filter(osmium.filter.KeyFilter('shop'))
    for obj in processor:
        tags = dict(obj.tags)
        brands = matching_brands(tags)
        if not brands:
            continue
        kind = obj.type_str()
        record = {'type': {'n': 'node', 'w': 'way', 'r': 'relation'}[kind], 'id': obj.id, 'tags': tags}
        if kind == 'n':
            if obj.location.valid():
                points[obj.id] = (obj.location.lat, obj.location.lon)
                record.update(lat=obj.location.lat, lon=obj.location.lon)
        elif kind == 'w':
            ways[obj.id] = [node.ref for node in obj.nodes]
        else:
            relations[obj.id] = [(member.type, member.ref) for member in obj.members]
        records[(kind, obj.id)] = (record, brands)

    for _ in range(32):
        needed = {ref for members in relations.values() for kind, ref in members if kind == 'r'} - relations.keys()
        if not needed:
            break
        found = 0
        for obj in osmium.FileProcessor(str(pbf), osmium.osm.RELATION).with_filter(osmium.filter.IdFilter(needed)):
            relations[obj.id] = [(member.type, member.ref) for member in obj.members]
            found += 1
        if not found:
            break
    else:
        raise RuntimeError('Nested relation depth exceeds safety limit')

    needed_ways = {ref for members in relations.values() for kind, ref in members if kind == 'w'} - ways.keys()
    if needed_ways:
        for obj in osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(osmium.filter.IdFilter(needed_ways)):
            ways[obj.id] = [node.ref for node in obj.nodes]
    needed_nodes = {ref for refs in ways.values() for ref in refs}
    needed_nodes.update(ref for members in relations.values() for kind, ref in members if kind == 'n')
    needed_nodes -= points.keys()
    if needed_nodes:
        for obj in osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(needed_nodes)):
            if obj.location.valid():
                points[obj.id] = (obj.location.lat, obj.location.lon)

    def node_refs(kind, identifier, visiting=None):
        if kind == 'n':
            return {identifier}, identifier in points
        if kind == 'w':
            refs = ways.get(identifier)
            return set(refs or []), refs is not None
        visiting = set() if visiting is None else set(visiting)
        if identifier in visiting:
            return set(), False
        visiting.add(identifier)
        members = relations.get(identifier)
        if members is None:
            return set(), False
        refs, complete = set(), True
        for member_kind, member_id in members:
            child, valid = node_refs(member_kind, member_id, visiting)
            refs.update(child)
            complete = complete and valid
        return refs, complete

    output = {brand: [] for brand in PATTERNS}
    for (kind, identifier), (record, brands) in sorted(records.items()):
        if kind != 'n':
            refs, complete = node_refs(kind, identifier)
            coords = [points[node] for node in refs if node in points]
            if coords:
                longitudes = sorted((point[1] + 360) % 360 for point in coords)
                gaps = [(longitudes[(index + 1) % len(longitudes)]
                         + (360 if index == len(longitudes) - 1 else 0) - value, index)
                        for index, value in enumerate(longitudes)]
                _, index = max(gaps)
                left, right = longitudes[(index + 1) % len(longitudes)], longitudes[index]
                if right < left:
                    right += 360
                longitude = ((left + right) / 2 + 180) % 360 - 180
                record['center'] = {
                    'lat': (min(point[0] for point in coords) + max(point[0] for point in coords)) / 2,
                    'lon': longitude,
                }
            if not complete or len(coords) != len(refs) or not coords:
                record['_geometry_warning'] = 'Missing/incomplete geometry references in source extract'
        elif 'lat' not in record:
            record['_geometry_warning'] = 'Node has no valid location'
        for brand in brands:
            output[brand].append(record)
    return output


def results_for_state(state, args):
    cache_path = args.cache / (state + '.departments.json')
    cached = read_json(cache_path)
    if (not args.refresh_data and isinstance(cached, dict) and cached.get('schema') == SCHEMA
            and isinstance(cached.get('brands'), dict) and set(cached['brands']) == set(PATTERNS)):
        log(f'{state}: reusing cached department-store extraction')
        return cached
    local = args.pbf_dir / (state + '.osm.pbf') if args.pbf_dir else None
    if local:
        if not local.is_file():
            raise RuntimeError(f'Missing local state extract: {local}')
        pbf, digest, source = local, md5_file(local), str(local)
    else:
        pbf, digest, source = download_extract(state, args)
    data = {
        'schema': SCHEMA,
        'source': source,
        'source_md5': digest,
        'processed_utc': datetime.now(timezone.utc).isoformat(),
        'brands': extract_departments(pbf),
    }
    atomic_json(cache_path, data)
    if not local and not args.keep_pbf:
        pbf.unlink(missing_ok=True)
    return data


def table_name(brand):
    if brand not in PATTERNS:
        raise ValueError('Unknown department-store brand table')
    return 'department_' + brand


def ensure_tables(db, brands):
    db.execute('''
        CREATE TABLE IF NOT EXISTS department_scrape_runs (
            state TEXT NOT NULL,
            brand TEXT NOT NULL,
            source_md5 TEXT NOT NULL,
            record_count INTEGER NOT NULL,
            processed_utc TEXT NOT NULL,
            PRIMARY KEY (state, brand)
        )
    ''')
    for brand in brands:
        table = table_name(brand)
        db.execute(f'''
            CREATE TABLE IF NOT EXISTS "{table}" (
                osm_type TEXT NOT NULL CHECK(osm_type IN ('node', 'way', 'relation')),
                osm_id INTEGER NOT NULL,
                source_state TEXT NOT NULL,
                state TEXT,
                name TEXT,
                latitude REAL,
                longitude REAL,
                address TEXT,
                city TEXT,
                postal_code TEXT,
                phone TEXT,
                website TEXT,
                shop TEXT,
                opening_hours TEXT,
                brand TEXT,
                brand_wikidata TEXT,
                tags_json TEXT NOT NULL,
                geometry_warning TEXT,
                source_url TEXT NOT NULL,
                source_md5 TEXT NOT NULL,
                processed_utc TEXT NOT NULL,
                PRIMARY KEY (osm_type, osm_id, source_state)
            )
        ''')
        db.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_coordinates" ON "{table}"(latitude, longitude)')
        db.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_state" ON "{table}"(state)')


def tag_value(tags, name):
    value = tags.get(name)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def save_state(db_path, state, data, brands):
    processed = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute('PRAGMA journal_mode=WAL')
        ensure_tables(db, brands)
        for brand in brands:
            table = table_name(brand)
            records = data['brands'][brand]
            db.execute(f'DELETE FROM "{table}" WHERE source_state = ?', (state,))
            values = []
            for record in records:
                tags = record.get('tags') or {}
                point = record if record.get('lat') is not None else record.get('center') or {}
                house = tag_value(tags, 'addr:housenumber')
                street = tag_value(tags, 'addr:street') or tag_value(tags, 'addr:place')
                street_address = ' '.join(part for part in (house, street) if part)
                city = tag_value(tags, 'addr:city')
                region = tag_value(tags, 'addr:state') or state
                postal_code = tag_value(tags, 'addr:postcode')
                address = ', '.join(part for part in (
                    street_address, city, region, postal_code,
                ) if part and (street_address or city or postal_code)) or None
                values.append((
                    record['type'], record['id'], state,
                    region, tag_value(tags, 'name'),
                    point.get('lat'), point.get('lon'), address, city,
                    postal_code, tag_value(tags, 'phone') or tag_value(tags, 'contact:phone'),
                    tag_value(tags, 'website') or tag_value(tags, 'contact:website'), tag_value(tags, 'shop'),
                    tag_value(tags, 'opening_hours'), tag_value(tags, 'brand'),
                    tag_value(tags, 'brand:wikidata'), json.dumps(tags, ensure_ascii=False, sort_keys=True),
                    record.get('_geometry_warning'), data['source'], data['source_md5'], processed,
                ))
            db.executemany(f'''
                INSERT INTO "{table}" (
                    osm_type, osm_id, source_state, state, name, latitude, longitude, address, city,
                    postal_code, phone, website, shop, opening_hours, brand, brand_wikidata, tags_json,
                    geometry_warning, source_url, source_md5, processed_utc
                ) VALUES ({','.join('?' for _ in range(21))})
                ON CONFLICT(osm_type, osm_id, source_state) DO UPDATE SET
                    state=excluded.state, name=excluded.name, latitude=excluded.latitude,
                    longitude=excluded.longitude, address=excluded.address, city=excluded.city,
                    postal_code=excluded.postal_code, phone=excluded.phone, website=excluded.website,
                    shop=excluded.shop, opening_hours=excluded.opening_hours, brand=excluded.brand,
                    brand_wikidata=excluded.brand_wikidata, tags_json=excluded.tags_json,
                    geometry_warning=excluded.geometry_warning, source_url=excluded.source_url,
                    source_md5=excluded.source_md5, processed_utc=excluded.processed_utc
            ''', values)
            db.execute('''
                INSERT INTO department_scrape_runs(state, brand, source_md5, record_count, processed_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(state, brand) DO UPDATE SET source_md5=excluded.source_md5,
                    record_count=excluded.record_count, processed_utc=excluded.processed_utc
            ''', (state, brand, data['source_md5'], len(values), processed))


def run(args):
    failures = []
    report_path = args.root / 'department_extract_report.json'
    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'database': str(args.db), 'states': {}}
    try:
        for state in dict.fromkeys(args.states or REGIONS):
            try:
                data = results_for_state(state, args)
                save_state(args.db, state, data, args.brands)
                counts = {brand: len(data['brands'][brand]) for brand in args.brands}
                report['states'][state] = {'status': 'complete', 'counts': counts, 'source_md5': data['source_md5']}
                for brand, count in counts.items():
                    log(f'{state}/{brand}: saved {count} locations to {table_name(brand)}')
            except Exception as exc:
                failures.append(state)
                log(f'{state}: FAILED: {exc}. Download/cache retained; rerun to retry.')
                report['states'][state] = {'status': 'incomplete', 'error': str(exc)}
            atomic_json(report_path, report)
            if args.pause:
                time.sleep(args.pause)
    except KeyboardInterrupt:
        report['interrupted'] = True
        atomic_json(report_path, report)
        log('\nInterrupted; rerun the same command to resume from cached/downloaded data.')
        return 130
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    atomic_json(report_path, report)
    if failures:
        log('Incomplete states: ' + ' '.join(failures))
        return 1
    log(f'All selected states saved to {args.db}.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--states', nargs='+', type=str.upper, choices=list(REGIONS))
    parser.add_argument('--brands', nargs='+', choices=list(PATTERNS), default=list(PATTERNS))
    parser.add_argument('--list-brands', action='store_true')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='SQLite database path')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent, help='Report and cache parent directory')
    parser.add_argument('--cache', type=Path, help='Source/extraction cache directory')
    parser.add_argument('--base-url', default='https://download.geofabrik.de/north-america/us')
    parser.add_argument('--retries', type=int, default=6)
    parser.add_argument('--pause', type=float, default=2)
    parser.add_argument('--refresh-data', action='store_true', help='Download and parse the current provider snapshot')
    parser.add_argument('--keep-pbf', action='store_true')
    parser.add_argument('--pbf-dir', type=Path, help='Use local STATE.osm.pbf files instead of downloading')
    args = parser.parse_args(argv)
    if args.list_brands:
        print('\n'.join(PATTERNS))
        return 0
    args.brands = list(dict.fromkeys(args.brands))
    if args.retries < 0 or not 0 <= args.pause < 3600:
        parser.error('Invalid retry/pause options')
    if not args.pbf_dir and not shutil.which('curl'):
        parser.error('curl is required for Geofabrik downloads')
    try:
        import osmium
        assert hasattr(osmium, 'FileProcessor') and hasattr(osmium.filter, 'IdFilter')
    except (ImportError, AssertionError):
        parser.error('Install dependency: python -m pip install "osmium>=4.0,<5"')
    args.root.mkdir(parents=True, exist_ok=True)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.cache = args.cache or args.root / 'osm_extract_cache'
    args.cache.mkdir(parents=True, exist_ok=True)
    try:
        with locked(args.cache / 'extract.lock'):
            return run(args)
    except (RuntimeError, OSError) as exc:
        log(str(exc))
        return 2


if __name__ == '__main__':
    sys.exit(main())
