"""Fixed retest evidence for replacing one original tick of a sealed recording.

The payload keeps only the detector behaviour needed to answer any rational
replacement deviation: the entry amount and alarms accumulated before the
replaced tick, plus the affine response of every later tick.  Per-tick
deviations remain in the station ledger.
"""
from dataclasses import dataclass
from fractions import Fraction

from detector.intervals import RationalInterval
from detector.response import ResponsePiece, SegmentResponse, validate_response
from detector.step import check_entry_amount
from plans.revisions import PlanRevision, check_identifier
from plans.thresholds import parse_rational, parse_threshold
from recording.sequence import TickCoverage


@dataclass(frozen=True)
class RetestBinding:
    roll_id: str
    plan: PlanRevision
    recording_id: str
    coverage: TickCoverage
    tick: int

    def __post_init__(self):
        check_identifier(self.roll_id, 'roll ID')
        check_identifier(self.recording_id, 'recording ID')
        if not isinstance(self.plan, PlanRevision):
            raise ValueError('retest binding requires a fixed plan revision')
        if not isinstance(self.coverage, TickCoverage):
            raise ValueError('retest binding requires original tick coverage')
        if type(self.tick) is not int:
            raise ValueError('replacement tick must be an integer original tick')
        if not self.coverage.contains(self.tick):
            raise ValueError('replacement tick lies outside retest coverage')


@dataclass(frozen=True)
class RetestBehaviour:
    """Behaviour from the same recording around the replaced original tick."""

    entry: Fraction
    prefix_alarms: tuple[int, ...]
    suffix: SegmentResponse

    def __post_init__(self):
        object.__setattr__(self, 'entry', parse_rational(self.entry))
        object.__setattr__(self, 'prefix_alarms', tuple(self.prefix_alarms))
        if not isinstance(self.suffix, SegmentResponse):
            raise ValueError('retest suffix must be a fixed detector response')
        if any(type(number) is not int for number in self.prefix_alarms):
            raise ValueError('retest prefix alarms must be integer original ticks')


@dataclass(frozen=True)
class RetestEvidence:
    evidence_id: str
    binding: RetestBinding
    behaviour: RetestBehaviour


def identity_response(threshold: object) -> SegmentResponse:
    """Response for an empty suffix: the amount leaves the roll unchanged."""
    limit = parse_threshold(threshold)
    return SegmentResponse(limit, (ResponsePiece(RationalInterval(0, limit), 1, 0, ()),))


def validate_retest(evidence: RetestEvidence) -> None:
    check_identifier(evidence.evidence_id, 'evidence ID')
    binding = evidence.binding
    behaviour = evidence.behaviour
    if not isinstance(binding, RetestBinding):
        raise ValueError('retest evidence requires a fixed binding')
    if not isinstance(behaviour, RetestBehaviour):
        raise ValueError('retest evidence requires fixed behaviour')
    threshold = binding.plan.threshold
    check_entry_amount(threshold, behaviour.entry)
    suffix = behaviour.suffix
    if suffix.threshold != threshold:
        raise ValueError('retest suffix threshold differs from the fixed plan')
    validate_response(suffix)
    tick = binding.tick
    for number in behaviour.prefix_alarms:
        if not binding.coverage.contains(number) or number >= tick:
            raise ValueError('retest prefix alarm lies outside the ticks before the replacement')
    for left, right in zip(behaviour.prefix_alarms, behaviour.prefix_alarms[1:]):
        if left >= right:
            raise ValueError('retest prefix alarms must increase strictly')
    for piece in suffix.pieces:
        for number in piece.alarms:
            if not binding.coverage.contains(number) or number <= tick:
                raise ValueError('retest suffix alarm lies outside the ticks after the replacement')
