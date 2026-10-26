#!/usr/bin/env python3
"""Download static Geofabrik state extracts; create truck-stop JSON by state.
Python 3.9+, curl, and osmium>=4.0. No Overpass servers are used.
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
import subprocess
import sys
import time

REGIONS = dict(zip(
    'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split(),
    'alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota mississippi missouri montana nebraska nevada new-hampshire new-jersey new-mexico new-york north-carolina north-dakota ohio oklahoma oregon pennsylvania rhode-island south-carolina south-dakota tennessee texas utah vermont virginia washington west-virginia wisconsin wyoming district-of-columbia'.split()))


def normalized_name(name):
    return re.sub(r"[\s’'._-]+", '', name.casefold())


def load_brand_definitions():
    path = Path(__file__).resolve().with_name('truck_stop_brands.json')
    try:
        definitions = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'Cannot load {path}: {exc}. Keep truck_stop_brands.json beside this script.') from exc
    if not isinstance(definitions, dict) or not isinstance(definitions.get('brands'), list):
        raise ValueError('truck_stop_brands.json must contain a brands array')
    brands = definitions['brands']
    if not brands:
        raise ValueError('truck_stop_brands.json must contain at least one brand')
    names = set()
    for item in brands:
        if not isinstance(item, dict):
            raise ValueError('Each brand definition must be an object')
        brand_names = item.get('names')
        if not isinstance(brand_names, list) or not brand_names or not all(
                isinstance(name, str) and name.strip() for name in brand_names):
            raise ValueError('Each brand must have a nonempty names array')
        names.update(normalized_name(name) for name in brand_names)
    return sorted(names)


try:
    TRUCK_STOP_BRAND_NAMES = load_brand_definitions()
except (RuntimeError, ValueError) as exc:
    print(f'Configuration error: {exc}', file=sys.stderr)
    raise SystemExit(2)

# Include the actual definitions in the cache key: editing IDs/aliases or adding
# brands must not silently reuse an older set of extracted results.
CONFIG_HASH = hashlib.sha256(json.dumps(TRUCK_STOP_BRAND_NAMES, sort_keys=True).encode()).hexdigest()[:16]
SCHEMA = 'geofabrik-truck-stops-v1-' + CONFIG_HASH


def log(text):
    print(text, flush=True)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def completed(path):
    data = read_json(path)
    return (isinstance(data, dict) and not data.get('remark')
            and isinstance(data.get('elements'), list)
            and (bool(data['elements']) or data.get('_download', {}).get('complete') is True)
            and all(isinstance(e, dict) and e.get('type') in ('node','way','relation')
                    and isinstance(e.get('id'), int) for e in data['elements']))


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as f:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                f.write(b'0'); f.flush()
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError('Another extract downloader is running in this cache folder') from exc
            try: yield
            finally:
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError('Another extract downloader is running in this cache folder') from exc
            try: yield
            finally: fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def md5_file(path):
    # This is a provider integrity checksum, not a password/security primitive.
    h = hashlib.md5()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def curl_command(args):
    return ['curl', '-4', '--location', '--fail', '--show-error', '--retry', str(args.retries),
            '--retry-all-errors', '--retry-delay', '5', '--connect-timeout', '20',
            '--speed-time', '60', '--speed-limit', '1024', '--user-agent', 'TruckStopExtractDownloader/1.0']


def download_extract(state, args):
    slug = REGIONS[state]
    url = args.base_url.rstrip('/') + '/' + slug + '-latest.osm.pbf'
    pbf = args.cache / (state + '.osm.pbf')
    part = args.cache / (state + '.osm.pbf.part')
    expected_path = args.cache / (state + '.download.json')
    for snapshot_attempt in range(3):
        log(f'{state}: fetching small integrity-check file from Geofabrik')
        result = subprocess.run(curl_command(args) + ['--silent', '--max-time', '120', url + '.md5'],
                                stdout=subprocess.PIPE, check=True, text=True)
        match = re.match(r'^\s*([0-9a-fA-F]{32})\b', result.stdout)
        if not match:
            raise RuntimeError('Provider did not return a valid MD5 checksum')
        expected = match.group(1).lower()
        old = read_json(expected_path)
        if not isinstance(old, dict) or old.get('md5') != expected or old.get('url') != url:
            if part.exists(): log(f'{state}: source snapshot changed; restarting partial file safely')
            part.unlink(missing_ok=True)
            atomic_json(expected_path, {'url': url, 'md5': expected})
        if pbf.exists():
            log(f'{state}: checking cached extract')
            if md5_file(pbf) == expected:
                return pbf, expected, url
            pbf.unlink()
        log(f'{state}: downloading static state file (resume enabled); curl shows progress below')
        command = curl_command(args) + ['--continue-at', '-', '--output', str(part), url]
        result = subprocess.run(command)
        if result.returncode in (33, 36):
            log(f'{state}: server cannot resume this partial file; restarting it')
            part.unlink(missing_ok=True)
            result = subprocess.run(command)
        if result.returncode:
            raise RuntimeError(f'File download failed (curl {result.returncode}); partial file kept for next run')
        log(f'{state}: verifying downloaded file checksum')
        if md5_file(part) == expected:
            os.replace(part, pbf)
            return pbf, expected, url
        # Latest files can change while downloading. Never process mixed snapshots.
        part.unlink(missing_ok=True)
        log(f'{state}: checksum mismatch; retrying a fresh snapshot ({snapshot_attempt + 1}/3)')
    raise RuntimeError('Checksum mismatch after three snapshot attempts; no JSON files replaced')


def is_truck_stop(tags):
    amenity = tags.get('amenity', '').casefold()
    highway = tags.get('highway', '').casefold()
    hgv_values = ('yes', 'designated', 'permissive', 'customers', 'official')
    has_hgv_access = any(
        value.casefold() in hgv_values
        for key, value in tags.items()
        if key in ('hgv', 'hgv:parking', 'parking:hgv', 'service:vehicle:truck')
    )
    has_hgv_fuel = any(key.casefold() in ('fuel:hgv_diesel', 'fuel:diesel_for_hgv')
                        and value.casefold() not in ('no', 'false')
                        for key, value in tags.items())
    explicit_truck_stop = amenity == 'truck_stop'
    service_area = highway == 'services'
    truck_fuel = amenity == 'fuel' and (has_hgv_access or has_hgv_fuel)
    brand_names = [normalized_name(tags.get(key, '')) for key in ('brand', 'name')]
    known_brand = any(
        site_name == brand_name or (len(brand_name) >= 5 and site_name.startswith(brand_name))
        for site_name in brand_names if site_name
        for brand_name in TRUCK_STOP_BRAND_NAMES
    )
    branded_site = known_brand and (amenity in ('fuel', 'truck_stop') or service_area)
    return explicit_truck_stop or service_area or truck_fuel or branded_site


def extract_truck_stops(pbf):
    import osmium
    records, ways, relations, points = {}, {}, {}, {}
    log('Pass 1: finding truck stops in the local state file')
    processor = osmium.FileProcessor(str(pbf))
    for obj in processor:
        tags = dict(obj.tags)
        if not is_truck_stop(tags):
            continue
        kind = obj.type_str()
        record = {'type': {'n':'node','w':'way','r':'relation'}[kind], 'id': obj.id, 'tags': tags}
        if kind == 'n':
            if obj.location.valid():
                points[obj.id] = (obj.location.lat, obj.location.lon)
                record.update(lat=obj.location.lat, lon=obj.location.lon)
        elif kind == 'w':
            ways[obj.id] = [n.ref for n in obj.nodes]
        else:
            relations[obj.id] = [(m.type, m.ref) for m in obj.members]
        records[(kind, obj.id)] = record
    # Gather nested relation geometry references, retaining only truck-stop geometry.
    for depth in range(32):
        needed = {ref for members in relations.values() for kind, ref in members if kind == 'r'} - relations.keys()
        if not needed:
            break
        found = 0
        for obj in osmium.FileProcessor(str(pbf), osmium.osm.RELATION).with_filter(osmium.filter.IdFilter(needed)):
            relations[obj.id] = [(m.type, m.ref) for m in obj.members]
            found += 1
        if found == 0:
            break
    else:
        raise RuntimeError('Nested relation depth exceeds safety limit')
    needed_ways = {ref for members in relations.values() for kind, ref in members if kind == 'w'} - ways.keys()
    if needed_ways:
        log('Pass 2: resolving truck-stop relation member ways')
        for obj in osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(osmium.filter.IdFilter(needed_ways)):
            ways[obj.id] = [n.ref for n in obj.nodes]
    needed_nodes = {ref for refs in ways.values() for ref in refs}
    needed_nodes.update(ref for members in relations.values() for kind, ref in members if kind == 'n')
    needed_nodes -= points.keys()
    if needed_nodes:
        log(f'Final geometry pass: resolving {len(needed_nodes)} referenced nodes')
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
            child, ok = node_refs(member_kind, member_id, visiting)
            refs.update(child); complete = complete and ok
        return refs, complete

    output = []
    for (kind, identifier), record in sorted(records.items()):
        if kind != 'n':
            refs, ok = node_refs(kind, identifier)
            coords = [points[n] for n in refs if n in points]
            if coords:
                latitudes = [p[0] for p in coords]
                longitudes = sorted((p[1] + 360) % 360 for p in coords)
                # Circular longitude bounds handle geometry crossing the dateline.
                gaps = [(longitudes[(i+1) % len(longitudes)] + (360 if i == len(longitudes)-1 else 0) - x, i)
                        for i, x in enumerate(longitudes)]
                _, i = max(gaps)
                left, right = longitudes[(i+1) % len(longitudes)], longitudes[i]
                if right < left: right += 360
                longitude = ((left + right)/2 + 180) % 360 - 180
                record['center'] = {'lat': (min(latitudes)+max(latitudes))/2, 'lon': longitude}
            if not ok or len(coords) != len(refs) or not coords:
                record['_geometry_warning'] = 'Missing/incomplete geometry references in source extract'
        elif 'lat' not in record:
            record['_geometry_warning'] = 'Node has no valid location'
        output.append(record)
    return output


def results_for_state(state, args):
    cache_path = args.cache / (state + '.truck_stops.json')
    cached = read_json(cache_path)
    if (not args.refresh_data and isinstance(cached, dict) and cached.get('schema') == SCHEMA
            and isinstance(cached.get('elements'), list)):
        log(f'{state}: reusing locally extracted truck stops')
        return cached
    local = args.pbf_dir / (state + '.osm.pbf') if args.pbf_dir else None
    if local:
        if not local.is_file():
            raise RuntimeError(f'Missing local state extract: {local}')
        pbf, digest, source = local, md5_file(local), str(local)
    else:
        pbf, digest, source = download_extract(state, args)
    data = {'schema': SCHEMA, 'source': source, 'source_md5': digest,
            'processed_utc': datetime.now(timezone.utc).isoformat(),
            'elements': extract_truck_stops(pbf)}
    atomic_json(cache_path, data)
    if not local and not args.keep_pbf:
        pbf.unlink(missing_ok=True)
    return data


def run(args):
    failures = []
    report_path = args.output / 'truck_stop_extract_report.json'
    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'states': {}}
    try:
        for state in dict.fromkeys(args.states or REGIONS):
            path = args.output / f'truck_stops_{state}.json'
            if not args.force and not args.refresh_data and completed(path):
                log(f'{state}: skipping existing completed JSON files')
                report['states'][state] = {'status': 'complete', 'existing': True}
                atomic_json(report_path, report)
                continue
            try:
                data = results_for_state(state, args)
                elements = data['elements']
                warnings = sum('_geometry_warning' in element for element in elements)
                final = {'version': .6, 'generator': 'TruckStopExtractDownloader/1.0',
                         'osm3s': {'copyright': '© OpenStreetMap contributors, ODbL'},
                         '_download': {'complete': True, 'state': state, 'source': data['source'],
                                       'source_md5': data['source_md5'],
                                       'processed_utc': data['processed_utc'], 'geometry_warnings': warnings,
                                       'state_assignment': 'Geofabrik regional extract membership; border overlap possible'},
                         'elements': elements}
                atomic_json(path, final)
                log(f'{state}: saved {len(elements)} truck stops, {warnings} geometry warnings -> {path}')
                report['states'][state] = {'status': 'complete', 'output': str(path)}
            except (Exception,) as exc:
                # Unexpected parser errors must not destroy existing JSON or cached PBF.
                failures.append(state)
                log(f'{state}: FAILED: {exc}. Download/cache retained; rerun to retry.')
                report['states'][state] = {'status': 'incomplete', 'error': str(exc)}
            atomic_json(report_path, report)
            if args.pause:
                time.sleep(args.pause)
    except KeyboardInterrupt:
        report['interrupted'] = True
        atomic_json(report_path, report)
        log('\nInterrupted; rerun the same command without --force/--refresh-data to resume.')
        return 130
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    atomic_json(report_path, report)
    if failures:
        log('Incomplete states: ' + ' '.join(failures))
        return 1
    log('All selected states complete.')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--states', nargs='+', type=str.upper, choices=list(REGIONS))
    p.add_argument('--output', type=Path, default=Path(__file__).resolve().parent,
                   help='Directory for state JSON files and report')
    p.add_argument('--cache', type=Path,
                   default=Path(__file__).resolve().parent / 'truck_stop_extract_cache')
    p.add_argument('--base-url', default='https://download.geofabrik.de/north-america/us')
    p.add_argument('--retries', type=int, default=6)
    p.add_argument('--pause', type=float, default=2)
    p.add_argument('--force', action='store_true', help='Rewrite selected final JSON from cached extracted results')
    p.add_argument('--refresh-data', action='store_true', help='Fetch current provider data and rewrite selected JSON')
    p.add_argument('--keep-pbf', action='store_true', help='Keep large state PBF files after extraction')
    p.add_argument('--pbf-dir', type=Path, help='Offline local STATE.osm.pbf directory; skip network entirely')
    # Accepted compatibility flags, with an explicit notice, for old invocation patterns.
    p.add_argument('--tiled', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--ipv4', action='store_true', help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    log(f"Loaded {len(TRUCK_STOP_BRAND_NAMES)} truck-stop brand aliases.")
    if args.retries < 0 or not 0 <= args.pause < 3600:
        p.error('Invalid retry/pause options')
    if not args.pbf_dir and not shutil.which('curl'):
        p.error('curl required: sudo apt install curl')
    try:
        import osmium
        assert hasattr(osmium, 'FileProcessor') and hasattr(osmium.filter, 'IdFilter')
    except (ImportError, AssertionError):
        p.error('Install dependency in your Python environment: python -m pip install "osmium>=4.0,<5"')
    if args.tiled or args.ipv4:
        log('Static extract mode: --tiled is unnecessary; file downloads already use IPv4.')
    args.output.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    try:
        with locked(args.cache / 'extract.lock'):
            return run(args)
    except (RuntimeError, OSError) as exc:
        log(str(exc)); return 2


if __name__ == '__main__':
    sys.exit(main())
