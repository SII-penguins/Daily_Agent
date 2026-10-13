"""Durable local reference ledger. Production D1 adapter is intentionally absent."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import secrets
import sqlite3

from .domain import ImportRejected, canonical_json

KEY_ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
STATUSES = {"queued", "metadata_saved", "pdf_pending", "complete", "already_exists", "failure"}


class BusyImport(RuntimeError):
    pass


class SqliteReceiptStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS receipts (
                job_id TEXT NOT NULL, paper_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(job_id,paper_id));
              CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                paper_id TEXT NOT NULL, status TEXT NOT NULL, occurred_at TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def lock(self):
        """Serialize local workers, including different jobs for the same library.

        OS releases lock on process exit; no expired-lease overlap is possible.
        This is NOT a distributed/Sites lock.
        """
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusyImport("Another import is running") from exc
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def ensure(self, plan):
        body = canonical_json({"report": plan.manifest.report_sha256,
                               "catalog": plan.manifest.catalog_sha256,
                               "library_scope": plan.library_scope,
                               "selected": [p.paper_id for p in plan.papers],
                               "include_pdf": plan.include_pdf, "execution_mode": "offline_mock"})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (plan.job_id,)).fetchone()
            if row and row[0] != body:
                raise ImportRejected("Job identity collision")
            db.execute("INSERT OR IGNORE INTO jobs VALUES (?,?)", (plan.job_id, body))
            for paper in plan.papers:
                key = "".join(secrets.choice(KEY_ALPHABET) for _ in range(8))
                attachment_key = "".join(secrets.choice(KEY_ALPHABET) for _ in range(8))
                receipt = {"job_id": plan.job_id, "paper_id": paper.paper_id,
                           "status": "queued", "created_key": key, "item_key": None,
                           "attachment_key": attachment_key, "metadata_saved": False,
                           "classification_saved": False, "pdf_verified": False,
                           "existing_item": False, "error_code": None, "retryable": False,
                           "execution_mode": "offline_mock"}
                db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?)",
                           (plan.job_id, paper.paper_id, canonical_json(receipt)))

    def get(self, job_id, paper_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM receipts WHERE job_id=? AND paper_id=?", (job_id, paper_id)).fetchone()
        if row is None:
            raise ImportRejected("Unknown receipt")
        return json.loads(row[0])

    def update(self, job_id, paper_id, **changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM receipts WHERE job_id=? AND paper_id=?", (job_id, paper_id)).fetchone()
            if not row:
                raise ImportRejected("Unknown receipt")
            receipt = {**json.loads(row[0]), **changes}
            if receipt["status"] not in STATUSES:
                raise ImportRejected("Invalid receipt status")
            db.execute("UPDATE receipts SET payload=? WHERE job_id=? AND paper_id=?",
                       (canonical_json(receipt), job_id, paper_id))
            db.execute("INSERT INTO events(job_id,paper_id,status,occurred_at) VALUES (?,?,?,?)",
                       (job_id, paper_id, receipt["status"], datetime.now(timezone.utc).isoformat()))
        return receipt

    def events(self, job_id, paper_id):
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT status FROM events WHERE job_id=? AND paper_id=? ORDER BY seq", (job_id, paper_id))]
