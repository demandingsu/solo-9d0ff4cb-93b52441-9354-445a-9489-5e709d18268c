"""SQLite 连接、建表与事务辅助。"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from datetime import datetime

from flask import current_app, g

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS batches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    label             TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    submission_count  INTEGER NOT NULL DEFAULT 0,
    conclusion_count  INTEGER NOT NULL DEFAULT 0,
    pairing_count     INTEGER NOT NULL DEFAULT 0,
    supply_status     TEXT NOT NULL DEFAULT 'ok'
                      CHECK (supply_status IN ('ok', 'suspended')),
    suspended_reason  TEXT
);

CREATE TABLE IF NOT EXISTS submission_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    row_no      TEXT NOT NULL UNIQUE,
    sample_no   TEXT NOT NULL,
    values_json TEXT NOT NULL,
    original_values_json TEXT
);

CREATE TABLE IF NOT EXISTS conclusion_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    row_no      TEXT NOT NULL UNIQUE,
    sample_no   TEXT NOT NULL,
    values_json TEXT NOT NULL,
    original_values_json TEXT
);

CREATE TABLE IF NOT EXISTS approved_pairs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            INTEGER NOT NULL REFERENCES batches(id),
    submission_row_no   TEXT NOT NULL UNIQUE
                        REFERENCES submission_rows(row_no),
    conclusion_row_no   TEXT NOT NULL UNIQUE
                        REFERENCES conclusion_rows(row_no)
);

CREATE TABLE IF NOT EXISTS candidate_columns (
    name            TEXT PRIMARY KEY,
    position        INTEGER NOT NULL UNIQUE,
    in_submission   INTEGER NOT NULL DEFAULT 0,
    in_conclusion   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS column_aliases (
    alias       TEXT PRIMARY KEY,
    canonical   TEXT NOT NULL REFERENCES candidate_columns(name),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS join_keys (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    columns_json        TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN ('active', 'invalid', 'superseded')),
    created_at          TEXT NOT NULL,
    verified_at         TEXT,
    invalidated_at      TEXT,
    invalidation_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_key
    ON join_keys((1)) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS supply_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER REFERENCES batches(id),
    event       TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_submission_batch
    ON submission_rows(batch_id);
CREATE INDEX IF NOT EXISTS idx_conclusion_batch
    ON conclusion_rows(batch_id);
CREATE INDEX IF NOT EXISTS idx_pairs_batch
    ON approved_pairs(batch_id);
CREATE INDEX IF NOT EXISTS idx_events_batch
    ON supply_events(batch_id);
"""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """对旧库做就地升级：行表补 original_values_json 列（留存合并前的原列名/原值）。"""
    for table in ("submission_rows", "conclusion_rows"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "original_values_json" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN original_values_json TEXT"
            )


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextlib.contextmanager
def write_transaction(conn: sqlite3.Connection):
    """显式写事务；任何异常都整体回滚，调用方无需自行回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def dumps(values: dict) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(raw: str) -> dict:
    return json.loads(raw) if raw else {}


def register_import_hooks(app) -> None:
    app.teardown_appcontext(close_db)
