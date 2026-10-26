# Complete restaurant downloader: 200 configured chains

This package contains the complete code, restaurant_brands.json with 200 chains,
and requirements.txt. No code edits are needed. The chain list is curated for
U.S. coverage, not a verified top-200 ranking by sales or location count.

## Run with an existing Python environment

Extract all files into the directory containing your current scripts and existing
*_states output folders. Keep download_restaurants.py and restaurant_brands.json
together. Open a terminal in that directory:

    python -m pip install -r requirements.txt
    python download_restaurants.py

It defaults to ALL 200 configured chains and ALL 50 states plus Washington, DC.
Each state is downloaded and parsed once for all chains, not once per chain.
Existing valid completed JSON files are skipped. Source caches from the previous
seven-chain version must be rebuilt because they did not extract the other chains.
Your existing JSON files do not need to be deleted.

## New Python environment on Ubuntu/Linux

    python3 -m venv restaurant_env
    source restaurant_env/bin/activate
    python -m pip install -r requirements.txt
    python download_restaurants.py

If venv creation fails due to missing ensurepip, install python3-venv first.
Network downloads use curl; install curl if absent. Python 3.9+ is required.

## Useful commands

Try one state first:

    python download_restaurants.py --states TX

Selected states:

    python download_restaurants.py --states TX LA OK

Selected chains:

    python download_restaurants.py --states TX --brands subway mcdonalds starbucks

Print all usable chain names for --brands:

    python download_restaurants.py --list-brands

Use existing output folders elsewhere:

    python download_restaurants.py --root /path/to/parent/of/chain_folders

Refresh source data and replace selected results:

    python download_restaurants.py --refresh-data

Normal reruns resume missing work and skip completed JSON. Do not use --force or
--refresh-data merely to resume after interruption. --force rewrites JSON using
a cached extract. --refresh-data downloads the current provider snapshot first.
If a refresh fails, previous JSON remains; repeat --refresh-data to retry it.

## Output and reliability

Output remains <brand>_states/<brand>_<STATE>.json, with an elements array.
Nodes have lat/lon; ways and relations have approximate bounding-box centers.
All mapped tags are retained, including opening_hours, addresses, phones, and
websites. Missing hours remain missing. Objects with incomplete geometry get an
explicit _geometry_warning; review them before spatial use.

This uses static Geofabrik state extracts, not Overpass. curl downloads over
IPv4 with retries, stalled-transfer detection, partial-file resume, and provider
MD5 integrity verification. Changing provider snapshots invalidate partial
transfers safely. JSON and extracted caches are written atomically. An OS lock
prevents simultaneous runs on the same cache. Status is recorded in
restaurant_extract_report.json. Completed extracted results are shared between
chain selections. The cache key includes IDs and aliases from the configuration;
changing the chain definitions invalidates stale extracted caches automatically.

State source files contain all regional OSM data and can exceed 1 GB. Allow disk
space for the largest current state file. Large PBFs are removed after successful
extraction by default; --keep-pbf retains them. Cached extracted JSON is kept.
Failed or interrupted downloads keep .part files so a later run can resume.
A parse interruption restarts the local parse, not the completed file download.

State assignment follows Geofabrik regional extract membership. Border overlap
can occur; deduplicate merged states by OSM type plus id. Separate OSM objects
for the same physical restaurant may still require proximity deduplication.
For exact point-in-state assignment, apply state polygons after export. Neither
OSM coverage nor current hours nor network availability is guaranteed.

## Additional options

    --cache PATH       Shared source/extraction cache (default osm_extract_cache)
    --output PATH      Specific output folder; exactly one brand must be selected
    --retries 6        curl retry count
    --pause 2          Seconds between states
    --base-url URL     Mirror with Geofabrik-style state filenames and .md5 files
    --pbf-dir PATH     Offline source directory: STATE.osm.pbf, e.g. TX.osm.pbf
    --keep-pbf         Retain large PBFs

Legacy --tiled/--ipv4 flags are accepted but unnecessary. Overpass --endpoint
flags are not supported by this static-file downloader.

Brand definitions were checked against the OpenStreetMap Name Suggestion Index.
Names and IDs are configuration data; a known ID takes priority over name matching.
Exact aliases are indexed first, followed by regex prefix matching. An explicitly
identified brand outside the configuration is not matched by a loose name alias.

Map data © OpenStreetMap contributors, ODbL:
https://www.openstreetmap.org/copyright
Geofabrik: https://download.geofabrik.de/north-america/us.html
Brand catalog: https://github.com/osmlab/name-suggestion-index
Parser: https://docs.osmcode.org/pyosmium/latest/
