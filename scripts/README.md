# Restaurant downloaders — static extracts, version 3

This replaces the Overpass approach. No Overpass server, API token, state-boundary
query, or public query execution is required. Python controls curl file downloads
and reads Geofabrik's regional OSM PBF files locally using pyosmium.

## Install and run

Keep ALL Python files from this archive together. Copy them into the folder where
you ran the previous scripts, alongside your existing mcdonalds_states,
subway_states, and other chain output folders. Replacing the scripts does not
require deleting any data folders.

In your existing Python virtual environment:

    python -m pip install -r requirements.txt
    python download_subway.py

If you need a new environment (Ubuntu/Linux):

    python3 -m venv restaurant_env
    source restaurant_env/bin/activate
    python -m pip install -r requirements.txt
    python download_subway.py

If venv creation reports missing ensurepip, install python3-venv first. curl is
also required for network downloads; install with sudo apt install curl if absent.
The offline --pbf-dir mode does not need curl. Python 3.9+ is required.

All seven chains in one run, sharing each state download and local parse:

    python download_restaurants.py

Selected states or chains:

    python download_subway.py --states AL MS
    python download_restaurants.py --states TX LA OK --brands subway mcdonalds

Individual entry scripts:

    python download_mcdonalds.py
    python download_burger_king.py
    python download_dominos.py
    python download_pizza_hut.py
    python download_wendys.py
    python download_popeyes.py
    python download_subway.py

Existing valid completed JSON files are skipped. Run from the same working
folder as before, or point --root at the parent of your chain output folders.
For a single brand, --output supports a specific output directory just as before.
Old Overpass checkpoints are no longer used; completed state JSON is reused.

## Recovery and caching

* Each needed state is a regular HTTPS download from Geofabrik, over IPv4.
* curl reconnects/retries failures, detects a stalled transfer, and resumes the
  .part file after interruption. A checksum sidecar associates it with a specific
  source snapshot. If the provider changes its latest snapshot, a partial file
  is restarted safely instead of joining bytes from different snapshots.
* Downloads are MD5-verified using the provider's published integrity checksum
  before parsing. A mismatch triggers a fresh attempt, never a completed JSON.
* Each state is scanned locally for all seven chains, even if only one is
  currently selected. Small extracted results are shared by subsequent scripts.
* By default the large PBF is removed ONLY after a successful extracted-cache
  write. Use --keep-pbf to retain it. Partial downloads remain after failures.
* Final JSON files are flushed and replaced atomically; failed extraction leaves
  existing final files intact. Multi-brand completion is per file, not a single
  transaction across all seven files. Re-running fills remaining missing files.
* An OS process lock prevents concurrent runs against the same cache directory.
  Run scripts one at a time. A crash releases the lock automatically.
* restaurant_extract_report.json records the selected states' statuses for the
  current run. Ctrl-C preserves partial files and completed results. Do not use
  --force or --refresh-data just to resume.

Normal re-runs reuse cached data; they do not automatically update source data.
Use --force to rewrite selected final JSON from an already extracted snapshot.
Use --refresh-data to download current provider data and rewrite selected finals.
If a refresh fails, old final files remain; repeat --refresh-data to retry the
refresh. A normal run will skip old completed final files.

## File sizes and boundaries

State files contain ALL regional OSM features, not just restaurants: expect tens
of MB to more than a GB per state. Transfers are larger than Overpass responses,
but each is a resumable static download shared across all seven chains. Local
processing reads the file several times and retains only matching restaurants
and their geometry references in memory. Maintain free disk space for the
largest current state file. No disk-space or speed guarantee is made.

State assignment follows Geofabrik extract membership. Regional extracts can
include complete geometry crossing state borders, so border overlap is possible.
For strict point-in-state assignment, apply an authoritative state polygon after
export. When combining state files, deduplicate by OSM type plus id. Separate
OSM objects for one physical restaurant still need a separate proximity review.

## Output

Output paths remain <brand>_states/<brand>_<STATE>.json. The JSON elements array
preserves type, id, tags, lat/lon for nodes, and center.lat/center.lon for ways and
relations. All mapped tags are kept, including opening_hours, addresses, phones,
websites, and drive-through hours. Centers use bounding-box midpoints; they are
approximate points, not entrances. Incomplete geometry is retained with an
explicit _geometry_warning, and the final metadata counts those warnings. Such
records may lack coordinates or have an approximate center from partial geometry;
review them before spatial use. Empty completed results mean no mapped match,
not proof of no actual restaurants. OSM coverage/hours can be missing or stale.

## Other options

    --states TX LA       Default: all 50 states plus DC
    --brands subway      Default: all seven for download_restaurants.py
    --root PATH          Parent of chain output folders (default current folder)
    --output PATH        Single-brand output directory
    --cache PATH         Shared cache folder (default osm_extract_cache)
    --retries 6          curl retries per transfer
    --pause 2            Seconds between states
    --keep-pbf           Keep large PBFs after successful extraction
    --force              Rewrite final files from extracted cache
    --refresh-data       Fetch fresh provider data and rewrite final files
    --base-url URL       Alternate provider/mirror with same state filenames
    --pbf-dir PATH       Offline mode: read STATE.osm.pbf files, e.g. AL.osm.pbf

--tiled and --ipv4 are accepted for compatibility; static downloads do not need
tiles and already use IPv4. --endpoint/--endpoints are no longer applicable.
The scripts never fall back to Overpass.

## Validation

Tested with real local PBF fixtures covering node, way, nested-relation geometry,
all seven brands, opening hours, missing geometry warnings, cache reuse,
existing-file skips, and reports. Also completed a live Geofabrik DC download,
checksum verification, and extraction for all seven chains on October 24, 2026.
Availability from your own network cannot be guaranteed. Unlike previous
versions, this release has been tested with an actual provider download.

Map data © OpenStreetMap contributors, ODbL.
https://www.openstreetmap.org/copyright
Extracts: https://download.geofabrik.de/north-america/us.html
Parser: https://docs.osmcode.org/pyosmium/latest/

## Import restaurant JSON into SQLite

`ingest_restaurants.py` reads the completed `*_states` folders and imports all
chains into one `restaurants` table in `database/chat_history.sqlite3`. The
`chain` column is the folder slug (for example, `mcdonalds` or `7_brew`). Rows
are keyed by chain, OSM type, and OSM ID, so the same OSM object can be retained
for more than one exported chain. Re-running the importer updates existing rows.

Run from the project root after the state JSON files are ready:

  python scripts/ingest_restaurants.py --states TX
  python scripts/ingest_restaurants.py --states TX LA --chains mcdonalds starbucks

The source directory defaults to `scripts-all-restaurants`. Use `--source-dir`
to select another export root and `--db` to select another SQLite file:

  python scripts/ingest_restaurants.py --source-dir /path/to/exports --states TX --db /path/to/restaurants.sqlite3

The table stores normalized name, address, state, phone, website, amenity,
cuisine, opening hours, drive-through, takeaway, and wheelchair fields when
present. Every OSM tag is also retained in `tags_json`. Missing optional fields,
including tags or coordinates, are stored as empty JSON or `NULL`; records
without coordinates remain available but cannot participate in location
searches. Node coordinates use `lat`/`lon`; ways and relations use their OSM
`center`. Coordinate indexes support bounding-box lookups for nearby searches.

The importer creates the table and indexes automatically. It imports only the
selected state/chain files and does not run the downloader or modify source JSON.
It reports file-level progress as the import runs. Alongside `restaurants`, it
maintains a `restaurant_names` lookup table containing each distinct, non-empty
restaurant name. Use this query to populate a dropdown:

  SELECT name FROM restaurant_names ORDER BY name;
Map data © OpenStreetMap contributors, ODbL:
https://www.openstreetmap.org/copyright
