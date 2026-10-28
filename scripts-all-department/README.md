# Department Store Extractor

This package downloads Geofabrik state extracts and saves configured U.S. department and off-price store locations into per-brand SQLite tables. The catalog is curated for broad national and regional coverage, not an exhaustive or ranked list.

## Setup

From the repository root:

```sh
python -m pip install -r scripts-all-department/requirements.txt
python scripts-all-department/download_department.py --states TX
```

Run without filters for every configured brand in all 50 states plus Washington, DC. Start with one state first:

```sh
python scripts-all-department/download_department.py --states TX --brands macys kohls dillards
python scripts-all-department/download_department.py --list-brands
```

## SQLite output

The default database is `database/chat_history.sqlite3`. Each brand is stored in a table named `department_<slug>`, for example `department_macys` or `department_nordstrom`. `department_scrape_runs` records source checksums, state, row counts, and processing time.

Tables include OSM type and ID, source state, name, coordinates, address, phone, website, shop type, hours, full tags JSON, geometry warnings, and source metadata. Way and relation objects use an approximate center when geometry is available.

Useful options:

```sh
python scripts-all-department/download_department.py --states CA NY
python scripts-all-department/download_department.py --states TX --brands macys nordstrom_rack tj_maxx
python scripts-all-department/download_department.py --db /path/to/locations.sqlite3 --states WA
python scripts-all-department/download_department.py --pbf-dir /path/to/local-pbf-folder --states TX
python scripts-all-department/download_department.py --refresh-data --states TX
python scripts-all-department/download_department.py --keep-pbf --states TX
```

The source/extraction cache and `department_extract_report.json` are under this directory by default. Interrupted downloads retain partial files; rerun the same command to resume. Normal reruns reuse cached extracts and upsert state rows. `--refresh-data` downloads the current provider snapshot and refreshes the selected states.

Only configured brands tagged with supported OSM `shop` values are included. Coverage and store details depend on OpenStreetMap contributions. Data is © OpenStreetMap contributors under the ODbL; this uses Geofabrik extracts, not Overpass.

- OpenStreetMap: https://www.openstreetmap.org/copyright
- Geofabrik extracts: https://download.geofabrik.de/north-america/us.html
