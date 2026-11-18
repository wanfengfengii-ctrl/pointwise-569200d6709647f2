from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction

from detector.intervals import RationalInterval, check_partition, partition_at
from detector.step import advance_tick, check_entry_amount
from detector.trace import TraceResult
from plans.thresholds import parse_rational, parse_threshold
from recording.ingest import OriginalTick


@dataclass(frozen=True)
class ResponsePiece:
    interval: RationalInterval
    slope: int
    intercept: Fraction
    alarms: tuple[int, ...]

    def __post_init__(self):
        if type(self.slope) is not int or self.slope not in (0, 1):
            raise ValueError('response slope must equal zero or one')
        object.__setattr__(self, 'intercept', parse_rational(self.intercept))
        object.__setattr__(self, 'alarms', tuple(self.alarms))

    def exit_amount(self, entry: Fraction) -> Fraction:
        return self.slope * entry + self.intercept


@dataclass(frozen=True)
class SegmentResponse:
    threshold: Fraction
    pieces: tuple[ResponsePiece, ...]

    def __post_init__(self):
        object.__setattr__(self, 'threshold', parse_threshold(self.threshold))
        object.__setattr__(self, 'pieces', tuple(self.pieces))


def _advance_piece(piece: ResponsePiece, tick: OriginalTick, threshold: Fraction):
    shifted = piece.intercept + tick.deviation
    if piece.slope:
        intervals = partition_at(piece.interval, (-shifted, threshold - shifted))
    else:
        intervals = (piece.interval,)
    # Each lower bound belongs to its interval. A threshold hit at that bound resets it.
    for interval in intervals:
        previous = piece.exit_amount(interval.start)
        step = advance_tick(threshold, previous, tick.deviation)
        if step.alarm:
            yield ResponsePiece(interval, 0, step.amount, (*piece.alarms, tick.number))
        elif piece.slope and previous + tick.deviation >= 0:
            yield ResponsePiece(interval, 1, shifted, piece.alarms)
        else:
            yield ResponsePiece(interval, 0, step.amount, piece.alarms)


def build_response(ticks: Iterable[OriginalTick], threshold: object) -> SegmentResponse:
    limit = parse_threshold(threshold)
    pieces = (ResponsePiece(RationalInterval(0, limit), 1, Fraction(0), ()),)
    previous = None
    for tick in ticks:
        if previous is not None and tick.number != previous + 1:
            raise ValueError('response requires consecutive original ticks')
        pieces = tuple(next_piece for piece in pieces for next_piece in _advance_piece(piece, tick, limit))
        previous = tick.number
    if previous is None:
        raise ValueError('response requires at least one tick')
    response = SegmentResponse(limit, pieces)
    validate_response(response)
    return response


def validate_response(response: SegmentResponse) -> None:
    limit = response.threshold
    check_partition((piece.interval for piece in response.pieces), limit)
    for piece in response.pieces:
        low = piece.exit_amount(piece.interval.start)
        high_limit = piece.exit_amount(piece.interval.stop)
        if not 0 <= low < limit or high_limit > limit:
            raise ValueError('response exit amount lies outside [0, threshold)')
        if any(type(number) is not int for number in piece.alarms):
            raise ValueError('response alarms must be integer original ticks')
        if any(left >= right for left, right in zip(piece.alarms, piece.alarms[1:])):
            raise ValueError('response alarms must increase strictly')


def evaluate_response(response: SegmentResponse, entry: object) -> TraceResult:
    validate_response(response)
    amount = check_entry_amount(response.threshold, entry)
    for piece in response.pieces:
        if piece.interval.contains(amount):
            return TraceResult(piece.alarms, piece.exit_amount(amount))
    raise ValueError('response does not cover the requested entry amount')
