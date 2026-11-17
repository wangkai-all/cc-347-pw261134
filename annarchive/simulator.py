"""Source-site simulator.

A tiny threaded HTTP server that pretends to be an announcement publisher.
It exists so the archiver can be exercised against *real* HTTP downloads and
real mid-flight mutations, without depending on any external service.

Capabilities required by the brief:

* ``GET  /announcements/<slug>``      - announcement body (Markdown)
* ``GET  /files/<name>``              - attachment bytes at a stable URL
* ``POST /__admin__/correct-body``    - replace the body *while a download
                                        is in progress*
* ``POST /__admin__/replace-file``    - replace attachment bytes at the very
                                        same URL (old and new keep distinct
                                        content ids and hashes)
* torn reads: ``GET /files/<name>?torn_after=N&torn_to=<id>`` streams N
  bytes of the current file then the remainder of the replacement file in
  one chunked response — the deterministic version of "the file was swapped
  halfway through the download".
* ``?chunk_delay=seconds`` slows each chunk so tests can interleave an admin
  mutation with an in-flight download.

All state lives under a simulator root directory passed in by the caller; the
simulator never touches the archiver's data directory.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .hashing import sha256_bytes, sha256_file

_STATE_FILE = "state.json"
_CONTENT_DIR = "content"


class SimState:
    """File-backed, lock-protected simulator state."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.content_dir = self.root / _CONTENT_DIR
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / _STATE_FILE
        self.lock = threading.RLock()
        if not self.state_path.exists():
            self._write({
                "announcements": {},
                "files": {},
                "events": [],
            })

    # ------------------------------------------------------------- storage
    def _write(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.state_path)

    def read(self) -> dict:
        with self.lock:
            return json.loads(self.state_path.read_text(encoding="utf-8"))

    def record_event(self, kind: str, **detail) -> None:
        with self.lock:
            state = self.read()
            state["events"].append({"ts": time.time(), "kind": kind,
                                    "detail": detail})
            self._write(state)

    def add_content(self, data: bytes) -> str:
        """Store content bytes, keyed by their own sha256 (deduplicated)."""
        digest = sha256_bytes(data)
        path = self.content_dir / f"{digest}.bin"
        if not path.exists():
            path.write_bytes(data)
        return digest

    def content_path(self, content_id: str) -> Path:
        return self.content_dir / f"{content_id}.bin"

    # --------------------------------------------------------- admin ops
    def put_body(self, slug: str, body: str, reason: str) -> dict:
        with self.lock:
            state = self.read()
            content_id = self.add_content(body.encode("utf-8"))
            ann = state["announcements"].setdefault(slug, {"history": []})
            ann["current"] = content_id
            ann["media_type"] = "text/markdown; charset=utf-8"
            ann["history"].append({"content_id": content_id,
                                   "reason": reason, "ts": time.time()})
            self._write(state)
            self.record_event("correct-body", slug=slug, reason=reason,
                              content_id=content_id)
            return {"slug": slug, "content_id": content_id,
                    "sha256": content_id}

    def replace_file(self, url_path: str, data: bytes,
                     note: str = "") -> dict:
        """Replace bytes at a stable URL.  Old content stays on disk."""
        with self.lock:
            state = self.read()
            content_id = self.add_content(data)
            entry = state["files"].setdefault(
                url_path, {"versions": [], "current": None})
            entry["current"] = content_id
            entry["versions"].append(
                {"content_id": content_id, "size": len(data),
                 "sha256": sha256_bytes(data), "ts": time.time(),
                 "note": note})
            self._write(state)
            self.record_event("replace-file", path=url_path,
                              content_id=content_id, note=note)
            return {"path": url_path, "content_id": content_id,
                    "sha256": entry["versions"][-1]["sha256"],
                    "size": len(data)}

    def resolve_file(self, url_path: str) -> dict | None:
        state = self.read()
        entry = state["files"].get(url_path)
        if not entry:
            return None
        return entry["versions"][-1] if entry["current"] is None else next(
            v for v in entry["versions"] if v["content_id"] == entry["current"])


# ---------------------------------------------------------------- HTTP layer

class _Handler(BaseHTTPRequestHandler):
    server_version = "annarchive-sim/1.0"

    @property
    def sim(self) -> SimState:
        return self.server.sim_state  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # quiet by default
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- helpers
    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _write_chunk(self, data: bytes) -> None:
        # RFC 9112 chunk framing: "<hex length>\r\n" + chunk + "\r\n"
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()
    # ---------------------------------------------------------------- GET
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/health":
            self._send_json(200, {"ok": True})
            return
        if path == "/__admin__/state":
            self._send_json(200, self.sim.read())
            return

        if path.startswith("/announcements/"):
            slug = path[len("/announcements/"):]
            state = self.sim.read()
            ann = state["announcements"].get(slug)
            if not ann:
                self._send_json(404, {"error": "unknown announcement"})
                return
            content_id = ann["current"]
            data = (self.sim.content_path(content_id)).read_bytes()
            digest, _ = sha256_file(self.sim.content_path(content_id))
            self.send_response(200)
            self.send_header("Content-Type", ann.get(
                "media_type", "text/markdown; charset=utf-8"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", f'"{digest}"')
            self.send_header("Last-Modified",
                             time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                           time.gmtime()))
            self.send_header("X-Content-Id", content_id)
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/files/"):
            self._serve_file(path, query)
            return

        self._send_json(404, {"error": "not found", "path": path})

    def _serve_file(self, path: str, query: dict) -> None:
        version = self.sim.resolve_file(path)
        if version is None:
            self._send_json(404, {"error": "unknown file", "path": path})
            return

        chunk_delay = float(query.get("chunk_delay", ["0"])[0])
        chunk_size = int(query.get("chunk_size", ["65536"])[0])
        torn_after = query.get("torn_after", [None])[0]
        torn_to = query.get("torn_to", [None])[0]

        current_path = self.sim.content_path(version["content_id"])
        digest, size = sha256_file(current_path)

        if torn_after is None:
            # ordinary stable response
            data = current_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", f'"{digest}"')
            self.send_header("X-Content-Id", version["content_id"])
            self.end_headers()
            offset = 0
            while offset < len(data):
                self.wfile.write(data[offset:offset + chunk_size])
                self.wfile.flush()
                offset += chunk_size
                if chunk_delay:
                    time.sleep(chunk_delay)
            return

        # torn response: chunked, first N bytes of current content, then the
        # replacement content (re-opened by id, or by path if no id given).
        after = int(torn_after)
        if torn_to:
            swap_path = self.sim.content_path(torn_to)
        else:
            swap_path = current_path  # re-open later to pick up admin swaps
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Torn", f"after={after}")
        self.send_header("X-Content-Id-Start", version["content_id"])
        self.end_headers()

        with open(current_path, "rb") as fh:
            remaining = after
            while remaining > 0:
                chunk = fh.read(min(chunk_size, remaining))
                if not chunk:
                    break
                self._write_chunk(chunk)
                remaining -= len(chunk)
                if chunk_delay:
                    time.sleep(chunk_delay)

        with open(swap_path, "rb") as fh2:
            # always restart from the beginning: with an explicit torn_to id
            # this reads the replacement file whole; without it, re-opening
            # the same path here picks up any admin swap that landed mid-read
            fh2.seek(0)
            while True:
                chunk = fh2.read(chunk_size)
                if not chunk:
                    break
                self._write_chunk(chunk)
                if chunk_delay:
                    time.sleep(chunk_delay)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        self.sim.record_event("torn-served", path=path, torn_after=after,
                              torn_to=torn_to)

    # --------------------------------------------------------------- POST
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/__admin__/correct-body":
            payload = self._read_json()
            result = self.sim.put_body(
                payload["slug"], payload["body"],
                payload.get("reason", "correction"))
            self._send_json(200, result)
            return
        if path == "/__admin__/replace-file":
            payload = self._read_json()
            data = base64.b64decode(payload["content_base64"])
            result = self.sim.replace_file(
                payload["path"], data, payload.get("note", ""))
            self._send_json(200, result)
            return
        self._send_json(404, {"error": "unknown admin route", "path": path})


def make_server(host: str, port: int, sim_root: Path,
                verbose: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.sim_state = SimState(sim_root)  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Run the source simulator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--sim-root", required=True)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    server = make_server(args.host, args.port, Path(args.sim_root),
                         verbose=args.verbose)
    print(f"simulator listening on http://{args.host}:{args.port} "
          f"(state under {args.sim_root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
