#!/usr/bin/env python3
"""Scrape Love's state search results into SQLite.

Install: python -m pip install playwright beautifulsoup4
         python -m playwright install chromium
Run:     python scripts/scrape_loves.py --states TX
         python scripts/scrape_loves.py
Scope: all data present in search-result cards, including hidden HTML fields;
this does not crawl the separate store detail pages. Re-runs update stores.
"""
import argparse
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

STATES = 'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split()
BASE = 'https://www.loves.com/location-and-fuel-price-search/locationsearchresults?state='
CARD = 'section[data-index] article'
AMENITY_COLUMNS = {
    'has_private_showers': 'Private Showers',
    'has_amazon_pickup': 'Amazon Pickup',
    'has_cat_scales': 'CAT Scales',
    'has_free_wifi': 'Free Wi-Fi',
    'has_atm': 'ATM',
    'has_transflo': 'Transflo',
    'has_truck_parking': 'Truck Parking',
    'has_bulk_def': 'Bulk DEF',
    'has_rfid_pump_start': 'RFID Pump Start',
    'has_tire_services': 'Tire Services',
    'has_bulk_propane': 'Bulk Propane',
    'has_propane_tank_exchange': 'Propane Tank Exchange',
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS loves_locations (
 store_number TEXT PRIMARY KEY, state TEXT NOT NULL, city TEXT, address TEXT,
 postal_code TEXT, highway TEXT, exit TEXT, phone TEXT, fax TEXT,
 latitude REAL, longitude REAL, truck_parking INTEGER, def_lanes INTEGER,
 shower_count INTEGER, detail_url TEXT, maps_url TEXT,
 fuel_prices_json TEXT NOT NULL, amenities_json TEXT NOT NULL,
 restaurants_json TEXT NOT NULL, hours_json TEXT NOT NULL,
 payment_types_json TEXT NOT NULL, station_codes_json TEXT NOT NULL,
 sections_json TEXT NOT NULL, images_json TEXT NOT NULL,
 raw_text TEXT NOT NULL, raw_html TEXT NOT NULL,
 source_url TEXT NOT NULL, scraped_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loves_locations_state ON loves_locations(state);
CREATE TABLE IF NOT EXISTS loves_location_amenities (
 store_number TEXT NOT NULL REFERENCES loves_locations(store_number),
 amenity TEXT NOT NULL, icon_url TEXT,
 PRIMARY KEY (store_number, amenity)
);
CREATE INDEX IF NOT EXISTS idx_loves_amenity ON loves_location_amenities(amenity);
CREATE TABLE IF NOT EXISTS loves_scrape_runs (
 id INTEGER PRIMARY KEY, state TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, expected_count INTEGER, scraped_count INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL, error TEXT
);
'''


def now():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn):
    """Create tables, add missing amenity columns, and backfill old records."""
    conn.executescript(SCHEMA)
    existing = {row[1] for row in conn.execute('PRAGMA table_info(loves_locations)')}
    with conn:
        for column in AMENITY_COLUMNS:
            if column not in existing:
                conn.execute(f'ALTER TABLE loves_locations ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0 CHECK ({column} IN (0,1))')
        # Existing JSON remains authoritative for backfilling added columns.
        for number, encoded in conn.execute('SELECT store_number, amenities_json FROM loves_locations').fetchall():
            try:
                amenities = {str(name).casefold() for name in json.loads(encoded)}
            except (ValueError, TypeError):
                logging.warning('Cannot backfill amenities for store %s: invalid JSON', number)
                continue
            values = [int(name.casefold() in amenities) for name in AMENITY_COLUMNS.values()]
            conn.execute('UPDATE loves_locations SET ' + ','.join(f'{col}=?' for col in AMENITY_COLUMNS) + ' WHERE store_number=?', values + [number])


def parse_card(html, state, source):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    text = lambda el: el.get_text(' ', strip=True) if el else ''
    link = next((a for a in soup.select('a[href]') if re.fullmatch(r'Store\s*#\s*\d+', text(a))), None)
    if link is None:
        raise ValueError('Result card has no store number; markup may have changed')
    number = re.search(r'\d+', text(link)).group()
    info = soup.select_one('[data-row="1"]')
    summary = [text(li) for li in info.select('ul li')] if info else []
    address_link = soup.select_one('a[href*="google.com/maps"]')
    address = text(address_link)
    maps = address_link.get('href', '') if address_link else ''
    query = parse_qs(urlparse(maps).query).get('query', [''])[0]
    coords = re.fullmatch(r'(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)', query)
    raw = text(soup)
    def field(label, numeric=False):
        for li in soup.select('[data-row="2"] li'):
            match = re.match(re.escape(label) + r'\s*:\s*(.*)', text(li), re.I)
            if match:
                value = match.group(1)
                return int(value) if numeric and value.isdigit() else (None if numeric else value)
        return None
    sections = {}
    for h in soup.select('h5'):
        parent = h.parent
        sections[text(h)] = {'text': text(parent), 'items': [text(li) for li in parent.select('li')]}
    amenities = list(dict.fromkeys(text(img.parent) for img in soup.select('img[alt="amenity"]') if text(img.parent)))
    images = [{'alt': i.get('alt'), 'src': i.get('src'), 'title': i.get('title')} for i in soup.select('img')]
    restaurants = [i for i in images if i['alt'] == 'restaurant']
    hours_el = soup.select_one('[data-row="3"]')
    hours = {'text': text(hours_el), 'embedded': []}
    if hours_el:
        for el in hours_el.select('[data-content]'):
            try:
                hours['embedded'].append(json.loads(el['data-content']))
            except (ValueError, TypeError):
                pass
    prices = {}
    for li in soup.select('[data-group] li'):
        heading = li.select_one('h3')
        label = li.select_one('span')
        if heading and label:
            price_text = re.sub(r'\s+', '', text(heading))
            prices[text(label)] = {'display': price_text, 'price': float(price_text.lstrip('$')) if re.fullmatch(r'\$?\d+\.\d+', price_text) else None}
    zip_match = re.search(r'\b\d{5}(?:-\d{4})?\s*$', address)
    result = dict(store_number=number, state=state,
        city=summary[1].rsplit(',', 1)[0].strip() if len(summary)>1 else None,
        address=address, postal_code=zip_match.group() if zip_match else None,
        highway=summary[2] if len(summary)>2 else None,
        exit=summary[0] if summary else None, phone=field('Tel'), fax=field('Fax'),
        latitude=float(coords[1]) if coords else None, longitude=float(coords[2]) if coords else None,
        truck_parking=field('Truck Parking', True), def_lanes=field('DEF Lanes', True),
        shower_count=field('Shower Count', True), detail_url=urljoin(source, link['href']), maps_url=maps,
        fuel_prices_json=prices, amenities_json=amenities, restaurants_json=restaurants,
        hours_json=hours, payment_types_json=sections.get('Accepted Payment Types', {}).get('items', []),
        station_codes_json=sections.get('Station Codes', {}).get('items', []),
        sections_json=sections, images_json=images, raw_text=raw, raw_html=html,
        source_url=source, scraped_at=now())
    amenity_names = {name.casefold() for name in amenities}
    result.update({column: int(name.casefold() in amenity_names)
                   for column, name in AMENITY_COLUMNS.items()})
    for key in result:
        if key.endswith('_json'):
            result[key] = json.dumps(result[key], ensure_ascii=False)
    return result


def save(conn, record):
    keys = list(record)
    updates = ','.join(f'{k}=excluded.{k}' for k in keys if k != 'store_number')
    conn.execute(f"INSERT INTO loves_locations ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)}) ON CONFLICT(store_number) DO UPDATE SET {updates}", list(record.values()))
    # Replace this store's current amenities so removed amenities do not linger.
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(record['raw_html'], 'html.parser')
    conn.execute('DELETE FROM loves_location_amenities WHERE store_number=?', (record['store_number'],))
    for image in soup.select('img[alt="amenity"]'):
        name = image.parent.get_text(' ', strip=True)
        if name:
            conn.execute('INSERT OR REPLACE INTO loves_location_amenities VALUES (?,?,?)',
                         (record['store_number'], name,
                          urljoin(record['source_url'], image.get('src') or image.get('data-src') or '')))


def expand_cards(page, timeout):
    """Expand only collapsed cards; never toggle already-expanded cards closed."""
    cards = page.locator(CARD)
    # Snapshot count: newly appended cards are handled on the next scroll pass.
    for index in range(cards.count()):
        card = cards.nth(index)
        if card.get_attribute('data-learn-more') != 'false':
            continue
        control = card.locator('[data-row="8"] [class*="learn-more-link"]')
        if control.count() == 0:
            raise RuntimeError('Collapsed store card has no Learn More control; markup changed')
        control.click(timeout=timeout * 1000)
        page.wait_for_function(
            '(index) => document.querySelectorAll("section[data-index] article")[index]?.getAttribute("data-learn-more") === "true"',
            arg=index, timeout=timeout * 1000)


def scrape_state(page, conn, state, args, run_id):
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    source = BASE + state
    response = page.goto(source, wait_until='domcontentloaded', timeout=args.timeout * 1000)
    if response and response.status >= 400:
        raise RuntimeError(f'HTTP {response.status}: {source}')
    heading = page.get_by_role('heading', name=re.compile(r'\d+\s*location\(s\) found', re.I))
    heading.wait_for(timeout=args.timeout * 1000)
    expected = int(re.search(r'\d+', heading.first.inner_text()).group())
    conn.execute('UPDATE loves_scrape_runs SET expected_count=? WHERE id=?', (expected, run_id))
    conn.commit()
    seen = set()
    idle = 0
    for step in range(args.max_scrolls):
        expand_cards(page, args.timeout)
        htmls = page.locator(CARD).evaluate_all('(cards) => cards.map(c => c.outerHTML)')
        before = len(seen)
        with conn:
            for html in htmls:
                record = parse_card(html, state, source)
                save(conn, record)
                seen.add(record['store_number'])
            conn.execute('UPDATE loves_scrape_runs SET scraped_count=? WHERE id=?', (len(seen), run_id))
        logging.info('%s: %d/%d stores (scroll %d)', state, len(seen), expected, step)
        if len(seen) > expected:
            raise RuntimeError(f'Found {len(seen)} stores, exceeding reported total {expected}')
        # Scroll the window AND any scrollable ancestor of the results list.
        page.evaluate('''() => {
            const list = document.querySelector('.infinite-scroll-component');
            for (let el = list; el; el = el.parentElement) {
                if (el.scrollHeight > el.clientHeight) el.scrollTop = el.scrollHeight;
            }
            window.scrollTo(0, document.documentElement.scrollHeight);
        }''')
        idle = idle + 1 if len(seen) == before else 0
        if idle >= args.idle_rounds:
            if len(seen) != expected:
                raise RuntimeError(f'Incomplete state: {len(seen)}/{expected}; no new stores after {idle} waits')
            return
        count = len(htmls)
        try:
            page.wait_for_function('(n) => document.querySelectorAll("section[data-index] article").length > n', arg=count, timeout=args.scroll_wait * 1000)
        except PlaywrightTimeout:
            pass
    raise RuntimeError(f'Scroll limit reached: {len(seen)}/{expected} stores')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, default=Path('data/loves.sqlite3'))
    parser.add_argument('--states', nargs='+', default=STATES, help='State codes, space or comma separated; default all 50 states plus DC')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--timeout', type=int, default=90, help='Navigation/initial results timeout in seconds')
    parser.add_argument('--scroll-wait', type=int, default=4, help='Wait for each new batch in seconds')
    parser.add_argument('--idle-rounds', type=int, default=5)
    parser.add_argument('--max-scrolls', type=int, default=300)
    parser.add_argument('--state-delay', type=float, default=2)
    args = parser.parse_args()
    states = list(dict.fromkeys(s.upper() for token in args.states for s in token.split(',')))
    if any(s not in STATES for s in states):
        parser.error('Unknown state code')
    if min(args.timeout, args.scroll_wait, args.idle_rounds, args.max_scrolls) < 1 or args.state_delay < 0:
        parser.error('Timeouts and limits must be positive; delay cannot be negative')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        from playwright.sync_api import sync_playwright
        import bs4  # noqa: F401
    except ImportError:
        parser.exit(2, 'Install dependencies: python -m pip install playwright beautifulsoup4\nThen: python -m playwright install chromium\n')
    args.db.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    with sqlite3.connect(args.db) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        ensure_schema(conn)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            try:
                for state in states:
                    run_id = conn.execute("INSERT INTO loves_scrape_runs(state,started_at,status) VALUES (?,?,'running')", (state, now())).lastrowid
                    conn.commit()
                    page = context.new_page()
                    try:
                        scrape_state(page, conn, state, args, run_id)
                        conn.execute("UPDATE loves_scrape_runs SET status='complete',finished_at=? WHERE id=?", (now(), run_id))
                    except Exception as exc:
                        failures.append(state)
                        logging.exception('%s failed', state)
                        conn.execute("UPDATE loves_scrape_runs SET status='failed',finished_at=?,error=? WHERE id=?", (now(), str(exc), run_id))
                    finally:
                        conn.commit()
                        page.close()
                    time.sleep(args.state_delay)
            finally:
                browser.close()
    logging.info('Database: %s', args.db.resolve())
    if failures:
        logging.error('Failed/incomplete states (rerun with --states): %s', ' '.join(failures))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
