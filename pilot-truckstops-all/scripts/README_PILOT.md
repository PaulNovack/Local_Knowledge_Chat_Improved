# Pilot, Flying J and ONE9 SQLite scraper

Requires Python 3.10+. Standalone: Love's scripts are not required.

## Install (from the extracted folder)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r scripts/requirements.txt
python -m playwright install chromium
```

On Linux, if Chromium reports missing system libraries, install them with:
`python -m playwright install --with-deps chromium` (may require administrator privileges).

## Small test first

```bash
python scripts/scrape_pilot.py \
  --db data/pilot.sqlite3 \
  --seed-url 'https://pilotcompany.com/locations/search?lat=35.960600&lng=-83.920700&zoom=10' \
  --max-searches 1 --max-details 3 --headed
```

The `--seed-url` argument is repeatable and also accepts a store detail URL.

## Search using all available U.S. ZIP coordinates

```bash
python scripts/download_us_zipcodes.py
python scripts/scrape_pilot.py --zip-csv data/us_zipcodes.csv --db data/pilot.sqlite3
```

The downloader fetches the GeoNames US postal dataset and converts it into
`zip,lat,lng,state` CSV. GeoNames (https://www.geonames.org/) is attributed under
CC BY 4.0. This third-party dataset is not an authoritative, guaranteed complete
USPS roster. Existing CSVs with zip,lat,lng or latitude,longitude are also accepted.
Each ZIP is searched through its representative coordinates, not typed into the
site's autocomplete field. ZIPs with identical coordinates share one search job.
A full ZIP crawl can take many hours because thousands of overlapping searches
are possible. The script intentionally defaults to a two-second pause per page.

## Geographic grid alternative

```bash
python scripts/scrape_pilot.py --db data/pilot.sqlite3
```

Default: overlapping 1-degree searches over the contiguous U.S. bounds
24,-125,50,-66. Use --bounds SOUTH WEST NORTH EAST to change coverage. Alaska,
Hawaii, territories, and Canada are not in the default grid. ZIP CSV mode uses
all valid input coordinates rather than the grid bounds.

## Resume, retry and refresh

Run the same command again: completed search URLs and stores are skipped. Database
queues persist after interruption. Nearby-store links discovered on detail pages
are also queued. To process the discovered detail queue before completing discovery:

```bash
python scripts/scrape_pilot.py --db data/pilot.sqlite3 --details-only
```

Use `--max-searches 100 --max-details 50` for bounded batches, `--retry-failed` to
retry failed jobs, `--refresh` to requeue completed jobs once at startup, or
`--discover-only` to collect links first. Use the same DB path to resume.

Deduplication uses canonical detail URLs and known store numbers. URLs are
collected from pages; store URLs are not guessed. Different URL aliases without
an exposed store number can require a visit to identify them, but they still
update one store row. Child records have unique keys and are refreshed atomically.

## Data model

All names use the pilot_ prefix, so --db can point at the same file as Love's.
Tables are created automatically. Missing scalar columns in pilot_locations are
added automatically. The original Love's data is not modified.

- pilot_locations: store ID, brand, name, address, city/state/postcode/country,
  phone, fax, interstate, coordinates and source URL.
- pilot_amenities / pilot_location_amenities: amenity lookup and store junction,
  including numeric counts (showers, lanes, parking, ATMs, etc.).
- pilot_facilities: store, named restaurants and named truck-care facilities.
- pilot_hours / pilot_hour_days: unique time intervals and applicable weekdays.
  Monday is 0; Sunday is 6. A shared Monday-Saturday interval is stored once,
  with six weekday junction rows. Handles split shifts, overnight intervals,
  24-hour and closed schedules. Unknown schedules remain unknown.
- pilot_hours_grouped: view joining facilities, time rules and weekday lists,
  suitable for concise speech rendering in your application. It does not itself
  generate a spoken sentence.
- pilot_fuel_prices: exact integer thousandths of the displayed dollar amount;
  price_mills / 1000.0 gives displayed price. No currency is inferred.
- pilot_attributes: individually queryable panel facts and contact fields.
- pilot_ev_connectors: charger labels, connector types, rated kW and available
  status fields where exposed in markup.
- pilot_links: order, parking, service and other links within each detail section.
- pilot_sections / pilot_snapshots: raw evidence, text and HTML for additional
  fields not yet mapped to named columns.
- pilot_searches / pilot_detail_queue: resumable discovery and detail jobs,
  status, error messages and result counts.

Examples (requires the sqlite3 CLI):

```sql
SELECT store_number, facility_type, name, weekdays, opens_at, closes_at,
       is_24_hours, is_closed, parse_status
FROM pilot_hours_grouped
ORDER BY store_number, name, hours_id;

SELECT l.store_number, l.name, a.name AS amenity, x.quantity
FROM pilot_locations l
JOIN pilot_location_amenities x USING(store_number)
JOIN pilot_amenities a ON a.id=x.amenity_id
WHERE a.name LIKE '%Shower%';

SELECT status, COUNT(*) FROM pilot_detail_queue GROUP BY status;
SELECT * FROM pilot_searches WHERE possibly_truncated=1;
```

## Scope and validation

The current Pilot page returned 16 search results at the supplied Knoxville seed
and exposes actual store links. Restaurant "Show more hours" and amenity "Show
more" controls are expanded before extraction. Nearby links are followed too.
This is not limited to five results. No verified nationwide result limit was
found. --split-at 50 is a configurable conservative heuristic, not a site limit.
Grid searches at that threshold, or with fewer collected links than the page's
reported count, subdivide up to --max-depth 2. Arbitrary seed/ZIP searches have
no cell boundaries to subdivide and remain flagged when possibly truncated.

Do not equate a finished queue with proof that every national location was found.
ZIP coverage, search radii, missing nearby links, site changes and unverified
result limits can leave gaps. The run reports unresolved potentially truncated
searches. A national authoritative count is not inferred.

Store hours not explicitly shown remain unknown; an Open Now badge is not used
to invent weekly hours. Equal opening/closing clock values are preserved as
an overnight interval rather than assumed to mean 24 hours. An explicit 24-hour
label sets the is_24_hours flag. Times are local wall times; timezone and holiday
exceptions are not inferred. Volatile availability/prices include observation
timestamps and should not be treated as live after the scrape.

Parser/schema/deduplication checks passed using representative markup, including
grouped weekly hours, quantities, exact fuel values and foreign-key integrity.
Live page structure and expansion controls were inspected. Full local browser
execution could not be tested here because the Chromium download was truncated;
a nationwide scrape has not been run. Run the small test before a long crawl.

## Multiple restaurants, service numbers, and expanded details

Every restaurant is a separate pilot_facilities row. Each restaurant card's
Show more hours control is opened, along with collapsed store/service hours
controls when exposed with aria-expanded. The inspected Knoxville store itself
shows an explicit Open 24 Hours label rather than a store-hours dropdown.

pilot_contacts stores multiple phone and fax numbers with facility_id ownership,
label, normalized number, extension, telephone URL, original text, section,
source URL and observation time. Store, restaurant and truck-care contacts are
kept separate. Unlinked US-style numbers in restaurant/service blocks are also
collected. International formatting that cannot be confidently normalized is
preserved. This collects phone numbers exposed by Pilot's location page, not
numbers obtained by crawling external restaurant ordering sites.

pilot_parking_options stores each vehicle category's available spaces, displayed
price in cents, and disabled status. pilot_availability stores timestamped parking,
shower and EV totals/availability and EV price in thousandths per kWh when shown.
pilot_food_offerings stores named food offerings/descriptions/images, while
pilot_images records panel image URLs and alt text. Fuel blends and update labels
are captured independently from the price. All original sections remain available.

```sql
SELECT l.store_number, f.facility_type, f.name, c.contact_type, c.label,
       c.phone_number, c.extension, c.telephone_url
FROM pilot_contacts c
JOIN pilot_locations l USING(store_number)
LEFT JOIN pilot_facilities f ON f.id=c.facility_id
ORDER BY l.store_number, f.name, c.contact_type;
```

The included data/us_zipcodes.csv has 41,488 distinct GeoNames US ZIP coordinate
records downloaded when this package was built. Run the downloader to refresh.
A full ZIP crawl may take days. To collect a manageable discovery batch and then
scrape its stores, use:

```bash
python scripts/scrape_pilot.py --zip-csv data/us_zipcodes.csv --db data/pilot.sqlite3 --max-searches 100 --max-details 200
```

Repeat that command to resume, or use --details-only to finish queued stores
before running more discovery. The ZIP includes no pre-scraped Pilot database.
