"""Conservative CDL following-gap calculations for explicit length/speed questions."""
from dataclasses import dataclass
import math
import re

NUM = r'[-+]?(?:\d+(?:\.\d+)?|\.\d+)'
LENGTH = re.compile(rf'(?<![\w.])({NUM})\s*[- ]?\s*(?:foot|feet|ft)\b', re.I)
SPEED = re.compile(
    rf'(?<![\w.])({NUM})\s*[- ]?\s*'
    r'(mph|miles?\s+(?:per|an?)\s+hour|mi/h)\b', re.I)
TIMING = re.compile(
    r'\b(?:seconds?|time|how\s+long|how\s+quickly|how\s+much\s+time|'
    r'how\s+(?:much\s+)?(?:following\s+)?distance|(?:safe\s+)?following\s+distance|'
    r'(?:stopping|braking)\s+distance)\b', re.I)
INTENT = re.compile(r'\b(?:stop|stops|stopped|stopping|follow|following|headway)\b', re.I)
EXCLUDE = re.compile(
    r'\b(?:why|compare|comparison|versus|vs|always|double|doubling|'
    r'brake\s+fade|brake\s+failure|inspect|inspection|repair|oil|pressure|'
    r'engine|hazmat|hydroplan|additional\s+information|what\s+else)\b', re.I)
CONTEXT = re.compile(r'\b(?:truck|tractor|trailer|rig|righ|vehicle|combination|bus|semi)\b', re.I)
RANGE = re.compile(rf'{NUM}\s*(?:or|to|through|[-–—])\s*{NUM}\s*(?:mph|kph|km/h|feet|foot|ft)\b', re.I)


@dataclass
class FollowingDistanceResult:
    handled: bool
    status: str
    answer: str = ''
    length_feet: float | None = None
    speed_mph: float | None = None
    following_gap_seconds: float | None = None
    formula: str | None = None


def _normalize(text: str) -> str:
    return text.lower().replace('\u2011', '-').replace('\u2212', '-').replace('\u2019', "'")


def _parse(text: str) -> tuple[float, float] | None:
    if not isinstance(text, str):
        raise TypeError('text must be a string')
    text = _normalize(text)
    if not (CONTEXT.search(text) and INTENT.search(text) and TIMING.search(text)):
        return None
    if EXCLUDE.search(text) or RANGE.search(text):
        return None
    if text.count('?') > 1 or re.search(r';|\n|\b(?:also|and\s+(?:how|what|why|explain|tell))\b', text):
        return None

    lengths = [float(match[1]) for match in LENGTH.finditer(text)]
    speeds = [float(match[1]) for match in SPEED.finditer(text)]
    if len(lengths) != 1 or len(speeds) != 1:
        return None
    length, speed = lengths[0], speeds[0]
    if not (math.isfinite(length) and math.isfinite(speed) and length > 0 and speed > 0):
        return None
    return length, speed


def answer_following_distance(text: str) -> FollowingDistanceResult:
    """Calculate the CDL following-gap rule, not physical stopping distance."""
    parsed = _parse(text)
    if parsed is None:
        return FollowingDistanceResult(False, 'not_applicable')

    length, speed = parsed
    extra = int(speed > 40 and not math.isclose(speed, 40, abs_tol=1e-9, rel_tol=0))
    seconds = length / 10 + extra
    formula = f'{length:g} / 10 + {extra} = {seconds:g} seconds'
    answer = (
        f'Using the CDL following-gap rule, allow at least {seconds:g} seconds '
        f'in good conditions, assuming {length:g} feet is the total vehicle '
        f'or combination length. Calculation: {formula}. Allow one second '
        'per 10 feet, plus one additional second when speed is over 40 mph. '
        'This is following time, not the physical time needed to stop. '
        'Increase the gap when weather, traffic, or road conditions require it. '
        'To calculate it yourself, divide the total combination length in feet '
        'by 10, then add 1 second if speed is over 40 mph.'
    )
    return FollowingDistanceResult(True, 'calculated', answer, length, speed, seconds, formula)