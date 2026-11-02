"""Resolve registered routes that need deterministic lookup after vector search."""
import json
import re
import sqlite3
import math
from difflib import SequenceMatcher
from datetime import date
from pathlib import Path


_ONES = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
}
_SMALL_NUMBERS = {**_ONES, 'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13,
                  'fourteen': 14, 'fifteen': 15, 'sixteen': 16, 'seventeen': 17,
                  'eighteen': 18, 'nineteen': 19}
_TENS = {'twenty': 20, 'thirty': 30}
_PRE_TRIP_INSPECTION = re.compile(r'\bpre[ -]?trip(?:ping)?\b.*\binspection\b', re.I)
_CLOSEST_LOVES = re.compile(r'\b(?:closest|nearest|newest)\b.*\blove(?:s|\'s)?\b', re.I)
_CLOSEST_ROADYS = re.compile(
    r"\b(?:closest|nearest|newest)\b.*\b(?:roady(?:'s|s)?|roadies?|road\s+he['’]?s)\b", re.I
)
_CLOSEST_RESTAURANT = re.compile(r'\b(?:closest|nearest|newest)\b', re.I)
_CLOSEST_DEPARTMENT_STORE = re.compile(r'\b(?:closest|nearest)\b', re.I)
_CLOSEST_GROCERY_STORE = re.compile(r'\b(?:closest|nearest)\b', re.I)
_GROCERY_CONTEXT = re.compile(r'\b(?:grocer(?:y|ies)|supermarkets?)\b', re.I)
_LIST_GROCERY_STORES = re.compile(
    r'\b(?:show|give|display|list)\b.*\b(?:list|grocer(?:y|ies)|supermarkets?)\b', re.I,
)
_RESTAURANT_CHAIN_ALIASES = {
    'bubba gump': 'bubba_gump_shrimp_company',
    'bubblegum shrimp': 'bubba_gump_shrimp_company',
    'kentucky fried chicken': 'kfc',
    'potbellies': 'potbelly',
    'torches taco': 'torchy_s_tacos',
}
_DEPARTMENT_STORE_CATALOG = json.loads(
    (Path(__file__).resolve().parents[1] / 'scripts-all-department' / 'department_brands.json')
    .read_text(encoding='utf-8')
)
_DEPARTMENT_STORE_SLUGS = {brand['slug'] for brand in _DEPARTMENT_STORE_CATALOG}
_DEPARTMENT_STORE_NAMES = sorted(
    ((brand['slug'], name) for brand in _DEPARTMENT_STORE_CATALOG for name in brand['names']),
    key=lambda item: (-len(item[1]), item[1].casefold()),
)
_GROCERY_STORE_CATALOG = json.loads(
    (Path(__file__).resolve().parents[1] / 'scripts-all-grocery' / 'grocery_brands.json')
    .read_text(encoding='utf-8')
)
_GROCERY_STORE_SLUGS = {brand['slug'] for brand in _GROCERY_STORE_CATALOG}
_GROCERY_STORE_NAMES = sorted(
    ((brand['slug'], name) for brand in _GROCERY_STORE_CATALOG for name in brand['names']),
    key=lambda item: (-len(item[1]), item[1].casefold()),
)
_SUPPORTED_RESTAURANTS = re.compile(r'\b(?:support(?:ed|s)?|available|offer(?:ed|s)?|carry)\b', re.I)
_NUMBER_WORDS = set(_SMALL_NUMBERS) | set(_TENS)
_US_STATE_NAMES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming',
}
for _tens_word in _TENS:
    for _ones_word in _ONES:
        _NUMBER_WORDS.add(f'{_tens_word} {_ones_word}')
        _NUMBER_WORDS.add(f'{_tens_word}-{_ones_word}')
_NUMBER_WORD_PATTERN = '|'.join(re.escape(word) for word in sorted(_NUMBER_WORDS, key=len, reverse=True))
_STEP_NUMBER = re.compile(rf'\bstep\s+(\d+|{_NUMBER_WORD_PATTERN})(?![a-z0-9-])', re.I)
_NUMBER_DIRECTION_QUERY = re.compile(
    rf'\b(?:show|open|give)\b.*?\bdirections?\b.*?\b(?:for|to)\s+'
    rf'(?:number\s+|#\s*)?(?P<number>\d+|{_NUMBER_WORD_PATTERN})(?![a-z0-9-])',
    re.I,
)
_ROUTE_HANDLERS = {}
_WEEKDAYS = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')
_WEEKDAY_INDEX = {day[:2].casefold(): index for index, day in enumerate(_WEEKDAYS)}
_OPENING_HOURS_TIME = re.compile(r'^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$')
_OPENING_HOURS_DAYS = re.compile(r'^(?:[A-Za-z]{2}(?:-[A-Za-z]{2})?)(?:,(?:[A-Za-z]{2}(?:-[A-Za-z]{2})?))*$')
_WEEKDAY_ABBREVIATIONS = {
    'mon': 'Monday', 'tue': 'Tuesday', 'wed': 'Wednesday', 'thu': 'Thursday',
    'fri': 'Friday', 'sat': 'Saturday', 'sun': 'Sunday',
}
_WEEKDAY_ABBREVIATION = re.compile(r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b\.?', re.I)
_SPOKEN_WEEKDAY_ORDER = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
_SPOKEN_WEEKDAY_LABEL = re.compile(
    r'\b(?P<start>' + '|'.join(_SPOKEN_WEEKDAY_ORDER) + r')'
    r'(?:\s*(?:-|–|—|thru|to)\s*(?P<end>' + '|'.join(_SPOKEN_WEEKDAY_ORDER) + r'))?\s*:',
    re.I,
)


def _format_clock(hour, minute):
    suffix = 'AM' if hour < 12 else 'PM'
    display_hour = hour % 12 or 12
    time = str(display_hour) if minute == 0 else f'{display_hour}:{minute:02d}'
    return f'{time} {suffix}'


def _format_opening_hours(value):
    """Format basic weekly OSM hours, retaining source text for complex rules."""
    raw_hours = str(value or '').strip()
    if not raw_hours:
        return raw_hours

    schedules = [None] * 7
    for rule in raw_hours.split(';'):
        rule = rule.strip()
        if not rule:
            return raw_hours
        parts = rule.split(None, 1)
        if len(parts) == 2 and _OPENING_HOURS_DAYS.fullmatch(parts[0]):
            day_selector, hours = parts
        elif len(parts) == 1:
            day_selector, hours = None, parts[0]
        else:
            return raw_hours

        selected_days = set(range(7))
        if day_selector:
            selected_days = set()
            for day_range in day_selector.split(','):
                endpoints = day_range.split('-')
                if any(endpoint.casefold() not in _WEEKDAY_INDEX for endpoint in endpoints):
                    return raw_hours
                start = _WEEKDAY_INDEX[endpoints[0].casefold()]
                end = _WEEKDAY_INDEX[endpoints[-1].casefold()]
                index = start
                while True:
                    selected_days.add(index)
                    if index == end:
                        break
                    index = (index + 1) % 7

        if hours.casefold() in {'off', 'closed'}:
            for day in selected_days:
                schedules[day] = ()
            continue

        intervals = []
        for interval in hours.split(','):
            match = _OPENING_HOURS_TIME.fullmatch(interval.strip())
            if not match:
                return raw_hours
            start_hour, start_minute, end_hour, end_minute = map(int, match.groups())
            if (start_hour > 24 or end_hour > 24 or start_minute > 59 or end_minute > 59
                    or (start_hour == 24 and start_minute != 0)
                    or (end_hour == 24 and end_minute != 0)):
                return raw_hours
            intervals.append((start_hour, start_minute, end_hour, end_minute))
        for day in selected_days:
            schedules[day] = (schedules[day] or ()) + tuple(intervals)

    def schedule_text(schedule):
        if not schedule:
            return 'Closed'
        formatted = []
        for start_hour, start_minute, end_hour, end_minute in schedule:
            if (start_hour, start_minute, end_hour, end_minute) == (0, 0, 24, 0):
                formatted.append('Open 24 hours')
            else:
                formatted.append(
                    f'{_format_clock(start_hour, start_minute)} to '
                    f'{_format_clock(end_hour % 24, end_minute)}'
                )
        return ' and '.join(formatted)

    grouped = []
    start = 0
    while start < 7:
        end = start
        while end + 1 < 7 and schedules[end + 1] == schedules[start]:
            end += 1
        day_label = _WEEKDAYS[start] if start == end else f'{_WEEKDAYS[start]} thru {_WEEKDAYS[end]}'
        grouped.append(f'{day_label}: {schedule_text(schedules[start])}')
        start = end + 1
    return '; '.join(grouped)


_STATE_ABBREVIATION = re.compile(
    r'(?<![A-Za-z])(' + '|'.join(_US_STATE_NAMES) + r')(?![A-Za-z])', re.I
)
_ZIP_CODE = re.compile(r'(?<!\S)\d{5}(?:-\d{4})?(?=\s*(?:,|$))')


def _format_restaurant_address(value):
    address = _ZIP_CODE.sub('', str(value or ''))
    address = _STATE_ABBREVIATION.sub(
        lambda match: _US_STATE_NAMES[match.group(1).upper()], address
    )
    address = re.sub(r'\s+,', ',', address)
    address = re.sub(r',\s*,', ',', address)
    return re.sub(r'\s{2,}', ' ', address).strip(' ,')


def _format_phone_for_speech(value):
    phone = re.sub(r'^\+1(?:[-\s]*)', '', str(value or '').strip())
    return phone.replace('-', ' ')


def _expand_weekday_abbreviations(value):
    return _WEEKDAY_ABBREVIATION.sub(
        lambda match: _WEEKDAY_ABBREVIATIONS[match.group(1).casefold()], str(value or '')
    )


def _format_spoken_hours(value):
    hours = _expand_weekday_abbreviations(value)
    hours = re.sub(
        r'(\b\d{1,2}(?::\d{2})?\s*(?:(?:a\.m\.|p\.m\.|am|pm))?)\s*[-–—]\s*(?=\d)',
        r'\1 to ', hours, flags=re.I,
    )
    hours = re.sub(r'\b([ap])\.m\.', lambda match: f'{match.group(1).upper()}M', hours, flags=re.I)

    weekday_labels = list(_SPOKEN_WEEKDAY_LABEL.finditer(hours))
    if not weekday_labels:
        return hours

    day_hours = {}
    for index, label in enumerate(weekday_labels):
        end = weekday_labels[index + 1].start() if index + 1 < len(weekday_labels) else len(hours)
        schedule = hours[label.end():end].strip().rstrip(';').strip()
        start_day = _SPOKEN_WEEKDAY_ORDER.index(label.group('start').title())
        end_day = _SPOKEN_WEEKDAY_ORDER.index((label.group('end') or label.group('start')).title())
        day = start_day
        while True:
            day_hours[day] = schedule
            if day == end_day:
                break
            day = (day + 1) % len(_SPOKEN_WEEKDAY_ORDER)

    grouped = []
    day = 0
    while day < len(_SPOKEN_WEEKDAY_ORDER):
        if day not in day_hours:
            day += 1
            continue
        end_day = day
        while (end_day + 1 in day_hours
               and day_hours[end_day + 1] == day_hours[day]):
            end_day += 1
        day_label = _SPOKEN_WEEKDAY_ORDER[day]
        if end_day > day:
            day_label += f' thru {_SPOKEN_WEEKDAY_ORDER[end_day]}'
        grouped.append(f'{day_label}: {day_hours[day]}')
        day = end_day + 1
    return '; '.join(grouped)


def register_router_function(name, matcher, minimum_key=None, maximum_key=None):
    """Register a question matcher for a named route stored in routerFunctions."""
    _ROUTE_HANDLERS[str(name).strip().casefold()] = (matcher, minimum_key, maximum_key)


def _pre_trip_step(question):
    if not _PRE_TRIP_INSPECTION.search(question):
        return None
    match = _STEP_NUMBER.search(question)
    if not match:
        return None
    token = match.group(1).lower().replace('-', ' ')
    if token.isdigit():
        step = int(token)
    elif token in _SMALL_NUMBERS:
        step = _SMALL_NUMBERS[token]
    else:
        parts = token.split()
        step = _TENS.get(parts[0], 0) + (_ONES.get(parts[1], 0) if len(parts) == 2 else 0)
    return step


def _number_from_token(value):
    normalized = value.casefold().replace('-', ' ').strip()
    if normalized.isdecimal():
        return int(normalized)
    if normalized in _SMALL_NUMBERS:
        return _SMALL_NUMBERS[normalized]
    parts = normalized.split()
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _ONES:
        return _TENS[parts[0]] + _ONES[parts[1]]
    return None


def _show_number_direction_number(question):
    match = _NUMBER_DIRECTION_QUERY.search(question)
    return _number_from_token(match.group('number')) if match else None


register_router_function('Pre-trip Text', _pre_trip_step, minimum_key=1, maximum_key=39)
register_router_function('Closest Loves', lambda question: True if _CLOSEST_LOVES.search(question) else None)
register_router_function('Closest Roadys', lambda question: True if _CLOSEST_ROADYS.search(question) else None)
register_router_function('Closest Restaurant', lambda question: True if _CLOSEST_RESTAURANT.search(question) else None)
register_router_function(
    'Closest Department Store',
    lambda question: True if _CLOSEST_DEPARTMENT_STORE.search(question) else None,
)
register_router_function(
    'Closest Grocery Store',
    lambda question: True if _CLOSEST_GROCERY_STORE.search(question) else None,
)
register_router_function(
    'List Grocer Stores',
    lambda question: _grocery_store_from_question(question)
    if _LIST_GROCERY_STORES.search(question) else None,
)
register_router_function('Show Number Direction', _show_number_direction_number)
register_router_function(
    'Supported Restaurants',
    lambda question: True if re.search(r'\brestaurants?\b', question, re.I)
    and _SUPPORTED_RESTAURANTS.search(question) else None,
)


def _distance_miles(first, second):
    latitude1, longitude1 = map(math.radians, first)
    latitude2, longitude2 = map(math.radians, second)
    latitude_delta = latitude2 - latitude1
    longitude_delta = longitude2 - longitude1
    haversine = math.sin(latitude_delta / 2) ** 2 + math.cos(latitude1) * math.cos(latitude2) * math.sin(longitude_delta / 2) ** 2
    return 3958.7613 * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))


def _bearing_degrees(first, second):
    latitude1, longitude1 = map(math.radians, first)
    latitude2, longitude2 = map(math.radians, second)
    longitude_delta = longitude2 - longitude1
    y = math.sin(longitude_delta) * math.cos(latitude2)
    x = math.cos(latitude1) * math.sin(latitude2) - math.sin(latitude1) * math.cos(latitude2) * math.cos(longitude_delta)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _closest_loves_from_db(db, session_id, current_location=None):
    if not session_id:
        return None
    try:
        events = db.execute('''
            SELECT latitude, longitude
            FROM user_location_events
            WHERE session_id=? AND latitude IS NOT NULL AND longitude IS NOT NULL
              AND recorded_at >= datetime('now', '-10 minutes')
            ORDER BY id DESC
            LIMIT 100
        ''', (session_id,)).fetchall()
        available_columns = {row[1] for row in db.execute('PRAGMA table_info(loves_locations)')}
    except sqlite3.OperationalError:
        return None
    store_columns = ['store_number', 'latitude', 'longitude']
    optional_columns = (
        'address', 'phone', 'store_hours', 'fuel_prices_json', 'truck_parking', 'shower_count', 'amenities_json',
        'restaurants_json', 'restaurant_hours_json', 'scraped_at',
    )
    store_columns.extend(column for column in optional_columns if column in available_columns)
    try:
        stores = db.execute(
            f"SELECT {', '.join(store_columns)} FROM loves_locations "
            'WHERE latitude IS NOT NULL AND longitude IS NOT NULL'
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if (not events and current_location is None) or not stores:
        return None

    current = current_location or (events[0][0], events[0][1])
    previous = next(((row[0], row[1]) for row in events
                     if _distance_miles(current, (row[0], row[1])) >= 0.05), None)
    heading = _bearing_degrees(previous, current) if previous else None
    candidates = []
    for row in stores:
        store = dict(zip(store_columns, row))
        store_number, latitude, longitude = (
            store['store_number'], store['latitude'], store['longitude'],
        )
        location = (latitude, longitude)
        distance = _distance_miles(current, location)
        if heading is not None:
            bearing = _bearing_degrees(current, location)
            angle_delta = abs((bearing - heading + 180) % 360 - 180)
            if angle_delta > 75:
                continue
        candidates.append((distance, str(store_number), store))

    found_ahead = bool(candidates)
    if not candidates:
        candidates = [
            (_distance_miles(current, (store['latitude'], store['longitude'])),
             str(store['store_number']), store)
            for store in (dict(zip(store_columns, row)) for row in stores)
        ]
    distance, store_number, store = min(candidates, key=lambda candidate: candidate[:2])
    return {
        'store_number': store_number,
        'distance_miles': distance,
        'heading_known': heading is not None,
        'found_ahead': found_ahead if heading is not None else False,
        'store_details': store,
    }


def find_closest_loves(path, session_id, current_location=None):
    """Find the closest scraped Love's store using a session's recent GPS fixes."""
    with sqlite3.connect(path) as db:
        return _closest_loves_from_db(db, session_id, current_location)


def _closest_roadys_from_db(db, session_id, current_location=None):
    if not session_id:
        return None
    try:
        events = db.execute('''
            SELECT latitude, longitude
            FROM user_location_events
            WHERE session_id=? AND latitude IS NOT NULL AND longitude IS NOT NULL
              AND recorded_at >= datetime('now', '-10 minutes')
            ORDER BY id DESC
            LIMIT 100
        ''', (session_id,)).fetchall()
        locations = db.execute('''
            SELECT location_id, name, address, city, state, postal_code, latitude, longitude,
                   location_type_code, group_code, interstate_exit, has_rewards,
                   rewards_multiplier, directions_url
            FROM roadys_locations
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        ''').fetchall()
    except sqlite3.OperationalError:
        return None
    if (not events and current_location is None) or not locations:
        return None

    current = current_location or (events[0][0], events[0][1])
    previous = next(((row[0], row[1]) for row in events
                     if _distance_miles(current, (row[0], row[1])) >= 0.05), None)
    heading = _bearing_degrees(previous, current) if previous else None
    location_columns = (
        'location_id', 'name', 'address', 'city', 'state', 'postal_code', 'latitude', 'longitude',
        'location_type_code', 'group_code', 'interstate_exit', 'has_rewards',
        'rewards_multiplier', 'directions_url',
    )
    candidates = []
    for row in locations:
        location = dict(zip(location_columns, row))
        distance = _distance_miles(current, (location['latitude'], location['longitude']))
        if heading is not None:
            bearing = _bearing_degrees(current, (location['latitude'], location['longitude']))
            angle_delta = abs((bearing - heading + 180) % 360 - 180)
            if angle_delta > 75:
                continue
        candidates.append((distance, str(location['location_id']), location))

    found_ahead = bool(candidates)
    if not candidates:
        candidates = [
            (_distance_miles(current, (location['latitude'], location['longitude'])),
             str(location['location_id']), location)
            for location in (dict(zip(location_columns, row)) for row in locations)
        ]
    distance, _, location = min(candidates, key=lambda candidate: candidate[:2])
    return {
        'distance_miles': distance,
        'heading_known': heading is not None,
        'found_ahead': found_ahead if heading is not None else False,
        'location_details': location,
    }


def find_closest_roadys(path, session_id, current_location=None):
    """Find the closest scraped Roady's location using recent session GPS fixes."""
    with sqlite3.connect(path) as db:
        return _closest_roadys_from_db(db, session_id, current_location)


def _restaurant_chain_from_question(db, question):
    try:
        names = db.execute('''
            SELECT chain, name, COUNT(*) AS occurrences
            FROM restaurants
            WHERE name IS NOT NULL AND trim(name) != ''
            GROUP BY chain, name
            ORDER BY LENGTH(name) DESC, occurrences DESC
        ''').fetchall()
    except sqlite3.OperationalError:
        return None

    available_chains = {str(chain).casefold(): str(chain) for chain, _, _ in names}
    for alias, chain in _RESTAURANT_CHAIN_ALIASES.items():
        if chain in available_chains and re.search(r'\b' + re.escape(alias) + r'\b', question, re.I):
            return available_chains[chain]

    for chain, name, _ in names:
        tokens = re.findall(r'[a-z0-9]+', str(name).casefold())
        if not tokens:
            continue
        pattern = r'(?<![a-z0-9])' + r'[^a-z0-9]*'.join(map(re.escape, tokens)) + r'(?![a-z0-9])'
        if re.search(pattern, question, re.IGNORECASE):
            return str(chain)
    return None


def _restaurant_chain_from_sources(db, question, sources):
    if not _CLOSEST_RESTAURANT.search(question):
        return None
    try:
        names = db.execute('''
            SELECT chain, name FROM restaurants
            WHERE chain IS NOT NULL AND name IS NOT NULL AND trim(name) != ''
        ''').fetchall()
    except sqlite3.OperationalError:
        return None

    names_by_chain = {}
    for chain, name in names:
        names_by_chain.setdefault(str(chain).casefold(), (str(chain), set()))[1].update(
            token for token in re.findall(r'[a-z0-9]+', str(name).casefold())
            if len(token) >= 4
        )
    question_tokens = [
        token for token in re.findall(r'[a-z0-9]+', question.casefold())
        if len(token) >= 5 and token not in {'closest', 'nearest', 'restaurant', 'direction', 'travel'}
    ]
    for source in sources or ():
        if str(source.get('routerFunction', '')).strip().casefold() != 'closest restaurant':
            continue
        chain = str(source.get('key') or '').strip()
        catalog_entry = names_by_chain.get(chain.casefold())
        if catalog_entry is None:
            continue
        canonical_chain, name_tokens = catalog_entry
        if any(
            SequenceMatcher(None, query_token, name_token).ratio() >= 0.72
            for query_token in question_tokens
            for name_token in name_tokens
        ):
            return canonical_chain
    return None


def _closest_restaurant_from_db(db, chain, session_id):
    if not session_id:
        return None
    try:
        events = db.execute('''
            SELECT latitude, longitude
            FROM user_location_events
            WHERE session_id=? AND latitude IS NOT NULL AND longitude IS NOT NULL
              AND recorded_at >= datetime('now', '-10 minutes')
            ORDER BY id DESC
            LIMIT 100
        ''', (session_id,)).fetchall()
        rows = db.execute('''
                 SELECT name, latitude, longitude, address, street, city, state, postal_code,
                   phone, website, opening_hours, drive_through
            FROM restaurants
            WHERE chain=? COLLATE NOCASE AND latitude IS NOT NULL AND longitude IS NOT NULL
        ''', (chain,)).fetchall()
    except sqlite3.OperationalError:
        return None
    if not events or not rows:
        return None

    columns = (
        'name', 'latitude', 'longitude', 'address', 'street', 'city', 'state', 'postal_code',
        'phone', 'website', 'opening_hours', 'drive_through',
    )
    current = (events[0][0], events[0][1])
    previous = next(((row[0], row[1]) for row in events
                     if _distance_miles(current, (row[0], row[1])) >= 0.05), None)
    heading = _bearing_degrees(previous, current) if previous else None
    candidates = []
    for row in rows:
        restaurant = dict(zip(columns, row))
        location = (restaurant['latitude'], restaurant['longitude'])
        distance = _distance_miles(current, location)
        if heading is not None:
            bearing = _bearing_degrees(current, location)
            angle_delta = abs((bearing - heading + 180) % 360 - 180)
            if angle_delta > 75:
                continue
        candidates.append((distance, restaurant))

    found_ahead = bool(candidates)
    if not candidates:
        candidates = [
            (_distance_miles(current, (restaurant['latitude'], restaurant['longitude'])), restaurant)
            for row in rows
            for restaurant in [dict(zip(columns, row))]
        ]
    distance, restaurant = min(candidates, key=lambda candidate: candidate[0])
    return {
        'restaurant': restaurant,
        'distance_miles': distance,
        'heading_known': heading is not None,
        'found_ahead': found_ahead if heading is not None else False,
    }


def _closest_restaurant_answer(db, question, chain, session_id):
    match = _closest_restaurant_from_db(db, chain, session_id)
    directions = None
    if match is None:
        name = chain.replace('_', ' ').title()
        answer = f'I need a recent location and imported {name} coordinates to find the closest restaurant.'
    else:
        restaurant = match['restaurant']
        name = str(restaurant.get('name') or chain.replace('_', ' ').title())
        distance = match['distance_miles']
        if match['heading_known'] and match['found_ahead']:
            answer = f'The closest {name} in the direction you are heading is about {distance:.1f} miles away.'
        elif match['heading_known']:
            answer = f'I could not find a {name} ahead in your direction of travel. The closest one overall is about {distance:.1f} miles away.'
        else:
            answer = f'The closest {name} is about {distance:.1f} miles away. I do not have enough recent movement sent from your device to determine your direction.'
        address = restaurant.get('address')
        if not address:
            address = ', '.join(str(restaurant[field]) for field in ('city', 'state', 'postal_code') if restaurant.get(field))
        address = _format_restaurant_address(address)
        directions = {
            'address': address,
            'latitude': restaurant.get('latitude'),
            'longitude': restaurant.get('longitude'),
        }
        details = []
        for field, label in (
            ('address', 'Address'), ('opening_hours', 'Hours'), ('drive_through', 'Drive-through'),
            ('phone', 'Phone'),
        ):
            value = address if field == 'address' else restaurant.get(field)
            if field == 'opening_hours' and value:
                value = _format_opening_hours(value)
            if field == 'phone' and value:
                value = _format_phone_for_speech(value)
            if value:
                details.append(f'{label}: {value}')
        answer += '\n\n' + '\n'.join(details)
        if restaurant.get('state') and not str(restaurant.get('street') or '').strip():
            answer += ('\n\nI can not give you the exact address. Our data is provided by '
                       'overpass it has a latitude and longitude but no street address. '
                       'I will contact the restaurant to provide a better API or update Overpass '
                       'with their location data')
    return {
        'id': f'closest-restaurant-{chain}',
        'point_id': f'closest-restaurant-{chain}',
        'question': question,
        'text': answer,
        'url': '',
        'source': 'router',
        'score': 1.0,
        'routerFunction': 'Closest Restaurant',
        'key': chain,
        'directions': directions,
    }


def _department_store_from_question(question):
    normalized_question = re.sub(r'[^a-z0-9]+', ' ', question.casefold()).strip()
    for brand, name in _DEPARTMENT_STORE_NAMES:
        normalized_name = re.sub(r'[^a-z0-9]+', ' ', name.casefold()).strip()
        if re.search(
                r'(?<![a-z0-9])' + re.escape(normalized_name) + r'(?![a-z0-9])',
                normalized_question):
            return brand
    return None


def _closest_department_store_from_db(db, brand, session_id):
    if not session_id:
        return None
    try:
        events = db.execute('''
            SELECT latitude, longitude
            FROM user_location_events
            WHERE session_id=? AND latitude IS NOT NULL AND longitude IS NOT NULL
              AND recorded_at >= datetime('now', '-10 minutes')
            ORDER BY id DESC
            LIMIT 100
        ''', (session_id,)).fetchall()
    except sqlite3.OperationalError:
        return None
    if not events:
        return None

    brands = [brand] if brand else sorted(_DEPARTMENT_STORE_SLUGS)
    locations = []
    columns = (
        'name', 'latitude', 'longitude', 'address', 'city', 'state', 'postal_code',
        'phone', 'website', 'opening_hours',
    )
    for candidate_brand in brands:
        if candidate_brand not in _DEPARTMENT_STORE_SLUGS:
            continue
        try:
            rows = db.execute(f'''
                SELECT name, latitude, longitude, address, city, state, postal_code,
                       phone, website, opening_hours
                FROM "department_{candidate_brand}"
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            ''').fetchall()
        except sqlite3.OperationalError:
            continue
        locations.extend(
            (candidate_brand, dict(zip(columns, row))) for row in rows
        )
    if not locations:
        return None

    current = (events[0][0], events[0][1])
    previous = next(((row[0], row[1]) for row in events
                     if _distance_miles(current, (row[0], row[1])) >= 0.05), None)
    heading = _bearing_degrees(previous, current) if previous else None
    candidates = []
    for candidate_brand, location in locations:
        coordinates = (location['latitude'], location['longitude'])
        distance = _distance_miles(current, coordinates)
        if heading is not None:
            bearing = _bearing_degrees(current, coordinates)
            angle_delta = abs((bearing - heading + 180) % 360 - 180)
            if angle_delta > 75:
                continue
        candidates.append((distance, candidate_brand, location))

    found_ahead = bool(candidates)
    if not candidates:
        candidates = [
            (_distance_miles(current, (location['latitude'], location['longitude'])),
             candidate_brand, location)
            for candidate_brand, location in locations
        ]
    distance, candidate_brand, location = min(candidates, key=lambda item: item[0])
    return {
        'brand': candidate_brand,
        'location': location,
        'distance_miles': distance,
        'heading_known': heading is not None,
        'found_ahead': found_ahead if heading is not None else False,
    }


def _closest_department_store_answer(db, question, brand, session_id):
    match = _closest_department_store_from_db(db, brand, session_id)
    selected_brand = match['brand'] if match else brand
    brand_names = next(
        (item['names'] for item in _DEPARTMENT_STORE_CATALOG if item['slug'] == selected_brand),
        ['department store'],
    )
    brand_name = brand_names[0]
    directions = None
    if match is None:
        if not session_id:
            answer = 'I need a recent location to find the closest department store.'
        else:
            answer = f'I do not have imported {brand_name} locations available to search yet.'
    else:
        location = match['location']
        name = str(location.get('name') or brand_name)
        distance = match['distance_miles']
        if match['heading_known'] and match['found_ahead']:
            answer = f'The closest {name} in the direction you are heading is about {distance:.1f} miles away.'
        elif match['heading_known']:
            answer = f'I could not find a {name} ahead in your direction of travel. The closest one overall is about {distance:.1f} miles away.'
        else:
            answer = f'The closest {name} is about {distance:.1f} miles away. I do not have enough recent movement to determine your direction.'
        address = str(location.get('address') or '').strip()
        state = str(location.get('state') or '').strip()
        state_names = {state.casefold()}
        if state.upper() in _US_STATE_NAMES:
            state_names.add(_US_STATE_NAMES[state.upper()].casefold())
        if address.casefold() in state_names:
            address = ''
        if not address:
            address = ', '.join(
            str(location[field]) for field in ('city', 'state', 'postal_code') if location.get(field)
            )
        if address.casefold() in state_names:
            address = ''
        address = _format_restaurant_address(address)
        directions = {
            'address': address,
            'latitude': location.get('latitude'),
            'longitude': location.get('longitude'),
        }
        details = []
        for field, label in (
            ('address', 'Address'), ('opening_hours', 'Hours'), ('phone', 'Phone'),
            ('website', 'Website'),
        ):
            value = address if field == 'address' else location.get(field)
            if field == 'opening_hours' and value:
                value = _format_opening_hours(value)
            if field == 'phone' and value:
                value = _format_phone_for_speech(value)
            if value:
                details.append(f'{label}: {value}')
        if not address:
            details.append('Address: not available in the imported data.')
        answer += '\n\n' + '\n'.join(details)
    return {
        'id': f'closest-department-store-{selected_brand or "any"}',
        'point_id': f'closest-department-store-{selected_brand or "any"}',
        'question': question,
        'text': answer,
        'url': '',
        'source': 'router',
        'score': 1.0,
        'routerFunction': 'Closest Department Store',
        'key': selected_brand or '',
        'directions': directions,
    }


def _grocery_store_from_question(question):
    normalized_question = re.sub(r'[^a-z0-9]+', ' ', question.casefold()).strip()
    for brand, name in _GROCERY_STORE_NAMES:
        normalized_name = re.sub(r'[^a-z0-9]+', ' ', name.casefold()).strip()
        if re.search(
                r'(?<![a-z0-9])' + re.escape(normalized_name) + r'(?![a-z0-9])',
                normalized_question):
            return brand
    return None


def _grocery_locations_from_db(db, brand):
    brands = [brand] if brand else sorted(_GROCERY_STORE_SLUGS)
    locations = []
    columns = (
        'name', 'latitude', 'longitude', 'address', 'city', 'state', 'postal_code',
        'phone', 'website', 'opening_hours',
    )
    for candidate_brand in brands:
        if candidate_brand not in _GROCERY_STORE_SLUGS:
            continue
        try:
            rows = db.execute(f'''
                SELECT name, latitude, longitude, address, city, state, postal_code,
                       phone, website, opening_hours
                FROM "grocery_{candidate_brand}"
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            ''').fetchall()
        except sqlite3.OperationalError:
            continue
        locations.extend(
            (candidate_brand, dict(zip(columns, row))) for row in rows
        )
    return locations


def _closest_grocery_store_from_db(db, brand, session_id):
    if not session_id:
        return None
    try:
        events = db.execute('''
            SELECT latitude, longitude
            FROM user_location_events
            WHERE session_id=? AND latitude IS NOT NULL AND longitude IS NOT NULL
              AND recorded_at >= datetime('now', '-10 minutes')
            ORDER BY id DESC
            LIMIT 100
        ''', (session_id,)).fetchall()
    except sqlite3.OperationalError:
        return None
    if not events:
        return None

    locations = _grocery_locations_from_db(db, brand)
    if not locations:
        return None

    current = (events[0][0], events[0][1])
    previous = next(((row[0], row[1]) for row in events
                     if _distance_miles(current, (row[0], row[1])) >= 0.05), None)
    heading = _bearing_degrees(previous, current) if previous else None
    candidates = []
    for candidate_brand, location in locations:
        coordinates = (location['latitude'], location['longitude'])
        distance = _distance_miles(current, coordinates)
        if heading is not None:
            bearing = _bearing_degrees(current, coordinates)
            angle_delta = abs((bearing - heading + 180) % 360 - 180)
            if angle_delta > 75:
                continue
        candidates.append((distance, candidate_brand, location))

    found_ahead = bool(candidates)
    if not candidates:
        candidates = [
            (_distance_miles(current, (location['latitude'], location['longitude'])),
             candidate_brand, location)
            for candidate_brand, location in locations
        ]
    distance, candidate_brand, location = min(candidates, key=lambda item: item[0])
    return {
        'brand': candidate_brand,
        'location': location,
        'distance_miles': distance,
        'heading_known': heading is not None,
        'found_ahead': found_ahead if heading is not None else False,
    }


def _closest_grocery_store_answer(db, question, brand, session_id):
    match = _closest_grocery_store_from_db(db, brand, session_id)
    selected_brand = match['brand'] if match else brand
    brand_names = next(
        (item['names'] for item in _GROCERY_STORE_CATALOG if item['slug'] == selected_brand),
        ['grocery store'],
    )
    brand_name = brand_names[0]
    directions = None
    if match is None:
        if not session_id:
            answer = 'I need a recent location to find the closest grocery store.'
        else:
            answer = f'I do not have imported {brand_name} locations available to search yet.'
    else:
        location = match['location']
        name = str(location.get('name') or brand_name)
        distance = match['distance_miles']
        if match['heading_known'] and match['found_ahead']:
            answer = f'The closest {name} in the direction you are heading is about {distance:.1f} miles away.'
        elif match['heading_known']:
            answer = f'I could not find a {name} ahead in your direction of travel. The closest one overall is about {distance:.1f} miles away.'
        else:
            answer = f'The closest {name} is about {distance:.1f} miles away. I do not have enough recent movement to determine your direction.'
        address = str(location.get('address') or '').strip()
        state = str(location.get('state') or '').strip()
        state_names = {state.casefold()}
        if state.upper() in _US_STATE_NAMES:
            state_names.add(_US_STATE_NAMES[state.upper()].casefold())
        if address.casefold() in state_names:
            address = ''
        if not address:
            address = ', '.join(
                str(location[field]) for field in ('city', 'state', 'postal_code') if location.get(field)
            )
        if address.casefold() in state_names:
            address = ''
        address = _format_restaurant_address(address)
        directions = {
            'address': address,
            'latitude': location.get('latitude'),
            'longitude': location.get('longitude'),
        }
        details = []
        for field, label in (
            ('address', 'Address'), ('opening_hours', 'Hours'), ('phone', 'Phone'),
            ('website', 'Website'),
        ):
            value = address if field == 'address' else location.get(field)
            if field == 'opening_hours' and value:
                value = _format_opening_hours(value)
            if field == 'phone' and value:
                value = _format_phone_for_speech(value)
            if value:
                details.append(f'{label}: {value}')
        if not address:
            details.append('Address: not available in the imported data.')
        answer += '\n\n' + '\n'.join(details)
    return {
        'id': f'closest-grocery-store-{selected_brand or "any"}',
        'point_id': f'closest-grocery-store-{selected_brand or "any"}',
        'question': question,
        'text': answer,
        'url': '',
        'source': 'router',
        'score': 1.0,
        'routerFunction': 'Closest Grocery Store',
        'key': selected_brand or '',
        'directions': directions,
    }


def _grocery_store_address(location):
    address = str(location.get('address') or '').strip()
    state = str(location.get('state') or '').strip()
    state_names = {state.casefold()}
    if state.upper() in _US_STATE_NAMES:
        state_names.add(_US_STATE_NAMES[state.upper()].casefold())
    if address.casefold() in state_names:
        address = ''
    if not address:
        address = ', '.join(
            str(location[field]) for field in ('city', 'state', 'postal_code') if location.get(field)
        )
    if address.casefold() in state_names:
        address = ''
    return _format_restaurant_address(address)


def _list_grocery_stores_answer(db, question, brand, session_id):
    def routed(text, stores=None, key=''):
        return {
            'id': f'grocery-store-list-{key or "unknown"}',
            'point_id': f'grocery-store-list-{key or "unknown"}',
            'question': question,
            'text': text,
            'url': '',
            'source': 'router',
            'score': 1.0,
            'routerFunction': 'List Grocer Stores',
            'key': key,
            'directionsList': stores or [],
        }

    if brand is None:
        return routed('Please name a grocery brand so I can list its nearest stores.')
    if not session_id:
        return routed('I need a recent location to list nearby grocery stores.', key=brand)

    try:
        current_row = db.execute('''
            SELECT latitude, longitude
            FROM user_location_events
            WHERE session_id=? AND latitude IS NOT NULL AND longitude IS NOT NULL
              AND recorded_at >= datetime('now', '-10 minutes')
            ORDER BY id DESC
            LIMIT 1
        ''', (session_id,)).fetchone()
    except sqlite3.OperationalError:
        current_row = None
    if not current_row:
        return routed('I need a recent location to list nearby grocery stores.', key=brand)

    locations = _grocery_locations_from_db(db, brand)
    if not locations:
        brand_name = next(
            (item['names'][0] for item in _GROCERY_STORE_CATALOG if item['slug'] == brand),
            'grocery store',
        )
        return routed(f'I do not have imported {brand_name} locations available to list yet.', key=brand)

    current = (current_row[0], current_row[1])
    ranked = sorted(
        (
            (_distance_miles(current, (location['latitude'], location['longitude'])), location)
            for candidate_brand, location in locations if candidate_brand == brand
        ),
        key=lambda item: item[0],
    )[:5]
    brand_name = next(
        (item['names'][0] for item in _GROCERY_STORE_CATALOG if item['slug'] == brand),
        'grocery store',
    )
    stores = []
    for number, (distance, location) in enumerate(ranked, 1):
        name = str(location.get('name') or brand_name)
        address = _grocery_store_address(location)
        stores.append({
            'number': number,
            'name': name,
            'address': address,
            'latitude': location['latitude'],
            'longitude': location['longitude'],
            'distance_miles': round(distance, 1),
        })

    db.execute('DELETE FROM grocery_store_lists WHERE session_id = ?', (session_id,))
    db.executemany(
        '''
        INSERT INTO grocery_store_lists(session_id, position, brand, location_json)
        VALUES (?, ?, ?, ?)
        ''',
        [
            (session_id, store['number'], brand, json.dumps(store, ensure_ascii=False))
            for store in stores
        ],
    )
    text = f'Here are the {len(stores)} nearest {brand_name} grocery stores:'
    for store in stores:
        address = store['address'] or 'Address not available'
        text += (
            f"\n{store['number']}. {store['name']} — "
            f"{store['distance_miles']:.1f} miles away — {address}"
        )
    text += '\nChoose a store below or ask, “Show me directions for 1.”'
    return routed(text, stores, brand)


def _show_number_direction_answer(db, question, number, session_id):
    def routed(text, direction=None):
        return {
            'id': f'show-number-direction-{number}',
            'point_id': f'show-number-direction-{number}',
            'question': question,
            'text': text,
            'url': '',
            'source': 'router',
            'score': 1.0,
            'routerFunction': 'Show Number Direction',
            'key': number,
            'directions': direction,
        }

    if not session_id:
        return routed('I cannot find a numbered grocery list for this chat. Ask for a grocery-store list first.')
    rows = db.execute('''
        SELECT position, location_json
        FROM grocery_store_lists
        WHERE session_id = ?
        ORDER BY position
    ''', (session_id,)).fetchall()
    if not rows:
        return routed('I do not have a numbered grocery-store list for this chat. Ask for a grocery-store list first.')
    if number < 1 or number > len(rows):
        return routed(f'Choose a store number from 1 through {len(rows)} in your latest grocery-store list.')

    position, location_json = next(row for row in rows if row[0] == number)
    direction = json.loads(location_json)
    address = direction.get('address') or 'address unavailable'
    return routed(
        f"Opening directions for number {position}: {direction['name']}, {address}.",
        direction,
    )


def _decode_json(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _supported_restaurants_answer(db, question):
    try:
        rows = db.execute('''
            SELECT name FROM restaurant_names
            WHERE name IS NOT NULL AND trim(name) != '' AND "primary" = 1
            ORDER BY name COLLATE NOCASE
        ''').fetchall()
        has_any_names = db.execute('''
            SELECT 1 FROM restaurant_names
            WHERE name IS NOT NULL AND trim(name) != '' LIMIT 1
        ''').fetchone()
    except sqlite3.OperationalError:
        rows = []
        has_any_names = None
    if not rows and not has_any_names:
        try:
            rows = db.execute('''
                SELECT DISTINCT trim(name) FROM restaurants
                WHERE name IS NOT NULL AND trim(name) != ''
                ORDER BY trim(name) COLLATE NOCASE
            ''').fetchall()
        except sqlite3.OperationalError:
            rows = []

    names = []
    seen = set()
    for row in rows:
        name = str(row[0]).strip()
        if name and name.casefold() not in seen:
            names.append(name)
            seen.add(name.casefold())
    answer = ('The restaurants currently supported are:\n' + '\n'.join(names)
              if names else 'There are no supported restaurants in the system yet.')
    return {
        'id': 'supported-restaurants',
        'point_id': 'supported-restaurants',
        'question': question,
        'text': answer,
        'url': '',
        'source': 'router',
        'score': 1.0,
        'routerFunction': 'Supported Restaurants',
        'key': '',
    }


def _restaurant_names(value):
    restaurants = _decode_json(value, [])
    names = []
    known_names = {'chesters': "Chester's", 'mcdonalds': "McDonald's"}
    for restaurant in restaurants:
        if isinstance(restaurant, str):
            name = restaurant.strip()
        elif isinstance(restaurant, dict):
            name = str(restaurant.get('title') or '').strip()
            if not name:
                image = str(restaurant.get('src') or '').rsplit('/', 1)[-1]
                name = image.rsplit('.', 1)[0].replace('-', ' ').replace('_', ' ').title()
                key = name.casefold().replace("'", '').replace(' ', '')
                name = known_names.get(key, name)
        else:
            continue
        if name and name.casefold() not in {item.casefold() for item in names}:
            names.append(name)
    return names


def _closest_loves_details(store):
    details = store['store_details']
    lines = []

    address = str(details.get('address') or 'Not listed')
    address = re.sub(r'\bHwy\b\.?', 'Highway', address, flags=re.I)
    address = re.sub(r'\bI\s*-\s*(\d+)\b', r'Interstate \1', address, flags=re.I)
    address = _format_restaurant_address(address)
    lines.append(f'Address: {address}')
    phone = details.get('phone')
    if phone:
        lines.append(f'Phone: {_format_phone_for_speech(phone)}')
    store_hours = str(details.get('store_hours') or '').strip()
    if store_hours:
        lines.append(f'Store hours: {_format_spoken_hours(store_hours)}')

    prices = _decode_json(details.get('fuel_prices_json'), {})
    diesel_prices = []
    if isinstance(prices, dict):
        for label, price_data in prices.items():
            if 'diesel' not in str(label).casefold():
                continue
            display = price_data.get('display') if isinstance(price_data, dict) else None
            price = price_data.get('price') if isinstance(price_data, dict) else price_data
            if not display and isinstance(price, (int, float)):
                display = f'${price:.3f}'
            if display:
                spoken_label = str(label).strip().title()
                diesel_prices.append(f'{spoken_label}: {display} per gallon.')
    lines.append('Diesel prices:')
    lines.extend(diesel_prices or ['Not listed'])
    if details.get('scraped_at') and diesel_prices:
        scraped_date = str(details['scraped_at'])[:10]
        try:
            parsed_date = date.fromisoformat(scraped_date)
            scraped_date = f'{parsed_date:%B} {parsed_date.day}, {parsed_date.year}'
        except ValueError:
            pass
        lines.append(f'Fuel prices last scraped on {scraped_date}.')

    parking = details.get('truck_parking')
    if parking is None:
        parking_line = 'Not listed'
    elif int(parking) > 0:
        parking_line = f'Yes ({int(parking)} spaces listed)'
    else:
        parking_line = 'No spaces listed'
    lines.append(f'Truck parking: {parking_line}')

    amenities = _decode_json(details.get('amenities_json'), [])
    amenity_names = [str(item).casefold() for item in amenities]
    shower_count = details.get('shower_count')
    if shower_count is not None and int(shower_count) > 0:
        shower_line = f'Yes ({int(shower_count)} private showers)'
    elif any('shower' in item for item in amenity_names):
        shower_line = 'Yes'
    else:
        shower_line = 'Not listed'
    lines.append(f'Showers: {shower_line}')

    has_laundry = any('laundry' in item for item in amenity_names)
    lines.append(f"Laundry: {'Yes' if has_laundry else 'Not listed'}")

    restaurants = _restaurant_names(details.get('restaurants_json'))
    if len(restaurants) > 1:
        restaurant_text = f"{', '.join(restaurants[:-1])} and {restaurants[-1]}"
    else:
        restaurant_text = restaurants[0] if restaurants else 'Not listed'
    lines.append(f'Restaurants: {restaurant_text}')
    restaurant_hours = _decode_json(details.get('restaurant_hours_json'), [])
    if isinstance(restaurant_hours, dict):
        restaurant_hours = [restaurant_hours]
    hours_lines = []
    if isinstance(restaurant_hours, list):
        for restaurant in restaurant_hours:
            if not isinstance(restaurant, dict):
                continue
            name = str(restaurant.get('name') or 'Restaurant').strip()
            hours = str(restaurant.get('hours') or '').strip()
            if not hours:
                schedule = restaurant.get('schedule')
                if isinstance(schedule, list):
                    hours = '; '.join(str(item).strip() for item in schedule if str(item).strip())
                elif schedule:
                    hours = str(schedule).strip()
            if hours:
                hours_lines.append(f'{name}: {_format_spoken_hours(hours)}')
    if hours_lines:
        lines.append('Restaurant hours:')
        lines.extend(hours_lines)
    return '\n'.join(lines)


def _closest_loves_answer(db, question, session_id):
    match = _closest_loves_from_db(db, session_id)
    directions = None
    if match is None:
        answer = 'I need a recent location and scraped Love\'s store coordinates to find the closest stop. Allow location access and try again after the next location update.'
    else:
        details = match['store_details']
        address = str(details.get('address') or '').strip()
        if address and address.casefold() != 'not listed':
            address = re.sub(r'\bHwy\b\.?' , 'Highway', address, flags=re.I)
            address = re.sub(r'\bI\s*-\s*(\d+)\b', r'Interstate \1', address, flags=re.I)
            address = _format_restaurant_address(address)
        directions = {
            'address': address,
            'latitude': details.get('latitude'),
            'longitude': details.get('longitude'),
        }
        store = match['store_number']
        distance = match['distance_miles']
        if match['heading_known'] and match['found_ahead']:
            answer = f"The closest Love's in the direction you're heading is store {store}, about {distance:.1f} miles away."
        elif match['heading_known']:
            answer = f"I couldn't find a Love's ahead in your recent direction of travel. The closest Love's overall is store {store}, about {distance:.1f} miles away."
        else:
            answer = f"The closest Love's is store {store}, about {distance:.1f} miles away. I don't have enough recent movement to determine your direction."
        answer += '\n\n' + _closest_loves_details(match)
    return {
        'id': 'closest-loves',
        'point_id': 'closest-loves',
        'question': question,
        'text': answer,
        'url': '',
        'source': 'router',
        'score': 1.0,
        'routerFunction': 'Closest Loves',
        'key': '',
        'directions': directions,
    }


def _closest_roadys_details(db, location):
    details = location['location_details']
    lines = []

    def add_section(title, values):
        if values:
            lines.extend(['', f'{title}:'])
            lines.extend(values)

    state = details.get('state')
    if state:
        state = _US_STATE_NAMES.get(str(state).strip().upper(), state)
    street_address = re.sub(
        r'\bHwy\b\.?', 'Highway', str(details.get('address') or ''), flags=re.I
    )
    address_parts = [street_address, details.get('city'), state, details.get('postal_code')]
    address = ', '.join(str(part).strip() for part in address_parts if part)
    if address:
        lines.append(f'Address: {address}')
    if details.get('interstate_exit'):
        interstate_exit = re.sub(r'\bHwy\b\.?', 'Highway', str(details['interstate_exit']), flags=re.I)
        if not re.search(rf'(?<!\w){re.escape(interstate_exit)}(?!\w)', street_address, flags=re.I):
            lines.append(f'Interstate exit: {interstate_exit}')
    if details.get('has_rewards') is not None:
        rewards = 'Yes' if details['has_rewards'] else 'No'
        multiplier = details.get('rewards_multiplier')
        if multiplier:
            rewards += f" ({multiplier:g}x multiplier)"
        lines.append(f'Rewards: {rewards}')

    facility_rows = db.execute(
        'SELECT facility_id, kind, name FROM roadys_facilities WHERE location_id=? ORDER BY kind, name',
        (details['location_id'],),
    ).fetchall()
    hours_rows = db.execute('''
        SELECT f.facility_id, f.kind, f.name, h.raw_text
        FROM roadys_hours h JOIN roadys_facilities f USING(facility_id)
        WHERE f.location_id=? ORDER BY f.kind, f.name, h.rule_number
    ''', (details['location_id'],)).fetchall()
    hours_by_facility = {}
    for facility_id, _, _, raw_text in hours_rows:
        if raw_text:
            normalized_hours = ' '.join(raw_text.split()).casefold()
            if normalized_hours in {'open 24 hours', '24 hours', '24/7', 'open 24/7'}:
                raw_text = 'Open 24 hours a day 7 days a week'
            hours_by_facility.setdefault(facility_id, []).append(raw_text)
    has_store_hours = any(kind == 'store' and raw_text for _, kind, _, raw_text in hours_rows)

    restaurants = []
    other_facilities = []
    location_name = ' '.join(str(details.get('name') or '').split()).casefold()
    for facility_id, kind, name in facility_rows:
        if kind == 'restaurant':
            hours = list(dict.fromkeys(hours_by_facility.get(facility_id, [])))
            restaurants.append(f'{name}: {", ".join(hours)}' if hours else name)
        else:
            if kind == 'store' and ' '.join(str(name).split()).casefold() == location_name:
                continue
            other_facilities.append(f'{name} ({kind})')
    if restaurants:
        add_section('Restaurants', list(dict.fromkeys(restaurants)))
    if other_facilities:
        add_section('Facilities', list(dict.fromkeys(other_facilities)))

    amenity_rows = db.execute('''
        SELECT a.name, la.raw_value, la.boolean_value, la.numeric_value
        FROM roadys_location_amenities la
        JOIN roadys_amenities a USING(amenity_id)
        WHERE la.location_id=? ORDER BY a.name
    ''', (details['location_id'],)).fetchall()
    if amenity_rows:
        amenities = []
        for name, raw_value, boolean_value, numeric_value in amenity_rows:
            normalized_name = ''.join(char for char in str(name).casefold() if char.isalnum())
            if normalized_name in {'seats', 'gasbrand', 'chains', 'fuelservice'} or (
                normalized_name == 'storehours' and has_store_hours
            ):
                continue
            if boolean_value is not None:
                value = 'Yes' if boolean_value else 'No'
            elif raw_value:
                value = raw_value
            elif numeric_value is not None:
                value = str(numeric_value)
            else:
                value = None
            if isinstance(value, str):
                value = re.sub(
                    r'\bopen\s+24/7\b',
                    'Open 24 hours a day 7 days a week',
                    value,
                    flags=re.I,
                )
            if value == 'Yes':
                if normalized_name == 'internetwireless':
                    amenities.append('Wireless Internet')
                elif normalized_name == 'laundryservices':
                    amenities.append('Laundry Services')
                else:
                    amenities.append(name)
            else:
                amenities.append(f'{name}: {value}' if value else name)
        add_section('Amenities', amenities)

    other_hours = list(dict.fromkeys(
        'Open 24 hours a day 7 days a week'
        if ' '.join(raw_text.split()).casefold() in {'open 24 hours', '24 hours', '24/7', 'open 24/7'}
        else raw_text
        for _, kind, name, raw_text in hours_rows
        if kind != 'restaurant' and raw_text
    ))
    if other_hours:
        add_section('Hours', other_hours)

    contact_rows = db.execute('''
        SELECT label, number, extension FROM roadys_contacts
        WHERE location_id=? ORDER BY label, number
    ''', (details['location_id'],)).fetchall()
    if contact_rows:
        def display_phone(number):
            digits = ''.join(char for char in str(number) if char.isdigit())
            if len(digits) == 11 and digits.startswith('1'):
                digits = digits[1:]
            if len(digits) == 10:
                return f'{digits[:3]} {digits[3:6]} {digits[6:]}'
            return number

        contacts = [f'{label}: {display_phone(number)}' + (f' ext. {extension}' if extension else '')
                    for label, number, extension in contact_rows]
        add_section('Contact', contacts)

    fuel_prices = db.execute('''
        SELECT fuel_type, price_mills, observed_at FROM roadys_fuel_prices
        WHERE location_id=? ORDER BY fuel_type
    ''', (details['location_id'],)).fetchall()
    if fuel_prices:
        prices = [f'{fuel_type}: ${price_mills / 1000:.3f} per gallon' for fuel_type, price_mills, _ in fuel_prices]
        lines.append('Fuel prices: ' + '; '.join(prices))
    return '\n'.join(lines)


def _closest_roadys_answer(db, question, session_id):
    match = _closest_roadys_from_db(db, session_id)
    directions = None
    if match is None:
        answer = "I need a recent location and Roady's coordinates to find the closest stop. Allow location access and try again after the next location update."
    else:
        details = match['location_details']
        directions = {
            'address': ', '.join(str(part).strip() for part in (
                details.get('address'), details.get('city'), details.get('state'), details.get('postal_code'),
            ) if part),
            'latitude': details.get('latitude'),
            'longitude': details.get('longitude'),
        }
        name = str(details.get('name') or "Roady's")
        name = re.sub(r'\baka\b', 'also known as', name, flags=re.I)
        distance = match['distance_miles']
        if match['heading_known'] and match['found_ahead']:
            answer = f"The closest Roady's in the direction you're heading is {name}, about {distance:.1f} miles away."
        elif match['heading_known']:
            answer = f"I couldn't find a Roady's ahead in your recent direction of travel. The closest overall is {name}, about {distance:.1f} miles away."
        else:
            answer = f"The closest Roady's is {name}, about {distance:.1f} miles away. I don't have enough recent movement to determine your direction."
        rendered_details = _closest_roadys_details(db, match)
        if rendered_details:
            answer += '\n\n' + rendered_details
    return {
        'id': 'closest-roadys',
        'point_id': 'closest-roadys',
        'question': question,
        'text': answer,
        'url': '',
        'source': 'router',
        'score': 1.0,
        'routerFunction': 'Closest Roadys',
        'key': '',
        'directions': directions,
    }


def resolve_routed_answer(topic, question, path=None, session_id=None, sources=None):
    """Return the matching routed source or None when no registered route matches."""
    path = Path(path) if path is not None else Path('database/chat_history.sqlite3')
    try:
        with sqlite3.connect(path) as db:
            route_names = [row[0] for row in db.execute('SELECT name FROM routerFunctions ORDER BY id')]
            registered_routes = {str(name).strip().casefold() for name in route_names}
            if ('list grocer stores' in registered_routes
                    and _LIST_GROCERY_STORES.search(question)):
                return _list_grocery_stores_answer(
                    db, question, _grocery_store_from_question(question), session_id,
                )
            direction_number = _show_number_direction_number(question)
            if 'show number direction' in registered_routes and direction_number is not None:
                return _show_number_direction_answer(
                    db, question, direction_number, session_id,
                )
            for source in sources or ():
                source_route = str(source.get('routerFunction', '')).strip().casefold()
                if source_route == 'closest grocery store' and _CLOSEST_GROCERY_STORE.search(question):
                    brand = _grocery_store_from_question(question)
                    source_key = str(source.get('key') or '').strip()
                    if brand is None and source_key in _GROCERY_STORE_SLUGS:
                        brand = source_key
                    if brand is not None or _GROCERY_CONTEXT.search(question) or source_key in _GROCERY_STORE_SLUGS:
                        return _closest_grocery_store_answer(db, question, brand, session_id)
                if source_route == 'closest department store' and _CLOSEST_DEPARTMENT_STORE.search(question):
                    if _GROCERY_CONTEXT.search(question):
                        continue
                    brand = _department_store_from_question(question)
                    source_key = str(source.get('key') or '').strip()
                    if brand is None and source_key in _DEPARTMENT_STORE_SLUGS:
                        brand = source_key
                    return _closest_department_store_answer(db, question, brand, session_id)
                if source_route == 'closest restaurant':
                    chain = _restaurant_chain_from_question(db, question)
                    if chain:
                        return _closest_restaurant_answer(db, question, chain, session_id)
                if source_route == 'closest loves' and _CLOSEST_LOVES.search(question):
                    return _closest_loves_answer(db, question, session_id)
                if source_route == 'closest roadys' and _CLOSEST_ROADYS.search(question):
                    return _closest_roadys_answer(db, question, session_id)

            for route_name, matcher, answer_handler in (
                ('closest loves', _CLOSEST_LOVES, _closest_loves_answer),
                ('closest roadys', _CLOSEST_ROADYS, _closest_roadys_answer),
            ):
                if route_name in registered_routes and matcher.search(question):
                    return answer_handler(db, question, session_id)
            if ('closest restaurant' in {str(name).strip().casefold() for name in route_names}
                    and _CLOSEST_RESTAURANT.search(question)):
                chain = _restaurant_chain_from_question(db, question)
                if chain is None:
                    chain = _restaurant_chain_from_sources(db, question, sources)
                if chain is not None:
                    return _closest_restaurant_answer(db, question, chain, session_id)
                restaurant_context = re.search(r'\brestaurants?\b', question, re.I) or any(
                    str(source.get('routerFunction', '')).strip().casefold() == 'closest restaurant'
                    and re.search(r'\brestaurants?\b', str(source.get('question', '')), re.I)
                    for source in sources or ()
                )
                if sources is not None and restaurant_context:
                    return {
                        'id': 'closest-restaurant-unknown',
                        'point_id': 'closest-restaurant-unknown',
                        'question': question,
                        'text': 'Sorry, I do not have data for that restaurant.',
                        'url': '',
                        'source': 'router',
                        'score': 1.0,
                        'routerFunction': 'Closest Restaurant',
                        'key': '',
                    }
            if ('closest grocery store' in registered_routes
                    and _CLOSEST_GROCERY_STORE.search(question)
                    and _GROCERY_CONTEXT.search(question)):
                brand = _grocery_store_from_question(question)
                return _closest_grocery_store_answer(db, question, brand, session_id)
            if ('closest department store' in registered_routes
                    and _CLOSEST_DEPARTMENT_STORE.search(question)):
                brand = _department_store_from_question(question)
                department_context = re.search(r'\bdepartment\s+stores?\b', question, re.I)
                source_context = any(
                    str(source.get('routerFunction', '')).strip().casefold() == 'closest department store'
                    and re.search(r'\bdepartment\s+stores?\b', str(source.get('question', '')), re.I)
                    for source in sources or ()
                )
                if (brand or department_context or source_context) and not _GROCERY_CONTEXT.search(question):
                    return _closest_department_store_answer(db, question, brand, session_id)
            for route_name in route_names:
                handler = _ROUTE_HANDLERS.get(str(route_name).strip().casefold())
                if handler is None:
                    continue
                matcher, minimum_key, maximum_key = handler
                key = matcher(question)
                if key is None:
                    continue
                if str(route_name).strip().casefold() == 'closest loves':
                    return _closest_loves_answer(db, question, session_id)
                if str(route_name).strip().casefold() == 'closest roadys':
                    return _closest_roadys_answer(db, question, session_id)
                if str(route_name).strip().casefold() == 'supported restaurants':
                    return _supported_restaurants_answer(db, question)
                if ((minimum_key is not None and key < minimum_key)
                        or (maximum_key is not None and key > maximum_key)):
                    valid_range = f'{minimum_key} through {maximum_key}'
                    return {
                        'id': 1,
                        'point_id': f'no-such-step:{key}',
                        'question': question,
                        'text': f'There is no Step {key} in the pre-trip inspection. Valid steps are {valid_range}.',
                        'url': '',
                        'source': 'router',
                        'score': 1.0,
                        'routerFunction': route_name,
                        'key': key,
                    }
                row = db.execute('''
                    SELECT id, question, answer, text, url, source, routerFunction, "key"
                    FROM topic_data
                    WHERE topic = ? COLLATE NOCASE
                      AND routerFunction = ? COLLATE NOCASE
                      AND CAST("key" AS INTEGER) = ?
                    ORDER BY CASE WHEN question = ? COLLATE NOCASE THEN 0 ELSE 1 END,
                             question COLLATE NOCASE, id
                    LIMIT 1
                ''', (topic, route_name, key, question)).fetchone()
                if row is None:
                    continue
                return {
                    'id': 1,
                    'point_id': str(row[0]),
                    'question': row[1],
                    'text': row[2] or row[3] or '',
                    'url': row[4] or '',
                    'source': row[5] or 'user_additions',
                    'score': 1.0,
                    'routerFunction': route_name,
                    'key': row[7],
                }
    except sqlite3.OperationalError:
        return None
    return None