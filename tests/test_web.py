import json
import threading
import time
import urllib.request

from annarchive import db
from annarchive.config import Config
from annarchive.pipeline import run_full_pipeline
from annarchive.seed import SLUG
from annarchive.webapp import make_server
from tests._base import SimCase


class WebTests(SimCase):
    def setUp(self):
        super().setUp()
        self.web = make_server("127.0.0.1", 0, self.cfg, self.base_url)
        self.web_port = self.web.server_address[1]
        self.web_url = f"http://127.0.0.1:{self.web_port}"
        self.web_thread = threading.Thread(target=self.web.serve_forever,
                                           daemon=True)
        self.web_thread.start()
        time.sleep(0.1)

    def tearDown(self):
        self.web.shutdown()
        self.web.server_close()
        super().tearDown()

    def _get(self, path: str) -> tuple[int, bytes, dict]:
        req = urllib.request.Request(self.web_url + path)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def test_pages_render_and_refetch_job_runs(self):
        for path in ("/", "/sim", "/jobs", "/events"):
            status, body, _ = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("公告取证归档".encode(), body)

        # trigger a real pipeline through the web job queue; urllib follows
        # the 303 by default, so disable redirects to assert it directly
        import urllib.parse
        form = urllib.parse.urlencode({
            "source_url": self.source_url, "slug": SLUG}).encode()
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
        # custom opener that does not follow redirects
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        no_redirect = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(self.web_url + "/actions/refetch",
                                     data=form, method="POST")
        try:
            no_redirect.open(req, timeout=10)
            self.fail("expected HTTP 303")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 303)
            self.assertTrue(exc.headers["Location"].startswith("/"))

        # poll the JSON API until the job settles
        for _ in range(50):
            status, body, _ = self._get("/api/jobs")
            jobs = json.loads(body)
            if jobs and jobs[0]["state"] in ("done", "error"):
                break
            time.sleep(0.1)
        self.assertEqual(jobs[0]["state"], "done", jobs[0])

        # the announcement now appears on the home page
        status, body, _ = self._get("/")
        self.assertIn(SLUG.encode(), body)

        ann = db.find_announcement(db.connect(self.cfg.db_path), SLUG)
        versions = db.list_versions(db.connect(self.cfg.db_path), ann["id"])
        vid = versions[0]["id"]
        status, body, headers = self._get(f"/versions/{vid}")
        self.assertEqual(status, 200)
        self.assertIn("锁定正文".encode(), body)

        # graph + diff endpoints
        status, _, _ = self._get(f"/graph?slug={SLUG}")
        self.assertEqual(status, 200)
        status, _, _ = self._get(f"/diff?a={vid}&b={vid}")
        self.assertEqual(status, 200)

    def test_export_endpoint_streams_zip(self):
        run_full_pipeline(self.conn, self.cfg, self.source_url, SLUG)
        self.admin("/__admin__/correct-body",
                   {"slug": SLUG, "body": self.seed_info["body_v2"],
                    "reason": "fix"})
        r = run_full_pipeline(self.conn, self.cfg, self.source_url, SLUG)
        vid = r["fetch"]["version_id"]
        status, body, headers = self._get(f"/versions/{vid}/export")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertTrue(headers["Content-Disposition"].endswith(".zip\""))
        import zipfile
        with zipfile.ZipFile(__import__("io").BytesIO(body)) as zf:
            self.assertIn("manifest.json", zf.namelist())


if __name__ == "__main__":
    unittest.main()
