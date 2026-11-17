"""Small HTTP layer built on urllib.

Every request records request headers, response headers, status and final
URL so the provenance rows contain real network evidence.  Downloads stream
to a caller-provided path; nothing untrusted is imported or executed.
"""
from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None,
                 headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    request_headers: dict[str, str]
    response_headers: dict[str, str]
    body: bytes


def _headers_dict(hdrs) -> dict[str, str]:
    return {k: v for k, v in hdrs.items()}


def http_get(url: str, timeout: float = 20.0) -> FetchResult:
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": "annarchive/1.0 (+provenance-archiver)",
        "Accept": "*/*",
    })
    request_headers = dict(req.header_items())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return FetchResult(
                url=url,
                final_url=resp.geturl(),
                status=resp.status,
                request_headers=request_headers,
                response_headers=_headers_dict(resp.headers),
                body=body,
            )
    except urllib.error.HTTPError as exc:
        raise HttpError(f"HTTP {exc.code} for {url}", exc.code,
                        _headers_dict(exc.headers)) from exc
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise HttpError(f"network error for {url}: {exc}") from exc


def http_download(url: str, dest: Path, timeout: float = 30.0,
                  chunk_size: int = 64 * 1024,
                  on_bytes: Callable[[int], None] | None = None,
                  ) -> FetchResult:
    """Stream ``url`` to ``dest``.  Metadata is returned like :func:`http_get`."""
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": "annarchive/1.0 (+provenance-archiver)",
        "Accept": "*/*",
    })
    request_headers = dict(req.header_items())
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            final_url = resp.geturl()
            response_headers = _headers_dict(resp.headers)
            total = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
                    if on_bytes:
                        on_bytes(total)
            return FetchResult(
                url=url, final_url=final_url, status=status,
                request_headers=request_headers,
                response_headers=response_headers, body=b"")
    except urllib.error.HTTPError as exc:
        raise HttpError(f"HTTP {exc.code} for {url}", exc.code,
                        _headers_dict(exc.headers)) from exc
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise HttpError(f"network error for {url}: {exc}") from exc
