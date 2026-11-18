import json
import sqlite3
from contextlib import closing
from fractions import Fraction

from archive.commit import ArchiveStore
from archive.read import read_retest
from detector.trace import evaluate_ticks
from evidence.codec import decode_segment
from evidence.export import export_retest, export_segment
from evidence.publish import publish_retest
from evidence.retest_codec import decode_retest
from recording.ingest import OriginalTick
from recording.sequence import TickCoverage
from reports.issue import issue_retest
from retest_examples import DEVIATIONS
from tests.support import ProjectCase, record_count


def table_count(path, table):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]


def brute_force(first, deviations, tick, candidate, threshold):
    pairs = [(number, candidate if number == tick else deviation)
             for number, deviation in enumerate(deviations, first)]
    result = evaluate_ticks([OriginalTick(number, value) for number, value in pairs], threshold)
    return result.alarms, result.amount


def shown(pieces):
    return [(piece.lo, piece.hi, piece.lo_closed, piece.hi_closed, piece.alarms)
            for piece in pieces]


class RetestStationCase(ProjectCase):
    def prepare(self, recording_id, deviations, evidence_id, tick):
        recording = self.station.seal(recording_id, 1, deviations)
        self.station.publish(recording_id, evidence_id + '-segment', 1, 1)
        publish_retest(self.station.recordings, self.station.segments, recording_id,
                       evidence_id, tick)
        target = self.transfer / f'{evidence_id}.retest'
        payload = export_retest(self.station.segments, evidence_id, target)
        return recording, payload

    def issue(self, recording_id, coverage, tick, payload, *args):
        return issue_retest(self.archive, self.station.assignment, coverage, recording_id,
                            tick, payload, *args)


class RetestWorkedTests(RetestStationCase):
    def test_worked_table(self):
        cases = (
            ('short-a', DEVIATIONS['short-a'], 1, (3, 3), (2, 2), (), (), None),
            ('short-b', DEVIATIONS['short-b'], 1, (3, 3), (2, 2), (), (), None),
        )
        for recording_id, deviations, tick, replacement, final, required, forbidden, _ in cases:
            with self.subTest(recording_id=recording_id):
                recording, payload = self.prepare(recording_id, deviations, recording_id, tick)
                issued = self.issue(recording_id, recording.coverage, tick, payload,
                                    replacement, required, forbidden, final)
                if recording_id == 'short-a':
                    self.assertEqual(len(issued.result.pieces), 1)
                    piece = issued.result.pieces[0]
                    self.assertEqual((piece.lo, piece.hi), (Fraction(3), Fraction(3)))
                    self.assertEqual(piece.alarms, ())
                    self.assertEqual(piece.amount_at(Fraction(3)), Fraction(2))
                else:
                    self.assertEqual(issued.result.pieces, ())

    def test_middle_none(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'middle-piece', 2)
        issued = self.issue('middle', recording.coverage, 2, payload, (-6, 3), (), (), (2, 2))
        self.assertEqual(shown(issued.result.pieces), [
            (Fraction(-6), Fraction(-4), True, True, ()),
            (Fraction(1), Fraction(3), True, True, (2,)),
        ])
        for piece in issued.result.pieces:
            self.assertEqual(piece.slope, 0)
            self.assertEqual(piece.intercept, Fraction(2))

    def test_middle_required_forbidden(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'middle-piece', 2)
        issued = self.issue('middle', recording.coverage, 2, payload, (-6, 3), (3,), (2,), (0, 0))
        self.assertEqual(shown(issued.result.pieces), [
            (Fraction(-1), Fraction(1), True, False, (3,)),
        ])
        self.assertEqual(issued.result.pieces[0].intercept, Fraction(0))

    def test_middle_affine(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'middle-piece', 2)
        issued = self.issue('middle', recording.coverage, 2, payload, (-6, 3), (), (),
                            (Fraction(5, 2), Fraction(7, 2)))
        self.assertEqual(shown(issued.result.pieces), [
            (Fraction(-7, 2), Fraction(-5, 2), True, True, ()),
        ])
        piece = issued.result.pieces[0]
        self.assertEqual(piece.slope, 1)
        self.assertEqual(piece.intercept, Fraction(6))
        self.assertEqual(piece.amount_at(Fraction(-3)), Fraction(3))

    def test_tail(self):
        recording, payload = self.prepare('tail', DEVIATIONS['tail'], 'tail-piece', 2)
        issued = self.issue('tail', recording.coverage, 2, payload, (0, 4), (), (), (3, 3))
        self.assertEqual(shown(issued.result.pieces), [
            (Fraction(3), Fraction(3), True, True, ()),
        ])
        self.assertEqual(issued.result.pieces[0].amount_at(Fraction(3)), Fraction(3))


class RetestEvidenceTests(RetestStationCase):
    def test_distinct_from_ordinary(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        body = json.loads(payload)
        self.assertEqual(body['version'], 2)
        self.assertEqual(body['kind'], 'retest')
        self.assertEqual(body['binding']['tick'], 2)
        self.assertEqual(set(body['binding']),
                         {'roll_id', 'plan', 'recording_id', 'coverage', 'tick'})
        self.assertEqual(set(body['behaviour']), {'entry', 'prefix_alarms', 'suffix'})
        ordinary = self.transfer / 'check-2-segment.json'
        export_segment(self.station.segments, 'check-2-segment', ordinary)
        with self.assertRaises(ValueError):
            decode_segment(payload)
        restored = decode_retest(payload)
        self.assertEqual(restored.binding.coverage, recording.coverage)
        self.assertEqual(restored.binding.tick, 2)
        self.assertEqual(restored.behaviour.entry, Fraction(4))
        self.assertEqual(restored.behaviour.prefix_alarms, ())
        self.assertEqual(decode_retest(payload), restored)

    def test_repeat_export_same_bytes(self):
        self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        first = export_retest(self.station.segments, 'check-2', self.transfer / 'a.json')
        second = export_retest(self.station.segments, 'check-2', self.transfer / 'b.json')
        self.assertEqual(first, second)
        self.assertEqual((self.transfer / 'a.json').read_bytes(), first)

    def test_unknown_and_conflicting(self):
        self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        with self.assertRaises(KeyError):
            export_retest(self.station.segments, 'absent', self.transfer / 'none.json')
        with self.assertRaises(KeyError):
            publish_retest(self.station.recordings, self.station.segments, 'absent', 'x', 2)
        with self.assertRaisesRegex(ValueError, 'conflicting fixed evidence ID'):
            publish_retest(self.station.recordings, self.station.segments, 'middle', 'check-2', 3)
        with self.assertRaisesRegex(ValueError, 'conflicting fixed evidence ID'):
            publish_retest(self.station.recordings, self.station.segments, 'middle',
                           'check-2-segment', 2)

    def test_illegal_tick(self):
        self.station.seal('middle', 1, DEVIATIONS['middle'])
        for tick in (0, 4, True, '2', 2.0):
            with self.subTest(tick=tick), self.assertRaises(ValueError):
                publish_retest(self.station.recordings, self.station.segments, 'middle',
                               f'bad-{tick!r}', tick)
        self.assertEqual(record_count(self.station.path, 'published_retests'), 0)

    def test_publish_rollback(self):
        self.station.seal('middle', 1, DEVIATIONS['middle'])
        self.station.publish('middle', 'ordinary', 1, 1)
        with closing(sqlite3.connect(self.station.path)) as db, db:
            db.execute('''
                CREATE TRIGGER reject_retest AFTER INSERT ON published_retests
                WHEN NEW.evidence_id = 'rejected'
                BEGIN SELECT RAISE(ABORT, 'retest write rejected'); END
            ''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'retest write rejected'):
            publish_retest(self.station.recordings, self.station.segments, 'middle',
                           'rejected', 2)
        self.assertEqual(record_count(self.station.path, 'published_retests'), 0)
        with self.assertRaises(KeyError):
            self.station.segments.get_retest('rejected')
        self.assertEqual(self.station.segments.get_published('ordinary').binding.coverage,
                         TickCoverage(1, 1))


class RetestAcceptTests(RetestStationCase):
    def test_payload_must_match(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        other = self.new_station('other', roll_id='roll-B')
        other.seal('middle', 1, DEVIATIONS['middle'])
        publish_retest(other.recordings, other.segments, 'middle', 'other-piece', 2)
        other_payload = export_retest(other.segments, 'other-piece', self.transfer / 'other.json')
        other_plan = self.new_station('other-plan', revision='r2', threshold=6)
        other_plan.seal('middle', 1, DEVIATIONS['middle'])
        publish_retest(other_plan.recordings, other_plan.segments, 'middle', 'plan-piece', 2)
        plan_payload = export_retest(other_plan.segments, 'plan-piece',
                                     self.transfer / 'plan.json')
        cases = (
            (self.station.assignment, recording.coverage, 'middle', 2, other_payload, 'roll mismatch'),
            (self.station.assignment, recording.coverage, 'middle', 2, plan_payload,
             'plan revision mismatch'),
            (self.station.assignment, recording.coverage, 'other-rec', 2, payload, 'recording mismatch'),
            (self.station.assignment, recording.coverage, 'middle', 3, payload, 'tick mismatch'),
            (self.station.assignment, TickCoverage(1, 2), 'middle', 2, payload, 'coverage mismatch'),
        )
        for assignment, coverage, recording_id, tick, blob, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    issue_retest(self.archive, assignment, coverage, recording_id, tick,
                                 blob, (-6, 3), (), (), (2, 2))
                self.assertEqual(self.archive_counts(), (0, 0))
                self.assertEqual(table_count(self.archive.path, 'retest_reports'), 0)
                self.assertEqual(table_count(self.archive.path, 'retest_reports'), 0)

    def test_mixed_source_piece(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        other = self.new_station('other', roll_id='roll-Z')
        other.seal('middle', 1, DEVIATIONS['middle'])
        publish_retest(other.recordings, other.segments, 'middle', 'foreign', 2)
        foreign = export_retest(other.segments, 'foreign', self.transfer / 'foreign.json')
        with self.assertRaisesRegex(ValueError, 'roll mismatch'):
            issue_retest(self.archive, self.station.assignment, recording.coverage, 'middle',
                         2, foreign, (-6, 3), (), (), (2, 2))
        self.assertEqual(table_count(self.archive.path, 'retest_reports'), 0)

    def test_same_roll_other_recording(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        self.station.seal('sibling', 1, DEVIATIONS['middle'])
        publish_retest(self.station.recordings, self.station.segments, 'sibling', 'sibling-piece', 2)
        sibling = export_retest(self.station.segments, 'sibling-piece',
                                self.transfer / 'sibling.json')
        with self.assertRaisesRegex(ValueError, 'recording mismatch'):
            issue_retest(self.archive, self.station.assignment, recording.coverage, 'middle',
                         2, sibling, (-6, 3), (), (), (2, 2))
        self.assertEqual(table_count(self.archive.path, 'retest_reports'), 0)
        self.assertEqual(table_count(self.archive.path, 'retest_receipts'), 0)

    def test_bad_requests(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        coverage = recording.coverage
        bad = (
            ((-6, 3), (), (), (3, -6)),
            ((-6,), (), (), (2, 2)),
            ((-6, 3), (3,), (3,), (2, 2)),
            ((-6, 3), (9,), (), (2, 2)),
            ((-6, 3), (True,), (), (2, 2)),
            ((-6, 3), (), (2.0,), (2, 2)),
            ((-6, 3), (), (), ('1/0', 2)),
        )
        for replacement, required, forbidden, final in bad:
            with self.subTest(replacement=replacement, required=required,
                              forbidden=forbidden, final=final):
                with self.assertRaises(ValueError):
                    issue_retest(self.archive, self.station.assignment, coverage, 'middle', 2,
                                 payload, replacement, required, forbidden, final)
                self.assertEqual(table_count(self.archive.path, 'retest_reports'), 0)
                self.assertEqual(table_count(self.archive.path, 'retest_receipts'), 0)

    def test_empty_set_issues(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        issued = self.issue('middle', recording.coverage, 2, payload, (100, 200), (3,), (), (0, 0))
        self.assertEqual(issued.result.pieces, ())
        lookup = read_retest(self.archive, issued.report_id)
        self.assertTrue(lookup.found)
        self.assertEqual(lookup.report, issued)


class RetestArchiveTests(RetestStationCase):
    def test_request_and_receipt_commit(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        issued = self.issue('middle', recording.coverage, 2, payload,
                            (-6, 3), (3,), (2,), ('5/2', '7/2'))
        result = issued.result
        self.assertEqual(result.recording_id, 'middle')
        self.assertEqual(result.tick, 2)
        self.assertEqual(result.replacement, (Fraction(-6), Fraction(3)))
        self.assertEqual(result.final_amount, (Fraction(5, 2), Fraction(7, 2)))
        self.assertEqual(result.required_alarms, frozenset({3}))
        self.assertEqual(result.forbidden_alarms, frozenset({2}))
        self.assertEqual(result.reference.evidence_id, 'check-2')
        from hashlib import sha256
        self.assertEqual(result.reference.digest, sha256(payload).hexdigest())
        self.assertEqual(issued.receipt.report_id, issued.report_id)

    def test_reopen_same_result(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        issued = self.issue('middle', recording.coverage, 2, payload, (-6, 3), (), (), (2, 2))
        reopened = ArchiveStore(self.archive.path)
        reopened.initialize()
        lookup = read_retest(reopened, issued.report_id)
        self.assertTrue(lookup.found)
        self.assertEqual(lookup.report, issued)
        self.assertEqual(lookup.requested_id, issued.report_id)
        self.assertFalse(read_retest(reopened, 'absent-report').found)
        self.assertIsNone(read_retest(reopened, 'absent-report').report)

    def test_commit_failure_leaves_nothing(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        existing = self.issue('middle', recording.coverage, 2, payload, (-6, 3), (), (), (2, 2))
        with closing(sqlite3.connect(self.archive.path)) as db, db:
            db.execute('''
                CREATE TRIGGER fail_retest_receipt BEFORE INSERT ON retest_receipts
                WHEN EXISTS (SELECT 1 FROM retest_reports WHERE report_id = NEW.report_id)
                BEGIN SELECT RAISE(ABORT, 'retest receipt failed'); END
            ''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'retest receipt failed'):
            self.issue('middle', recording.coverage, 2, payload, (-6, 3), (), (), (2, 2))
        self.archive = ArchiveStore(self.archive.path)
        self.archive.initialize()
        self.assertEqual(table_count(self.archive.path, 'retest_reports'), 1)
        self.assertEqual(table_count(self.archive.path, 'retest_receipts'), 1)
        self.assertEqual(read_retest(self.archive, existing.report_id).report, existing)
        with closing(sqlite3.connect(self.archive.path)) as db, db:
            db.execute('DROP TRIGGER fail_retest_receipt')
        recovered = self.issue('middle', recording.coverage, 2, payload, (-6, 3), (), (), (2, 2))
        self.assertEqual(table_count(self.archive.path, 'retest_reports'), 2)
        self.assertEqual(read_retest(self.archive, recovered.report_id).report, recovered)

    def test_isolated_from_v1_tables(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        whole = self.transfer / 'whole.json'
        export_segment(self.station.segments, 'check-2-segment', whole)
        issued = self.issue('middle', recording.coverage, 2, payload, (-6, 3), (), (), (2, 2))
        self.assertEqual(table_count(self.archive.path, 'reports'), 0)
        self.assertEqual(table_count(self.archive.path, 'receipts'), 0)
        self.assertFalse(read_retest(self.archive, 'ordinary').found)


class RetestOracleTests(RetestStationCase):
    def test_head_and_single(self):
        single = self.station.seal('single', 7, (2,))
        self.station.seal('longer', 1, (4, 2, -1))
        publish_retest(self.station.recordings, self.station.segments, 'single', 'single-piece', 7)
        publish_retest(self.station.recordings, self.station.segments, 'longer', 'head-piece', 1)
        single_payload = export_retest(self.station.segments, 'single-piece',
                                       self.transfer / 'single.json')
        head_payload = export_retest(self.station.segments, 'head-piece',
                                     self.transfer / 'head.json')
        for recording_id, coverage, tick, payload, deviations, first in (
                ('single', single.coverage, 7, single_payload, (2,), 7),
                ('longer', TickCoverage(1, 3), 1, head_payload, (4, 2, -1), 1)):
            for candidate in (Fraction(-6), Fraction(-1, 3), Fraction(0), Fraction(1),
                              Fraction(9, 2), Fraction(6)):
                alarms, amount = brute_force(first, deviations, tick, candidate, 5)
                issued = self.issue(recording_id, coverage, tick, payload,
                                    (Fraction(-6), Fraction(6)), (), (),
                                    (Fraction(0), Fraction(5)))
                matching = [piece for piece in issued.result.pieces
                            if piece.lo <= candidate <= piece.hi
                            and not (candidate == piece.lo and not piece.lo_closed)
                            and not (candidate == piece.hi and not piece.hi_closed)]
                expected_ok = Fraction(0) <= amount < Fraction(5) and 5 > amount
                with self.subTest(recording_id=recording_id, candidate=candidate):
                    self.assertEqual(bool(matching), expected_ok,
                                     msg=f'{issued.result.pieces!r}')

    def test_rule_matches_oracle(self):
        recording, payload = self.prepare('middle', DEVIATIONS['middle'], 'check-2', 2)
        issued = self.issue('middle', recording.coverage, 2, payload, (-6, 3), (), (), (2, 2))
        for candidate in (Fraction(-6), Fraction(-5), Fraction(-4), Fraction(0),
                          Fraction(1), Fraction(2), Fraction(3)):
            alarms, amount = brute_force(1, DEVIATIONS['middle'], 2, candidate, 5)
            hits = [piece for piece in issued.result.pieces
                    if piece.lo < candidate < piece.hi
                    or (candidate == piece.lo and piece.lo_closed)
                    or (candidate == piece.hi and piece.hi_closed)]
            feasible = amount == 2
            with self.subTest(candidate=candidate):
                self.assertEqual(bool(hits), feasible)
                if hits:
                    self.assertEqual(hits[0].alarms, alarms)
                    self.assertEqual(hits[0].amount_at(candidate), amount)
