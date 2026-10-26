#!/usr/bin/env python3
"""Discover Pilot/ Flying J / ONE9 stops and scrape expanded detail pages.

Install: python -m pip install -r scripts/requirements.txt
         python -m playwright install chromium
Run: python scripts/scrape_pilot.py --zip-csv data/us_zipcodes.csv
    python scripts/scrape_pilot.py --db data/pilot.sqlite3  # optional database override
Test: python scripts/scrape_pilot.py --seed-url 'https://pilotcompany.com/locations/search?lat=35.960600&lng=-83.920700&zoom=10' --max-searches 1 --max-details 3

Default grid covers the contiguous US. --bounds SOUTH WEST NORTH EAST changes
coverage. Search truncation is uncertain: a geographic crawl cannot guarantee
all stops. Capped searches are subdivided; max-depth saturation is recorded.
Existing completed jobs are skipped unless --refresh is used.
"""
import argparse
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

ORIGIN = 'https://pilotcompany.com'
DEFAULT_DB = Path(__file__).resolve().parents[2] / 'database' / 'chat_history.sqlite3'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS pilot_locations (
 store_number TEXT PRIMARY KEY, name TEXT, brand TEXT, address TEXT, city TEXT,
 state TEXT, postal_code TEXT, country TEXT, phone TEXT, fax TEXT, interstate TEXT,
 latitude REAL, longitude REAL, detail_url TEXT UNIQUE, discovered_at TEXT,
 scraped_at TEXT, status TEXT NOT NULL DEFAULT 'pending', error TEXT
);
CREATE TABLE IF NOT EXISTS pilot_searches (
 url TEXT PRIMARY KEY, south REAL, west REAL, north REAL, east REAL,
 depth INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
    result_count INTEGER, reported_count INTEGER, possibly_truncated INTEGER,
    zip_code TEXT, population INTEGER, population_density REAL,
 scraped_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS pilot_detail_queue (
 url TEXT PRIMARY KEY, store_number TEXT, status TEXT NOT NULL DEFAULT 'pending',
 discovered_from TEXT, scraped_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS pilot_amenities (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS pilot_location_amenities (
 store_number TEXT NOT NULL REFERENCES pilot_locations(store_number),
 amenity_id INTEGER NOT NULL REFERENCES pilot_amenities(id), quantity INTEGER,
 raw_text TEXT NOT NULL, PRIMARY KEY(store_number,amenity_id)
);
CREATE TABLE IF NOT EXISTS pilot_facilities (
 id INTEGER PRIMARY KEY, store_number TEXT NOT NULL REFERENCES pilot_locations(store_number),
 facility_type TEXT NOT NULL, name TEXT NOT NULL, phone TEXT, logo_url TEXT,
 raw_hours TEXT, status_text TEXT, UNIQUE(store_number,facility_type,name)
);
CREATE TABLE IF NOT EXISTS pilot_hours (
 id INTEGER PRIMARY KEY, facility_id INTEGER NOT NULL REFERENCES pilot_facilities(id) ON DELETE CASCADE,
 opens_at TEXT, closes_at TEXT, closes_next_day INTEGER NOT NULL DEFAULT 0,
 is_24_hours INTEGER NOT NULL DEFAULT 0, is_closed INTEGER NOT NULL DEFAULT 0,
 parse_status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_contacts (
 id INTEGER PRIMARY KEY,
 store_number TEXT NOT NULL REFERENCES pilot_locations(store_number),
 facility_id INTEGER REFERENCES pilot_facilities(id) ON DELETE CASCADE,
 contact_type TEXT NOT NULL, label TEXT NOT NULL,
 phone_number TEXT NOT NULL, extension TEXT NOT NULL DEFAULT '',
 telephone_url TEXT, raw_text TEXT NOT NULL, source_section TEXT NOT NULL,
 source_url TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS pilot_contact_identity ON pilot_contacts
 (store_number, COALESCE(facility_id,0), contact_type, phone_number, extension);
CREATE TABLE IF NOT EXISTS pilot_hour_days (
 hours_id INTEGER NOT NULL REFERENCES pilot_hours(id) ON DELETE CASCADE,
 weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6), interval_number INTEGER NOT NULL,
 raw_text TEXT NOT NULL, PRIMARY KEY(hours_id,weekday,interval_number)
);
CREATE TABLE IF NOT EXISTS pilot_fuel_prices (
 store_number TEXT NOT NULL REFERENCES pilot_locations(store_number), fuel_type TEXT NOT NULL,
 price_mills INTEGER, currency TEXT, blend TEXT, raw_text TEXT NOT NULL,
 updated_text TEXT, observed_at TEXT NOT NULL, PRIMARY KEY(store_number,fuel_type)
);
CREATE TABLE IF NOT EXISTS pilot_sections (
 store_number TEXT NOT NULL REFERENCES pilot_locations(store_number), section TEXT NOT NULL,
 raw_text TEXT NOT NULL, raw_html TEXT NOT NULL, observed_at TEXT NOT NULL,
 PRIMARY KEY(store_number,section)
);
CREATE TABLE IF NOT EXISTS pilot_attributes (
 store_number TEXT NOT NULL REFERENCES pilot_locations(store_number), section TEXT NOT NULL,
 name TEXT NOT NULL, value TEXT NOT NULL, unit TEXT,
 PRIMARY KEY(store_number,section,name)
);
CREATE TABLE IF NOT EXISTS pilot_links (
 store_number TEXT NOT NULL REFERENCES pilot_locations(store_number), section TEXT NOT NULL,
 label TEXT NOT NULL, url TEXT NOT NULL, PRIMARY KEY(store_number,section,label,url)
);
CREATE TABLE IF NOT EXISTS pilot_ev_connectors (
 store_number TEXT NOT NULL REFERENCES pilot_locations(store_number), connector_index INTEGER NOT NULL,
 name TEXT, connector_type TEXT, power_kw REAL, status_text TEXT, raw_text TEXT,
 PRIMARY KEY(store_number,connector_index)
);
CREATE TABLE IF NOT EXISTS pilot_parking_options (
 store_number TEXT NOT NULL REFERENCES pilot_locations(store_number),
 vehicle_type TEXT NOT NULL, available_spaces INTEGER, price_cents INTEGER,
 disabled INTEGER NOT NULL DEFAULT 0, raw_text TEXT NOT NULL, observed_at TEXT NOT NULL,
 PRIMARY KEY(store_number,vehicle_type)
);
CREATE TABLE IF NOT EXISTS pilot_availability (
 store_number TEXT NOT NULL REFERENCES pilot_locations(store_number),
 service TEXT NOT NULL, available INTEGER, total INTEGER,
 price_mills INTEGER, price_unit TEXT, updated_text TEXT, observed_at TEXT NOT NULL,
 PRIMARY KEY(store_number,service)
);
CREATE TABLE IF NOT EXISTS pilot_food_offerings (
 store_number TEXT NOT NULL REFERENCES pilot_locations(store_number),
 name TEXT NOT NULL, description TEXT, image_url TEXT,
 PRIMARY KEY(store_number,name)
);
CREATE TABLE IF NOT EXISTS pilot_images (
 store_number TEXT NOT NULL REFERENCES pilot_locations(store_number),
 section TEXT NOT NULL, url TEXT NOT NULL, alt_text TEXT,
 PRIMARY KEY(store_number,section,url)
);
CREATE TABLE IF NOT EXISTS pilot_snapshots (
 store_number TEXT PRIMARY KEY REFERENCES pilot_locations(store_number),
 html TEXT NOT NULL, text TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS pilot_hours_grouped AS
SELECT f.store_number,f.facility_type,f.name,h.id AS hours_id,h.opens_at,h.closes_at,
 h.closes_next_day,h.is_24_hours,h.is_closed,h.parse_status,
 (SELECT group_concat(weekday,',') FROM
  (SELECT DISTINCT weekday FROM pilot_hour_days d WHERE d.hours_id=h.id ORDER BY weekday)) AS weekdays
FROM pilot_facilities f JOIN pilot_hours h ON h.facility_id=f.id;
'''


def now():
    return datetime.now(timezone.utc).isoformat()


def migrate(conn):
    conn.executescript(SCHEMA)
    # Add future scalar fields to existing databases without dropping data.
    fields = {'name':'TEXT','brand':'TEXT','address':'TEXT','city':'TEXT','state':'TEXT',
              'postal_code':'TEXT','country':'TEXT','phone':'TEXT','fax':'TEXT','interstate':'TEXT',
              'latitude':'REAL','longitude':'REAL','detail_url':'TEXT','discovered_at':'TEXT',
              'scraped_at':'TEXT','status':"TEXT NOT NULL DEFAULT 'pending'",'error':'TEXT'}
    present = {r[1] for r in conn.execute('PRAGMA table_info(pilot_locations)')}
    for name, sql_type in fields.items():
        if name not in present:
            conn.execute(f'ALTER TABLE pilot_locations ADD COLUMN {name} {sql_type}')
    search_fields = {'zip_code':'TEXT','population':'INTEGER','population_density':'REAL'}
    present = {r[1] for r in conn.execute('PRAGMA table_info(pilot_searches)')}
    for name, sql_type in search_fields.items():
        if name not in present:
            conn.execute(f'ALTER TABLE pilot_searches ADD COLUMN {name} {sql_type}')
    conn.commit()


def text(el):
    return el.get_text(' ', strip=True) if el else ''


def normalized_phone(value):
    value=re.sub(r'^tel:', '', value.strip(), flags=re.I)
    extension=re.search(r'(?:;ext=|\bext\.?\s*|\bx\s*)(\d+)\s*$',value,re.I)
    ext=extension[1] if extension else ''
    if extension:value=value[:extension.start()]
    digits=re.sub(r'\D','',value)
    if len(digits)==10:
        return '+1'+digits,ext
    if len(digits)==11 and digits.startswith('1'):
        return '+'+digits,ext
    if value.startswith('+') and 7<=len(digits)<=15:
        return '+'+digits,ext
    return value.strip(),ext


def save_contacts(conn,soup,record,stamp):
    number=record['store_number']
    scopes={(r['facility_type'],r['name']):r['id'] for r in conn.execute('SELECT id,facility_type,name FROM pilot_facilities WHERE store_number=?',(number,))}
    store_id=next((fid for (kind,name),fid in scopes.items() if kind=='store'),None)
    phone_pattern=re.compile(r'(?:\+?1[ .-]?)?(?:\(\d{3}\)|\b\d{3})[ .-]?\d{3}[ .-]\d{4}(?:\s*(?:ext\.?|x)\s*\d+)?',re.I)
    def add(value,kind,label,fid,section,href=None):
        phone,ext=normalized_phone(value)
        if phone:
            conn.execute('''INSERT INTO pilot_contacts
            (store_number,facility_id,contact_type,label,phone_number,extension,telephone_url,raw_text,source_section,source_url,observed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET
            telephone_url=COALESCE(excluded.telephone_url,pilot_contacts.telephone_url),
            observed_at=excluded.observed_at''',(number,fid,kind,label,phone,ext,href,value,section,record['detail_url'],stamp))
    # Include contact fields even when rendered as plain text rather than links.
    for el in soup.select('#section-contact .detail-item'):
        label=text(el.select_one('.title'))
        if re.search(r'phone|fax|tel|service',label,re.I):
            value=text(el.select_one('.value') or el.select_one('a'))
            for match in phone_pattern.finditer(value):
                add(match[0],'fax' if 'fax' in label.lower() else 'phone',label,store_id,'contact')
    # Restrict extraction to location panels, excluding corporate/footer contacts.
    for section in soup.select('[role="tabpanel"][id]'):
        section_name=section['id'].removeprefix('section-')
        for link in section.select('a[href^="tel:"]'):
            fid=store_id if section_name=='contact' else None
            label=text(link) or 'Phone'
            for parent in link.parents:
                if parent==section:break
                heading=parent.select_one('h3')
                if heading:
                    name=text(heading)
                    kind='restaurant' if section_name=='food' else 'truck_care'
                    if (kind,name) in scopes:
                        fid=scopes[(kind,name)];label=name+' phone';break
            add(link['href'],'phone',label,fid,section_name,link['href'])
        # Restaurants and services may expose an unlinked second phone/fax.
        candidates=section.select('[aria-label="Restaurant listings"] article') if section_name=='food' else (section.select('li') if section_name=='truck-care' else [])
        for block in candidates:
            name=text(block.select_one('h3'))
            kind='restaurant' if section_name=='food' else 'truck_care'
            fid=scopes.get((kind,name))
            if fid:
                for value in phone_pattern.finditer(text(block)):
                    prefix=text(block)[max(0,value.start()-12):value.start()].lower()
                    contact_type='fax' if re.search(r'fax\s*:?\s*$',prefix) else 'phone'
                    add(value[0],contact_type,name+' '+contact_type,fid,section_name)


def detail_links(html, source):
    links = set()
    for a in BeautifulSoup(html, 'html.parser').select('a[href]'):
        url = urljoin(source, a['href']).split('#')[0].split('?')[0]
        parsed = urlparse(url)
        if parsed.hostname in ('pilotcompany.com','www.pilotcompany.com') and re.fullmatch(r'/locations/(?:us|ca)/[^/]+/[^/]+/[^/]+/?', parsed.path):
            links.add(url)
    return sorted(links)


def queue_links(conn, html, source):
    links = detail_links(html, source)
    soup = BeautifulSoup(html,'html.parser')
    ids = {}
    for a in soup.select('a[href]'):
        url = urljoin(source,a['href']).split('#')[0].split('?')[0]
        if url not in links:
            continue
        parent = a.parent
        for _ in range(5):
            if parent is None:break
            match = re.search(r'(?:Pilot|Flying J|ONE9)[^#]{0,100}#\s*(\d+)',text(parent),re.I)
            if match:
                ids[url]=match[1]
                break
            parent=parent.parent
    for url in links:
        number=ids.get(url)
        if number and conn.execute('SELECT 1 FROM pilot_detail_queue WHERE store_number=?',(number,)).fetchone():
            continue
        conn.execute('INSERT OR IGNORE INTO pilot_detail_queue(url,store_number,discovered_from) VALUES (?,?,?)',(url,number,source))
    return len(links)


def search_url(lat, lon):
    return ORIGIN + '/locations/search?' + urlencode({'lat':f'{lat:.6f}','lng':f'{lon:.6f}','zoom':10})


def seed_grid(conn, bounds, step):
    south, west, north, east = bounds
    lat = south
    while lat < north:
        lon = west
        while lon < east:
            n, e = min(north,lat+step), min(east,lon+step)
            conn.execute('INSERT OR IGNORE INTO pilot_searches(url,south,west,north,east) VALUES (?,?,?,?,?)',
                         (search_url((lat+n)/2,(lon+e)/2),lat,lon,n,e))
            lon += step
        lat += step
    conn.commit()


def subdivide(conn, row):
    s,w,n,e = (row[x] for x in ['south','west','north','east'])
    if s is None:
        return False
    y,x = (s+n)/2,(w+e)/2
    for a,b,c,d in [(s,w,y,x),(s,x,y,e),(y,w,n,x),(y,x,n,e)]:
        conn.execute('INSERT OR IGNORE INTO pilot_searches(url,south,west,north,east,depth) VALUES (?,?,?,?,?,?)',
                     (search_url((a+c)/2,(b+d)/2),a,b,c,d,row['depth']+1))
    return True


def load(page, url, timeout, detail=False):
    response = page.goto(url, wait_until='domcontentloaded', timeout=timeout*1000)
    if response and response.status >= 400:
        raise RuntimeError(f'HTTP {response.status} for {url}')
    if detail:
        page.locator('h1').filter(has_text=re.compile(r'#\s*\d+')).wait_for(timeout=timeout*1000)
        page.locator('#section-contact .detail-item').first.wait_for(timeout=timeout*1000)
        # These controls insert additional HTML, so all must be expanded.
        for name in ['Show more hours','Show more']:
            for button in page.get_by_role('button',name=name,exact=True).all():
                if button.is_visible():
                    button.click(timeout=timeout*1000)
                    controls = button.get_attribute('aria-controls')
                    if controls:
                        page.locator('[id="'+controls+'"]').wait_for(state='visible',timeout=timeout*1000)
        # Open any additional store/service hours disclosure, never collapse it.
        for button in page.get_by_role('button',name=re.compile(r'hours',re.I)).all():
            if button.is_visible() and button.get_attribute('aria-expanded')=='false':
                button.click(timeout=timeout*1000)
                controls=button.get_attribute('aria-controls')
                if controls:
                    page.locator('[id="'+controls+'"]').wait_for(state='visible',timeout=timeout*1000)
    else:
        page.wait_for_function('''() => document.querySelector('[aria-label="Location results"]') || /No locations|0 locations found|Results \\(0\\)/i.test(document.body.innerText)''',timeout=timeout*1000)
    # Scroll window and result list until links and text remain stable.
    last, idle = None, 0
    for _ in range(30):
        current = page.locator('body').inner_text()
        idle = idle+1 if current == last else 0
        if idle >= 3:
            break
        last = current
        page.evaluate('''() => {
            let el=document.querySelector('[aria-label="Location results"]');
            for (; el; el=el.parentElement) if(el.scrollHeight>el.clientHeight) el.scrollTop=el.scrollHeight;
            window.scrollTo(0,document.documentElement.scrollHeight);
        }''')
        page.wait_for_timeout(400)
    return page.content()


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    heading = text(soup.select_one('h1'))
    match = re.search(r'#\s*(\d+)', heading)
    if not match:
        raise ValueError('No Pilot store number in detail heading')
    number = match[1]
    contact = {}
    for el in soup.select('#section-contact .detail-item'):
        key, value = text(el.select_one('.title')), text(el.select_one('.value'))
        if not value:
            value = text(el.select_one('a'))
        if key:
            contact[key.lower()] = value
    parts = urlparse(url).path.strip('/').split('/')
    record = {'store_number':number,'name':text(soup.select_one('[data-testid="hero-text-heading"]')) or re.sub(r'^Open 24 Hours\s*','',heading,flags=re.I),
              'brand':'Flying J' if 'Flying J' in heading else ('ONE9' if 'ONE9' in heading else 'Pilot'),
              'address':contact.get('address'), 'phone':contact.get('phone'), 'fax':contact.get('fax'),
              'interstate':contact.get('interstate'),'country':parts[1].upper(),'state':parts[2].upper(),
              'city':None,'postal_code':None,'latitude':None,'longitude':None,'detail_url':url,
              'scraped_at':now(),'status':'complete','error':None}
    cityline = text(soup.select_one('#section-contact .city-state'))
    city = re.fullmatch(r'(.*),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?|[A-Z]\d[A-Z]\s*\d[A-Z]\d)', cityline)
    if city:
        record.update(city=city[1],state=city[2],postal_code=city[3])
    coords = re.fullmatch(r'(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)',contact.get('coordinates',''))
    if coords:
        record.update(latitude=float(coords[1]),longitude=float(coords[2]))
    amenities = []
    for li in soup.select('#section-amenities li'):
        raw = text(li)
        m = re.fullmatch(r'(.*?)\s*\((\d+)\)',raw)
        if raw:
            amenities.append((m[1].strip() if m else raw,int(m[2]) if m else None,raw))
    facilities = []
    store_hours = '24 hours' if re.search(r'Open 24 Hours',heading,re.I) else ''
    store_rows=[]
    for row in soup.select('.hop-row'):
        if row.find_parent(attrs={'aria-label':'Restaurant listings'}) or row.find_parent(id='section-truck-care'):
            continue
        day,value=text(row.select_one('.hop-day')),text(row.select_one('.hop-time'))
        if day:store_rows.append(f'{day} : {value}')
    if store_rows:store_hours=' '.join(store_rows)
    facilities.append({'kind':'store','name':record['name'],'phone':record['phone'],'logo':None,'status':None,'hours':store_hours})
    for el in soup.select('[aria-label="Restaurant listings"] article'):
        name = text(el.select_one('h3'))
        if not name:
            raise ValueError('Restaurant has no name')
        rows = []
        for li in el.select('li[aria-label], li.hop-row'):
            day = text(li.select_one('.hop-day'))
            value = text(li.select_one('.hop-time'))
            if day:
                rows.append(f'{day} : {value}')
        image = el.select_one('img')
        facilities.append({'kind':'restaurant','name':name,'phone':None,'logo':urljoin(url,image['src']) if image and image.get('src') else None,
                           'status':text(el.select_one('.restaurant-status')),'hours':' '.join(rows)})
    for el in soup.select('#section-truck-care li'):
        name = text(el.select_one('h3'))
        if name:
            facilities.append({'kind':'truck_care','name':name,'phone':text(el.select_one('a[href^="tel:"]')),'logo':None,'status':None,'hours':''})
    fuels = []
    for el in soup.select('#section-fuel li'):
        raw = text(el)
        price = re.search(r'\$(\d+\.\d+)',raw)
        if price:
            label = text(el.select_one('.chip-name')) or raw[:price.start()].strip()
            blend = text(el.select_one('.chip-badge')) or None
            fuels.append((label,int(Decimal(price[1])*1000),raw,blend))
    return record, amenities, facilities, fuels, soup


def save_detail(conn, html, url):
    record, amenities, facilities, fuels, soup = parse_detail(html,url)
    number, stamp = record['store_number'], record['scraped_at']
    columns = list(record)
    conn.execute('INSERT INTO pilot_locations('+','.join(columns)+',discovered_at) VALUES ('+','.join('?' for _ in columns)+',?) ON CONFLICT(store_number) DO UPDATE SET '+','.join(f'{k}=COALESCE(excluded.{k},pilot_locations.{k})' for k in columns if k not in ('store_number','error'))+',error=NULL',list(record.values())+[stamp])
    for table in ['pilot_location_amenities','pilot_fuel_prices','pilot_sections','pilot_attributes','pilot_links','pilot_ev_connectors','pilot_parking_options','pilot_availability','pilot_food_offerings','pilot_images','pilot_contacts']:
        conn.execute(f'DELETE FROM {table} WHERE store_number=?',(number,))
    for name,quantity,raw in amenities:
        conn.execute('INSERT OR IGNORE INTO pilot_amenities(name) VALUES (?)',(name,))
        conn.execute('INSERT OR REPLACE INTO pilot_location_amenities SELECT ?,id,?,? FROM pilot_amenities WHERE name=?',(number,quantity,raw,name))
    active = []
    for f in facilities:
        conn.execute('''INSERT INTO pilot_facilities(store_number,facility_type,name,phone,logo_url,raw_hours,status_text)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(store_number,facility_type,name) DO UPDATE SET
        phone=excluded.phone,logo_url=excluded.logo_url,raw_hours=excluded.raw_hours,status_text=excluded.status_text''',
        (number,f['kind'],f['name'],f['phone'],f['logo'],f['hours'],f['status']))
        fid = conn.execute('SELECT id FROM pilot_facilities WHERE store_number=? AND facility_type=? AND name=?',(number,f['kind'],f['name'])).fetchone()[0]
        active.append(fid)
        conn.execute('DELETE FROM pilot_hours WHERE facility_id=?',(fid,))
        grouped = {}
        for interval in structured_hours({'hours':f['hours']}):
            key = tuple(interval[k] for k in ['opens_at','closes_at','closes_next_day','is_24_hours','is_closed','parse_status'])
            if key not in grouped:
                grouped[key] = conn.execute('INSERT INTO pilot_hours(facility_id,opens_at,closes_at,closes_next_day,is_24_hours,is_closed,parse_status) VALUES (?,?,?,?,?,?,?)',(fid,)+key).lastrowid
            conn.execute('INSERT INTO pilot_hour_days VALUES (?,?,?,?)',(grouped[key],interval['weekday'],interval['interval_number'],interval['raw_text']))
    for row in conn.execute('SELECT id FROM pilot_facilities WHERE store_number=?',(number,)).fetchall():
        if row[0] not in active:
            conn.execute('DELETE FROM pilot_facilities WHERE id=?',(row[0],))
    save_contacts(conn,soup,record,stamp)
    for label,mills,raw,blend in fuels:
        conn.execute('INSERT OR REPLACE INTO pilot_fuel_prices VALUES (?,?,?,?,?,?,?,?)',(number,label,mills,None,blend,raw,text(soup.select_one('#section-fuel .updated-at')) or None,stamp))
    for section in soup.select('[role="tabpanel"][id]'):
        name = section['id'].removeprefix('section-')
        raw = text(section)
        conn.execute('INSERT OR REPLACE INTO pilot_sections VALUES (?,?,?,?,?)',(number,name,raw,str(section),stamp))
        for a in section.select('a[href]'):
            conn.execute('INSERT OR IGNORE INTO pilot_links VALUES (?,?,?,?)',(number,name,text(a),urljoin(url,a['href'])))
        for image in section.select('img[src]'):
            conn.execute('INSERT OR REPLACE INTO pilot_images VALUES (?,?,?,?)',(number,name,urljoin(url,image['src']),image.get('alt')))
        for el in section.select('.detail-item'):
            label,value = text(el.select_one('.title')),text(el.select_one('.value') or el.select_one('a'))
            if label:
                conn.execute('INSERT OR REPLACE INTO pilot_attributes VALUES (?,?,?,?,NULL)',(number,name,label,value))
        # Preserve every panel's lists as individually queryable facts as well.
        for index,li in enumerate(section.select('li'),1):
            conn.execute('INSERT OR REPLACE INTO pilot_attributes VALUES (?,?,?,?,NULL)',(number,name,f'list_item_{index}',text(li)))
        if name=='parking':
            match = re.search(r'(\d+) general parking spaces',raw,re.I)
            if match:
                conn.execute('INSERT OR REPLACE INTO pilot_attributes VALUES (?,?,?,?,?)',(number,name,'general_spaces',match[1],'spaces'))
            for tile in section.select('[data-testid^="parking-type-tile-"]'):
                vehicle=text(tile.select_one('.label'))
                available=re.search(r'\d+',text(tile.select_one('.availability')))
                price=re.search(r'\$(\d+(?:\.\d+)?)',text(tile.select_one('.price')))
                conn.execute('INSERT OR REPLACE INTO pilot_parking_options VALUES (?,?,?,?,?,?,?)',
                    (number,vehicle or tile['data-testid'],int(available[0]) if available else None,int(Decimal(price[1])*100) if price else None,
                     int(tile.get('aria-disabled')=='true'),text(tile),stamp))
            counts=re.search(r'(\d+)\s+of\s+(\d+)',text(section.select_one('.availability-count')))
            if counts:
                conn.execute('INSERT OR REPLACE INTO pilot_availability VALUES (?,?,?,?,?,?,?,?)',
                    (number,'prime_parking',int(counts[1]),int(counts[2]),None,None,text(section.select_one('.parking-timestamp')),stamp))
        if name=='amenities':
            group=section.select_one('[role="group"][aria-label]')
            counts=re.search(r'(\d+)\s+of\s+(\d+)\s+showers',group.get('aria-label','') if group else '',re.I)
            if counts:
                conn.execute('INSERT OR REPLACE INTO pilot_availability VALUES (?,?,?,?,?,?,?,?)',
                    (number,'showers',int(counts[1]),int(counts[2]),None,None,group.get('aria-label'),stamp))
        if name=='food':
            for article in section.select('[aria-label="Pilot Food Favorites"] article'):
                title=text(article.select_one('h3'))
                image=article.select_one('img[src]')
                if title:
                    conn.execute('INSERT OR REPLACE INTO pilot_food_offerings VALUES (?,?,?,?)',
                        (number,title,text(article.select_one('p')),urljoin(url,image['src']) if image else None))
        if name=='ev':
            counts=re.search(r'(\d+)\s+of\s+(\d+)',text(section.select_one('[data-testid="ev-charging-availability-tile"]')))
            price=re.search(r'\$(\d+(?:\.\d+)?)',text(section.select_one('[data-testid="ev-charging-price-chip"]')))
            if counts or price:
                conn.execute('INSERT OR REPLACE INTO pilot_availability VALUES (?,?,?,?,?,?,?,?)',
                    (number,'ev_charging',int(counts[1]) if counts else None,int(counts[2]) if counts else None,
                     int(Decimal(price[1])*1000) if price else None,'kWh' if price else None,text(section.select_one('.ev-charging-timestamp')),stamp))
            for i,el in enumerate(section.select('li'),1):
                raw_ev=text(el)
                power=re.search(r'(\d+(?:\.\d+)?)\s*kW',raw_ev)
                paragraphs=[text(p) for p in el.select('p')]
                conn.execute('INSERT INTO pilot_ev_connectors VALUES (?,?,?,?,?,?,?)',
                    (number,i,paragraphs[0] if paragraphs else None,paragraphs[1].split('•')[0].strip() if len(paragraphs)>1 else None,float(power[1]) if power else None,text(el.select_one('.ev-charging-card-status-label') or el.select_one('[class*="status"]')),raw_ev))
    conn.execute('INSERT OR REPLACE INTO pilot_snapshots VALUES (?,?,?,?)',(number,html,text(soup),stamp))
    queue_links(conn,html,url)
    # Merge any previously queued alias URL for the same store into this job.
    conn.execute('DELETE FROM pilot_detail_queue WHERE store_number=? AND url<>?',(number,url))
    conn.execute("UPDATE pilot_detail_queue SET status='complete',store_number=?,scraped_at=?,error=NULL WHERE url=?",(number,stamp,url))
    return number


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

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
    raw = entry['hours']
    markers = list(re.finditer(r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\s*:', raw, re.I))
    by_day = {}
    if markers:
        for index, match in enumerate(markers):
            by_day[DAYS.index(match[1].title())] = raw[match.end():markers[index+1].start() if index+1 < len(markers) else len(raw)].strip()
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


def seed_zips(conn, path):
    """CSV header: zip,lat,lng (latitude/longitude/lon also accepted)."""
    import csv
    rows = []
    with path.open(newline='', encoding='utf-8-sig') as stream:
        for row in csv.DictReader(stream):
            row = {k.strip().lower():v for k,v in row.items() if k}
            lat = float(row.get('lat') or row.get('latitude') or '')
            lon = float(row.get('lng') or row.get('lon') or row.get('longitude') or '')
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError('ZIP CSV contains invalid coordinates')
            population = int(row['population']) if row.get('population') else None
            density = float(row['population_density']) if row.get('population_density') else None
            rows.append((row.get('zip','').strip(), lat, lon, population, density))
    rows.sort(key=lambda row: (
        row[4] is None and row[3] is None,
        -(row[4] if row[4] is not None else row[3] if row[3] is not None else 0),
        row[0],
    ))
    for zipcode, lat, lon, population, density in rows:
        conn.execute('''INSERT INTO pilot_searches(url,zip_code,population,population_density)
            VALUES (?,?,?,?) ON CONFLICT(url) DO UPDATE SET
            zip_code=COALESCE(pilot_searches.zip_code,excluded.zip_code),
            population=COALESCE(pilot_searches.population,excluded.population),
            population_density=COALESCE(pilot_searches.population_density,excluded.population_density)''',
            (search_url(lat,lon), zipcode or None, population, density))
    conn.commit()
    logging.info('Loaded %d ZIP coordinate seeds',len(rows))


def next_search_job(conn):
    return conn.execute('''SELECT * FROM pilot_searches WHERE status='pending'
        ORDER BY (zip_code IS NULL), depth, population_density DESC,
                 population DESC, zip_code, url LIMIT 1''').fetchone()


def main():
    p = argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db',type=Path,default=DEFAULT_DB,
                   help=f'SQLite database (default: {DEFAULT_DB})')
    p.add_argument('--zip-csv',type=Path,
                   help='Use ZIP centroids instead of grid; generated CSV includes Census population_density')
    p.add_argument('--seed-url',action='append',help='Search or store detail URL; repeatable; replaces default grid')
    p.add_argument('--bounds',nargs=4,type=float,default=[24,-125,50,-66],metavar=('SOUTH','WEST','NORTH','EAST'))
    p.add_argument('--step',type=float,default=1.0,help='Grid spacing in degrees')
    p.add_argument('--split-at',type=int,default=50,help='Conservative saturation threshold, NOT a verified site limit')
    p.add_argument('--max-depth',type=int,default=2)
    p.add_argument('--max-searches',type=int,help='Limit this invocation; pending searches remain for next run')
    p.add_argument('--max-details',type=int,help='Limit this invocation; pending stores remain for next run')
    p.add_argument('--details-only',action='store_true',help='Process pending detail queue only')
    p.add_argument('--discover-only',action='store_true')
    p.add_argument('--refresh',action='store_true',help='Requeue completed searches/stores once at start')
    p.add_argument('--retry-failed',action='store_true')
    p.add_argument('--headed',action='store_true')
    p.add_argument('--timeout',type=int,default=90)
    p.add_argument('--delay',type=float,default=2)
    a=p.parse_args()
    if a.step<=0 or a.split_at<1 or a.max_depth<0 or a.timeout<1 or a.delay<0 or any(x is not None and x<1 for x in [a.max_searches,a.max_details]):
        p.error('Invalid limits, timeouts, step, or delay')
    s,w,n,e=a.bounds
    if not -90<=s<n<=90 or not -180<=w<e<=180:
        p.error('Invalid geographic bounds')
    if a.details_only and a.discover_only:
        p.error('Choose --details-only or --discover-only')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    a.db.parent.mkdir(parents=True,exist_ok=True)
    failed=0
    with sqlite3.connect(a.db) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        migrate(conn)
        # Stable store IDs prevent duplicate detail jobs even when URLs differ.
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS pilot_queue_store_id ON pilot_detail_queue(store_number) WHERE store_number IS NOT NULL')
        if a.refresh:
            conn.execute("UPDATE pilot_searches SET status='pending' WHERE status='complete'")
            conn.execute("UPDATE pilot_detail_queue SET status='pending' WHERE status='complete'")
        if a.retry_failed:
            conn.execute("UPDATE pilot_searches SET status='pending' WHERE status='failed'")
            conn.execute("UPDATE pilot_detail_queue SET status='pending' WHERE status='failed'")
        if a.zip_csv:
            seed_zips(conn,a.zip_csv)
        if a.seed_url:
            for url in a.seed_url:
                parsed=urlparse(url)
                if parsed.hostname not in ('pilotcompany.com','www.pilotcompany.com'):
                    p.error('Seed URLs must be on pilotcompany.com')
                if detail_links(f'<a href="{url}"></a>',ORIGIN):
                    conn.execute('INSERT OR IGNORE INTO pilot_detail_queue(url,discovered_from) VALUES (?,?)',(url.split('#')[0].split('?')[0],'seed'))
                elif parsed.path.rstrip('/')=='/locations/search':
                    conn.execute('INSERT OR IGNORE INTO pilot_searches(url) VALUES (?)',(url,))
                else:
                    p.error('Seed must be a search or location detail URL')
        elif not a.zip_csv and not a.details_only:
            seed_grid(conn,a.bounds,a.step)
        conn.commit()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=not a.headed)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000})
                searches=0
                while not a.details_only and (a.max_searches is None or searches<a.max_searches):
                    job=next_search_job(conn)
                    if job is None:break
                    if job['zip_code']:
                        logging.info('Searching ZIP %s (population density: %s people/sq mi)',
                                     job['zip_code'],job['population_density'])
                    try:
                        html=load(page,job['url'],a.timeout)
                        soup=BeautifulSoup(html,'html.parser')
                        result_list=soup.select_one('[aria-label="Location results"]')
                        found=detail_links(str(result_list) if result_list else html,job['url'])
                        match=re.search(r'Results\s*\((\d+)\)',text(soup))
                        reported=int(match[1]) if match else None
                        truncated=len(found)>=a.split_at or (reported is not None and len(found)<reported)
                        with conn:
                            queue_links(conn,str(result_list) if result_list else html,job['url'])
                            if truncated and job['depth']<a.max_depth:
                                subdivide(conn,job)
                            conn.execute("UPDATE pilot_searches SET status='complete',result_count=?,reported_count=?,possibly_truncated=?,scraped_at=?,error=NULL WHERE url=?",(len(found),reported,int(truncated),now(),job['url']))
                        logging.info('Search %d: %d links; reported=%s; possibly truncated=%s',searches+1,len(found),reported,truncated)
                    except Exception as exc:
                        failed+=1
                        logging.exception('Search failed: %s',job['url'])
                        with conn:conn.execute("UPDATE pilot_searches SET status='failed',error=? WHERE url=?",(str(exc),job['url']))
                    searches+=1
                    time.sleep(a.delay)
                details=0
                while not a.discover_only and (a.max_details is None or details<a.max_details):
                    job=conn.execute("SELECT * FROM pilot_detail_queue WHERE status='pending' ORDER BY url LIMIT 1").fetchone()
                    if job is None:break
                    try:
                        html=load(page,job['url'],a.timeout,True)
                        with conn:number=save_detail(conn,html,job['url'])
                        logging.info('Store %s saved (%d detail pages this run)',number,details+1)
                    except Exception as exc:
                        failed+=1
                        logging.exception('Store detail failed: %s',job['url'])
                        with conn:conn.execute("UPDATE pilot_detail_queue SET status='failed',error=? WHERE url=?",(str(exc),job['url']))
                    details+=1
                    time.sleep(a.delay)
            finally:
                browser.close()
        for table in ['pilot_searches','pilot_detail_queue']:
            logging.info('%s: %s',table,[tuple(r) for r in conn.execute(f'SELECT status,count(*) FROM {table} GROUP BY status')])
        unresolved=conn.execute('SELECT count(*) FROM pilot_searches WHERE possibly_truncated=1 AND (depth>=? OR south IS NULL)',(a.max_depth,)).fetchone()[0]
        logging.info('%d searches remain possibly truncated; national completeness is not established.',unresolved)
    return 1 if failed else 0


if __name__=='__main__':
    raise SystemExit(main())
