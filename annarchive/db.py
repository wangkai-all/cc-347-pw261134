"""SQLite persistence.

The schema enforces the core invariant at the storage layer:

* every download job is bound to a *version* and a *reference* at creation
  time (FKs), so a late-arriving job can only ever write its own version;
* ``attachment_provenance`` carries its own URL/hash/source audit per
  reference per version even when the underlying blob is shared;
* a blob's identity is its observed SHA-256; references point at blobs but
  keep the declared hash alongside, so a same-URL replacement that changes
  content yields a distinct blob.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS announcements (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    source_url  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id                    INTEGER PRIMARY KEY,
    announcement_id       INTEGER NOT NULL REFERENCES announcements(id),
    version_no            INTEGER NOT NULL,
    body_blob_sha256      TEXT NOT NULL,
    body_url              TEXT NOT NULL,
    body_status           INTEGER,
    body_headers_json     TEXT NOT NULL DEFAULT '{}',
    fetched_at            TEXT NOT NULL,
    locked_at             TEXT,
    status                TEXT NOT NULL CHECK (
                              status IN ('locked','downloading','downloaded',
                                         'failed','published')),
    fail_reason           TEXT,
    UNIQUE(announcement_id, version_no)
);

CREATE TABLE IF NOT EXISTS blobs (
    sha256        TEXT PRIMARY KEY,
    size_bytes    INTEGER NOT NULL,
    media_type    TEXT,
    stored_path   TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachment_refs (
    id                 INTEGER PRIMARY KEY,
    version_id         INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    link_index         INTEGER NOT NULL,
    label              TEXT NOT NULL,
    source_url         TEXT NOT NULL,
    declared_sha256    TEXT,
    UNIQUE(version_id, link_index)
);

CREATE TABLE IF NOT EXISTS attachment_provenance (
    id                INTEGER PRIMARY KEY,
    version_id        INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    ref_id            INTEGER NOT NULL REFERENCES attachment_refs(id),
    download_job_id   INTEGER NOT NULL,
    source_url        TEXT NOT NULL,          -- URL actually requested
    final_url         TEXT,                   -- after any redirects
    status_code       INTEGER,
    request_headers_json  TEXT NOT NULL DEFAULT '{}',
    response_headers_json TEXT NOT NULL DEFAULT '{}',
    bytes_expected    INTEGER,
    bytes_received    INTEGER,
    declared_sha256   TEXT,
    observed_sha256   TEXT,
    blob_sha256       TEXT REFERENCES blobs(sha256),
    outcome           TEXT NOT NULL CHECK (
                          outcome IN ('pending','downloaded','verified',
                                      'mismatch','http_error','network_error')),
    attempts          INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    started_at        TEXT,
    finished_at       TEXT,
    UNIQUE(version_id, ref_id)               -- one provenance per ref per version
);

CREATE TABLE IF NOT EXISTS download_jobs (
    id            INTEGER PRIMARY KEY,
    version_id    INTEGER NOT NULL REFERENCES versions(id),
    ref_id        INTEGER NOT NULL REFERENCES attachment_refs(id),
    created_at    TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'queued'
                  CHECK (state IN ('queued','running','done','error')),
    CONSTRAINT job_version_binding UNIQUE (id, version_id)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_versions_ann ON versions(announcement_id);
CREATE INDEX IF NOT EXISTS idx_refs_version ON attachment_refs(version_id);
CREATE INDEX IF NOT EXISTS idx_prov_version ON attachment_provenance(version_id);
CREATE INDEX IF NOT EXISTS idx_jobs_version ON download_jobs(version_id);
CREATE INDEX IF NOT EXISTS idx_events_kind  ON events(kind);

-- Structural enforcement of "a late download can never bind a new version":
-- once a job or provenance row is created against a version, its version_id
-- is immutable. SQLite foreign keys alone allow UPDATE reparenting, so an
-- explicit trigger rejects it.
CREATE TRIGGER IF NOT EXISTS trg_job_version_immutable
BEFORE UPDATE OF version_id ON download_jobs
WHEN OLD.version_id <> NEW.version_id
BEGIN
    SELECT RAISE(ABORT, 'download job version binding is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_prov_version_immutable
BEFORE UPDATE OF version_id ON attachment_provenance
WHEN OLD.version_id <> NEW.version_id
BEGIN
    SELECT RAISE(ABORT, 'provenance version binding is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_ref_version_immutable
BEFORE UPDATE OF version_id ON attachment_refs
WHEN OLD.version_id <> NEW.version_id
BEGIN
    SELECT RAISE(ABORT, 'reference version binding is immutable');
END;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------- utilities

def log_event(conn: sqlite3.Connection, kind: str, entity: str,
              entity_id: Any = None, **detail: Any) -> None:
    conn.execute(
        "INSERT INTO events(ts, kind, entity, entity_id, detail_json) "
        "VALUES (?,?,?,?,?)",
        (utcnow(), kind, entity,
         None if entity_id is None else str(entity_id),
         json.dumps(detail, ensure_ascii=False, sort_keys=True)),
    )


def insert_blob(conn: sqlite3.Connection, sha256: str, size: int,
                media_type: str | None, stored_path: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO blobs(sha256, size_bytes, media_type, "
        "stored_path, first_seen_at) VALUES (?,?,?,?,?)",
        (sha256, size, media_type, stored_path, utcnow()),
    )


def get_blob(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM blobs WHERE sha256=?",
                        (sha256,)).fetchone()


def find_announcement(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM announcements WHERE slug=?",
                        (slug,)).fetchone()


def list_announcements(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT a.*, "
        "  (SELECT COUNT(*) FROM versions v WHERE v.announcement_id=a.id) "
        "    AS version_count, "
        "  (SELECT MAX(v.version_no) FROM versions v "
        "     WHERE v.announcement_id=a.id AND v.status='published') "
        "    AS published_version "
        "FROM announcements a ORDER BY a.id"))


def list_versions(conn: sqlite3.Connection, announcement_id: int,
                  ) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM versions WHERE announcement_id=? ORDER BY version_no",
        (announcement_id,)))


def get_version(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM versions WHERE id=?",
                        (version_id,)).fetchone()


def get_refs(conn: sqlite3.Connection, version_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM attachment_refs WHERE version_id=? ORDER BY link_index",
        (version_id,)))


def get_provenance(conn: sqlite3.Connection, version_id: int,
                   ) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT p.*, r.label, r.link_index FROM attachment_provenance p "
        "JOIN attachment_refs r ON r.id = p.ref_id "
        "WHERE p.version_id=? ORDER BY r.link_index",
        (version_id,)))


def pending_provenance(conn: sqlite3.Connection, version_id: int,
                       ) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM attachment_provenance WHERE version_id=? "
        "AND outcome IN ('pending','http_error','network_error') "
        "ORDER BY id",
        (version_id,)))


def create_job(conn: sqlite3.Connection, version_id: int, ref_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO download_jobs(version_id, ref_id, created_at, state) "
        "VALUES (?,?,?, 'queued')",
        (version_id, ref_id, utcnow()))
    return int(cur.lastrowid)


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM download_jobs WHERE id=?",
                        (job_id,)).fetchone()


def list_events(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))
