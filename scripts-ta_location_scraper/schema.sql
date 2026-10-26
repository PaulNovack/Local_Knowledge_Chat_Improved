
CREATE TABLE IF NOT EXISTS ta_imports(import_id INTEGER PRIMARY KEY,filename TEXT NOT NULL,file_sha256 TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,row_count INTEGER,error TEXT);
CREATE TABLE IF NOT EXISTS ta_locations(site_id TEXT PRIMARY KEY,location_id TEXT UNIQUE,brand TEXT,state TEXT,city TEXT,name TEXT,directions TEXT,address TEXT,po_box TEXT,postal_code TEXT,latitude REAL,longitude REAL,gas_brand TEXT,first_import_id INTEGER NOT NULL,last_import_id INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ta_locations_state ON ta_locations(state);
CREATE TABLE IF NOT EXISTS ta_facilities(facility_id INTEGER PRIMARY KEY,site_id TEXT NOT NULL REFERENCES ta_locations(site_id),kind TEXT NOT NULL,name TEXT NOT NULL,UNIQUE(site_id,kind,name));
CREATE TABLE IF NOT EXISTS ta_contacts(contact_id INTEGER PRIMARY KEY,facility_id INTEGER NOT NULL REFERENCES ta_facilities(facility_id),kind TEXT NOT NULL,number TEXT NOT NULL,extension TEXT NOT NULL DEFAULT '',raw_value TEXT NOT NULL,UNIQUE(facility_id,kind,number,extension));
CREATE TABLE IF NOT EXISTS ta_amenities(amenity_id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS ta_location_amenities(site_id TEXT NOT NULL REFERENCES ta_locations(site_id),amenity_id INTEGER NOT NULL REFERENCES ta_amenities(amenity_id),availability INTEGER,quantity REAL,raw_values_json TEXT NOT NULL,PRIMARY KEY(site_id,amenity_id));
CREATE TABLE IF NOT EXISTS ta_hours(hours_id INTEGER PRIMARY KEY,facility_id INTEGER NOT NULL REFERENCES ta_facilities(facility_id),rule_number INTEGER NOT NULL,opens_at TEXT,closes_at TEXT,closes_next_day INTEGER NOT NULL DEFAULT 0,is_24_hours INTEGER NOT NULL DEFAULT 0,is_closed INTEGER NOT NULL DEFAULT 0,parse_status TEXT NOT NULL,raw_text TEXT,UNIQUE(facility_id,rule_number));
CREATE TABLE IF NOT EXISTS ta_hour_days(hours_id INTEGER NOT NULL REFERENCES ta_hours(hours_id) ON DELETE CASCADE,weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),PRIMARY KEY(hours_id,weekday));
CREATE TABLE IF NOT EXISTS ta_source_columns(sheet TEXT NOT NULL,column_number INTEGER NOT NULL,header TEXT NOT NULL,PRIMARY KEY(sheet,column_number));
CREATE TABLE IF NOT EXISTS ta_source_rows(site_id TEXT NOT NULL REFERENCES ta_locations(site_id),sheet TEXT NOT NULL,row_number INTEGER NOT NULL,import_id INTEGER NOT NULL,values_json TEXT NOT NULL,PRIMARY KEY(site_id,sheet));
CREATE TABLE IF NOT EXISTS ta_import_issues(issue_id INTEGER PRIMARY KEY,import_id INTEGER NOT NULL,site_id TEXT,sheet TEXT,row_number INTEGER,field TEXT,issue TEXT NOT NULL,raw_value TEXT);
CREATE VIEW IF NOT EXISTS ta_hours_grouped AS
SELECT f.site_id,f.kind,f.name,h.*,(SELECT group_concat(weekday,',') FROM
 (SELECT weekday FROM ta_hour_days d WHERE d.hours_id=h.hours_id ORDER BY weekday)) AS weekdays
FROM ta_facilities f JOIN ta_hours h USING(facility_id);
