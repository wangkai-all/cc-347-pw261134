"""Torn downloads and mid-flight mutations — the scenarios the locked-version
design exists to defeat."""
from __future__ import annotations

import threading
import time
import urllib.request

from annarchive import db
from annarchive.config import safe_digest
from annarchive.pipeline import download_version, fetch_body
from annarchive.seed import SLUG, initial_files
from tests._base import SimCase


class TornDownloadTests(SimCase):
    def test_torn_response_is_quarantined_and_fails_verification(self):
        import base64
        # stage the rev B bytes at the same URL first; the locked v2 body
        # still declares the v1 hash, so a response mixing v1 prefix with
        # rev B suffix cannot match it.
        new_pdf = initial_files()["__replacement__/files/bulletin.pdf"]
        replacement = self.admin("/__admin__/replace-file", {
            "path": "/files/bulletin.pdf",
            "content_base64": base64.b64encode(new_pdf).decode("ascii"),
            "note": "rev B staged"})
        old_content_id = self.hashes["files/bulletin.pdf"]

        # v2 body gives a correct bulletin.pdf link with declared v1 hash
        self.admin("/__admin__/correct-body",
                   {"slug": SLUG, "body": self.seed_info["body_v2"],
                    "reason": "fix link"})
        locked = fetch_body(self.conn, self.cfg, self.source_url, SLUG)
        version_id = locked.version_id

        # point the frozen ref's URL at the torn endpoint: first 120 bytes
        # of the OLD content, remainder of whatever is now current (rev B)
        ref = next(r for r in db.get_refs(self.conn, version_id)
                   if r["source_url"].endswith("/files/bulletin.pdf"))
        torn_url = (f"{self.base_url}/files/bulletin.pdf"
                    f"?torn_after=120&torn_to={old_content_id}"
                    "&chunk_size=64&chunk_delay=0.01")
        self.conn.execute("UPDATE attachment_refs SET source_url=? WHERE id=?",
                          (torn_url, ref["id"]))

        summary = download_version(self.conn, self.cfg, version_id,
                                   max_attempts=1)
        pdf = next(r for r in summary["refs"]
                   if r["ref_id"] == ref["id"])
        self.assertEqual(pdf["outcome"], "mismatch")
        self.assertNotEqual(pdf["observed"], pdf["declared"])
        self.assertNotEqual(pdf["observed"], replacement["sha256"])

        # bytes landed in quarantine, never in the content-addressed store
        quarantined = list(self.cfg.quarantine_dir.glob("*.bin"))
        self.assertTrue(any(safe_digest(pdf["observed"]) in q.name
                            for q in quarantined))
        self.assertIsNone(db.get_blob(self.conn, pdf["observed"]))

        # full source evidence was still captured
        prov = self.conn.execute(
            "SELECT * FROM attachment_provenance WHERE ref_id=?",
            (ref["id"],)).fetchone()
        self.assertEqual(prov["outcome"], "mismatch")
        self.assertIn("X-Torn", prov["response_headers_json"])
        self.assertGreater(prov["bytes_received"], 0)

    def test_body_corrected_mid_download_keeps_old_version_locked(self):
        # lock v1 (wrong pdf link), start its downloads in a thread, and
        # correct the body while downloads are in flight
        locked = fetch_body(self.conn, self.cfg, self.source_url, SLUG)
        v1_id = locked.version_id
        v1_refs = [(r["id"], r["source_url"])
                   for r in db.get_refs(self.conn, v1_id)]

        def slow_download():
            # each thread opens its own SQLite connection
            tconn = db.connect(self.cfg.db_path)
            try:
                # make the csv slow so the correction lands mid-flight
                csv_ref = next(r for r in db.get_refs(tconn, v1_id)
                               if r["source_url"].endswith("schedule.csv"))
                slow_url = csv_ref["source_url"] + \
                    "?chunk_delay=0.05&chunk_size=64"
                tconn.execute(
                    "UPDATE attachment_refs SET source_url=? WHERE id=?",
                    (slow_url, csv_ref["id"]))
                time.sleep(0.05)
                download_version(tconn, self.cfg, v1_id, max_attempts=1)
            finally:
                tconn.close()

        worker = threading.Thread(target=slow_download)
        worker.start()
        time.sleep(0.1)
        self.admin("/__admin__/correct-body",
                   {"slug": SLUG, "body": self.seed_info["body_v2"],
                    "reason": "corrected during download"})
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())

        # the in-flight version still points at the frozen v1 URLs
        refs_after = db.get_refs(self.conn, v1_id)
        self.assertEqual(
            sorted(u for _, u in v1_refs if "bulletin-draft" in u),
            [r["source_url"].split("?")[0] for r in refs_after
             if "bulletin-draft" in r["source_url"]])

        # a fresh fetch discovers the correction as a *new* version
        v2 = fetch_body(self.conn, self.cfg, self.source_url, SLUG)
        self.assertEqual(v2.version_no, 2)
        self.assertNotEqual(v2.version_id, v1_id)

        # provenance rows created during the flight are bound to v1 only
        stray = self.conn.execute(
            "SELECT COUNT(*) AS n FROM attachment_provenance "
            "WHERE version_id=?", (v2.version_id,)).fetchone()
        self.assertEqual(stray["n"], 0)

    def test_late_job_can_never_bind_newer_version(self):
        # structural guarantee: a job created for v1 carries v1's id in the
        # database; even after v2 exists, finishing that job writes v1 only.
        v1 = fetch_body(self.conn, self.cfg, self.source_url, SLUG)
        job_id = db.create_job(self.conn, v1.version_id,
                               db.get_refs(self.conn, v1.version_id)[0]["id"])
        job = db.get_job(self.conn, job_id)
        self.assertEqual(job["version_id"], v1.version_id)

        self.admin("/__admin__/correct-body",
                   {"slug": SLUG, "body": self.seed_info["body_v2"],
                    "reason": "v2"})
        v2 = fetch_body(self.conn, self.cfg, self.source_url, SLUG)

        # there is no API path to move the job; the FK binding is immutable
        with self.assertRaises(Exception):
            self.conn.execute(
                "UPDATE download_jobs SET version_id=? WHERE id=?",
                (v2.version_id, job_id))
        refreshed = db.get_job(self.conn, job_id)
        self.assertEqual(refreshed["version_id"], v1.version_id)

    def test_replacement_bytes_serve_at_same_url_with_distinct_hash(self):
        # direct simulator check: URL unchanged, ETag/content id change
        url = f"{self.base_url}/files/bulletin.pdf"
        with urllib.request.urlopen(url) as resp:
            first_etag = resp.headers["ETag"]
            first_id = resp.headers["X-Content-Id"]
        new_pdf = initial_files()["__replacement__/files/bulletin.pdf"]
        import base64
        swapped = self.admin("/__admin__/replace-file", {
            "path": "/files/bulletin.pdf",
            "content_base64": base64.b64encode(new_pdf).decode("ascii"),
            "note": "swap"})
        with urllib.request.urlopen(url) as resp:
            second_etag = resp.headers["ETag"]
            second_id = resp.headers["X-Content-Id"]
        self.assertNotEqual(first_etag, second_etag)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(second_id, swapped["content_id"])


if __name__ == "__main__":
    unittest.main()
