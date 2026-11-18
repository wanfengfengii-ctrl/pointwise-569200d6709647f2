from collections.abc import Iterable
from uuid import uuid4

from archive.commit import ArchiveStore, IssuedReport
from archive.receipts import make_receipt
from archive.retest import IssuedRetest, commit_retest_report
from plans.assignments import RollAssignment
from recording.sequence import TickCoverage
from reports.accept import accept_segments
from reports.compose import compose_report
from reports.retest import build_result


def issue_report(archive: ArchiveStore, assignment: RollAssignment,
                 coverage: TickCoverage, payloads: Iterable[bytes]) -> IssuedReport:
    accepted = accept_segments(assignment, coverage, payloads)
    result = compose_report(accepted)
    return archive.commit_issue(result)


def issue_retest(archive: ArchiveStore, assignment: RollAssignment, coverage: TickCoverage,
                 recording_id: str, tick: int, payload: bytes, replacement,
                 required_alarms, forbidden_alarms, final_amount) -> IssuedRetest:
    result = build_result(assignment, coverage, recording_id, tick, payload,
                          replacement, required_alarms, forbidden_alarms, final_amount)
    report_id = uuid4().hex
    issued = IssuedRetest(report_id, result, make_receipt(report_id))
    return commit_retest_report(archive, issued)
