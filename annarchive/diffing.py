"""Version-to-version comparison: body unified diff plus reference changes."""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from . import db


@dataclass
class RefChange:
    link_index: int
    label: str
    kind: str                 # added | removed | url-changed | hash-changed | unchanged
    left_url: str | None = None
    right_url: str | None = None
    left_hash: str | None = None
    right_hash: str | None = None


@dataclass
class VersionDiff:
    left_version_no: int
    right_version_no: int
    body_diff: list[str] = field(default_factory=list)
    ref_changes: list[RefChange] = field(default_factory=list)
    body_changed: bool = False


def _body_text(conn, version_row) -> str:
    blob = db.get_blob(conn, version_row["body_blob_sha256"])
    if not blob:
        return ""
    with open(blob["stored_path"], "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def diff_versions(conn, left_id: int, right_id: int) -> VersionDiff:
    left = db.get_version(conn, left_id)
    right = db.get_version(conn, right_id)
    if left is None or right is None:
        raise ValueError("unknown version id")

    left_text = _body_text(conn, left)
    right_text = _body_text(conn, right)
    body_diff = list(difflib.unified_diff(
        left_text.splitlines(keepends=False),
        right_text.splitlines(keepends=False),
        fromfile=f"v{left['version_no']}",
        tofile=f"v{right['version_no']}", lineterm=""))

    left_refs = {r["link_index"]: r for r in db.get_refs(conn, left_id)}
    right_refs = {r["link_index"]: r for r in db.get_refs(conn, right_id)}
    changes: list[RefChange] = []
    for idx in sorted(set(left_refs) | set(right_refs)):
        a, b = left_refs.get(idx), right_refs.get(idx)
        if a and not b:
            changes.append(RefChange(idx, a["label"], "removed",
                                     left_url=a["source_url"],
                                     left_hash=a["declared_sha256"]))
        elif b and not a:
            changes.append(RefChange(idx, b["label"], "added",
                                     right_url=b["source_url"],
                                     right_hash=b["declared_sha256"]))
        else:
            if a["source_url"] != b["source_url"]:
                kind = "url-changed"
            elif a["declared_sha256"] != b["declared_sha256"]:
                kind = "hash-changed"
            else:
                kind = "unchanged"
            changes.append(RefChange(
                idx, b["label"], kind, left_url=a["source_url"],
                right_url=b["source_url"], left_hash=a["declared_sha256"],
                right_hash=b["declared_sha256"]))

    return VersionDiff(
        left_version_no=left["version_no"],
        right_version_no=right["version_no"],
        body_diff=body_diff, ref_changes=changes,
        body_changed=bool(body_diff))
