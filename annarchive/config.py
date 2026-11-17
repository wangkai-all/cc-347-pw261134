"""Filesystem layout and configuration objects.

Untrusted downloaded bytes live only under ``data/blobs`` (final),
``data/inflight`` (downloads in progress) and ``data/quarantine`` (bytes that
failed verification).  Application code never imports from those directories;
paths are always passed explicitly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    db_path: Path
    blob_dir: Path
    inflight_dir: Path
    quarantine_dir: Path
    export_dir: Path

    @classmethod
    def from_root(cls, root: str | os.PathLike[str] | Path) -> "Config":
        data_dir = Path(root).resolve()
        cfg = cls(
            data_dir=data_dir,
            db_path=data_dir / "archive.db",
            blob_dir=data_dir / "blobs",
            inflight_dir=data_dir / "inflight",
            quarantine_dir=data_dir / "quarantine",
            export_dir=data_dir / "exports",
        )
        for d in (cfg.blob_dir, cfg.inflight_dir, cfg.quarantine_dir,
                  cfg.export_dir):
            d.mkdir(parents=True, exist_ok=True)
        return cfg

    def blob_path(self, sha256: str) -> Path:
        # sharded: blobs/ab/cd/abcdef... so a single directory stays small
        return self.blob_dir / sha256[:2] / sha256[2:4] / sha256

    def inflight_path(self, job_id: int) -> Path:
        return self.inflight_dir / f"job-{job_id}.part"

    def quarantine_path(self, job_id: int, observed_sha256: str) -> Path:
        safe = safe_digest(observed_sha256)
        return self.quarantine_dir / f"job-{job_id}-{safe}.bin"


def safe_digest(sha: str) -> str:
    """Keep quarantine filenames filesystem-safe even for malformed digests."""
    return "".join(c for c in sha if c.isalnum())[:64] or "unknown"
