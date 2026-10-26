#!/usr/bin/env python3
"""Download the public feed used by Roady's full location-details screen.
Python 3.10+, standard library only. Every table has roadys_ prefix.
python scripts/scrape_roadys.py --db data/truck_stops.sqlite3
"""
import argparse
import hashlib
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SOURCE_PAGE='https://www.roadys.com/roadys-map'
API='https://interstate2.roadys.com/api/public/locations'
DEFAULT_DB=Path(__file__).resolve().parents[2]/'database'/'chat_history.sqlite3'
SCHEMA='''
CREATE TABLE IF NOT EXISTS roadys_schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roadys_location_types(code TEXT PRIMARY KEY, source_id INTEGER, name TEXT);
CREATE TABLE IF NOT EXISTS roadys_locations(
 location_id INTEGER PRIMARY KEY, name TEXT NOT NULL, address TEXT, city TEXT, state TEXT,
 postal_code TEXT, latitude REAL, longitude REAL, location_type_code TEXT,
 group_code TEXT, interstate_exit TEXT, has_rewards INTEGER, rewards_multiplier REAL,
 directions_url TEXT, source_url TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL, last_seen_run INTEGER, content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS roadys_locations_state ON roadys_locations(state);
CREATE TABLE IF NOT EXISTS roadys_amenities(amenity_id INTEGER PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roadys_location_amenities(
 location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),
 amenity_id INTEGER NOT NULL REFERENCES roadys_amenities(amenity_id),
 raw_value TEXT, boolean_value INTEGER, numeric_value REAL, numeric_is_minimum INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(location_id,amenity_id)
);
CREATE TABLE IF NOT EXISTS roadys_amenity_values(
 location_id INTEGER NOT NULL,amenity_id INTEGER NOT NULL,value_index INTEGER NOT NULL,
 value TEXT NOT NULL, PRIMARY KEY(location_id,amenity_id,value_index),
 FOREIGN KEY(location_id,amenity_id) REFERENCES roadys_location_amenities(location_id,amenity_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS roadys_facilities(
 facility_id INTEGER PRIMARY KEY,location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),
 kind TEXT NOT NULL,name TEXT NOT NULL,source_amenity TEXT,
 UNIQUE(location_id,kind,name)
);
CREATE TABLE IF NOT EXISTS roadys_contacts(
 contact_id INTEGER PRIMARY KEY, location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),
 facility_id INTEGER REFERENCES roadys_facilities(facility_id), kind TEXT NOT NULL,
 label TEXT NOT NULL, number TEXT NOT NULL, extension TEXT NOT NULL DEFAULT '',raw_value TEXT NOT NULL,
 UNIQUE(location_id,label,number,extension)
);
CREATE TABLE IF NOT EXISTS roadys_hours(
 hours_id INTEGER PRIMARY KEY,facility_id INTEGER NOT NULL REFERENCES roadys_facilities(facility_id),
 source_amenity TEXT NOT NULL,rule_number INTEGER NOT NULL,opens_at TEXT,closes_at TEXT,
 closes_next_day INTEGER NOT NULL DEFAULT 0,is_24_hours INTEGER NOT NULL DEFAULT 0,
 is_closed INTEGER NOT NULL DEFAULT 0,day_scope TEXT NOT NULL,parse_status TEXT NOT NULL,
 raw_text TEXT NOT NULL, UNIQUE(facility_id,source_amenity,rule_number)
);
CREATE TABLE IF NOT EXISTS roadys_hour_days(
 hours_id INTEGER NOT NULL REFERENCES roadys_hours(hours_id) ON DELETE CASCADE,
 weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),PRIMARY KEY(hours_id,weekday)
);
CREATE TABLE IF NOT EXISTS roadys_hour_references(
 hours_id INTEGER PRIMARY KEY REFERENCES roadys_hours(hours_id) ON DELETE CASCADE,
 referenced_amenity TEXT NOT NULL, referenced_raw_text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roadys_payment_types(payment_id INTEGER PRIMARY KEY,kind TEXT NOT NULL,name TEXT NOT NULL,UNIQUE(kind,name));
CREATE TABLE IF NOT EXISTS roadys_location_payments(
 location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),payment_id INTEGER NOT NULL REFERENCES roadys_payment_types(payment_id),
 PRIMARY KEY(location_id,payment_id)
);
CREATE TABLE IF NOT EXISTS roadys_food_selections(location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),name TEXT NOT NULL,PRIMARY KEY(location_id,name));
CREATE TABLE IF NOT EXISTS roadys_fuel_brands(location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),name TEXT NOT NULL,PRIMARY KEY(location_id,name));
CREATE TABLE IF NOT EXISTS roadys_fuel_prices(
 location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),fuel_type TEXT NOT NULL,
 price_mills INTEGER NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(location_id,fuel_type)
);
CREATE TABLE IF NOT EXISTS roadys_source_fields(
 location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),field_name TEXT NOT NULL,
 value_type TEXT NOT NULL,value_json TEXT NOT NULL,PRIMARY KEY(location_id,field_name)
);
CREATE TABLE IF NOT EXISTS roadys_raw_locations(
 location_id INTEGER PRIMARY KEY REFERENCES roadys_locations(location_id),payload_json TEXT NOT NULL,observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roadys_runs(
 run_id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,
 source_url TEXT NOT NULL,source_count INTEGER,selected_count INTEGER,saved_count INTEGER DEFAULT 0,
 error TEXT
);
CREATE VIEW IF NOT EXISTS roadys_hours_grouped AS
SELECT f.location_id,f.name,f.kind,h.*,
 (SELECT group_concat(weekday,',') FROM
  (SELECT weekday FROM roadys_hour_days d WHERE d.hours_id=h.hours_id ORDER BY weekday)) AS weekdays
FROM roadys_hours h JOIN roadys_facilities f USING(facility_id);
'''


def now():return datetime.now(timezone.utc).isoformat()


def migrate(conn):
    conn.executescript(SCHEMA)
    conn.execute('INSERT OR IGNORE INTO roadys_schema_versions VALUES (1,?)',(now(),))
    conn.commit()


def fetch(url,timeout,retries):
    for attempt in range(retries+1):
        try:
            with urlopen(Request(url,headers={'User-Agent':'RoadysLocationScraper/1.0','Accept':'application/json'}),timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            if exc.code not in (429,500,502,503,504) or attempt==retries:raise
        except (URLError,TimeoutError):
            if attempt==retries:raise
        time.sleep(min(2**attempt,30))


def validate(payload):
    if not isinstance(payload,dict) or payload.get('success') is not True:
        raise ValueError('Public feed did not return success=true')
    data=payload.get('data',{})
    rows=data.get('locations')
    if not isinstance(rows,list) or not rows:raise ValueError('Feed has no locations; refusing empty update')
    by_id={}
    for row in rows:
        if not isinstance(row,dict) or not str(row.get('locationid','')).isdigit() or not row.get('locationname'):
            raise ValueError('Location missing a valid source ID/name')
        if not isinstance(row.get('amenities'),list):raise ValueError('Location has no full amenities list')
        identity=int(row['locationid'])
        if identity in by_id and by_id[identity]!=row:raise ValueError(f'Conflicting duplicate location ID {identity}')
        for a in row['amenities']:
            if not str(a.get('id','')).isdigit() or not a.get('name'):raise ValueError('Invalid amenity record')
        by_id[identity]=row
    return list(by_id.values()),data.get('location_types',[])


DAYS={'m':0,'mon':0,'monday':0,'tu':1,'tue':1,'tues':1,'tuesday':1,'w':2,'wed':2,'wednesday':2,'th':3,'thur':3,'thurs':3,'thu':3,'thursday':3,'f':4,'fri':4,'friday':4,'sat':5,'saturday':5,'sun':6,'sunday':6}
DAY=r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Thurs|Tues|Thur|Mon|Tue|Wed|Thu|Fri|Sat|Sun|Th|Tu|M|W|F)'
CLOCK=r'(?:\d{1,2}(?::\d{2})?\s*:?\s*[ap]\.?\s*m\.?|midnight|noon)'
RANGE=re.compile(r'('+CLOCK+r')\s*(?:-|–|—|to)\s*('+CLOCK+r')',re.I)


def clock(raw):
    value=re.sub(r'[.\s]','',raw.lower())
    if value=='midnight':return '00:00'
    if value=='noon':return '12:00'
    value=re.sub(r':(?=[ap]m$)','',value)
    match=re.fullmatch(r'(\d{1,2})(?::(\d{2}))?([ap])m',value)
    if not match:raise ValueError('Ambiguous clock value')
    h,m=int(match[1]),int(match[2] or 0)
    if not 1<=h<=12 or m>59:raise ValueError('Invalid clock value')
    return f'{h%12+(12 if match[3]=="p" else 0):02d}:{m:02d}'


def weekday_set(label):
    result=set()
    for match in re.finditer(r'\b('+DAY+r')\s*-\s*('+DAY+r')\b',label,re.I):
        first,last=DAYS[match[1].lower()],DAYS[match[2].lower()]
        result.update((first+i)%7 for i in range((last-first)%7+1))
    for match in re.finditer(r'\b('+DAY+r')\b',label,re.I):result.add(DAYS[match[1].lower()])
    if re.search(r'7\s*days?(?: a week)?|all week|daily|every day',label,re.I):result=set(range(7))
    return sorted(result)


def parse_hours(raw):
    """Keep ambiguous clocks/days unknown; parse clear prefix/suffix day rules."""
    source=str(raw or '').strip()
    base={'opens_at':None,'closes_at':None,'closes_next_day':0,'is_24_hours':0,'is_closed':0,
          'day_scope':'unspecified','parse_status':'unparsed','raw_text':source,'days':[]}
    if re.fullmatch(r'(?:open\s*)?(?:24\s*(?:hours?|hrs?)|24/7)',source,re.I):
        return [dict(base,is_24_hours=1,day_scope='daily',parse_status='parsed',days=list(range(7)))]
    if source.upper()=='SAME':return [dict(base,parse_status='reference_unresolved')]
    if re.fullmatch('closed',source,re.I):return [dict(base,is_closed=1,parse_status='parsed')]
    normalized=re.sub(r'\b(?:thru|through)\b','-',source,flags=re.I)
    normalized=re.sub(r'('+DAY+r')\s+to\s+('+DAY+r')',r'\1-\2',normalized,flags=re.I)
    normalized=re.sub(r'\band\b','&',normalized,flags=re.I)
    normalized=re.sub(r'('+DAY+r')(?=\d)',r'\1 ',normalized,flags=re.I)
    def clean_days(label):
        clean=re.sub(r'\b('+DAY+r')\b','',label,flags=re.I)
        clean=re.sub(r'7\s*days?(?: a week)?|all week|daily|every day','',clean,flags=re.I)
        return not re.sub(r'[\s&/\-.]','',clean)
    rules=[]
    for clause in re.split(r'\s*[,;]\s*',normalized):
        found=list(RANGE.finditer(clause))
        if not found:
            days=weekday_set(clause)
            rules.append(dict(base,days=days,day_scope='weekly' if days else 'unspecified'))
            continue
        prefix=bool(weekday_set(clause[:found[0].start()]))
        for index,match in enumerate(found):
            if prefix:
                label=clause[found[index-1].end() if index else 0:match.start()]
                if index==len(found)-1 and not clean_days(clause[match.end():]):return [base]
            else:
                label=clause[match.end():found[index+1].start() if index+1<len(found) else len(clause)]
                if index==0 and not clean_days(clause[:match.start()]):return [base]
            if not clean_days(label):return [base]
            try:opening,closing=clock(match[1]),clock(match[2])
            except ValueError:return [base]
            days=weekday_set(label)
            rules.append(dict(base,opens_at=opening,closes_at=closing,closes_next_day=int(closing<=opening),
                              day_scope='weekly' if days else 'unspecified',parse_status='parsed',days=days))
    # A bare initial interval followed by day-specific clauses is ambiguous.
    # Do not silently assign its days to the following interval.
    if len(rules)>1 and rules[0]['parse_status']=='parsed' and not rules[0]['days'] and any(r['days'] for r in rules[1:]):
        return [base]
    # If two intervals claim the same weekday, retain both (split shifts).
    grouped={}
    for rule in rules:
        key=tuple(rule[k] for k in ['opens_at','closes_at','closes_next_day','is_24_hours','is_closed','day_scope','parse_status'])
        if key not in grouped:grouped[key]=rule
        else:grouped[key]['days']=sorted(set(grouped[key]['days']+rule['days']))
    return list(grouped.values()) or [base]


def split_values(raw):
    return list(dict.fromkeys(x.strip() for x in str(raw).split(',') if x.strip()))


def phone_parts(raw):
    value=raw.strip();ext=re.search(r'(?:ext\.?|x)\s*(\d+)\s*$',value,re.I)
    extension=ext[1] if ext else ''
    if ext:value=value[:ext.start()]
    digits=re.sub(r'\D','',value)
    if len(digits)==10:value='+1'+digits
    elif len(digits)==11 and digits[0]=='1':value='+'+digits
    return value,extension


def facility(conn,identity,kind,name,source):
    conn.execute('INSERT OR IGNORE INTO roadys_facilities(location_id,kind,name,source_amenity) VALUES (?,?,?,?)',(identity,kind,name,source))
    return conn.execute('SELECT facility_id FROM roadys_facilities WHERE location_id=? AND kind=? AND name=?',(identity,kind,name)).fetchone()[0]


def contact(conn,identity,fid,label,raw,kind='phone'):
    number,extension=phone_parts(raw)
    conn.execute('INSERT OR IGNORE INTO roadys_contacts(location_id,facility_id,kind,label,number,extension,raw_value) VALUES (?,?,?,?,?,?,?)',
                 (identity,fid,kind,label,number,extension,raw))


def save_location(conn,row,stamp,run_id,url):
    identity=int(row['locationid']);raw_json=json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    digest=hashlib.sha256(raw_json.encode()).hexdigest()
    lat,lon=row.get('locationlat'),row.get('locationlong')
    directions=f'https://www.google.com/maps/dir/?api=1&destination={lat},{lon}' if lat is not None and lon is not None else None
    conn.execute('''INSERT INTO roadys_locations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(location_id) DO UPDATE SET name=excluded.name,address=excluded.address,city=excluded.city,
    state=excluded.state,postal_code=excluded.postal_code,latitude=excluded.latitude,longitude=excluded.longitude,
    location_type_code=excluded.location_type_code,group_code=excluded.group_code,interstate_exit=excluded.interstate_exit,
    has_rewards=excluded.has_rewards,rewards_multiplier=excluded.rewards_multiplier,directions_url=excluded.directions_url,
    source_url=excluded.source_url,last_seen_at=excluded.last_seen_at,last_seen_run=excluded.last_seen_run,content_hash=excluded.content_hash''',
    (identity,row['locationname'],row.get('locationaddress'),row.get('locationcity'),row.get('locationstate'),row.get('locationzip'),
     lat,lon,row.get('locationtype'),row.get('locationgroup'),row.get('locationlocation'),
     None if row.get('has_rewards') is None else int(bool(row['has_rewards'])),row.get('rewards_multiplier'),directions,url,stamp,stamp,run_id,digest))
    for table in ['roadys_contacts','roadys_hours','roadys_location_amenities','roadys_location_payments','roadys_food_selections','roadys_fuel_brands','roadys_fuel_prices','roadys_source_fields']:
        if table=='roadys_hours':conn.execute('DELETE FROM roadys_hours WHERE facility_id IN (SELECT facility_id FROM roadys_facilities WHERE location_id=?)',(identity,))
        else:conn.execute(f'DELETE FROM {table} WHERE location_id=?',(identity,))
    store=facility(conn,identity,'store',row['locationname'],'locationname')
    active={store}
    if row.get('locationphonenumber'):contact(conn,identity,store,'Store phone',row['locationphonenumber'])
    for key,value in row.items():
        conn.execute('INSERT INTO roadys_source_fields VALUES (?,?,?,?)',(identity,key,type(value).__name__,json.dumps(value,ensure_ascii=False)))
    for a in row['amenities']:
        aid,name=int(a['id']),a['name'];raw=a.get('value');value='' if raw is None else str(raw).strip()
        conn.execute('INSERT INTO roadys_amenities VALUES (?,?) ON CONFLICT(amenity_id) DO UPDATE SET name=excluded.name',(aid,name))
        numeric=re.fullmatch(r'(\d+(?:\.\d+)?)(\+)?',value)
        boolean=int(value) if value in ('0','1') else None
        conn.execute('INSERT INTO roadys_location_amenities VALUES (?,?,?,?,?,?)',
                     (identity,aid,raw,boolean,float(numeric[1]) if numeric else None,int(bool(numeric and numeric[2]))))
        for i,item in enumerate(split_values(value)):
            conn.execute('INSERT INTO roadys_amenity_values VALUES (?,?,?,?)',(identity,aid,i,item))
        if name in ('Accepted Credit Cards','Accepted Fuel Cards'):
            kind='fuel' if name=='Accepted Fuel Cards' else 'credit'
            for item in split_values(value):
                conn.execute('INSERT OR IGNORE INTO roadys_payment_types(kind,name) VALUES (?,?)',(kind,item))
                conn.execute('INSERT OR IGNORE INTO roadys_location_payments SELECT ?,payment_id FROM roadys_payment_types WHERE kind=? AND name=?',(identity,kind,item))
        if name in ('Food Selection','Gas Brand'):
            table='roadys_food_selections' if name=='Food Selection' else 'roadys_fuel_brands'
            for item in split_values(value):conn.execute(f'INSERT OR IGNORE INTO {table} VALUES (?,?)',(identity,item))
        fid=None
        if name in ('Chain(s)','Chain / Name'):
            kind='restaurant' if name=='Chain(s)' else 'lodging'
            for item in split_values(value):active.add(facility(conn,identity,kind,item,name))
        if 'hours' in name.lower() or name=='Open 24/7':
            if name=='Open 24/7' and value not in ('1','true','True'):continue
            fid=store if name in ('Store Hours','Open 24/7') else facility(conn,identity,'service',name.removesuffix(' Hours'),name)
            active.add(fid)
            reference=None
            schedule='24/7' if name=='Open 24/7' else value
            if value.upper()=='SAME' and name!='Service Hours':
                reference=next((str(x['value']) for x in row['amenities'] if x['name']=='Service Hours' and str(x.get('value','')).upper()!='SAME'),None)
                if reference:schedule=reference
            for index,rule in enumerate(parse_hours(schedule),1):
                if reference:
                    rule['raw_text']=value
                    if rule['parse_status']=='parsed':rule['parse_status']='resolved_reference'
                hid=conn.execute('INSERT INTO roadys_hours(facility_id,source_amenity,rule_number,opens_at,closes_at,closes_next_day,is_24_hours,is_closed,day_scope,parse_status,raw_text) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (fid,name,index,rule['opens_at'],rule['closes_at'],rule['closes_next_day'],rule['is_24_hours'],rule['is_closed'],rule['day_scope'],rule['parse_status'],rule['raw_text'])).lastrowid
                conn.executemany('INSERT INTO roadys_hour_days VALUES (?,?)',[(hid,day) for day in rule['days']])
                if reference:conn.execute('INSERT INTO roadys_hour_references VALUES (?,?,?)',(hid,'Service Hours',reference))
        # A few service-hours fields actually contain repair phone numbers.
        for match in re.finditer(r'(?:\+?1[ .-]?)?(?:\(\d{3}\)|\b\d{3})[ .-]?\d{3}[ .-]\d{4}(?:\s*(?:ext\.?|x)\s*\d+)?',value,re.I):
            contact(conn,identity,fid,name,match[0],'fax' if 'fax' in name.lower() else 'phone')
    for old in conn.execute('SELECT facility_id FROM roadys_facilities WHERE location_id=?',(identity,)).fetchall():
        if old[0] not in active:conn.execute('DELETE FROM roadys_facilities WHERE facility_id=?',(old[0],))
    if row.get('diesel_price') is not None:
        conn.execute('INSERT INTO roadys_fuel_prices VALUES (?,?,?,?)',(identity,'diesel',int(Decimal(str(row['diesel_price']))*1000),stamp))
    conn.execute('INSERT INTO roadys_raw_locations VALUES (?,?,?) ON CONFLICT(location_id) DO UPDATE SET payload_json=excluded.payload_json,observed_at=excluded.observed_at',(identity,raw_json,stamp))


def main():
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db',type=Path,default=DEFAULT_DB)
    p.add_argument('--url',default=API,help='Public locator feed URL')
    p.add_argument('--input',type=Path,help='Import a previously downloaded JSON feed instead of network')
    p.add_argument('--export-json',type=Path,help='Optionally save the downloaded source feed')
    p.add_argument('--states',nargs='+',help='Optional state codes, space or comma separated')
    p.add_argument('--limit',type=int,help='Optional small test import limit')
    p.add_argument('--timeout',type=int,default=60)
    p.add_argument('--retries',type=int,default=2)
    a=p.parse_args()
    if a.timeout<1 or a.retries<0 or (a.limit is not None and a.limit<1):p.error('Invalid timeout, retries, or limit')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    a.db.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(a.db) as conn:
        conn.execute('PRAGMA foreign_keys=ON');conn.execute('PRAGMA journal_mode=WAL');migrate(conn)
        run=conn.execute("INSERT INTO roadys_runs(started_at,status,source_url) VALUES (?,'running',?)",(now(),a.url)).lastrowid;conn.commit()
        try:
            payload=json.loads(a.input.read_text(encoding='utf-8')) if a.input else fetch(a.url,a.timeout,a.retries)
            rows,types=validate(payload)
            source_count=len(rows)
            if a.export_json:
                a.export_json.parent.mkdir(parents=True,exist_ok=True)
                a.export_json.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
            states={s.upper() for token in (a.states or []) for s in token.split(',')}
            if states:rows=[r for r in rows if r.get('locationstate','').upper() in states]
            if a.limit:rows=rows[:a.limit]
            stamp=now()
            # One transaction: a parse error cannot leave half-refreshed child tables.
            with conn:
                for t in types:
                    conn.execute('INSERT INTO roadys_location_types VALUES (?,?,?) ON CONFLICT(code) DO UPDATE SET source_id=excluded.source_id,name=excluded.name',(t['locationtype'],t.get('locationtypeid'),t.get('locationtypename')))
                for row in rows:save_location(conn,row,stamp,run,a.url)
                conn.execute("UPDATE roadys_runs SET status='complete',finished_at=?,source_count=?,selected_count=?,saved_count=? WHERE run_id=?",(now(),source_count,len(rows),len(rows),run))
            logging.info('Saved %d unique locations of %d in source to %s',len(rows),source_count,a.db)
            logging.info('Total stored locations: %d; unparsed hours: %d',conn.execute('SELECT count(*) FROM roadys_locations').fetchone()[0],conn.execute("SELECT count(*) FROM roadys_hours WHERE parse_status NOT IN ('parsed','resolved_reference')").fetchone()[0])
        except Exception as exc:
            with conn:conn.execute("UPDATE roadys_runs SET status='failed',finished_at=?,error=? WHERE run_id=?",(now(),str(exc),run))
            logging.exception('Import failed; existing location data preserved')
            return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
