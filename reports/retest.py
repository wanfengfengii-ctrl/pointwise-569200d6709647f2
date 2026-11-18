"""Exact rational replacement-range issuing for a fixed retest piece."""
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256

from detector.response import SegmentResponse
from evidence.retest import RetestEvidence, validate_retest
from evidence.retest_codec import decode_retest
from plans.assignments import RollAssignment
from plans.revisions import check_identifier, revision_from_payload, revision_payload
from plans.thresholds import decode_rational, encode_rational, parse_rational
from recording.sequence import TickCoverage


@dataclass(frozen=True)
class RetestPiece:
    lo: Fraction
    hi: Fraction
    lo_closed: bool
    hi_closed: bool
    alarms: tuple[int, ...]
    slope: int | Fraction
    intercept: Fraction

    def __post_init__(self):
        lo = parse_rational(self.lo)
        hi = parse_rational(self.hi)
        if lo > hi:
            raise ValueError('retest piece has an inverted interval')
        if type(self.lo_closed) is not bool or type(self.hi_closed) is not bool:
            raise ValueError('retest piece closed flags must be boolean')
        if lo == hi and not (self.lo_closed and self.hi_closed):
            raise ValueError('a single-point retest piece must be closed on both ends')
        if type(self.slope) is bool or not isinstance(self.slope, (int, Fraction)):
            raise ValueError('retest piece slope must be an exact number')
        alarms = tuple(self.alarms)
        if any(type(number) is not int for number in alarms):
            raise ValueError('retest piece alarms must be integer original ticks')
        if any(left >= right for left, right in zip(alarms, alarms[1:])):
            raise ValueError('retest piece alarms must increase strictly')
        object.__setattr__(self, 'lo', lo)
        object.__setattr__(self, 'hi', hi)
        object.__setattr__(self, 'alarms', alarms)
        object.__setattr__(self, 'intercept', parse_rational(self.intercept))

    def amount_at(self, value: Fraction) -> Fraction:
        return self.slope * parse_rational(value) + self.intercept


@dataclass(frozen=True)
class RetestReference:
    evidence_id: str
    digest: str

    def __post_init__(self):
        check_identifier(self.evidence_id, 'evidence ID')
        if not isinstance(self.digest, str) or len(self.digest) != 64:
            raise ValueError('invalid retest content digest')
        if any(character not in '0123456789abcdef' for character in self.digest):
            raise ValueError('invalid retest content digest')


@dataclass(frozen=True)
class RetestResult:
    assignment: RollAssignment
    coverage: TickCoverage
    recording_id: str
    tick: int
    replacement: tuple[Fraction, Fraction]
    required_alarms: frozenset[int]
    forbidden_alarms: frozenset[int]
    final_amount: tuple[Fraction, Fraction]
    reference: RetestReference
    pieces: tuple[RetestPiece, ...]

    def __post_init__(self):
        check_identifier(self.recording_id, 'recording ID')
        if type(self.tick) is not int or self.tick is True:
            raise ValueError('replacement tick must be an integer original tick')
        if not self.coverage.contains(self.tick):
            raise ValueError('replacement tick lies outside retest coverage')
        for value in (self.replacement, self.final_amount):
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ValueError('retest result interval must have two rational endpoints')
            if parse_rational(value[0]) > parse_rational(value[1]):
                raise ValueError('retest result interval must have ordered rational endpoints')
        object.__setattr__(self, 'replacement',
                           (parse_rational(self.replacement[0]), parse_rational(self.replacement[1])))
        object.__setattr__(self, 'final_amount',
                           (parse_rational(self.final_amount[0]), parse_rational(self.final_amount[1])))
        object.__setattr__(self, 'required_alarms', frozenset(self.required_alarms))
        object.__setattr__(self, 'forbidden_alarms', frozenset(self.forbidden_alarms))
        pieces = tuple(self.pieces)
        if any(not isinstance(piece, RetestPiece) for piece in pieces):
            raise ValueError('retest result pieces must be fixed retest pieces')
        previous = None
        for piece in pieces:
            if piece.lo < self.replacement[0] or piece.hi > self.replacement[1]:
                raise ValueError('retest piece lies outside the replacement interval')
            if previous is not None:
                if piece.lo < previous.hi:
                    raise ValueError('retest pieces must be ordered and disjoint')
                if piece.lo == previous.hi and previous.hi_closed and piece.lo_closed:
                    raise ValueError('retest pieces must be ordered and disjoint')
            previous = piece
        object.__setattr__(self, 'pieces', pieces)


def _suffix_piece(suffix: SegmentResponse, entry: Fraction):
    for piece in suffix.pieces:
        if piece.interval.contains(entry):
            return piece
    raise ValueError('retest suffix does not cover the entry amount')


def _state_at(evidence: RetestEvidence, value: Fraction, limit: Fraction):
    """Return (alarms, slope, intercept, final) for a candidate deviation."""
    behaviour = evidence.behaviour
    tick = evidence.binding.tick
    entry = behaviour.entry
    if value <= -entry:
        piece = _suffix_piece(behaviour.suffix, Fraction(0))
        return ((*behaviour.prefix_alarms, *piece.alarms), 0, piece.intercept,
                piece.intercept)
    if value >= limit - entry:
        piece = _suffix_piece(behaviour.suffix, Fraction(0))
        alarms = (*behaviour.prefix_alarms, tick, *piece.alarms)
        return (alarms, 0, piece.intercept, piece.intercept)
    position = entry + value
    piece = _suffix_piece(behaviour.suffix, position)
    slope = piece.slope
    intercept = slope * entry + piece.intercept
    return ((*behaviour.prefix_alarms, *piece.alarms), slope, intercept,
            slope * value + intercept)


def _breakpoints(evidence: RetestEvidence, limit: Fraction) -> set[Fraction]:
    entry = evidence.behaviour.entry
    points = {-entry, limit - entry}
    for piece in evidence.behaviour.suffix.pieces:
        points.add(piece.interval.start - entry)
        points.add(piece.interval.stop - entry)
    return points


def _alarms_ok(alarms, required, forbidden) -> bool:
    observed = set(alarms)
    return required <= observed and not (forbidden & observed)


def _clip(lo: Fraction, hi: Fraction, low: Fraction, high: Fraction, slope, intercept):
    """Intersect an open cell with the affine final-amount constraint."""
    if slope == 0:
        if not low <= intercept <= high:
            return None
        return lo, hi, False, False
    left = low - intercept
    right = high - intercept
    start = max(lo, left)
    stop = min(hi, right)
    if start > stop:
        return None
    start_closed = start > lo
    stop_closed = stop < hi
    if start == stop and not (start_closed and stop_closed):
        return None
    return start, stop, start_closed, stop_closed


def solve_pieces(evidence: RetestEvidence, replacement, required, forbidden,
                 final_amount) -> tuple[RetestPiece, ...]:
    limit = evidence.binding.plan.threshold
    low_x, high_x = replacement
    low_a, high_a = final_amount
    points = _breakpoints(evidence, limit)
    positions = sorted({low_x, high_x, *(point for point in points if low_x <= point <= high_x)})
    segments: list[RetestPiece] = []
    for position in positions:
        alarms, slope, intercept, final = _state_at(evidence, position, limit)
        if _alarms_ok(alarms, required, forbidden) and low_a <= final <= high_a:
            segments.append(RetestPiece(position, position, True, True, alarms, slope, intercept))
    for left, right in zip(positions, positions[1:]):
        sample = (left + right) / 2
        alarms, slope, intercept, _ = _state_at(evidence, sample, limit)
        if not _alarms_ok(alarms, required, forbidden):
            continue
        clipped = _clip(left, right, low_a, high_a, slope, intercept)
        if clipped is None:
            continue
        start, stop, start_closed, stop_closed = clipped
        segments.append(RetestPiece(start, stop, start_closed, stop_closed, alarms,
                                    slope, intercept))
    segments.sort(key=lambda piece: (piece.lo, not piece.lo_closed))
    merged: list[RetestPiece] = []
    for piece in segments:
        if merged:
            previous = merged[-1]
            if (previous.hi == piece.lo and (previous.hi_closed or piece.lo_closed)
                    and previous.alarms == piece.alarms
                    and previous.slope == piece.slope
                    and previous.intercept == piece.intercept):
                merged[-1] = RetestPiece(previous.lo, piece.hi, previous.lo_closed,
                                         piece.hi_closed, piece.alarms, piece.slope,
                                         piece.intercept)
                continue
        merged.append(piece)
    return tuple(merged)


def _check_alarm_items(items, coverage: TickCoverage, label: str) -> frozenset[int]:
    if isinstance(items, (set, frozenset)):
        values = list(items)
    elif isinstance(items, (list, tuple)):
        values = list(items)
    else:
        raise ValueError(f'{label} must be an iterable of original ticks')
    for value in values:
        if type(value) is not int or not coverage.contains(value):
            raise ValueError(f'{label} contains an invalid original tick')
    return frozenset(values)


def _interval(value, label: str) -> tuple[Fraction, Fraction]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f'{label} must be a two-ended rational interval')
    low = parse_rational(value[0])
    high = parse_rational(value[1])
    if low > high:
        raise ValueError(f'{label} low end exceeds high end')
    return low, high


def accept_retest(assignment: RollAssignment, coverage: TickCoverage, recording_id: str,
                  tick: int, payload: bytes) -> RetestEvidence:
    if not isinstance(assignment, RollAssignment):
        raise ValueError('expected a fixed roll assignment')
    if not isinstance(coverage, TickCoverage):
        raise ValueError('expected a fixed tick coverage')
    check_identifier(recording_id, 'recording ID')
    if type(tick) is not int:
        raise ValueError('replacement tick must be an integer original tick')
    if not coverage.contains(tick):
        raise ValueError('replacement tick lies outside the fixed coverage')
    if type(payload) is not bytes:
        raise ValueError('retest payload must be bytes')
    evidence = decode_retest(payload)
    binding = evidence.binding
    if binding.roll_id != assignment.roll_id:
        raise ValueError('roll mismatch in retest evidence')
    if binding.plan != assignment.plan:
        raise ValueError('plan revision mismatch in retest evidence')
    if binding.recording_id != recording_id:
        raise ValueError('recording mismatch in retest evidence')
    if binding.coverage != coverage:
        raise ValueError('coverage mismatch in retest evidence')
    if binding.tick != tick:
        raise ValueError('replacement tick mismatch in retest evidence')
    validate_retest(evidence)
    return evidence


def build_result(assignment: RollAssignment, coverage: TickCoverage, recording_id: str,
                 tick: int, payload: bytes, replacement, required_alarms,
                 forbidden_alarms, final_amount) -> RetestResult:
    evidence = accept_retest(assignment, coverage, recording_id, tick, payload)
    low_x, high_x = _interval(replacement, 'replacement')
    low_a, high_a = _interval(final_amount, 'final amount')
    required = _check_alarm_items(required_alarms, coverage, 'required alarms')
    forbidden = _check_alarm_items(forbidden_alarms, coverage, 'forbidden alarms')
    if required & forbidden:
        raise ValueError('required and forbidden alarms must be disjoint')
    pieces = solve_pieces(evidence, (low_x, high_x), required, forbidden, (low_a, high_a))
    reference = RetestReference(evidence.evidence_id, sha256(payload).hexdigest())
    return RetestResult(assignment, coverage, recording_id, tick, (low_x, high_x),
                        required, forbidden, (low_a, high_a), reference, pieces)


def piece_payload(piece: RetestPiece) -> dict:
    return {
        'lo': encode_rational(piece.lo),
        'hi': encode_rational(piece.hi),
        'lo_closed': piece.lo_closed,
        'hi_closed': piece.hi_closed,
        'alarms': list(piece.alarms),
        'slope': encode_rational(piece.slope),
        'intercept': encode_rational(piece.intercept),
    }


def result_payload(result: RetestResult) -> dict:
    return {
        'roll_id': result.assignment.roll_id,
        'plan': revision_payload(result.assignment.plan),
        'coverage': [result.coverage.first, result.coverage.last],
        'recording_id': result.recording_id,
        'tick': result.tick,
        'replacement': [encode_rational(result.replacement[0]),
                        encode_rational(result.replacement[1])],
        'required_alarms': sorted(result.required_alarms),
        'forbidden_alarms': sorted(result.forbidden_alarms),
        'final_amount': [encode_rational(result.final_amount[0]),
                         encode_rational(result.final_amount[1])],
        'reference': {'evidence_id': result.reference.evidence_id,
                      'digest': result.reference.digest},
        'pieces': [piece_payload(piece) for piece in result.pieces],
    }


def result_from_payload(payload: object) -> RetestResult:
    names = {'roll_id', 'plan', 'coverage', 'recording_id', 'tick', 'replacement',
             'required_alarms', 'forbidden_alarms', 'final_amount', 'reference', 'pieces'}
    if type(payload) is not dict or payload.keys() != names:
        raise ValueError('invalid retest result payload')
    reference = payload['reference']
    if type(reference) is not dict or reference.keys() != {'evidence_id', 'digest'}:
        raise ValueError('invalid retest result reference')
    pieces = tuple(
        RetestPiece(decode_rational(item['lo']), decode_rational(item['hi']),
                    item['lo_closed'], item['hi_closed'], tuple(item['alarms']),
                    decode_rational(item['slope']), decode_rational(item['intercept']))
        for item in payload['pieces']
    )
    return RetestResult(
        RollAssignment(payload['roll_id'], revision_from_payload(payload['plan'])),
        TickCoverage(*payload['coverage']),
        payload['recording_id'],
        payload['tick'],
        (decode_rational(payload['replacement'][0]), decode_rational(payload['replacement'][1])),
        frozenset(payload['required_alarms']),
        frozenset(payload['forbidden_alarms']),
        (decode_rational(payload['final_amount'][0]), decode_rational(payload['final_amount'][1])),
        RetestReference(reference['evidence_id'], reference['digest']),
        pieces,
    )
