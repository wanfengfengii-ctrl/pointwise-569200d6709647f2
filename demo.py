import argparse
import json
import shutil
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.commit import ArchiveStore
from archive.export import export_report
from archive.read import read_report, read_retest
from detector.trace import preview
from evidence.export import export_segment, export_retest
from evidence.publish import SegmentStore, publish_retest, publish_segment
from plans.assignments import initialize_plan_store, register_plan
from plans.thresholds import encode_rational
from recording.ledger import RecordingLedger
from recording.segments import seal_recording
from recording.sequence import TickCoverage
from reports.issue import issue_report, issue_retest
from retest_examples import DEVIATIONS, WORKED


def result_values(result):
    return {'alarms': list(result.alarms), 'amount': encode_rational(result.amount)}


def run_demo(scenario):
    with TemporaryDirectory(prefix='surface-inspection-') as directory:
        root = Path(directory)
        station = root / 'station'
        transfer = root / 'transfer'
        quality = root / 'quality'
        for working_directory in (station, transfer, quality):
            working_directory.mkdir()
        path = station / 'station.sqlite'
        initialize_plan_store(path)
        recordings = RecordingLedger(path)
        recordings.initialize()
        segments = SegmentStore(path)
        segments.initialize()
        identity = {key: scenario[key] for key in ('roll_id', 'plan_id', 'revision', 'unit')}
        assignment = register_plan(path, **identity, threshold=scenario['threshold'])
        handoffs = []
        previews = {}
        for shift in scenario['shifts']:
            coverage = TickCoverage(*shift['coverage'])
            rows = [dict(identity, tick=number, deviation=value) for number, value in shift['ticks']]
            recording_id = shift['recording_id']
            evidence_id = shift['evidence_id']
            seal_recording(recordings, recording_id, assignment.roll_id, coverage, rows)
            previews[recording_id] = result_values(preview(recordings, recording_id))
            publish_segment(recordings, segments, recording_id, evidence_id, coverage)
            output = transfer / f'segment-{len(handoffs)}.json'
            export_segment(segments, evidence_id, output)
            handoffs.append(output)

        shutil.rmtree(station)
        archive = ArchiveStore(quality / 'archive.sqlite')
        archive.initialize()
        issued = issue_report(archive, assignment, TickCoverage(*scenario['coverage']),
                              (output.read_bytes() for output in reversed(handoffs)))
        actual = result_values(issued.result)
        expected = {'alarms': [2], 'amount': [0, 1]}
        lookup = read_report(ArchiveStore(archive.path), issued.report_id)
        if lookup.report != issued or actual != expected:
            raise RuntimeError(f'demo report differs from the worked sample: {actual!r}')
        expected_previews = {'white': {'alarms': [], 'amount': [4, 1]},
                             'night': {'alarms': [], 'amount': [1, 1]}}
        if previews != expected_previews:
            raise RuntimeError(f'demo station previews differ from the worked sample: {previews!r}')
        destination = transfer / 'inspection-report.json'
        delivery = export_report(archive, issued.report_id, destination)
        delivered = json.loads(destination.read_bytes())
        expected_delivery = {
            'report_id': issued.report_id,
            'receipt_id': issued.receipt.receipt_id,
            'evidence_ids': ['CW-17-white', 'CW-17-night'],
            **expected,
        }
        delivered_result = delivered['result']
        actual_delivery = {
            'report_id': delivered['report_id'], 'receipt_id': delivered['receipt']['receipt_id'],
            'evidence_ids': [reference['evidence_id'] for reference in delivered_result['references']],
            'alarms': delivered_result['alarms'], 'amount': delivered_result['amount'],
        }
        if (delivery.payload != destination.read_bytes() or actual_delivery != expected_delivery
                or delivered['receipt']['report_id'] != issued.report_id):
            raise RuntimeError(f'demo delivery differs from the issued report: {actual_delivery!r}')
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'compose', 'input': scenario, 'expected': expected, 'actual': actual,
            'downstream_expected': expected_delivery, 'downstream_actual': actual_delivery,
            'code_paths': ['recording/segments.py', 'evidence/publish.py', 'reports/issue.py',
                           'reports/compose.py', 'archive/export.py'],
            'oracle': 'Manual recurrence: amount 4 at tick 1, 6 at tick 2 alarms and resets, -1 at tick 3 floors to 0.',
        }, sort_keys=True))
    run_retest_demo()


def _render_piece(piece):
    return {
        'lo': encode_rational(piece.lo), 'hi': encode_rational(piece.hi),
        'lo_closed': piece.lo_closed, 'hi_closed': piece.hi_closed,
        'alarms': list(piece.alarms),
        'slope': encode_rational(piece.slope), 'intercept': encode_rational(piece.intercept),
    }


def _render_interval(bounds):
    return [encode_rational(bounds[0]), encode_rational(bounds[1])]


def run_retest_demo():
    """Detach the station, issue every worked retest case, then reopen and claim."""
    with TemporaryDirectory(prefix='retest-inspection-') as directory:
        root = Path(directory)
        station_root = root / 'station'
        station_root.mkdir()
        path = station_root / 'station.sqlite'
        initialize_plan_store(path)
        recordings = RecordingLedger(path)
        recordings.initialize()
        segments = SegmentStore(path)
        segments.initialize()
        assignment = register_plan(path, 'roll-retest', 'surface', 'r1', 'du', 5)
        for name, deviations in DEVIATIONS.items():
            coverage = TickCoverage(1, len(deviations))
            rows = [{'roll_id': assignment.roll_id, 'plan_id': assignment.plan.plan_id,
                     'revision': assignment.plan.revision, 'unit': assignment.plan.unit.value,
                     'tick': number, 'deviation': value}
                    for number, value in enumerate(deviations, 1)]
            seal_recording(recordings, name, assignment.roll_id, coverage, rows)
        transfers = root / 'transfer'
        transfers.mkdir()
        payloads = {}
        coverages = {}
        for recording_id, tick in ((name, case[0]) for name, *case in WORKED):
            key = (recording_id, tick)
            if key in payloads:
                continue
            evidence_id = f'retest-{recording_id}-{tick}'
            publish_retest(recordings, segments, recording_id, evidence_id, tick)
            payloads[key] = export_retest(segments, evidence_id,
                                          transfers / f'{evidence_id}.retest')
            coverages[key] = recordings.get_sealed(recording_id).coverage

        shutil.rmtree(station_root)
        archive = ArchiveStore(root / 'quality.sqlite')
        archive.initialize()
        issued_ids = []
        for recording_id, tick, replacement, final, required, forbidden, expected in WORKED:
            payload = payloads[(recording_id, tick)]
            coverage = coverages[(recording_id, tick)]
            issued = issue_retest(archive, assignment, coverage, recording_id, tick, payload,
                                  replacement, required, forbidden, final)
            actual = tuple((piece.lo, piece.hi, piece.lo_closed, piece.hi_closed,
                            piece.alarms, piece.slope, piece.intercept)
                           for piece in issued.result.pieces)
            expected_tuple = tuple(_expected_piece(piece) for piece in expected)
            if actual != expected_tuple:
                raise RuntimeError(f'retest {recording_id} tick {tick} differs from the worked '
                                   f'table: {actual!r}')
            issued_ids.append(issued.report_id)
            print('POINTWISE_RETEST ' + json.dumps({
                'case_id': f'retest-{recording_id}-{tick}',
                'recording_id': recording_id, 'tick': tick,
                'replacement': _render_interval(issued.result.replacement),
                'final_amount': _render_interval(issued.result.final_amount),
                'required_alarms': sorted(issued.result.required_alarms),
                'forbidden_alarms': sorted(issued.result.forbidden_alarms),
                'evidence_id': issued.result.reference.evidence_id,
                'digest': issued.result.reference.digest,
                'pieces': [_render_piece(piece) for piece in issued.result.pieces],
                'receipt': {'receipt_id': issued.receipt.receipt_id,
                            'report_id': issued.receipt.report_id},
            }, sort_keys=True))

        reopened = ArchiveStore(archive.path)
        reopened.initialize()
        for report_id in issued_ids:
            lookup = read_retest(reopened, report_id)
            if not lookup.found or lookup.report.report_id != report_id:
                raise RuntimeError(f'reopened retest lookup failed for {report_id}')
        print('POINTWISE_RETEST_REOPEN ' + json.dumps({
            'report_ids': issued_ids,
            'pieces_per_report': [len(read_retest(reopened, report_id).report.result.pieces)
                                  for report_id in issued_ids],
            'receipts': [read_retest(reopened, report_id).report.receipt.receipt_id
                         for report_id in issued_ids],
        }, sort_keys=True))


def _expected_piece(values):
    lo, hi, lo_closed, hi_closed, alarms, slope, intercept = values
    return (Fraction(lo), Fraction(hi), lo_closed, hi_closed, tuple(alarms),
            Fraction(slope), Fraction(intercept))


def main():
    parser = argparse.ArgumentParser(description='Run the CW-17 surface inspection example')
    parser.add_argument('sample', type=Path, choices=(Path('sample/roll.json'),))
    args = parser.parse_args()
    run_demo(json.loads(args.sample.read_text(encoding='utf-8')))


if __name__ == '__main__':
    main()
