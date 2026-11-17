import io
import json
import zipfile

from annarchive import db
from annarchive.diffing import diff_versions
from annarchive.exporter import build_export_zip
from annarchive.pipeline import run_full_pipeline
from annarchive.seed import SLUG
from tests._base import SimCase


class EvidenceTests(SimCase):
    def _three_versions(self):
        run_full_pipeline(self.conn, self.cfg, self.source_url, SLUG)
        self.admin("/__admin__/correct-body",
                   {"slug": SLUG, "body": self.seed_info["body_v2"],
                    "reason": "fix"})
        run_full_pipeline(self.conn, self.cfg, self.source_url, SLUG)
        versions = db.list_versions(
            self.conn, db.find_announcement(self.conn, SLUG)["id"])
        return [int(v["id"]) for v in versions]

    def test_export_zip_contains_raw_evidence_and_manifest(self):
        ids = self._three_versions()
        v2_id = ids[-1]
        path = build_export_zip(self.conn, self.cfg, v2_id)
        self.assertTrue(path.exists())

        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("events.jsonl", names)
            self.assertTrue(any(n.startswith("body/body-v") for n in names))
            manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(manifest["version"]["version_no"], 2)
            atts = manifest["attachments"]
            self.assertEqual(len(atts), 2)
            for att in atts:
                self.assertEqual(att["outcome"], "verified")
                self.assertEqual(att["observed_sha256"], att["blob_sha256"])
                # provenance is per-version raw evidence
                prov = json.loads(
                    zf.read(att["stored_as"].replace("blob.bin",
                                                     "provenance.json")))
                self.assertEqual(prov["version_id"], v2_id)
                self.assertIn("response-headers.json",
                              [n.split("/")[-1]
                               for n in names if n.startswith("attachments/")])
            # archived bytes hash exactly to what provenance claims
            for att in atts:
                data = zf.read(att["stored_as"])
                import hashlib
                self.assertEqual(hashlib.sha256(data).hexdigest(),
                                 att["blob_sha256"])

    def test_diff_detects_url_change_then_hash_change(self):
        ids = self._three_versions()
        v1_id, v2_id = ids[0], ids[1]
        d12 = diff_versions(self.conn, v1_id, v2_id)
        self.assertTrue(d12.body_changed)
        kinds = {c.link_index: c.kind for c in d12.ref_changes}
        self.assertIn("url-changed", kinds.values())

        # build v3: same URL, replaced bytes, updated declared hash
        import base64
        from annarchive.seed import initial_files
        new_pdf = initial_files()["__replacement__/files/bulletin.pdf"]
        self.admin("/__admin__/replace-file", {
            "path": "/files/bulletin.pdf",
            "content_base64": base64.b64encode(new_pdf).decode("ascii"),
            "note": "rev B"})
        self.admin("/__admin__/correct-body",
                   {"slug": SLUG, "body": self.seed_info["body_v3"],
                    "reason": "hash"})
        run_full_pipeline(self.conn, self.cfg, self.source_url, SLUG)
        versions = db.list_versions(
            self.conn, db.find_announcement(self.conn, SLUG)["id"])
        v3_id = int(versions[-1]["id"])
        d23 = diff_versions(self.conn, v2_id, v3_id)
        pdf_change = next(c for c in d23.ref_changes if c.kind != "unchanged")
        self.assertEqual(pdf_change.kind, "hash-changed")
        self.assertEqual(pdf_change.left_url, pdf_change.right_url)
        self.assertNotEqual(pdf_change.left_hash, pdf_change.right_hash)


if __name__ == "__main__":
    unittest.main()
