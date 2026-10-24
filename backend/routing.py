"""Resolve registered routes that need deterministic lookup after vector search."""
import re
import sqlite3
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
_NUMBER_WORDS = set(_SMALL_NUMBERS) | set(_TENS)
for _tens_word in _TENS:
    for _ones_word in _ONES:
        _NUMBER_WORDS.add(f'{_tens_word} {_ones_word}')
        _NUMBER_WORDS.add(f'{_tens_word}-{_ones_word}')
_NUMBER_WORD_PATTERN = '|'.join(re.escape(word) for word in sorted(_NUMBER_WORDS, key=len, reverse=True))
_STEP_NUMBER = re.compile(rf'\bstep\s+(\d+|{_NUMBER_WORD_PATTERN})(?![a-z0-9-])', re.I)
_ROUTE_HANDLERS = {}


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


register_router_function('Pre-trip Text', _pre_trip_step, minimum_key=1, maximum_key=39)


def resolve_routed_answer(topic, question, path=None):
    """Return the matching routed source or None when no registered route matches."""
    path = Path(path) if path is not None else Path('database/chat_history.sqlite3')
    try:
        with sqlite3.connect(path) as db:
            route_names = [row[0] for row in db.execute('SELECT name FROM routerFunctions ORDER BY id')]
            for route_name in route_names:
                handler = _ROUTE_HANDLERS.get(str(route_name).strip().casefold())
                if handler is None:
                    continue
                matcher, minimum_key, maximum_key = handler
                key = matcher(question)
                if key is None:
                    continue
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