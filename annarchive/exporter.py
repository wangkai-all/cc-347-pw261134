"""Export the raw evidence behind a version as a self-contained ZIP.

Layout::

    manifest.json                 - index of everything in the archive
    body/body-v<n>.md             - exact locked body bytes
    headers/body-headers.json     - body response headers as captured
    attachments/<label-safe>/
        provenance.json           - this version's own source record
        request-headers.json
        response-headers.json
        blob.bin                 - verified content-addressed bytes
    events.jsonl                  - audit trail entries touching this version
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

from . import db
from .config import Config


def _safe(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or "attachment"


def build_export_zip(conn, cfg: Config, version_id: int,
                     dest: Path | None = None) -> Path:
    version = db.get_version(conn, version_id)
    if version is None:
        raise ValueError(f"version {version_id} not found")
    ann = conn.execute("SELECT * FROM announcements WHERE id=?",
                       (version["announcement_id"],)).fetchone()
    refs = db.get_refs(conn, version_id)
    provs = db.get_provenance(conn, version_id)
    body_blob = db.get_blob(conn, version["body_blob_sha256"])
    events = list(conn.execute(
        "SELECT * FROM events WHERE entity_id=? OR "
        "detail_json LIKE ? ORDER BY id",
        (str(version_id), f'%"version_id": {version_id}%')))

    dest = dest or (cfg.export_dir /
                    f"{ann['slug']}-v{version['version_no']}-evidence.zip")
    dest.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "announcement": {"slug": ann["slug"], "title": ann["title"],
                         "source_url": ann["source_url"]},
        "version": {"version_no": version["version_no"],
                    "status": version["status"],
                    "fetched_at": version["fetched_at"],
                    "locked_at": version["locked_at"],
                    "body_url": version["body_url"],
                    "body_sha256": version["body_blob_sha256"],
                    "body_status": version["body_status"],
                    "fail_reason": version["fail_reason"]},
        "attachments": [],
    }

    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if body_blob:
            zf.write(body_blob["stored_path"],
                     f"body/body-v{version['version_no']}.md")
        zf.writestr(
            f"headers/body-headers.json",
            json.dumps(json.loads(version["body_headers_json"]),
                       ensure_ascii=False, indent=2))

        prov_by_ref = {p["ref_id"]: p for p in provs}
        for ref in refs:
            p = prov_by_ref.get(ref["id"])
            folder = f"attachments/{ref['link_index']:02d}-{_safe(ref['label'])}"
            entry = {
                "link_index": ref["link_index"], "label": ref["label"],
                "source_url": ref["source_url"],
                "declared_sha256": ref["declared_sha256"],
            }
            if p:
                zf.writestr(f"{folder}/provenance.json",
                            json.dumps(dict(p), ensure_ascii=False, indent=2))
                zf.writestr(
                    f"{folder}/request-headers.json",
                    json.dumps(json.loads(p["request_headers_json"]),
                               ensure_ascii=False, indent=2))
                zf.writestr(
                    f"{folder}/response-headers.json",
                    json.dumps(json.loads(p["response_headers_json"]),
                               ensure_ascii=False, indent=2))
                entry.update({
                    "outcome": p["outcome"],
                    "observed_sha256": p["observed_sha256"],
                    "blob_sha256": p["blob_sha256"],
                    "status_code": p["status_code"],
                    "attempts": p["attempts"],
                    "download_job_id": p["download_job_id"],
                    "final_url": p["final_url"],
                    "bytes_received": p["bytes_received"],
                })
                if p["blob_sha256"]:
                    blob = db.get_blob(conn, p["blob_sha256"])
                    if blob:
                        zf.write(blob["stored_path"], f"{folder}/blob.bin")
                        entry["stored_as"] = f"{folder}/blob.bin"
            manifest["attachments"].append(entry)

        zf.writestr("events.jsonl",
                    "\n".join(json.dumps(dict(e), ensure_ascii=False)
                              for e in events))
        zf.writestr("manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))
    db.log_event(conn, "evidence-exported", "version", version_id,
                 path=str(dest), attachments=len(refs))
    return dest


def export_zip_bytes(conn, cfg: Config, version_id: int) -> io.BytesIO:
    """In-memory variant for the web UI's streaming download."""
    buffer = io.BytesIO()
    path = build_export_zip(conn, cfg, version_id)
    with open(path, "rb") as fh:
        buffer.write(fh.read())
    buffer.seek(0)
    return buffer
