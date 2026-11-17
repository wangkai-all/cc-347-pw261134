"""The three independent pipeline stages.

Stage 1  :func:`fetch_body`        - download body, parse refs, *lock* a new
                                     immutable version (URLs and declared
                                     hashes frozen).
Stage 2  :func:`download_version`  - one job + one provenance row per ref,
                                     bound to the version at creation time;
                                     blobs dedupe, provenance never does.
Stage 3  :func:`publish_version`   - gate that flips a version to published
                                     only when every binding is verified.

The binding rule is structural, not conventional: ``download_jobs`` and
``attachment_provenance`` carry ``version_id`` foreign keys from the moment
they are created, so bytes arriving late physically cannot be attached to a
version created later.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import Config
from .hashing import sha256_bytes, sha256_file
from .http_client import HttpError, http_download, http_get
from .parser import parse_body
from .seed import TITLE

MAX_ATTEMPTS = 3


class PipelineError(Exception):
    pass


@dataclass
class FetchOutcome:
    announcement_id: int
    version_id: int
    version_no: int
    created: bool          # False => body unchanged, existing version reused
    body_sha256: str


# ------------------------------------------------------------ stage 1: body

def fetch_body(conn, cfg: Config, source_url: str, slug: str,
               title: str = TITLE) -> FetchOutcome:
    """Fetch + lock.  A correction observed later cannot touch this row."""
    result = http_get(source_url)
    body_bytes = result.body
    body_hash = sha256_bytes(body_bytes)

    with db.transaction(conn):
        ann = db.find_announcement(conn, slug)
        if ann is None:
            cur = conn.execute(
                "INSERT INTO announcements(slug, title, source_url, "
                "created_at) VALUES (?,?,?,?)",
                (slug, title, source_url, db.utcnow()))
            announcement_id = int(cur.lastrowid)
            db.log_event(conn, "announcement-created", "announcement",
                         announcement_id, slug=slug, source_url=source_url)
        else:
            announcement_id = int(ann["id"])
            if ann["source_url"] != source_url:
                conn.execute("UPDATE announcements SET source_url=? WHERE id=?",
                             (source_url, announcement_id))

        # idempotency: identical body means no new version
        existing = conn.execute(
            "SELECT * FROM versions WHERE announcement_id=? "
            "AND body_blob_sha256=? ORDER BY version_no DESC LIMIT 1",
            (announcement_id, body_hash)).fetchone()
        if existing is not None:
            db.log_event(conn, "fetch-unchanged", "version", existing["id"],
                         body_sha256=body_hash)
            return FetchOutcome(announcement_id, int(existing["id"]),
                                int(existing["version_no"]), False, body_hash)

        version_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM versions "
            "WHERE announcement_id=?", (announcement_id,)).fetchone()[0]

        _store_blob(conn, cfg, body_hash, len(body_bytes),
                    result.response_headers.get("Content-Type",
                                                "text/markdown"),
                    body_bytes)
        cur = conn.execute(
            "INSERT INTO versions(announcement_id, version_no, "
            "body_blob_sha256, body_url, body_status, body_headers_json, "
            "fetched_at, locked_at, status) VALUES (?,?,?,?,?,?,?,?, 'locked')",
            (announcement_id, version_no, body_hash, source_url,
             result.status, _json(result.response_headers),
             db.utcnow(), db.utcnow()))
        version_id = int(cur.lastrowid)

        # freeze references exactly as the locked body declared them
        for ref in parse_body(body_bytes.decode("utf-8"), source_url):
            conn.execute(
                "INSERT INTO attachment_refs(version_id, link_index, label, "
                "source_url, declared_sha256) VALUES (?,?,?,?,?)",
                (version_id, ref.link_index, ref.label, ref.source_url,
                 ref.declared_sha256))

        db.log_event(conn, "body-locked", "version", version_id,
                     version_no=version_no, body_sha256=body_hash,
                     body_url=source_url, status=result.status,
                     ref_count=len(parse_body(
                         body_bytes.decode("utf-8"), source_url)))
    return FetchOutcome(announcement_id, version_id, version_no, True,
                        body_hash)


# ------------------------------------------------------ stage 2: attachments

def download_version(conn, cfg: Config, version_id: int,
                     max_attempts: int = MAX_ATTEMPTS) -> dict:
    """Download every frozen ref of this version and verify it.

    Independent of any other version: mutations on the source after lock
    only affect *new* versions.  Returns a summary dict.
    """
    version = db.get_version(conn, version_id)
    if version is None:
        raise PipelineError(f"version {version_id} does not exist")
    if version["status"] in ("published", "downloaded"):
        return {"version_id": version_id, "status": version["status"],
                "skipped": "already complete"}

    conn.execute("UPDATE versions SET status='downloading' WHERE id=?",
                 (version_id,))
    db.log_event(conn, "download-start", "version", version_id)

    refs = db.get_refs(conn, version_id)
    summary = {"version_id": version_id, "refs": [], "failures": []}

    for ref in refs:
        ref_id = int(ref["id"])
        # binding happens HERE and never changes afterwards. On a retry of a
        # failed/locked version we reuse the existing provenance row (and its
        # audit history) instead of inserting a duplicate, while still
        # creating a fresh job bound to the same immutable version.
        existing = conn.execute(
            "SELECT * FROM attachment_provenance WHERE version_id=? AND ref_id=?",
            (version_id, ref_id)).fetchone()
        job_id = db.create_job(conn, version_id, ref_id)
        if existing is None:
            conn.execute(
                "INSERT INTO attachment_provenance(version_id, ref_id, "
                "download_job_id, source_url, declared_sha256, outcome, "
                "started_at) VALUES (?,?,?,?,?, 'pending', ?)",
                (version_id, ref_id, job_id, ref["source_url"],
                 ref["declared_sha256"], db.utcnow()))
        else:
            conn.execute(
                "UPDATE attachment_provenance SET download_job_id=?, "
                "source_url=?, declared_sha256=?, outcome='pending', "
                "error=NULL, observed_sha256=NULL, blob_sha256=NULL, "
                "status_code=NULL, final_url=NULL, "
                "request_headers_json='{}', response_headers_json='{}', "
                "bytes_expected=NULL, bytes_received=NULL, attempts=0, "
                "started_at=? WHERE id=?",
                (job_id, ref["source_url"], ref["declared_sha256"],
                 db.utcnow(), existing["id"]))
        prov = conn.execute(
            "SELECT * FROM attachment_provenance WHERE version_id=? AND ref_id=?",
            (version_id, ref_id)).fetchone()
        result = _download_one(conn, cfg, version_id, ref, prov, job_id,
                               max_attempts)
        summary["refs"].append(result)
        if result["outcome"] != "verified":
            summary["failures"].append(result)

    with db.transaction(conn):
        if summary["failures"]:
            reasons = "; ".join(
                f"{f['label']}: {f['outcome']}({f.get('error') or 'hash mismatch'})"
                for f in summary["failures"])
            conn.execute(
                "UPDATE versions SET status='failed', fail_reason=? WHERE id=?",
                (reasons, version_id))
            db.log_event(conn, "download-failed", "version", version_id,
                         reason=reasons)
            summary["status"] = "failed"
        else:
            conn.execute("UPDATE versions SET status='downloaded', "
                         "fail_reason=NULL WHERE id=?", (version_id,))
            db.log_event(conn, "download-complete", "version", version_id,
                         ref_count=len(refs))
            summary["status"] = "downloaded"
    return summary


def _download_one(conn, cfg: Config, version_id: int, ref, prov,
                  job_id: int, max_attempts: int) -> dict:
    url = ref["source_url"]
    declared = ref["declared_sha256"]
    attempts = 0
    last_error = None

    while attempts < max_attempts:
        attempts += 1
        inflight = cfg.inflight_path(job_id)
        try:
            fetched = http_download(url, inflight)
            observed, size = sha256_file(inflight)
            with db.transaction(conn):
                conn.execute(
                    "UPDATE download_jobs SET state='running' WHERE id=?",
                    (job_id,))
                if declared is not None and observed != declared:
                    # untrusted bytes get quarantined, never enter blob store
                    qpath = cfg.quarantine_path(job_id, observed)
                    shutil.move(str(inflight), str(qpath))
                    conn.execute(
                        "UPDATE attachment_provenance SET attempts=?, "
                        "status_code=?, final_url=?, "
                        "request_headers_json=?, response_headers_json=?, "
                        "bytes_expected=?, bytes_received=?, "
                        "observed_sha256=?, outcome='mismatch', error=?, "
                        "finished_at=? WHERE id=?",
                        (attempts, fetched.status, fetched.final_url,
                         _json(fetched.request_headers),
                         _json(fetched.response_headers),
                         _expected_size(fetched), size, observed,
                         f"declared {declared} != observed {observed}",
                         db.utcnow(), prov["id"]))
                    conn.execute(
                        "UPDATE download_jobs SET state='error' WHERE id=?",
                        (job_id,))
                    db.log_event(conn, "hash-mismatch", "provenance",
                                 prov["id"], version_id=version_id,
                                 url=url, declared=declared,
                                 observed=observed,
                                 quarantine=str(qpath))
                    return {"ref_id": int(ref["id"]),
                            "label": ref["label"], "url": url,
                            "outcome": "mismatch",
                            "declared": declared, "observed": observed}

                # verified (or no declared hash: observed becomes the record)
                reused = db.get_blob(conn, observed) is not None
                final_blob_path = _store_blob_file(conn, cfg, observed, size,
                                                    fetched.response_headers,
                                                    inflight)
                conn.execute(
                    "UPDATE attachment_provenance SET attempts=?, "
                    "status_code=?, final_url=?, request_headers_json=?, "
                    "response_headers_json=?, bytes_expected=?, "
                    "bytes_received=?, declared_sha256=?, observed_sha256=?, "
                    "blob_sha256=?, outcome='verified', error=NULL, "
                    "finished_at=? WHERE id=?",
                    (attempts, fetched.status, fetched.final_url,
                     _json(fetched.request_headers),
                     _json(fetched.response_headers),
                     _expected_size(fetched), size, declared, observed,
                     observed, db.utcnow(), prov["id"]))
                conn.execute(
                    "UPDATE download_jobs SET state='done' WHERE id=?",
                    (job_id,))
                db.log_event(conn,
                             "blob-reused" if reused else "attachment-verified",
                             "provenance", prov["id"],
                             version_id=version_id, url=url,
                             blob_sha256=observed, reused=reused,
                             stored_path=str(final_blob_path),
                             attempts=attempts)
            return {"ref_id": int(ref["id"]), "label": ref["label"],
                    "url": url, "outcome": "verified",
                    "declared": declared, "observed": observed,
                    "reused": reused, "attempts": attempts}
        except HttpError as exc:
            last_error = str(exc)
            with db.transaction(conn):
                conn.execute(
                    "UPDATE attachment_provenance SET attempts=?, "
                    "status_code=?, response_headers_json=?, error=?, "
                    "outcome=? WHERE id=?",
                    (attempts, exc.status, _json(exc.headers), str(exc),
                     "http_error" if exc.status else "network_error",
                     prov["id"]))
                db.log_event(conn, "download-error", "provenance",
                             prov["id"], version_id=version_id, url=url,
                             attempt=attempts, error=str(exc))
        except OSError as exc:  # disk / IO problems
            last_error = str(exc)
            with db.transaction(conn):
                conn.execute(
                    "UPDATE attachment_provenance SET attempts=?, error=?, "
                    "outcome='network_error' WHERE id=?",
                    (attempts, str(exc), prov["id"]))

    # retries exhausted
    with db.transaction(conn):
        conn.execute(
            "UPDATE attachment_provenance SET outcome='network_error', "
            "finished_at=? WHERE id=?", (db.utcnow(), prov["id"]))
        conn.execute("UPDATE download_jobs SET state='error' WHERE id=?",
                     (job_id,))
    return {"ref_id": int(ref["id"]), "label": ref["label"], "url": url,
            "outcome": "network_error", "error": last_error,
            "attempts": attempts}


# ---------------------------------------------------------- stage 3: publish

def publish_version(conn, version_id: int) -> dict:
    version = db.get_version(conn, version_id)
    if version is None:
        raise PipelineError(f"version {version_id} does not exist")
    if version["status"] == "published":
        return {"version_id": version_id, "status": "published",
                "skipped": "already published"}

    provs = db.get_provenance(conn, version_id)
    refs = db.get_refs(conn, version_id)
    if len(provs) != len(refs) or any(p["outcome"] != "verified"
                                      for p in provs):
        raise PipelineError(
            f"version {version_id} is not fully verified; refusing to publish")

    with db.transaction(conn):
        conn.execute(
            "UPDATE versions SET status='published' WHERE id=?",
            (version_id,))
        db.log_event(conn, "version-published", "version", version_id,
                     version_no=version["version_no"],
                     attachments=[dict(p) for p in provs])
    return {"version_id": version_id, "status": "published",
            "attachment_count": len(provs)}


def run_full_pipeline(conn, cfg: Config, source_url: str, slug: str,
                      title: str = TITLE) -> dict:
    """Convenience: lock -> download -> publish, with each stage isolated."""
    fetched = fetch_body(conn, cfg, source_url, slug, title)
    result: dict = {"fetch": fetched.__dict__}
    if fetched.created or db.get_version(conn, fetched.version_id)[
            "status"] in ("locked", "downloading", "failed"):
        result["download"] = download_version(conn, cfg, fetched.version_id)
        if result["download"]["status"] == "downloaded":
            result["publish"] = publish_version(conn, fetched.version_id)
    else:
        result["download"] = {"skipped": True}
    return result


# ------------------------------------------------------------------ helpers

def _json(value) -> str:
    import json
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _expected_size(fetched) -> int | None:
    cl = fetched.response_headers.get("Content-Length")
    try:
        return int(cl) if cl is not None else None
    except (TypeError, ValueError):
        return None


def _store_blob(conn, cfg: Config, digest: str, size: int,
                media_type: str | None, data: bytes) -> Path:
    target = cfg.blob_path(digest)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    db.insert_blob(conn, digest, size, media_type, str(target))
    return target


def _store_blob_file(conn, cfg: Config, digest: str, size: int,
                     response_headers: dict, inflight: Path) -> Path:
    target = cfg.blob_path(digest)
    if target.exists():
        # identical bytes already archived: drop the duplicate download
        inflight.unlink(missing_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(inflight), str(target))
    db.insert_blob(conn, digest, size,
                   response_headers.get("Content-Type", "application/octet-stream"),
                   str(target))
    return target
