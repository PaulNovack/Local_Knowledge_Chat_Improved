import csv
import importlib.util
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / 'pilot-truckstops-all' / 'scripts' / 'scrape_pilot.py'
SPEC = importlib.util.spec_from_file_location('pilot_scraper', SCRIPT_PATH)
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


def test_zip_seeds_run_by_population_density_and_keep_zip_code(tmp_path):
    csv_path = tmp_path / 'zipcodes.csv'
    with csv_path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['zip', 'lat', 'lng', 'population', 'population_density'])
        writer.writerows([
            ['10003', 40.732, -73.989, 50000, 20000],
            ['10001', 40.750, -73.997, 30000, 10000],
            ['90001', 33.973, -118.249, 60000, 15000],
        ])

    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        pilot.migrate(conn)
        pilot.seed_zips(conn, csv_path)
        conn.execute("INSERT INTO pilot_searches(url) VALUES ('grid-search')")

        jobs = []
        while job := pilot.next_search_job(conn):
            jobs.append((job['zip_code'], job['population_density']))
            conn.execute("UPDATE pilot_searches SET status='complete' WHERE url=?", (job['url'],))

    assert jobs == [
        ('10003', 20000.0),
        ('90001', 15000.0),
        ('10001', 10000.0),
        (None, None),
    ]
    assert pilot.DEFAULT_DB == Path(__file__).resolve().parents[1] / 'database' / 'chat_history.sqlite3'


def test_migrate_adds_zip_metadata_to_existing_search_table():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('''CREATE TABLE pilot_searches (
            url TEXT PRIMARY KEY, south REAL, west REAL, north REAL, east REAL,
            depth INTEGER, status TEXT, result_count INTEGER, reported_count INTEGER,
            possibly_truncated INTEGER, scraped_at TEXT, error TEXT
        )''')
        pilot.migrate(conn)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(pilot_searches)')}

    assert {'zip_code', 'population', 'population_density'} <= columns