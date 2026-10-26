import sqlite3

from backend.core import ensure_chat_history_db
from backend.routing import _closest_roadys_from_db, resolve_routed_answer


def _roadys_test_db(path):
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE user_location_events(
                id INTEGER PRIMARY KEY, session_id TEXT, recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                latitude REAL, longitude REAL
            );
            CREATE TABLE roadys_locations(
                location_id INTEGER PRIMARY KEY, name TEXT, address TEXT, city TEXT, state TEXT,
                postal_code TEXT, latitude REAL, longitude REAL, location_type_code TEXT,
                group_code TEXT, interstate_exit TEXT, has_rewards INTEGER,
                rewards_multiplier REAL, directions_url TEXT
            );
            CREATE TABLE roadys_facilities(
                facility_id INTEGER PRIMARY KEY, location_id INTEGER, kind TEXT, name TEXT
            );
            CREATE TABLE roadys_amenities(amenity_id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE roadys_location_amenities(
                location_id INTEGER, amenity_id INTEGER, raw_value TEXT,
                boolean_value INTEGER, numeric_value REAL
            );
            CREATE TABLE roadys_hours(
                facility_id INTEGER, raw_text TEXT, parse_status TEXT, rule_number INTEGER
            );
            CREATE TABLE roadys_contacts(
                location_id INTEGER, label TEXT, number TEXT, extension TEXT
            );
            CREATE TABLE roadys_food_selections(location_id INTEGER, name TEXT);
            CREATE TABLE roadys_fuel_brands(location_id INTEGER, name TEXT);
            CREATE TABLE roadys_fuel_prices(
                location_id INTEGER, fuel_type TEXT, price_mills INTEGER, observed_at TEXT
            );
            CREATE TABLE routerFunctions(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            INSERT INTO routerFunctions(name) VALUES ('Closest Restaurant');
            INSERT INTO routerFunctions(name) VALUES ('Closest Roadys');
            INSERT INTO user_location_events(session_id, latitude, longitude)
                VALUES ('session', 30.0, -100.0), ('session', 30.0, -99.9);
            INSERT INTO roadys_locations VALUES
                (1, "Roady's Behind", '1 Back Rd', 'Austin', 'TX', '78701', 30.0, -99.91,
                 'travel_center', 'A', 'Exit 1', 1, 2.0, 'https://example.test/behind'),
                (2, "La Porte Travel Plaza aka Moody's Travel Plaza", '1829 Hwy 90 West', 'Bayou Vista', 'LA', '70380', 30.0, -99.8,
                 'travel_center', 'A', 'Hwy 225', 1, 2.0, 'https://example.test/ahead');
            INSERT INTO roadys_facilities VALUES
                (1, 2, 'restaurant', 'Roadhouse Cafe'),
                (2, 2, 'restaurant', 'Denny''s'),
                (3, 2, 'store', 'Roady''s Ahead'),
                (4, 2, 'store', 'La Porte Travel Plaza aka Moody''s Travel Plaza');
            INSERT INTO roadys_amenities VALUES
                (1, 'Truck parking'), (2, 'Wi-Fi'), (3, 'Seats'), (4, 'Gas Brand'),
                (5, 'Truckers Supplies'), (6, 'Trailer Drop'), (7, 'Chain(s)'),
                (8, 'Internet (Wireless)'), (9, 'Laundry Services'), (10, 'Motel / Hotel'),
                (11, 'Fuel Service'), (12, 'Store Hours');
            INSERT INTO roadys_location_amenities VALUES
                (2, 1, 'Available', 1, NULL), (2, 2, NULL, 1, NULL),
                (2, 3, '45', NULL, 45), (2, 3, '172', NULL, 172),
                (2, 4, 'Valero', NULL, NULL),
                (2, 5, '1', 1, 1), (2, 6, '1', 1, 1),
                (2, 7, 'Roadhouse Cafe', NULL, NULL), (2, 8, '1', 1, 1),
                (2, 9, '1', 1, 1), (2, 10, 'Onsite\nOpen 24/7', NULL, NULL),
                (2, 11, 'Self', NULL, NULL), (2, 12, '24/7', NULL, NULL);
            INSERT INTO roadys_hours VALUES (1, 'Open 24 hours', 'parsed', 1);
            INSERT INTO roadys_hours VALUES (3, '24/7', 'parsed', 1);
            INSERT INTO roadys_contacts VALUES (2, 'Store phone', '+12815425300', '');
            INSERT INTO roadys_food_selections VALUES (2, 'Breakfast');
            INSERT INTO roadys_fuel_brands VALUES (2, 'Roady Fuel');
            INSERT INTO roadys_fuel_prices VALUES (2, 'diesel', 3499, '2026-10-25');
        ''')


def test_closest_roadys_prefers_recent_travel_direction_and_returns_available_data(tmp_path):
    database = tmp_path / 'roadys.sqlite3'
    _roadys_test_db(database)

    with sqlite3.connect(database) as db:
        result = _closest_roadys_from_db(db, 'session')

    assert result['location_details']['name'] == "La Porte Travel Plaza aka Moody's Travel Plaza"
    assert result['heading_known'] is True
    assert result['found_ahead'] is True

    response = resolve_routed_answer(
        'CDLA', "Where's the nearest roadies in the direction I'm traveling?",
        database, 'session', sources=[{
            'routerFunction': 'Closest Restaurant',
            'question': 'What is the nearest restaurant in my direction?',
        }],
    )
    assert response['routerFunction'] == 'Closest Roadys'
    assert "La Porte Travel Plaza also known as Moody's Travel Plaza" in response['text']
    assert 'Address: 1829 Highway 90 West, Bayou Vista, Louisiana, 70380' in response['text']
    assert "La Porte Travel Plaza aka Moody's Travel Plaza (store)" not in response['text']
    assert "Roady's Ahead (store)" in response['text']
    assert 'Interstate exit: Highway 225' in response['text']
    assert 'Restaurants:\nDenny\'s\nRoadhouse Cafe: Open 24 hours a day 7 days a week' in response['text']
    assert 'Hours:\nOpen 24 hours a day 7 days a week' in response['text']
    assert 'Store Hours: 24/7' not in response['text']
    assert 'Roady\'s Ahead: 24/7' not in response['text']
    assert 'Roady\'s Ahead: Open 24 hours a day 7 days a week' not in response['text']
    assert response['text'].count('Roadhouse Cafe') == 1
    assert 'Amenities:\n' in response['text']
    assert 'Truck parking' in response['text']
    assert 'Truck parking: Yes' not in response['text']
    assert 'Trailer Drop' in response['text']
    assert 'Trailer Drop: Yes' not in response['text']
    assert 'Truckers Supplies' in response['text']
    assert 'Truckers Supplies: Yes' not in response['text']
    assert 'Wireless Internet' in response['text']
    assert 'Internet (Wireless): Yes' not in response['text']
    assert 'Laundry Services' in response['text']
    assert 'Laundry Services: Yes' not in response['text']
    assert 'Seats:' not in response['text']
    assert 'Gas Brand:' not in response['text']
    assert 'Chain(s):' not in response['text']
    assert 'Location type:' not in response['text']
    assert 'Group:' not in response['text']
    assert 'Store phone: 281 542 5300' in response['text']
    assert 'Food:' not in response['text']
    assert 'Fuel brands:' not in response['text']
    assert 'Fuel Service: Self' not in response['text']
    assert 'Fuel prices: diesel: $3.499 per gallon' in response['text']
    assert 'Directions:' not in response['text']
    assert 'Motel / Hotel: Onsite\nOpen 24 hours a day 7 days a week' in response['text']
    assert response['directions']['latitude'] == 30.0


def test_closest_roadys_omits_exit_when_it_repeats_the_address(tmp_path):
    database = tmp_path / 'roadys.sqlite3'
    _roadys_test_db(database)
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE roadys_locations SET address='9119 Highway 225', city='La Porte', "
            "state='TX', postal_code='77571' WHERE location_id=2"
        )

    response = resolve_routed_answer(
        'CDLA', "Where's the nearest roadies in the direction I'm traveling?",
        database, 'session', sources=[{
            'routerFunction': 'Closest Restaurant',
            'question': 'What is the nearest restaurant in my direction?',
        }],
    )

    assert 'Address: 9119 Highway 225, La Porte, Texas, 77571' in response['text']
    assert 'Interstate exit:' not in response['text']


def test_closest_roadys_matches_speech_to_text_homophone(tmp_path):
    database = tmp_path / 'roadys.sqlite3'
    _roadys_test_db(database)

    for question in (
        "What is the nearest road he's traveling in direction?",
        'What is the nearest road he’s traveling in direction?',
    ):
        response = resolve_routed_answer('CDLA', question, database, 'session')
        assert response['routerFunction'] == 'Closest Roadys'
        assert 'La Porte Travel Plaza' in response['text']


def test_fresh_database_initializes_closest_roadys_router_function(tmp_path):
    database = ensure_chat_history_db(tmp_path / 'fresh.sqlite3')

    with sqlite3.connect(database) as db:
        names = {row[0] for row in db.execute('SELECT name FROM routerFunctions')}

    assert 'Closest Roadys' in names