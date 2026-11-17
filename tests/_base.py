"""Shared test fixtures: real simulator on an ephemeral port, real archiver
data directory, nothing mocked at the HTTP layer."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from annarchive import db
from annarchive.cli import Config
from annarchive.seed import SLUG, seed_simulator
from annarchive.simulator import make_server


class SimCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sim_root = Path(tempfile.mkdtemp(prefix="test-sim-"))
        self.data_root = Path(tempfile.mkdtemp(prefix="test-archive-"))
        self.server = make_server("127.0.0.1", 0, self.sim_root)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.source_url = f"{self.base_url}/announcements/{SLUG}"
        self.seed_info = seed_simulator(self.base_url)
        self.cfg = Config.from_root(self.data_root)
        db.init_db(self.cfg.db_path)
        self.conn = db.connect(self.cfg.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()

    def admin(self, route: str, payload: dict) -> dict:
        import json
        import urllib.request
        req = urllib.request.Request(
            self.base_url + route,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    @property
    def hashes(self) -> dict[str, str]:
        return self.seed_info["hashes"]
