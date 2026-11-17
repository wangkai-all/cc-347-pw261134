"""Fictional seed data and deterministic content builders.

Nothing here describes a real organisation.  The scenario: the fictional
*Yunhe Observatory* publishes an observation bulletin.  v1 of the body points
at a mis-linked attachment and contains a corrected checksum block once the
publisher notices; attachment bytes can also be replaced at the same URL.
"""
from __future__ import annotations

import base64
import json
import urllib.request
from dataclasses import dataclass

from .hashing import sha256_bytes

SLUG = "yunhe-bulletin-2026-11"
TITLE = "云河天文台 2026 年 11 月观测公报"


# ------------------------------------------------------------ file builders

def build_bulletin_pdf(edition: str, body_lines: list[str]) -> bytes:
    """A tiny, valid one-page PDF (uncompressed, so content is inspectable)."""
    stream = ("BT /F1 14 Tf 72 760 Td "
              f"({edition}) Tj 0 -22 Td "
              + " 0 -20 Td ".join(f"({line}) Tj" for line in body_lines)
              + " ET").encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    return bytes(out)


def build_schedule_csv(rows: list[tuple[str, str, str]]) -> bytes:
    lines = ["date,object,visibility_window"]
    lines += [f"{d},{obj},{win}" for d, obj, win in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


# ----------------------------------------------------------- fixed contents

def initial_files() -> dict[str, bytes]:
    pdf_v1 = build_bulletin_pdf(
        "Yunhe Observatory Bulletin 2026-11 (draft A)",
        ["Provisional observation schedule.",
         "Checksum block in the announcement governs verification."])
    pdf_v2 = build_bulletin_pdf(
        "Yunhe Observatory Bulletin 2026-11 (rev B)",
        ["Revised narrative: meteor shower peak corrected to Nov 21.",
         "Same download URL, different bytes and hash."])
    csv_v1 = build_schedule_csv([
        ("2026-11-18", "M31 Andromeda", "19:40-04:10"),
        ("2026-11-21", "Leonid meteor shower", "23:00-03:30"),
        ("2026-11-27", "Jupiter occultation", "20:10-21:05"),
    ])
    return {
        "files/bulletin.pdf": pdf_v1,
        "files/schedule.csv": csv_v1,
        # replacement bytes kept aside for same-URL swap scenarios
        "__replacement__/files/bulletin.pdf": pdf_v2,
    }


# ------------------------------------------------------------------ bodies

@dataclass
class BodySpec:
    reason: str
    text: str


def body_v1(base_url: str, hashes: dict[str, str]) -> str:
    """v1: correct schedule link, *wrong* pdf link (typo) and a checksum
    block that already declares the real pdf hash — so the typo is
    discoverable when the body is re-fetched after the publisher corrects it.
    """
    return f"""# {TITLE}

各位观测员：

本期公报包含两项附件。观测计划表 CSV 可直接下载，PDF 公报链接将在更正版中修复。

- [观测计划表 CSV]({base_url}/files/schedule.csv)
- [PDF 公报（链接待更正）]({base_url}/files/bulletin-draft.pdf)

<!-- checksums
files/schedule.csv = {hashes['files/schedule.csv']} sha256
files/bulletin.pdf = {hashes['files/bulletin.pdf']} sha256
-->

发布单位：云河天文台科普联络组（虚构机构）
"""


def body_v2(base_url: str, hashes: dict[str, str]) -> str:
    """v2: corrected PDF link, checksum block unchanged in meaning."""
    return f"""# {TITLE}

各位观测员：

【更正】上一版正文中 PDF 公报链接误写为 bulletin-draft.pdf，现更正为
bulletin.pdf。观测计划表 CSV 链接与校验和不变。

- [观测计划表 CSV]({base_url}/files/schedule.csv)
- [PDF 公报（已更正链接）]({base_url}/files/bulletin.pdf)

<!-- checksums
files/schedule.csv = {hashes['files/schedule.csv']} sha256
files/bulletin.pdf = {hashes['files/bulletin.pdf']} sha256
-->

发布单位：云河天文台科普联络组（虚构机构）
"""


def body_v3(base_url: str, hashes: dict[str, str]) -> str:
    """v3: same PDF URL but bytes replaced — declared hash updated too."""
    return f"""# {TITLE}

各位观测员：

【第二次更正】PDF 公报在原地址更新内容（流星雨峰值表述修订），下载链接
不变，校验和更新如下。观测计划表 CSV 不受影响。

- [观测计划表 CSV]({base_url}/files/schedule.csv)
- [PDF 公报（原地址内容更新）]({base_url}/files/bulletin.pdf)

<!-- checksums
files/schedule.csv = {hashes['files/schedule.csv']} sha256
files/bulletin.pdf = {hashes['__replacement__/files/bulletin.pdf']} sha256
-->

发布单位：云河天文台科普联络组（虚构机构）
"""


# --------------------------------------------------------------- seeding API

def _admin_post(base_url: str, route: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base_url.rstrip("/") + route,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def seed_simulator(base_url: str) -> dict:
    """Load the fictional scenario into a running simulator.

    Returns a dict of content hashes so tests and the CLI can assert against
    the exact declared values.
    """
    files = initial_files()
    hashes = {name: sha256_bytes(data) for name, data in files.items()}

    for path, data in files.items():
        if path.startswith("__replacement__/"):
            continue  # staged, not served yet
        _admin_post(base_url, "/__admin__/replace-file",
                    {"path": "/" + path, "content_base64":
                     base64.b64encode(data).decode("ascii"),
                     "note": "initial publication"})

    v1 = body_v1(base_url, hashes)
    _admin_post(base_url, "/__admin__/correct-body",
                {"slug": SLUG, "body": v1, "reason": "initial publication"})
    return {"hashes": hashes, "body_v1": v1,
            "body_v2": body_v2(base_url, hashes),
            "body_v3": body_v3(base_url, hashes)}
