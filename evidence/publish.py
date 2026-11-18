import sqlite3
from contextlib import closing
from fractions import Fraction
from pathlib import Path

from detector.response import build_response
from detector.step import advance_tick
from evidence.binding import SegmentEvidence, make_binding
from evidence.codec import decode_segment, encode_segment
from evidence.retest import RetestBehaviour, RetestBinding, RetestEvidence, identity_response
from evidence.retest_codec import decode_retest, encode_retest
from plans.revisions import check_identifier
from recording.ledger import RecordingLedger
from recording.segments import select_ticks
from recording.sequence import TickCoverage


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                      (name,)).fetchone() is not None


def _present(db: sqlite3.Connection, table: str, evidence_id: str) -> bool:
    return _table_exists(db, table) and db.execute(
        f'SELECT 1 FROM {table} WHERE evidence_id = ?', (evidence_id,)).fetchone() is not None


class SegmentStore:
    def __init__(self, path: Path | str):
        self.path = path

    def initialize(self) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''
                CREATE TABLE IF NOT EXISTS published_segments (
                    evidence_id TEXT PRIMARY KEY NOT NULL, payload BLOB NOT NULL
                )
            ''')
            db.execute('''
                CREATE TABLE IF NOT EXISTS published_retests (
                    evidence_id TEXT PRIMARY KEY NOT NULL, payload BLOB NOT NULL
                )
            ''')

    def save_published(self, evidence: SegmentEvidence) -> SegmentEvidence:
        payload = encode_segment(evidence)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if _present(db, 'published_retests', evidence.evidence_id):
                raise ValueError('conflicting fixed evidence ID')
            row = db.execute('SELECT payload FROM published_segments WHERE evidence_id = ?',
                             (evidence.evidence_id,)).fetchone()
            if row is None:
                db.execute('INSERT INTO published_segments VALUES (?, ?)', (evidence.evidence_id, payload))
            elif row[0] != payload:
                raise ValueError('conflicting fixed evidence ID')
        return evidence

    def save_retest(self, evidence: RetestEvidence) -> RetestEvidence:
        payload = encode_retest(evidence)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if _present(db, 'published_segments', evidence.evidence_id):
                raise ValueError('conflicting fixed evidence ID')
            row = db.execute('SELECT payload FROM published_retests WHERE evidence_id = ?',
                             (evidence.evidence_id,)).fetchone()
            if row is None:
                db.execute('INSERT INTO published_retests VALUES (?, ?)',
                           (evidence.evidence_id, payload))
            elif row[0] != payload:
                raise ValueError('conflicting fixed evidence ID')
        return evidence

    def get_published(self, evidence_id: str) -> SegmentEvidence:
        check_identifier(evidence_id, 'evidence ID')
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT payload FROM published_segments WHERE evidence_id = ?',
                             (evidence_id,)).fetchone()
        if row is None:
            raise KeyError(evidence_id)
        evidence = decode_segment(row[0])
        if evidence.evidence_id != evidence_id:
            raise ValueError('fixed evidence ID differs from its store key')
        return evidence

    def get_retest(self, evidence_id: str) -> RetestEvidence:
        check_identifier(evidence_id, 'evidence ID')
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT payload FROM published_retests WHERE evidence_id = ?',
                             (evidence_id,)).fetchone()
        if row is None:
            raise KeyError(evidence_id)
        evidence = decode_retest(row[0])
        if evidence.evidence_id != evidence_id:
            raise ValueError('fixed evidence ID differs from its store key')
        return evidence


def publish_segment(recordings: RecordingLedger, store: SegmentStore, recording_id: str,
                    evidence_id: str, coverage: TickCoverage) -> SegmentEvidence:
    check_identifier(evidence_id, 'evidence ID')
    recording = recordings.get_sealed(recording_id)
    ticks = select_ticks(recording, coverage)
    binding = make_binding(recording, coverage)
    response = build_response(ticks, recording.assignment.plan.threshold)
    return store.save_published(SegmentEvidence(evidence_id, binding, response))


def build_retest_evidence(recording, evidence_id: str, tick: int) -> RetestEvidence:
    """Fix behaviour around one replaced original tick of a sealed recording."""
    if type(tick) is not int:
        raise ValueError('replacement tick must be an integer original tick')
    threshold = recording.assignment.plan.threshold
    amount = Fraction(0)
    alarms: list[int] = []
    entry = None
    suffix = ()
    for original in recording.ticks:
        if original.number == tick:
            entry = amount
            suffix = tuple(later for later in recording.ticks
                           if later.number > original.number)
            break
        step = advance_tick(threshold, amount, original.deviation)
        amount = step.amount
        if step.alarm:
            alarms.append(original.number)
    else:
        raise ValueError('replacement tick lies outside the sealed recording')
    if suffix:
        response = build_response(suffix, threshold)
    else:
        response = identity_response(threshold)
    binding = RetestBinding(recording.assignment.roll_id, recording.assignment.plan,
                            recording.recording_id, recording.coverage, tick)
    return RetestEvidence(evidence_id, binding,
                          RetestBehaviour(entry, tuple(alarms), response))


def publish_retest(recordings: RecordingLedger, store: SegmentStore, recording_id: str,
                   evidence_id: str, tick: int) -> RetestEvidence:
    check_identifier(evidence_id, 'evidence ID')
    recording = recordings.get_sealed(recording_id)
    evidence = build_retest_evidence(recording, evidence_id, tick)
    return store.save_retest(evidence)
