# Roady's truck-stop scraper

Python 3.10 or newer. No third-party packages or browser installation required.
All SQLite tables and views start with `roadys_`. The included database was
populated from the live public locator feed on October 25, 2026 (UTC).

## Run

From the extracted folder:

```bash
python3 scripts/scrape_roadys.py
```

By default, this writes Roady's tables into the repository's shared
`database/chat_history.sqlite3` file. The script creates its required tables
and view automatically. To choose a different SQLite file, pass `--db`:

```bash
python3 scripts/scrape_roadys.py --db /path/to/other.sqlite3
```

The script creates all required tables automatically. Source location IDs are
primary keys. Repeated runs update records instead of creating duplicates.
Each import runs in one transaction: a parsing failure rolls back location and
child-record changes, and the failure is logged in roadys_runs. No other chain's
tables are touched. Facilities retain stable IDs when names/kinds are unchanged.

Optional commands:

```bash
# Import Texas and Oklahoma only
python3 scripts/scrape_roadys.py --states TX OK

# Small test
python3 scripts/scrape_roadys.py --limit 5 --db data/test.sqlite3

# Refresh and save the complete feed for offline use
python3 scripts/scrape_roadys.py --export-json data/roadys_source.json

# Offline import from the included source snapshot
python3 scripts/scrape_roadys.py --input data/roadys_source.json

# Verify parser, duplicate handling, and transaction rollback
python3 -m unittest discover -s tests -p test_roadys.py
```

## Why a public feed instead of hundreds of clicks?

The requested page, https://www.roadys.com/roadys-map, embeds
https://interstate2.roadys.com/service/map. Its public client JavaScript loads
https://interstate2.roadys.com/api/public/locations. That unauthenticated JSON
response includes the complete amenities used by the View location details
screen. The full Rocket Station detail screen was opened and checked against
the source record. The UI had 265 cards, matching the 265 unique locations in
the feed. No ZIP grid or location-summary-only scraping is needed.

The package uses this observed public feed directly and saves every field. It
requires no login, API token or automation of map clicks. If the site changes
its feed or schema, the script fails explicitly rather than reporting success
with an empty result. --url can select a replacement public JSON endpoint only
if it retains the documented response shape.

The live import contains 265 locations, 6,237 location amenity records and 9,090
individual amenity values. This is the set exposed by Roady's map, including
PTP/network partners, service centers, fuel stops, C-stores and truck stops.
It is not a claim to cover all truck stops outside Roady's locator.

## Structured data

- roadys_locations: original location ID, name, street, city, state, postal code,
  coordinates, location-type/group codes, interstate/exit, rewards flag and
  multiplier, directions link, source and timestamps.
- roadys_location_types: the source's location-type lookup table.
- roadys_amenities: source amenity IDs and exact names.
- roadys_location_amenities: complete raw values, simple 0/1 flags, numbers and
  lower-bound markers. Parking `20+` is stored as 20 plus numeric_is_minimum=1,
  not as an exact count. Check the amenity name before interpreting a 0/1 value
  as presence: a pump count of 1 also has a numeric value of 1.
- roadys_amenity_values: one row per comma-separated source item. Preserves
  payment cards, package carriers, food selections, repair certifications,
  surfaces, fuel service types and every other listed value.
- roadys_facilities: separate store, restaurant, lodging and service facilities
  when the source identifies them. `Chain(s)` is the restaurant list;
  `Chain / Name` is the lodging list. Unusual source text is retained verbatim;
  named-facility records are not independently verified businesses.
- roadys_contacts: all exposed store/service phone and fax numbers, facility
  association where identifiable, source label, normalized number, extension and
  original text. Service phone numbers embedded in hours fields are extracted.
- roadys_payment_types / roadys_location_payments: credit/fuel card lookup and
  location junction table.
- roadys_food_selections / roadys_fuel_brands: individual values.
- roadys_fuel_prices: diesel prices in exact integer thousandths of a dollar
  (`price_mills / 1000.0`), with observation timestamps. The source does not
  provide the actual last fuel-price change time.
- roadys_source_fields: every original top-level field, type and JSON value,
  including fields added by the website in future responses.
- roadys_raw_locations: complete original location records, including amenities.
- roadys_runs / roadys_schema_versions: import status/counts/errors and schema.

The source's service menus include tire sales/repair, towing, alignment, body and
paint, welding, parts, truck washing, reefer work, propane, scales, shower counts,
parking, hotel information, accepted cards, and food/restaurant names. All
available entries are retained; absent information is not invented. The feed
currently exposes 72 distinct amenity names. The raw source and generic value
relations retain every name/value even without a specialized derived table.

The source does not expose separate restaurant phone numbers or restaurant
weekly schedules for most locations. None are fabricated. Existing contact or
schedule tables can represent them when the source actually provides them.
External restaurant, hotel, carrier and ordering websites are not crawled.

## Hours: structured with original evidence

- roadys_hours: one row per distinct interval, including opening/closing clock
  values, overnight, explicit 24-hour/closed flags, source amenity, parse status
  and original text.
- roadys_hour_days: applicable weekdays; Monday=0, Sunday=6. Monday-Saturday with
  the same time values uses one hours row plus six weekday relationships.
- roadys_hour_references: records the referenced Service Hours text when a
  repair/parts schedule says SAME. Parsed references use resolved_reference.
- roadys_hours_grouped: joined view with facility identity and ordered weekdays.

Handles common `5:am-11:pm` forms, AM/PM punctuation, midnight/noon, day ranges
such as M-F and Sun-Thurs, prefix/suffix day labels, compound schedules, overnight
closing times, and distinct store/parts/repair/service schedules. Identical rules
are grouped. Explicit 24/7 schedules apply to all seven weekdays.

A clock range without weekday information is parsed with day_scope=unspecified
and no weekday rows. Do not treat this as confirmed seven-day operation.
Missing AM/PM, missing closing times, unusual numeric encodings, compound prose
and source typos stay unparsed. Equal opening and closing clock values are not
automatically declared 24-hour operation. Times are local wall times; timezones,
holiday exceptions and date-specific hours are not inferred.

In the included database there are 468 parsed rules, 83 resolved-reference rules,
and 15 unresolved rules. Unresolved source entries are in
`data/roadys_hours_review.csv`, with location and source context. Some unresolved
rows are partial schedules whose other clauses parsed successfully. SAME can
also inherit an unresolved part of Service Hours; see the reference table.
Preserving an uncertainty is preferable to speaking incorrect operating hours.

## SQL examples

```sql
-- Store hours for a concise text-to-speech response
SELECT location_id,name,source_amenity,weekdays,opens_at,closes_at,
       closes_next_day,is_24_hours,day_scope,parse_status,raw_text
FROM roadys_hours_grouped
WHERE kind='store';

-- Places with at least 50 documented parking spaces
SELECT l.name,l.city,l.state,a.raw_value,a.numeric_is_minimum
FROM roadys_locations l JOIN roadys_location_amenities a USING(location_id)
JOIN roadys_amenities t USING(amenity_id)
WHERE t.name='Parking Spaces' AND a.numeric_value>=50;

-- Restaurant names and phone numbers
SELECT l.name,f.kind,f.name,c.label,c.number,c.extension
FROM roadys_locations l
JOIN roadys_facilities f USING(location_id)
LEFT JOIN roadys_contacts c ON c.facility_id=f.facility_id
ORDER BY l.name,f.kind,f.name;

-- Hours still needing review
SELECT g.location_id,g.name,g.source_amenity,g.raw_text,
       r.referenced_amenity,r.referenced_raw_text
FROM roadys_hours_grouped g
LEFT JOIN roadys_hour_references r USING(hours_id)
WHERE g.parse_status NOT IN ('parsed','resolved_reference');
```

Records absent from later source responses are retained rather than deleted.
Use last_seen_at/last_seen_run to identify their freshness. A filtered or limited
run updates only its selected locations; it does not mark others inactive.

## Included

- scripts/scrape_roadys.py: standalone scraper/importer
- requirements.txt: documents that no pip dependencies are required
- schema.sql: database schema for inspection
- tests/test_roadys.py: focused regression tests
- data/roadys.sqlite3: populated database
- data/roadys_source.json: original complete public feed snapshot
- data/roadys_hours_review.csv: remaining unclear hours
- this README

Validation: a full live download/import succeeded; an identical re-import did not
increase location counts. SQLite integrity_check returned ok, foreign_key_check
returned no errors, and the focused tests passed.
