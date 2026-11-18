"""Archive persistence for retest issuing.

Reports, their fixed request/result payload and the paired receipt are written
in one SQLite transaction.  Retest records live in their own tables so that the
V1 report format and lookup semantics stay untouched.
"""
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from archive.receipts import IssueReceipt, check_receipt
from reports.retest import RetestResult, result_from_payload, result_payload


@dataclass(frozen=True)
class IssuedRetest:
    report_id: str
    result: RetestResult
    receipt: IssueReceipt

    def __post_init__(self):
        check_receipt(self.report_id, self.receipt)
        if not isinstance(self.result, RetestResult):
            raise ValueError('retest issue requires a fixed retest result')


def create_retest_tables(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS retest_reports (
            report_id TEXT PRIMARY KEY NOT NULL, body TEXT NOT NULL
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS retest_receipts (
            receipt_id TEXT PRIMARY KEY NOT NULL, report_id TEXT NOT NULL UNIQUE,
            FOREIGN KEY (report_id) REFERENCES retest_reports (report_id)
        )
    ''')


def encode_retest_body(result: RetestResult) -> str:
    return json.dumps(result_payload(result), sort_keys=True, separators=(',', ':'))


def decode_retest_body(body: str) -> RetestResult:
    return result_from_payload(json.loads(body))


def save_retest(db: sqlite3.Connection, issued: IssuedRetest) -> IssuedRetest:
    """Write the report and receipt inside the caller's transaction."""
    db.execute('INSERT INTO retest_reports VALUES (?, ?)',
               (issued.report_id, encode_retest_body(issued.result)))
    db.execute('INSERT INTO retest_receipts VALUES (?, ?)',
               (issued.receipt.receipt_id, issued.receipt.report_id))
    return issued


def commit_retest_report(store, issued: IssuedRetest) -> IssuedRetest:
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('BEGIN IMMEDIATE')
        save_retest(db, issued)
    return issued


def load_retest_report(store, report_id: str) -> IssuedRetest | None:
    with closing(sqlite3.connect(store.path)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                      "AND name = 'retest_reports'").fetchone() is None:
            return None
        row = db.execute('''
            SELECT retest_reports.body, retest_receipts.receipt_id,
                   retest_receipts.report_id
            FROM retest_reports
            LEFT JOIN retest_receipts ON retest_receipts.report_id = retest_reports.report_id
            WHERE retest_reports.report_id = ?
        ''', (report_id,)).fetchone()
    if row is None:
        return None
    body, receipt_id, receipt_report_id = row
    if receipt_id is None:
        raise ValueError('archived retest report has no matching receipt')
    return IssuedRetest(report_id, decode_retest_body(body),
                        IssueReceipt(receipt_id, receipt_report_id))
