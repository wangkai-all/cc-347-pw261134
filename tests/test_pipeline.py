"""End-to-end pipeline behaviour against the real simulator."""
from __future__ import annotations

from annarchive import db
from annarchive.pipeline import (PipelineError, download_version, fetch_body,
                                 publish_version, run_full_pipeline)
from annarchive.seed import SLUG
from tests._base import SimCase


class PipelineTests(SimCase):
    def test_v1_wrong_link_fails_and_cannot_publish(self):
        outcome = fetch_body(self.conn, self.cfg, self.source_url, SLUG)
        self.assertTrue(outcome.created)
        v1_id = outcome.version_id

        summary = download_version(self.conn, self.cfg, v1_id)
        self.assertEqual(summary["status"], "failed")
        labels = {r["label"]: r for r in summary["refs"]}
        # csv is fine; the mis-linked pdf URL 404s
        self.assertEqual(labels["观测计划表 CSV"]["outcome"], "verified")
        self.assertIn(labels["PDF 公报（链接待更正）"]["outcome"],
                      ("http_error", "network_error"))

        version = db.get_version(self.conn, v1_id)
        self.assertEqual(version["status"], "failed")
        with self.assertRaises(PipelineError):
            publish_version(self.conn, v1_id)

    def test_correction_creates_v2_and_publishes(self):
        v1 = fetch_body(self.conn, self.cfg, self.source_url, SLUG)
        # source corrects the body *after* v1 was locked
        self.admin("/__admin__/correct-body",
                   {"slug": SLUG, "body": self.seed_info["body_v2"],
                    "reason": "fix pdf link"})
        v2 = fetch_body(self.conn, self.cfg, self.source_url, SLUG)

        self.assertNotEqual(v1.version_id, v2.version_id)
        self.assertEqual(v2.version_no, 2)

        # v1's frozen refs are untouched by the correction
        v1_refs = {r["source_url"] for r in db.get_refs(self.conn,
                                                        v1.version_id)}
        self.assertTrue(any("bulletin-draft.pdf" in u for u in v1_refs))
        v2_refs = {r["source_url"] for r in db.get_refs(self.conn,
                                                        v2.version_id)}
        self.assertTrue(any(u.endswith("/files/bulletin.pdf")
                            for u in v2_refs))

        summary = run_full_pipeline(self.conn, self.cfg, self.source_url,
                                    SLUG)
        # full pipeline reused v2 (body unchanged) then downloaded/published
        self.assertEqual(summary["fetch"]["version_id"], v2.version_id)
        self.assertEqual(db.get_version(self.conn, v2.version_id)["status"],
                         "published")

    def test_same_url_replacement_yields_new_blob_new_version(self):
        run_full_pipeline(self.conn, self.cfg, self.source_url, SLUG)
        self.admin("/__admin__/correct-body",
                   {"slug": SLUG, "body": self.seed_info["body_v2"],
                    "reason": "fix link"})
        v2 = run_full_pipeline(self.conn, self.cfg, self.source_url, SLUG)
        v2_id = v2["fetch"]["version_id"]

        # replace bytes at the very same URL and update the checksum block
        from annarchive.seed import initial_files
        import base64
        new_pdf = initial_files()["__replacement__/files/bulletin.pdf"]
        replacement = self.admin("/__admin__/replace-file", {
            "path": "/files/bulletin.pdf",
            "content_base64": base64.b64encode(new_pdf).decode("ascii"),
            "note": "rev B"})
        self.admin("/__admin__/correct-body",
                   {"slug": SLUG, "body": self.seed_info["body_v3"],
                    "reason": "hash update"})
        v3 = run_full_pipeline(self.conn, self.cfg, self.source_url, SLUG)
        v3_id = v3["fetch"]["version_id"]
        self.assertNotEqual(v2_id, v3_id)

        def pdf_blob(version_id: int) -> str | None:
            for p in db.get_provenance(self.conn, version_id):
                if p["source_url"].endswith("/files/bulletin.pdf"):
                    return p["blob_sha256"]
            return None

        self.assertNotEqual(pdf_blob(v2_id), pdf_blob(v3_id))
        self.assertEqual(pdf_blob(v3_id), replacement["sha256"])

        # two blobs exist on disk; both still readable (old version preserved)
        blobs = list(self.conn.execute("SELECT sha256 FROM blobs"))
        self.assertGreaterEqual(len(blobs), 4)  # 2 bodies + csv + 2 pdfs

    def test_blob_shared_but_provenance_separate(self):
        # publish v2 (all good)
        self.admin("/__admin__/correct-body",
                   {"slug": SLUG, "body": self.seed_info["body_v2"],
                    "reason": "fix link"})
        r2 = run_full_pipeline(self.conn, self.cfg, self.source_url, SLUG)
        v2_id = r2["fetch"]["version_id"]

        # a body edit that keeps every URL + declared hash identical must
        # still produce a new version, and its csv/pdf downloads reuse blobs
        tweaked = self.seed_info["body_v2"] + "\n（排版微调，附件不变）\n"
        self.admin("/__admin__/correct-body",
                   {"slug": SLUG, "body": tweaked, "reason": "cosmetic"})
        r3 = run_full_pipeline(self.conn, self.cfg, self.source_url, SLUG)
        v3_id = r3["fetch"]["version_id"]
        self.assertNotEqual(v2_id, v3_id)

        p2 = {(p["link_index"]): p for p in
              db.get_provenance(self.conn, v2_id)}
        p3 = {(p["link_index"]): p for p in
              db.get_provenance(self.conn, v3_id)}
        for idx in p2:
            self.assertEqual(p2[idx]["blob_sha256"], p3[idx]["blob_sha256"])
            # separate source records, separate jobs
            self.assertNotEqual(p2[idx]["id"], p3[idx]["id"])
            self.assertNotEqual(p2[idx]["download_job_id"],
                                p3[idx]["download_job_id"])
            self.assertEqual(p2[idx]["version_id"], v2_id)
            self.assertEqual(p3[idx]["version_id"], v3_id)

        # reuse event recorded, only one physical copy of the csv blob
        reuse_events = list(self.conn.execute(
            "SELECT * FROM events WHERE kind='blob-reused'"))
        self.assertGreaterEqual(len(reuse_events), 1)
        csv_blob = p2[0]["blob_sha256"]
        path = self.cfg.blob_path(csv_blob)
        self.assertTrue(path.exists())
        copies = list(self.cfg.blob_dir.rglob(csv_blob))
        self.assertEqual(len(copies), 1)

    def test_identical_refetch_does_not_create_version(self):
        first = fetch_body(self.conn, self.cfg, self.source_url, SLUG)
        again = fetch_body(self.conn, self.cfg, self.source_url, SLUG)
        self.assertFalse(again.created)
        self.assertEqual(first.version_id, again.version_id)

    def test_failed_version_can_retry_without_duplicate_provenance(self):
        locked = fetch_body(self.conn, self.cfg, self.source_url, SLUG)
        vid = locked.version_id
        first = download_version(self.conn, self.cfg, vid, max_attempts=1)
        self.assertEqual(first["status"], "failed")
        rows_after_first = self.conn.execute(
            "SELECT COUNT(*) AS n FROM attachment_provenance "
            "WHERE version_id=?", (vid,)).fetchone()["n"]

        # second run must reuse provenance rows, not violate the UNIQUE
        # constraint or accumulate duplicates; a new job is still recorded
        second = download_version(self.conn, self.cfg, vid, max_attempts=1)
        self.assertEqual(second["status"], "failed")  # v1 link still 404
        rows_after_second = self.conn.execute(
            "SELECT COUNT(*) AS n FROM attachment_provenance "
            "WHERE version_id=?", (vid,)).fetchone()["n"]
        self.assertEqual(rows_after_first, rows_after_second)

        # but each attempt produced its own immutable job row
        job_count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM download_jobs WHERE version_id=?",
            (vid,)).fetchone()["n"]
        self.assertEqual(job_count, rows_after_second * 2)


if __name__ == "__main__":
    unittest.main()
