# Grocery Store Extractor

This package downloads Geofabrik state extracts and saves major U.S. grocery and supermarket locations into per-brand SQLite tables. The configured catalog is curated for broad national and regional coverage; it is not an exhaustive list or a verified ranking.

## Setup

From the repository root:

```sh
python -m pip install -r scripts-all-grocery/requirements.txt
python scripts-all-grocery/download_grocery.py --states TX
```

Run the script directly from this directory with the same commands if preferred. It defaults to all configured brands and all 50 states plus Washington, DC. Start with one state to check the resulting tables:

```sh
python download_grocery.py --states TX --brands walmart kroger heb
python download_grocery.py --list-brands
```

## SQLite output

The default database is `database/chat_history.sqlite3`. Each configured brand is stored in its own table named `grocery_<slug>`, for example `grocery_aldi` or `grocery_kroger`. The `grocery_scrape_runs` table records source checksum, state, brand, row count, and processing time.

Brand tables include OSM type and ID, source state, name, coordinates, address fields, phone, website, shop type, opening hours, brand tags, full `tags_json`, geometry warnings, and source/checksum metadata. Way and relation locations use a bounding-box center when geometry is available. The composite key includes source state so regional boundary duplicates can be identified and deduplicated later.

Useful options:

```sh
python download_grocery.py --states TX LA OK
python download_grocery.py --states TX --brands costco trader_joes whole_foods
python download_grocery.py --db /path/to/grocery.sqlite3 --states CA
python download_grocery.py --pbf-dir /path/to/local-pbf-folder --states TX
python download_grocery.py --refresh-data --states TX
python download_grocery.py --keep-pbf --states TX
```

The default source cache and `grocery_extract_report.json` are under `scripts-all-grocery/`. Interrupted downloads retain partial files; rerun the same command to resume. Normal reruns reuse cached extracts and upsert rows. `--refresh-data` downloads the current provider snapshot and refreshes the selected state rows.

Only OSM objects tagged with a supported `shop` value and a configured grocery brand are included. OSM coverage and current store details are not guaranteed. Data is from OpenStreetMap contributors under the ODbL; Geofabrik state extracts are used, not Overpass.

## Ask and Train routing

`grocery_questions.jsonl` contains one `Closest Grocery Store` training question for each configured brand. Import it into the CDLA collection with:

```sh
python -m backend.add_records scripts-all-grocery/grocery_questions.jsonl --topic CDLA
```

This adds the records to `topic_data/user_additions/CDLA.jsonl` and indexes them in the CDLA Qdrant collection. Ask and Train exposes `Closest Grocery Store` as a router function; the key selects the matching `grocery_<slug>` table and uses recent location history to choose a store in the direction of travel when possible.

The `List Grocer Stores` router returns up to five nearby locations for a named brand, with a directions link for each. The `Show Number Direction` router opens the numbered location from the latest list in the same chat session; it accepts numeric digits and spoken number words.

- OpenStreetMap: https://www.openstreetmap.org/copyright
- Geofabrik extracts: https://download.geofabrik.de/north-america/us.html
