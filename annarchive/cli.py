"""Command line entry point:

    python -m annarchive <command> [options]

Run ``python -m annarchive --help`` for the full list.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

from . import db
from .config import Config
from .exporter import build_export_zip
from .pipeline import (download_version, fetch_body, publish_version,
                       run_full_pipeline)
from .seed import SLUG, initial_files, seed_simulator
from .simulator import make_server as make_sim_server


def _connect(cfg: Config):
    db.init_db(cfg.db_path)
    return db.connect(cfg.db_path)


# ------------------------------------------------------------------ commands

def cmd_simulator(args) -> None:
    server = make_sim_server(args.host, args.port, Path(args.sim_root),
                             verbose=args.verbose)
    print(f"simulator on http://{args.host}:{args.port} "
          f"(state: {args.sim_root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def cmd_seed(args) -> None:
    info = seed_simulator(args.base_url)
    print(json.dumps({"seeded": SLUG, "base_url": args.base_url,
                      "hashes": info["hashes"]}, ensure_ascii=False, indent=2))


def cmd_fetch(args) -> None:
    cfg = Config.from_root(args.data)
    conn = _connect(cfg)
    try:
        outcome = fetch_body(conn, cfg, args.url, args.slug)
        print(json.dumps(outcome.__dict__, ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_download(args) -> None:
    cfg = Config.from_root(args.data)
    conn = _connect(cfg)
    try:
        print(json.dumps(
            download_version(conn, cfg, args.version_id,
                             max_attempts=args.attempts),
            ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_publish(args) -> None:
    cfg = Config.from_root(args.data)
    conn = _connect(cfg)
    try:
        print(json.dumps(publish_version(conn, args.version_id),
                         ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_pipeline(args) -> None:
    cfg = Config.from_root(args.data)
    conn = _connect(cfg)
    try:
        print(json.dumps(run_full_pipeline(conn, cfg, args.url, args.slug),
                         ensure_ascii=False, indent=2, default=str))
    finally:
        conn.close()


def cmd_export(args) -> None:
    cfg = Config.from_root(args.data)
    conn = _connect(cfg)
    try:
        path = build_export_zip(conn, cfg, args.version_id)
        print(str(path))
    finally:
        conn.close()


def cmd_events(args) -> None:
    cfg = Config.from_root(args.data)
    conn = _connect(cfg)
    try:
        for e in db.list_events(conn, args.limit):
            print(f"{e['ts']}  {e['kind']:22s} {e['entity']}/"
                  f"{e['entity_id'] or '-'}  {e['detail_json']}")
    finally:
        conn.close()


def cmd_web(args) -> None:
    from .webapp import make_server
    cfg = Config.from_root(args.data)
    server = make_server(args.host, args.port, cfg, args.sim_url,
                         verbose=args.verbose)
    print(f"web UI on http://{args.host}:{args.port} (data: {args.data})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def cmd_replace_file(args) -> None:
    """CLI helper for same-URL replacement scenarios."""
    data = Path(args.file).read_bytes() if args.file else \
        initial_files()["__replacement__/files/bulletin.pdf"]
    payload = json.dumps({
        "path": args.path,
        "content_base64": base64.b64encode(data).decode("ascii"),
        "note": args.note,
    }).encode("utf-8")
    req = urllib.request.Request(
        args.base_url.rstrip("/") + "/__admin__/replace-file",
        data=payload, headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(resp.read().decode("utf-8"))


def cmd_correct_body(args) -> None:
    body = Path(args.file).read_text(encoding="utf-8")
    payload = json.dumps({"slug": args.slug, "body": body,
                          "reason": args.reason}).encode("utf-8")
    req = urllib.request.Request(
        args.base_url.rstrip("/") + "/__admin__/correct-body",
        data=payload, headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(resp.read().decode("utf-8"))


def cmd_seed_demo(args) -> None:
    """End-to-end demo against a fresh simulator + archive (all real HTTP).

    Starts its own simulator on an ephemeral port, seeds the fictional
    scenario, runs the full pipeline for v1, then performs the two mutation
    scenarios (body correction, same-URL replacement) and re-runs the
    pipeline so the archive ends with three provenance-locked versions.
    """
    import tempfile
    import threading

    from .hashing import sha256_bytes
    from .simulator import make_server

    sim_root = Path(tempfile.mkdtemp(prefix="sim-"))
    data_root = Path(tempfile.mkdtemp(prefix="archive-"))
    sim = make_sim_server("127.0.0.1", 0, sim_root)
    port = sim.server_address[1]
    threading.Thread(target=sim.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    time.sleep(0.2)

    info = seed_simulator(base)
    source_url = f"{base}/announcements/{SLUG}"
    cfg = Config.from_root(data_root)
    conn = _connect(cfg)

    print("== v1: lock -> download (v1 has a wrong PDF link) ==")
    r1 = run_full_pipeline(conn, cfg, source_url, SLUG)
    print(json.dumps(r1, ensure_ascii=False, indent=2, default=str))

    print("== correct body -> v2 ==")
    req = urllib.request.Request(
        base + "/__admin__/correct-body",
        data=json.dumps({"slug": SLUG, "body": info["body_v2"],
                         "reason": "fix pdf link"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()
    r2 = run_full_pipeline(conn, cfg, source_url, SLUG)
    print(json.dumps(r2["fetch"], ensure_ascii=False, indent=2))

    print("== same-URL replacement -> v3 ==")
    new_pdf = initial_files()["__replacement__/files/bulletin.pdf"]
    req = urllib.request.Request(
        base + "/__admin__/replace-file",
        data=json.dumps({
            "path": "/files/bulletin.pdf",
            "content_base64": base64.b64encode(new_pdf).decode(),
            "note": "rev B at same URL",
        }).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()
    req = urllib.request.Request(
        base + "/__admin__/correct-body",
        data=json.dumps({"slug": SLUG, "body": info["body_v3"],
                         "reason": "hash updated for rev B"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()
    r3 = run_full_pipeline(conn, cfg, source_url, SLUG)
    print(json.dumps(r3["fetch"], ensure_ascii=False, indent=2))

    print("== blob sharing / provenance separation ==")
    rows = list(conn.execute(
        "SELECT version_id, source_url, blob_sha256, download_job_id "
        "FROM attachment_provenance ORDER BY id"))
    for row in rows:
        print(dict(row))
    print("data root:", data_root)
    sim.shutdown()


# ------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="annarchive",
                                description="pointwise announcement archiver")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("simulator", help="run the source simulator")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--sim-root", required=True)
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_simulator)

    s = sub.add_parser("seed", help="seed fictional scenario into simulator")
    s.add_argument("--base-url", required=True)
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("fetch", help="stage 1: fetch and lock body")
    s.add_argument("--data", default="./var/data")
    s.add_argument("--url", required=True)
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("download", help="stage 2: download + verify refs")
    s.add_argument("--data", default="./var/data")
    s.add_argument("--version-id", type=int, required=True)
    s.add_argument("--attempts", type=int, default=3)
    s.set_defaults(func=cmd_download)

    s = sub.add_parser("publish", help="stage 3: publish verified version")
    s.add_argument("--data", default="./var/data")
    s.add_argument("--version-id", type=int, required=True)
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("pipeline", help="run all three stages in order")
    s.add_argument("--data", default="./var/data")
    s.add_argument("--url", required=True)
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_pipeline)

    s = sub.add_parser("export", help="export raw evidence zip")
    s.add_argument("--data", default="./var/data")
    s.add_argument("--version-id", type=int, required=True)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("events", help="print audit events")
    s.add_argument("--data", default="./var/data")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("web", help="run the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--data", default="./var/data")
    s.add_argument("--sim-url", default="http://127.0.0.1:8080")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_web)

    s = sub.add_parser("replace-file", help="admin: same-URL file replacement")
    s.add_argument("--base-url", required=True)
    s.add_argument("--path", default="/files/bulletin.pdf")
    s.add_argument("--file", help="local file; default = seeded rev B bytes")
    s.add_argument("--note", default="cli replacement")
    s.set_defaults(func=cmd_replace_file)

    s = sub.add_parser("correct-body", help="admin: replace announcement body")
    s.add_argument("--base-url", required=True)
    s.add_argument("--slug", default=SLUG)
    s.add_argument("--file", required=True)
    s.add_argument("--reason", default="cli correction")
    s.set_defaults(func=cmd_correct_body)

    s = sub.add_parser("seed-demo",
                       help="self-contained end-to-end demo (temp dirs)")
    s.set_defaults(func=cmd_seed_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
