#!/usr/bin/env python3
"""Follow the all-locations directory and enrich the existing Love's SQLite DB.
Keep this file beside scrape_loves.py. Uses scripts/requirements.txt.
python scripts/enrich_loves_locations.py --states TX
python scripts/enrich_loves_locations.py
"""
import argparse
import json
import logging
import re
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
if __package__:
    from .scrape_loves import DEFAULT_DB, ensure_schema, parse_card, save, now, AMENITY_COLUMNS
else:
    from scrape_loves import DEFAULT_DB, ensure_schema, parse_card, save, now, AMENITY_COLUMNS

DIRECTORY = 'https://www.loves.com/location-and-fuel-price-search/all-locations'
EXTRA_COLUMNS = {
    'location_name': 'TEXT', 'store_hours': 'TEXT', 'truck_care_hours': 'TEXT',
    'restaurant_hours_json': 'TEXT', 'facility_hours_json': 'TEXT',
    'additional_amenities_json': 'TEXT', 'specialty_food_json': 'TEXT',
    'detail_sections_json': 'TEXT', 'detail_raw_html': 'TEXT',
    'detail_raw_text': 'TEXT', 'detail_scraped_at': 'TEXT',
}


def migrate(conn):
    ensure_schema(conn)
    existing = {r[1] for r in conn.execute('PRAGMA table_info(loves_locations)')}
    with conn:
        for name, kind in EXTRA_COLUMNS.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE loves_locations ADD COLUMN {name} {kind}')
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS loves_location_hours (
            store_number TEXT NOT NULL REFERENCES loves_locations(store_number),
            facility_type TEXT NOT NULL, facility_name TEXT NOT NULL,
            hours_text TEXT NOT NULL, schedule_json TEXT NOT NULL,
            source_url TEXT NOT NULL, scraped_at TEXT NOT NULL,
            PRIMARY KEY(store_number, facility_type, facility_name)
        );
        CREATE TABLE IF NOT EXISTS loves_detail_runs (
            id INTEGER PRIMARY KEY, source_url TEXT NOT NULL, store_number TEXT,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, error TEXT
        );
        CREATE TABLE IF NOT EXISTS loves_facilities (
            id INTEGER PRIMARY KEY,
            store_number TEXT NOT NULL REFERENCES loves_locations(store_number),
            facility_type TEXT NOT NULL, name TEXT NOT NULL,
            raw_hours TEXT, source_url TEXT, scraped_at TEXT,
            UNIQUE(store_number,facility_type,name)
        );
        CREATE TABLE IF NOT EXISTS loves_facility_hours (
            id INTEGER PRIMARY KEY,
            facility_id INTEGER NOT NULL REFERENCES loves_facilities(id),
            opens_at TEXT, closes_at TEXT,
            closes_next_day INTEGER NOT NULL DEFAULT 0,
            is_24_hours INTEGER NOT NULL DEFAULT 0,
            is_closed INTEGER NOT NULL DEFAULT 0,
            parse_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS loves_facility_hour_days (
            hours_id INTEGER NOT NULL REFERENCES loves_facility_hours(id) ON DELETE CASCADE,
            weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
            interval_number INTEGER NOT NULL DEFAULT 1,
            raw_text TEXT NOT NULL,
            PRIMARY KEY(hours_id,weekday,interval_number)
        );
        CREATE TABLE IF NOT EXISTS loves_fuel_prices (
            store_number TEXT NOT NULL REFERENCES loves_locations(store_number),
            fuel_type TEXT NOT NULL, price_mills INTEGER,
            display_price TEXT, scraped_at TEXT NOT NULL,
            PRIMARY KEY(store_number,fuel_type)
        );
        CREATE TABLE IF NOT EXISTS loves_payment_types (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS loves_location_payment_types (
            store_number TEXT NOT NULL REFERENCES loves_locations(store_number),
            payment_type_id INTEGER NOT NULL REFERENCES loves_payment_types(id),
            PRIMARY KEY(store_number,payment_type_id)
        );
        CREATE TABLE IF NOT EXISTS loves_station_codes (
            store_number TEXT NOT NULL REFERENCES loves_locations(store_number),
            provider TEXT NOT NULL, code TEXT NOT NULL,
            PRIMARY KEY(store_number,provider)
        );
        CREATE VIEW IF NOT EXISTS loves_hours_grouped AS
        SELECT f.store_number, f.facility_type, f.name, h.id AS hours_id,
               h.opens_at, h.closes_at, h.closes_next_day,
               h.is_24_hours, h.is_closed, h.parse_status,
               (SELECT group_concat(weekday, ',') FROM
                   (SELECT DISTINCT weekday FROM loves_facility_hour_days d
                    WHERE d.hours_id=h.id ORDER BY weekday)) AS weekdays
        FROM loves_facilities f JOIN loves_facility_hours h ON h.facility_id=f.id;
        ''')


DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
DAY_NAMES = {
    'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6,
}
DAY_PATTERN = r'(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)'
DAY_MARKER = re.compile(rf'\b({DAY_PATTERN})\s*(?:[-–—]\s*({DAY_PATTERN}))?\s*:\s*', re.I)


def clock(value):
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?', value.strip(), re.I)
    if not match:
        raise ValueError('Unrecognized clock time')
    hour, minute = int(match[1]), int(match[2] or 0)
    if not 1 <= hour <= 12 or minute > 59:
        raise ValueError('Invalid clock time')
    return f'{hour % 12 + (12 if match[3].lower() == "p" else 0):02d}:{minute:02d}'


def structured_hours(entry):
    """Monday=0. Unrecognized/missing schedules stay explicitly unknown."""
    raw = str(entry.get('hours') or '')
    markers = list(DAY_MARKER.finditer(raw))
    by_day = {}
    if markers:
        for index, match in enumerate(markers):
            start_day = DAY_NAMES[match[1][:3].lower()]
            end_day = DAY_NAMES[(match[2] or match[1])[:3].lower()]
            value = raw[match.end():markers[index+1].start() if index+1 < len(markers) else len(raw)].strip()
            day = start_day
            while True:
                by_day[day] = value
                if day == end_day:
                    break
                day = (day + 1) % 7
    else:
        by_day = dict.fromkeys(range(7), raw)
    rows = []
    for day in range(7):
        value = by_day.get(day, '')
        base = dict(weekday=day, interval_number=1, opens_at=None, closes_at=None,
                    closes_next_day=0, is_24_hours=0, is_closed=0,
                    parse_status='unknown', raw_text=value)
        if re.fullmatch(r'(?:Open\s*)?24[\s-]*hours?', value, re.I):
            base.update(is_24_hours=1, parse_status='parsed')
            rows.append(base)
        elif re.fullmatch(r'Closed', value, re.I):
            base.update(is_closed=1, parse_status='parsed')
            rows.append(base)
        else:
            intervals = re.split(r'\s*(?:;|,|\band\b)\s*', value)
            parsed = []
            try:
                for i, interval in enumerate(intervals, 1):
                    start, end = re.split(r'\s*[–—-]\s*', interval)
                    opening, closing = clock(start), clock(end)
                    parsed.append(dict(base, interval_number=i, opens_at=opening, closes_at=closing,
                                       closes_next_day=int(closing <= opening), parse_status='parsed'))
                rows.extend(parsed)
            except ValueError:
                rows.append(base)
    return rows


def directory_links(html):
    result = {}
    for a in BeautifulSoup(html, 'html.parser').select('a[href]'):
        url = urljoin(DIRECTORY, a['href']).split('#')[0].split('?')[0]
        path = urlparse(url)
        match = re.match(r'^/locations/([a-z]{2})/[^/]+/[^/]+-(\d+)/?$', path.path, re.I)
        if path.hostname in ('www.loves.com', 'loves.com') and match:
            result[url] = {'url': url, 'state': match[1].upper(), 'number': match[2]}
    return list(result.values())


def parse_detail(html, item):
    soup = BeautifulSoup(html, 'html.parser')
    article = next((a for a in soup.select('article') if a.select_one('[data-row="1"]')), None)
    if article is None:
        raise ValueError('No store detail article found')
    identity = article.select_one('[class*="store-number"]')
    number = re.search(r'Store\s*#\s*(\d+)', identity.get_text(' ', strip=True) if identity else article.get_text(' ', strip=True))
    if not number or number[1] != item['number']:
        raise ValueError('Detail page store identity does not match directory URL')
    original = str(article)
    # Shared search-card parser expects an anchor; detail pages use a span.
    anchor = soup.new_tag('a', href=item['url'])
    anchor.string = f"Store #{number[1]}"
    article.append(anchor)
    record = parse_card(str(article), item['state'], item['url'])
    record['raw_html'] = original
    text = lambda el: (el.get_text(' ', strip=True) if hasattr(el, 'get_text') else str(el).strip()) if el else ''
    additional = list(dict.fromkeys(text(li) for li in article.select('[class*="additional-amenities"] li') if text(li)))
    record['amenities_json'] = json.dumps(list(dict.fromkeys(json.loads(record['amenities_json']) + additional)), ensure_ascii=False)
    facilities, restaurants = [], []
    hours = article.select_one('[data-row="3"]')
    if hours:
        for label in hours.select('label'):
            parent = label.parent
            label_text = text(label)
            value = text(parent).replace(label_text, '', 1).strip(' :,-')
            schedule = [text(li) for li in parent.select('li') if text(li)]
            if value or schedule:
                facilities.append({'name': label_text, 'hours': value,
                                   'schedule': schedule or [value]})
        for block in hours.select('[class*="restaurant-hours"]'):
            header = block.select_one('[class*="column-header"]')
            if header is None:
                logging.warning('Skipping unnamed restaurant-hours block for store %s', item['number'])
                continue
            name = ''.join(str(x) for x in header.contents if isinstance(x, str)).strip()
            name = re.sub(r'\s*[–—-]\s*$', '', name).strip()
            content = header.find_next_sibling('div')
            schedule = [text(li) for li in content.select('li')] if content else []
            hours_text = text(content)
            if hours_text or schedule:
                restaurants.append({'name': name or text(header), 'hours': hours_text,
                                    'schedule': schedule or [hours_text]})
        if not facilities and not restaurants and text(hours):
            facilities.append({'name': 'Store', 'hours': text(hours).removeprefix('Hours of Operation').strip(), 'schedule': []})
    specialty = [{'url': urljoin(item['url'], a['href']), 'images': [i.get('src') for i in a.select('img')]} for a in article.select('a[href]') if a.select_one('img[alt="food concept"]')]
    extra = {
        'location_name': text(soup.select_one('h1')),
        'store_hours': next((f['hours'] for f in facilities if f['name'].casefold().startswith('store')), None),
        'truck_care_hours': next((f['hours'] for f in facilities if 'truck care' in f['name'].casefold()), None),
        'restaurant_hours_json': json.dumps(restaurants, ensure_ascii=False),
        'facility_hours_json': json.dumps(facilities, ensure_ascii=False),
        'additional_amenities_json': json.dumps(additional, ensure_ascii=False),
        'specialty_food_json': json.dumps(specialty, ensure_ascii=False),
        'detail_sections_json': record['sections_json'],
        'detail_raw_html': html, 'detail_raw_text': text(soup.select_one('main') or soup),
        'detail_scraped_at': now(),
    }
    return record, extra, facilities, restaurants


def store_detail(conn, record, extra, facilities, restaurants):
    number = record['store_number']
    existing = conn.execute('SELECT * FROM loves_locations WHERE store_number=?', (number,)).fetchone()
    if existing:
        # Enrich missing core fields; preserve search prices and original evidence.
        old = dict(existing)
        for name in record:
            if name in AMENITY_COLUMNS:
                record[name] = max(record[name], old.get(name) or 0)
            elif name == 'amenities_json':
                record[name] = json.dumps(list(dict.fromkeys(json.loads(old[name]) + json.loads(record[name]))), ensure_ascii=False)
            elif old.get(name) not in (None, '', '[]', '{}'):
                record[name] = old[name]
        for name in ('store_hours', 'truck_care_hours', 'restaurant_hours_json', 'facility_hours_json'):
            if extra[name] in (None, '', '[]', '{}') and old.get(name) not in (None, '', '[]', '{}'):
                extra[name] = old[name]
        # save() refreshes icon amenities from raw_html; preserve all existing entries below.
        prior = conn.execute('SELECT * FROM loves_location_amenities WHERE store_number=?', (number,)).fetchall()
    else:
        prior = []
    detail_record = dict(record)
    save(conn, record)
    for row in prior:
        conn.execute('INSERT OR IGNORE INTO loves_location_amenities VALUES (?,?,?)', tuple(row))
    for name in json.loads(record['amenities_json']):
        conn.execute('INSERT OR IGNORE INTO loves_location_amenities VALUES (?,?,NULL)', (number, name))
    detail_soup = BeautifulSoup(extra['detail_raw_html'], 'html.parser')
    for image in detail_soup.select('main article img[alt="amenity"]'):
        name = image.parent.get_text(' ', strip=True)
        if name:
            conn.execute('INSERT OR REPLACE INTO loves_location_amenities VALUES (?,?,?)',
                         (number, name, urljoin(detail_record['detail_url'], image.get('src') or '')))
    conn.execute('UPDATE loves_locations SET ' + ','.join(f'{key}=?' for key in extra) + ' WHERE store_number=?', list(extra.values()) + [number])
    # A missing category is inconclusive; keep its previously saved schedules.
    active_ids = []
    for kind, rows in [('facility', facilities), ('restaurant', restaurants)]:
        if not rows:
            continue
        for row in rows:
            conn.execute('INSERT OR REPLACE INTO loves_location_hours VALUES (?,?,?,?,?,?,?)',
                         (number, kind, row['name'], row['hours'], json.dumps(row['schedule']), record['detail_url'], extra['detail_scraped_at']))
            conn.execute('''INSERT INTO loves_facilities
                (store_number,facility_type,name,raw_hours,source_url,scraped_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(store_number,facility_type,name)
                DO UPDATE SET raw_hours=excluded.raw_hours,source_url=excluded.source_url,scraped_at=excluded.scraped_at''',
                (number, kind, row['name'], row['hours'], detail_record['detail_url'], extra['detail_scraped_at']))
            facility_id = conn.execute('SELECT id FROM loves_facilities WHERE store_number=? AND facility_type=? AND name=?', (number, kind, row['name'])).fetchone()[0]
            active_ids.append(facility_id)
            old_hours = conn.execute('SELECT id FROM loves_facility_hours WHERE facility_id=?', (facility_id,)).fetchall()
            for old_hour in old_hours:
                conn.execute('DELETE FROM loves_facility_hour_days WHERE hours_id=?', (old_hour[0],))
            conn.execute('DELETE FROM loves_facility_hours WHERE facility_id=?', (facility_id,))
            rules = {}
            for interval in structured_hours(row):
                key = tuple(interval[k] for k in ('opens_at', 'closes_at', 'closes_next_day', 'is_24_hours', 'is_closed', 'parse_status'))
                if key not in rules:
                    rules[key] = conn.execute('INSERT INTO loves_facility_hours (facility_id,opens_at,closes_at,closes_next_day,is_24_hours,is_closed,parse_status) VALUES (?,?,?,?,?,?,?)',
                                              (facility_id,) + key).lastrowid
                conn.execute('INSERT INTO loves_facility_hour_days VALUES (?,?,?,?)',
                             (rules[key], interval['weekday'], interval['interval_number'], interval['raw_text']))
    fuel_prices = json.loads(detail_record['fuel_prices_json'])
    if fuel_prices:
        conn.execute('DELETE FROM loves_fuel_prices WHERE store_number=?', (number,))
        for fuel, price in fuel_prices.items():
            mills = int(Decimal(str(price['price'])) * 1000) if price['price'] is not None else None
            conn.execute('INSERT INTO loves_fuel_prices VALUES (?,?,?,?,?)',
                         (number, fuel, mills, price['display'], extra['detail_scraped_at']))
    payment_types = json.loads(detail_record['payment_types_json'])
    if payment_types:
        conn.execute('DELETE FROM loves_location_payment_types WHERE store_number=?', (number,))
        for name in payment_types:
            conn.execute('INSERT OR IGNORE INTO loves_payment_types(name) VALUES (?)', (name,))
            conn.execute('INSERT INTO loves_location_payment_types SELECT ?,id FROM loves_payment_types WHERE name=?', (number, name))
    station_codes = json.loads(detail_record['station_codes_json'])
    if station_codes:
        conn.execute('DELETE FROM loves_station_codes WHERE store_number=?', (number,))
        for value in station_codes:
            provider, separator, code = value.partition(':')
            if separator:
                conn.execute('INSERT OR REPLACE INTO loves_station_codes VALUES (?,?,?)', (number, provider.strip(), code.strip()))


def load_page(page, url, timeout, detail=False):
    response = page.goto(url, wait_until='domcontentloaded', timeout=timeout * 1000)
    if response and response.status >= 400:
        raise RuntimeError(f'HTTP {response.status}: {url}')
    selector = 'main article [data-row="1"]' if detail else 'a[href*="/locations/"]'
    page.locator(selector).first.wait_for(timeout=timeout * 1000)
    banner = page.get_by_role('button', name='Reject Non-Essential', exact=True)
    if banner.is_visible():
        banner.click()
    if detail:
        article = page.locator('main article').filter(has=page.locator('[data-row="1"]')).first
        if article.get_attribute('data-learn-more') == 'false':
            article.locator('[data-row="8"] [class*="learn-more-link"]').click()
            article.locator('[data-row="7"][data-expanded="true"]').wait_for(timeout=timeout * 1000)
    # Trigger lazy content, and wait briefly for client rendering to settle.
    page.evaluate('window.scrollTo(0, document.documentElement.scrollHeight)')
    page.wait_for_timeout(1000)
    return page.content()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db', type=Path, default=DEFAULT_DB)
    p.add_argument('--states', nargs='+', help='Optional state codes, e.g. TX OK or TX,OK')
    p.add_argument('--stores', nargs='+', help='Optional store numbers')
    p.add_argument('--limit', type=int, help='Maximum number of detail pages')
    p.add_argument('--skip-completed', action='store_true', help='Skip previously enriched stores')
    p.add_argument('--headed', action='store_true')
    p.add_argument('--timeout', type=int, default=90)
    p.add_argument('--delay', type=float, default=2)
    args = p.parse_args()
    if args.timeout < 1 or args.delay < 0 or (args.limit is not None and args.limit < 1):
        p.error('Timeout/limit must be positive and delay nonnegative')
    states = {s.upper() for token in (args.states or []) for s in token.split(',')}
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    args.db.parent.mkdir(parents=True, exist_ok=True)
    failed = []
    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        migrate(conn)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                page = context.new_page()
                items = directory_links(load_page(page, DIRECTORY, args.timeout))
                if not items:
                    raise RuntimeError('Directory contained no recognized store links; nothing scraped')
                items = [x for x in items if (not states or x['state'] in states) and (not args.stores or x['number'] in args.stores)]
                if args.skip_completed:
                    completed = {r[0] for r in conn.execute('SELECT store_number FROM loves_locations WHERE detail_scraped_at IS NOT NULL')}
                    items = [x for x in items if x['number'] not in completed]
                if args.limit:
                    items = items[:args.limit]
                logging.info('Selected %d store pages', len(items))
                for index, item in enumerate(items, 1):
                    run = conn.execute("INSERT INTO loves_detail_runs(source_url,store_number,started_at,status) VALUES (?,?,?,'running')", (item['url'], item['number'], now())).lastrowid
                    conn.commit()
                    try:
                        parsed = parse_detail(load_page(page, item['url'], args.timeout, True), item)
                        with conn:
                            store_detail(conn, *parsed)
                            conn.execute("UPDATE loves_detail_runs SET status='complete',finished_at=? WHERE id=?", (now(), run))
                        logging.info('%d/%d store %s: %d restaurants', index, len(items), item['number'], len(parsed[3]))
                    except Exception as exc:
                        failed.append(item['number'])
                        logging.exception('Store %s failed', item['number'])
                        with conn:
                            conn.execute("UPDATE loves_detail_runs SET status='failed',finished_at=?,error=? WHERE id=?", (now(), str(exc), run))
                    time.sleep(args.delay)
            finally:
                browser.close()
    if failed:
        logging.error('Rerun failed stores with --stores %s', ' '.join(failed))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
