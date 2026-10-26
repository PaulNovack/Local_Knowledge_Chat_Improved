
CREATE TABLE IF NOT EXISTS roadys_schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roadys_location_types(code TEXT PRIMARY KEY, source_id INTEGER, name TEXT);
CREATE TABLE IF NOT EXISTS roadys_locations(
 location_id INTEGER PRIMARY KEY, name TEXT NOT NULL, address TEXT, city TEXT, state TEXT,
 postal_code TEXT, latitude REAL, longitude REAL, location_type_code TEXT,
 group_code TEXT, interstate_exit TEXT, has_rewards INTEGER, rewards_multiplier REAL,
 directions_url TEXT, source_url TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL, last_seen_run INTEGER, content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS roadys_locations_state ON roadys_locations(state);
CREATE TABLE IF NOT EXISTS roadys_amenities(amenity_id INTEGER PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roadys_location_amenities(
 location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),
 amenity_id INTEGER NOT NULL REFERENCES roadys_amenities(amenity_id),
 raw_value TEXT, boolean_value INTEGER, numeric_value REAL, numeric_is_minimum INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(location_id,amenity_id)
);
CREATE TABLE IF NOT EXISTS roadys_amenity_values(
 location_id INTEGER NOT NULL,amenity_id INTEGER NOT NULL,value_index INTEGER NOT NULL,
 value TEXT NOT NULL, PRIMARY KEY(location_id,amenity_id,value_index),
 FOREIGN KEY(location_id,amenity_id) REFERENCES roadys_location_amenities(location_id,amenity_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS roadys_facilities(
 facility_id INTEGER PRIMARY KEY,location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),
 kind TEXT NOT NULL,name TEXT NOT NULL,source_amenity TEXT,
 UNIQUE(location_id,kind,name)
);
CREATE TABLE IF NOT EXISTS roadys_contacts(
 contact_id INTEGER PRIMARY KEY, location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),
 facility_id INTEGER REFERENCES roadys_facilities(facility_id), kind TEXT NOT NULL,
 label TEXT NOT NULL, number TEXT NOT NULL, extension TEXT NOT NULL DEFAULT '',raw_value TEXT NOT NULL,
 UNIQUE(location_id,label,number,extension)
);
CREATE TABLE IF NOT EXISTS roadys_hours(
 hours_id INTEGER PRIMARY KEY,facility_id INTEGER NOT NULL REFERENCES roadys_facilities(facility_id),
 source_amenity TEXT NOT NULL,rule_number INTEGER NOT NULL,opens_at TEXT,closes_at TEXT,
 closes_next_day INTEGER NOT NULL DEFAULT 0,is_24_hours INTEGER NOT NULL DEFAULT 0,
 is_closed INTEGER NOT NULL DEFAULT 0,day_scope TEXT NOT NULL,parse_status TEXT NOT NULL,
 raw_text TEXT NOT NULL, UNIQUE(facility_id,source_amenity,rule_number)
);
CREATE TABLE IF NOT EXISTS roadys_hour_days(
 hours_id INTEGER NOT NULL REFERENCES roadys_hours(hours_id) ON DELETE CASCADE,
 weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),PRIMARY KEY(hours_id,weekday)
);
CREATE TABLE IF NOT EXISTS roadys_hour_references(
 hours_id INTEGER PRIMARY KEY REFERENCES roadys_hours(hours_id) ON DELETE CASCADE,
 referenced_amenity TEXT NOT NULL, referenced_raw_text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roadys_payment_types(payment_id INTEGER PRIMARY KEY,kind TEXT NOT NULL,name TEXT NOT NULL,UNIQUE(kind,name));
CREATE TABLE IF NOT EXISTS roadys_location_payments(
 location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),payment_id INTEGER NOT NULL REFERENCES roadys_payment_types(payment_id),
 PRIMARY KEY(location_id,payment_id)
);
CREATE TABLE IF NOT EXISTS roadys_food_selections(location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),name TEXT NOT NULL,PRIMARY KEY(location_id,name));
CREATE TABLE IF NOT EXISTS roadys_fuel_brands(location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),name TEXT NOT NULL,PRIMARY KEY(location_id,name));
CREATE TABLE IF NOT EXISTS roadys_fuel_prices(
 location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),fuel_type TEXT NOT NULL,
 price_mills INTEGER NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(location_id,fuel_type)
);
CREATE TABLE IF NOT EXISTS roadys_source_fields(
 location_id INTEGER NOT NULL REFERENCES roadys_locations(location_id),field_name TEXT NOT NULL,
 value_type TEXT NOT NULL,value_json TEXT NOT NULL,PRIMARY KEY(location_id,field_name)
);
CREATE TABLE IF NOT EXISTS roadys_raw_locations(
 location_id INTEGER PRIMARY KEY REFERENCES roadys_locations(location_id),payload_json TEXT NOT NULL,observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roadys_runs(
 run_id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,
 source_url TEXT NOT NULL,source_count INTEGER,selected_count INTEGER,saved_count INTEGER DEFAULT 0,
 error TEXT
);
CREATE VIEW IF NOT EXISTS roadys_hours_grouped AS
SELECT f.location_id,f.name,f.kind,h.*,
 (SELECT group_concat(weekday,',') FROM
  (SELECT weekday FROM roadys_hour_days d WHERE d.hours_id=h.hours_id ORDER BY weekday)) AS weekdays
FROM roadys_hours h JOIN roadys_facilities f USING(facility_id);
