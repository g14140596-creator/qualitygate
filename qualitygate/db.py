import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
                    columns_json TEXT NOT NULL, rows_json TEXT NOT NULL, row_count INTEGER NOT NULL,
                    byte_count INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rulesets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
                    rules_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    ruleset_id TEXT NOT NULL REFERENCES rulesets(id), ruleset_name TEXT NOT NULL,
                    ruleset_version INTEGER NOT NULL, rules_json TEXT NOT NULL, references_json TEXT NOT NULL,
                    rules_hash TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled','interrupted')),
                    progress INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, duration_ms REAL,
                    summary_json TEXT, rule_results_json TEXT, error TEXT,
                    parent_id TEXT REFERENCES jobs(id), idempotency_key TEXT UNIQUE, request_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs(state,created_at);
                CREATE TABLE IF NOT EXISTS issues (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    row_number INTEGER NOT NULL, rule_id TEXT NOT NULL, column_name TEXT NOT NULL,
                    severity TEXT NOT NULL, value TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS issues_job_row ON issues(job_id,row_number,id);
                CREATE INDEX IF NOT EXISTS issues_job_rule ON issues(job_id,rule_id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    created_at TEXT NOT NULL, message TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def event(self, con, job_id, message):
        con.execute("INSERT INTO events(job_id,created_at,message) VALUES(?,?,?)", (job_id, now(), message))
