# Truck-stop state scraper and SQLite importer

This directory is a standalone copy of the restaurant workflow adapted to
truck-stop data. It downloads Geofabrik state extracts, writes one JSON file per
state, and imports those records into separate `truck_stops` and
`truck_stop_names` tables in the project SQLite database.

## Setup

Python 3.9+ and curl are required. From the repository root:

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -r scripts-all-truck-stops/requirements.txt

If venv creation fails because ensurepip is missing, install the Linux
`python3-venv` package first.

## Download state files

Try one state:

    python scripts-all-truck-stops/download_truck_stops.py --states TX

Download all states and Washington, DC:

    python scripts-all-truck-stops/download_truck_stops.py

By default, state JSON, the extraction cache, and the run report are written in
this directory. Output files are named `truck_stops_TX.json` and
`truck_stops_CA.json`. Existing completed state files are skipped on reruns.
Use `--refresh-data` to fetch the provider's current snapshot; `--force` rewrites
JSON from cached extracted results without downloading again. The optional
`--pbf-dir PATH` reads existing `STATE.osm.pbf` files without network access.

State PBFs can exceed 1 GB. The downloader removes a PBF after successful parsing
unless `--keep-pbf` is set. Download and parsed-result caches remain in
`truck_stop_extract_cache` by default. `--output PATH` and `--cache PATH` change
those directories.

## Import into SQLite

From the repository root:

    python scripts-all-truck-stops/ingest_truck_stops.py --states TX

Omit `--states TX` to import all matching state JSON files. The default database
is `database/chat_history.sqlite3`; use `--db PATH` to select another SQLite
file. The importer creates its tables and indexes if they do not exist, and
upserts records by OSM type and ID, so rerunning an import is safe. It does not
modify the restaurant tables.

## Truck-stop matching

`truck_stop_brands.json` is a curated supplemental alias list, not a complete
catalog of every national, regional, or independent operator. The scraper uses
it only in combination with a truck-stop context. Primary signals include
`amenity=truck_stop`, `highway=services`, HGV-capable fuel tags, and recognized
brand names at fuel sites. Generic fuel stations are not included solely because
they sell fuel. OpenStreetMap tagging and coverage vary, so review results for
the states and use cases that matter to you.

All mapped OSM tags are retained in `tags_json`, alongside normalized fields for
addresses, contact details, HGV access, parking, and diesel. Nodes use their
coordinates; ways and relations use approximate centers. Incomplete geometry is
marked with `_geometry_warning` in the JSON. Geofabrik regional boundaries can
overlap; deduplicate a combined export by OSM type and ID.

The downloader uses static Geofabrik extracts rather than Overpass. Downloads
use provider MD5 verification, resumable partial files, atomic JSON writes, and
a cache lock. Progress and state outcomes are written to
`truck_stop_extract_report.json`.

Map data © OpenStreetMap contributors, ODbL:
https://www.openstreetmap.org/copyright
Geofabrik: https://download.geofabrik.de/north-america/us.html
Parser: https://docs.osmcode.org/pyosmium/latest/
