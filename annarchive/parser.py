"""Parse an announcement body into frozen attachment references.

Supported body syntax (fictional but explicit, plain Markdown):

    # Title

    Some prose with an [attachment](http://host/files/a.pdf).

    <!-- checksums
    files/a.pdf = 9f2c... sha256
    files/b.csv = <sha256> sha256
    -->

The checksum block maps a *link target* (absolute URL or relative path) to a
declared SHA-256.  Relative link targets are resolved against the body URL.
The parser is pure: given identical bytes it always returns identical refs,
which is what makes a locked version reproducible.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<target>[^)\s]+)\)")
_CHECK_LINE_RE = re.compile(
    r"^\s*(?P<target>\S+)\s*=\s*(?P<digest>[0-9a-fA-F]{64})\s+sha256\s*$",
    re.MULTILINE)
_CHECKSUM_BLOCK_RE = re.compile(
    r"<!--\s*checksums\s*(?P<body>.*?)-->", re.DOTALL)


@dataclass(frozen=True)
class AttachmentRef:
    link_index: int
    label: str
    source_url: str
    declared_sha256: str | None

    def to_dict(self) -> dict:
        return {
            "link_index": self.link_index,
            "label": self.label,
            "source_url": self.source_url,
            "declared_sha256": self.declared_sha256,
        }


def _is_http_url(target: str) -> bool:
    return urlparse(target).scheme in ("http", "https")


def parse_body(body: str, body_url: str) -> list[AttachmentRef]:
    """Extract attachment references in document order."""
    declared: dict[str, str] = {}
    for block in _CHECKSUM_BLOCK_RE.finditer(body):
        for line in block.group("body").splitlines():
            m = _CHECK_LINE_RE.match(line)
            if m:
                declared[m.group("target").strip()] = \
                    m.group("digest").lower()

    # the checksum block may key entries by relative path while links use
    # absolute URLs (or vice versa); index every key by its path tail so
    # "files/a.pdf" matches "http://host/files/a.pdf"
    by_tail: dict[str, str] = {}
    for key, digest in declared.items():
        tail = urlparse(key).path.lstrip("/") if _is_http_url(key) else key
        by_tail.setdefault(tail, digest)

    def lookup(target: str, absolute: str) -> str | None:
        if target in declared:
            return declared[target]
        tail = urlparse(absolute).path.lstrip("/")
        return by_tail.get(tail)

    refs: list[AttachmentRef] = []
    seen_urls: set[str] = set()
    for m in _LINK_RE.finditer(body):
        target = m.group("target")
        label = m.group("label").strip()
        url = target if _is_http_url(target) else urljoin(body_url, target)
        declared_hash = lookup(target, url)
        # one reference per distinct target URL, in first-seen document order
        if url in seen_urls:
            continue
        seen_urls.add(url)
        refs.append(AttachmentRef(
            link_index=len(refs), label=label, source_url=url,
            declared_sha256=declared_hash))
    return refs
