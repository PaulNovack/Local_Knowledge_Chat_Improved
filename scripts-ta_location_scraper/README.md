# TA / Petro location workbook ingester

Python 3.10 or newer. Automatically downloads the current location-master XLSX
from https://www.ta-petro.com/location/ and imports it into SQLite. Also supports
a local XLSX file. All tables and views have a `ta_` prefix and can coexist with
Love's, Pilot and Roady's tables in the same database.

## Install

```bash
python3 -m pip install -r requirements.txt
```

## Download the current workbook and import

From the extracted folder:

```bash
python3 scripts/ingest_locations.py
```

The script reads the location page, finds its `Download Location Data` form,
By default, the script imports the TA-prefixed tables into the application's
`database/chat_history.sqlite3`; existing chat and other data are preserved.
Pass `--db PATH` to write to a different SQLite database. The script reads the
location page, finds its `Download Location Data` form,
then downloads the spreadsheet through that form's GET action. It does not guess
a dated filename. The observed form currently uses
https://www.ta-petro.com/api/locationsdata/download/ .
Downloaded workbooks are saved in data/downloads/. A temporary file is checked
for a valid XLSX archive before it replaces a local download. No login, browser,
API token or map-coordinate searches are required. If the form changes, it fails
explicitly rather than silently importing an HTML error page.

## Import a local file instead

```bash
python3 scripts/ingest_locations.py data/location_master.xlsx
```

Use any future filename as the positional argument. --sheet sheet1 restricts the
import to one sheet. Otherwise all sheets with the required location headers are
imported; other sheets are reported and skipped.

Other options:

```bash
python3 scripts/ingest_locations.py --download-dir data/downloads
python3 scripts/ingest_locations.py --db data/ta_locations.sqlite3
python3 scripts/ingest_locations.py --help
python3 -m unittest discover -s tests
```

## Contents and data model

The included database contains all 357 source rows. Brands are TA (193), TA
Express (81), Petro (77), Goasis (4) and Thorntons (2). The included workbook is
an unchanged copy of the uploaded file. The separate download step was also
successfully tested against the TA/Petro website and returned 357 locations.

- ta_locations: Site ID, Location ID, brand, state, city, name, directions,
  street, PO box, postal code, coordinates and gas brand. Site IDs and ZIP codes
  are TEXT to retain leading zeros. Site ID is the stable primary key; Location
  ID is unique when provided. Site 0487 has no source Location ID and is retained
  with NULL in that field, rather than assigned an invented ID.
- ta_facilities: store, full-service restaurants, quick-service restaurants,
  in-bay service and emergency roadside assistance. Restaurants are separate
  rows, even when a location contains several.
- ta_contacts: phone/fax numbers, extensions, original text, and facility links.
  The source has store contacts; restaurant/service contacts are not invented.
- ta_amenities / ta_location_amenities: a lookup and per-location junction table
  for all counts, convenience amenities, repair services, health services and
  fitness features. Blank means unknown/unreported (NULL), not confirmed absent.
  Explicit zero remains zero. 'y' is available=1. Counts have their own numeric
  quantity. A named Truck Wash is retained as available with the original value.
- ta_hours / ta_hour_days: structured facility-specific schedules. Identical
  weekday intervals share one hours row; Monday=0 through Sunday=6. Handles
  day ranges, overnight closing, explicit 24 Hours / 24/7/365, and closed days.
- ta_hours_grouped: facility information, hours rules and ordered weekday lists
  for concise text-to-speech summaries in your app.
- ta_source_columns / ta_source_rows: all 97 original column positions/labels
  and all raw row values (JSON arrays), without losing duplicate header names.
- ta_imports: filename, SHA-256, status, import timestamps, row count and errors.
- ta_import_issues: unresolved schedules, missing optional identifiers, unknown
  flags and conflicting duplicate-header values.

The header 'Interstate Speedzone Wifi' appears twice. Both columns are retained
in raw source rows. Their values are consolidated into one amenity only if they
agree or one is blank. Disagreements are flagged and the derived availability
is left unknown. raw_values_json preserves both entries.

## Hours and source limitations

All nonblank schedule texts in the workbook parsed except one Emergency Roadside
Assistance Hours value that says only 'Yes'. It remains unknown and appears in
`data/import_review.csv`. Missing schedules remain missing. Actual restaurant
hours and store opening hours are not present in this workbook and are not
inferred from the truck-service schedules.

The source repeatedly uses `12:00 AM - 12:00 AM` for full-day operation. These
rules have is_24_hours=1, closes_next_day=1 and
parse_status='interpreted_24_hours', explicitly recording the interpretation.
An explicit '24 Hours' has parse_status='parsed'. Noon and midnight remain
distinct: 12:00 AM - 12:00 PM is a 12-hour interval, not a 24-hour schedule.
12:00 AM - 11:59 PM is retained literally. Source typos such as a closing 11:00 AM
are not silently changed to PM. Times are local clock times; timezones and holiday
exceptions are not inferred.

Current database hours rules: 325 interpreted full-day, 217 parsed, 202 missing,
and 1 unknown. Data and schedule status remain queryable for your application.

## Repeated imports and errors

Run the command again to refresh. Site IDs prevent duplicate locations; facility
identity and child-table unique keys prevent duplicate restaurants, amenities,
contacts and schedules. Each selected location's children are refreshed in one
transaction; removed facilities/amenities disappear for that location. Locations
absent from a later workbook are retained, not deleted. last_import_id indicates
freshness. The source workbook is never modified.

Missing Site IDs, conflicting duplicate IDs, malformed coordinate/count values,
formula cells, and database errors stop the import. The location changes roll
back together while the failed run is recorded. Formula workbooks must first be
exported as calculated values; this ingester does not execute Excel formulas.

## Query examples

```sql
SELECT site_id, brand, city, state, name FROM ta_locations;

SELECT site_id,name,weekdays,opens_at,closes_at,closes_next_day,
       is_24_hours,is_closed,parse_status
FROM ta_hours_grouped WHERE kind='service';

SELECT l.name,a.name,x.availability,x.quantity
FROM ta_locations l JOIN ta_location_amenities x USING(site_id)
JOIN ta_amenities a USING(amenity_id)
WHERE a.name IN ('Showers','Truck Parking Spaces','Tire Services');

SELECT l.name,f.kind,f.name,c.kind,c.number,c.extension
FROM ta_locations l JOIN ta_facilities f USING(site_id)
LEFT JOIN ta_contacts c USING(facility_id);
```

Package includes script, requirements, schema.sql, tests, populated database,
unchanged sample workbook and the latest review CSV. A full website download and
import passed. Repeated imports preserved location/child counts, SQLite integrity
and foreign-key checks passed, and three focused regression tests passed.
