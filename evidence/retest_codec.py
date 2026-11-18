"""UTF-8 JSON codec for fixed retest evidence.

Retest payloads use format version ``2`` and a ``kind`` marker so that they can
never be confused with an ordinary V1 segment payload.  They are independently
decodable and validated without the station ledger.
"""
import json

from detector.intervals import RationalInterval
from detector.response import ResponsePiece, SegmentResponse
from evidence.retest import RetestBehaviour, RetestBinding, RetestEvidence, validate_retest
from plans.revisions import revision_from_payload, revision_payload
from plans.thresholds import decode_rational, encode_rational
from recording.sequence import TickCoverage

RETEST_VERSION = 2
RETEST_KIND = 'retest'


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate retest field {key}')
        result[key] = value
    return result


def _fields(value, names, label):
    if type(value) is not dict or value.keys() != names:
        raise ValueError(f'invalid {label} fields')
    return value


def _array(value, label):
    if type(value) is not list:
        raise ValueError(f'{label} must be an array')
    return value


def encode_piece(piece: ResponsePiece) -> dict:
    return {
        'start': encode_rational(piece.interval.start),
        'stop': encode_rational(piece.interval.stop),
        'slope': piece.slope,
        'intercept': encode_rational(piece.intercept),
        'alarms': list(piece.alarms),
    }


def decode_piece(value) -> ResponsePiece:
    piece = _fields(value, {'start', 'stop', 'slope', 'intercept', 'alarms'}, 'response piece')
    interval = RationalInterval(decode_rational(piece['start']), decode_rational(piece['stop']))
    return ResponsePiece(interval, piece['slope'], decode_rational(piece['intercept']),
                         tuple(_array(piece['alarms'], 'piece alarms')))


def encode_suffix(response: SegmentResponse) -> dict:
    return {
        'threshold': encode_rational(response.threshold),
        'pieces': [encode_piece(piece) for piece in response.pieces],
    }


def decode_suffix(value) -> SegmentResponse:
    data = _fields(value, {'threshold', 'pieces'}, 'retest suffix')
    return SegmentResponse(decode_rational(data['threshold']), tuple(
        decode_piece(piece) for piece in _array(data['pieces'], 'suffix pieces')
    ))


def encode_retest(evidence: RetestEvidence) -> bytes:
    validate_retest(evidence)
    binding = evidence.binding
    body = {
        'version': RETEST_VERSION,
        'kind': RETEST_KIND,
        'evidence_id': evidence.evidence_id,
        'binding': {
            'roll_id': binding.roll_id,
            'plan': revision_payload(binding.plan),
            'recording_id': binding.recording_id,
            'coverage': [binding.coverage.first, binding.coverage.last],
            'tick': binding.tick,
        },
        'behaviour': {
            'entry': encode_rational(evidence.behaviour.entry),
            'prefix_alarms': list(evidence.behaviour.prefix_alarms),
            'suffix': encode_suffix(evidence.behaviour.suffix),
        },
    }
    return (json.dumps(body, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')


def decode_retest(payload: bytes) -> RetestEvidence:
    if type(payload) is not bytes:
        raise ValueError('retest payload must be bytes')
    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('retest payload must be UTF-8 text') from exc
    body = _fields(json.loads(text, object_pairs_hook=_unique_fields),
                   {'version', 'kind', 'evidence_id', 'binding', 'behaviour'}, 'retest')
    if type(body['version']) is not int or body['version'] != RETEST_VERSION:
        raise ValueError('unsupported retest payload version')
    if body['kind'] != RETEST_KIND:
        raise ValueError('unsupported retest payload kind')
    binding_data = _fields(body['binding'],
                           {'roll_id', 'plan', 'recording_id', 'coverage', 'tick'}, 'retest binding')
    bounds = _array(binding_data['coverage'], 'retest coverage')
    if len(bounds) != 2:
        raise ValueError('retest coverage requires two original ticks')
    binding = RetestBinding(binding_data['roll_id'],
                            revision_from_payload(binding_data['plan']),
                            binding_data['recording_id'],
                            TickCoverage(*bounds),
                            binding_data['tick'])
    behaviour_data = _fields(body['behaviour'], {'entry', 'prefix_alarms', 'suffix'}, 'retest behaviour')
    behaviour = RetestBehaviour(decode_rational(behaviour_data['entry']),
                                tuple(_array(behaviour_data['prefix_alarms'],
                                             'retest prefix alarms')),
                                decode_suffix(behaviour_data['suffix']))
    evidence = RetestEvidence(body['evidence_id'], binding, behaviour)
    validate_retest(evidence)
    return evidence
