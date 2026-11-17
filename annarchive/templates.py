"""Minimal HTML rendering with stdlib string templates only."""
from __future__ import annotations

import html
import json

_CSS = """
:root { --bg:#0f1420; --panel:#171e2e; --line:#27314a; --ink:#e6ebf5;
        --muted:#93a0b8; --accent:#5db3ff; --ok:#46c98e; --warn:#f0b35e;
        --bad:#f06e7a; }
* { box-sizing:border-box; }
body { margin:0; font:14px/1.6 -apple-system,Segoe UI,Roboto,'Helvetica Neue',
       'PingFang SC','Microsoft YaHei',sans-serif; background:var(--bg);
       color:var(--ink); }
header { padding:14px 24px; border-bottom:1px solid var(--line);
         display:flex; gap:18px; align-items:center;
         background:var(--panel); }
header a { color:var(--muted); text-decoration:none; }
header a:hover { color:var(--accent); }
header .brand { color:var(--ink); font-weight:700; font-size:16px; }
main { max-width:1180px; margin:0 auto; padding:24px; }
h1 { font-size:20px; margin:0 0 16px; }
h2 { font-size:16px; margin:24px 0 10px; color:var(--accent); }
.panel { background:var(--panel); border:1px solid var(--line);
         border-radius:10px; padding:16px 18px; margin-bottom:16px; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line);
        vertical-align:top; }
th { color:var(--muted); font-weight:600; }
code,.mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
             font-size:12px; word-break:break-all; }
a { color:var(--accent); }
.badge { display:inline-block; padding:2px 9px; border-radius:999px;
         font-size:12px; border:1px solid var(--line); }
.badge.published { color:var(--ok); border-color:var(--ok); }
.badge.locked,.badge.downloading,.badge.downloaded { color:var(--accent); }
.badge.failed { color:var(--bad); border-color:var(--bad); }
.badge.verified { color:var(--ok); border-color:var(--ok); }
.badge.mismatch,.badge.http_error,.badge.network_error { color:var(--bad);
         border-color:var(--bad); }
.badge.pending { color:var(--warn); border-color:var(--warn); }
.btn { display:inline-block; background:var(--accent); color:#08111f;
       padding:7px 14px; border-radius:8px; text-decoration:none;
       font-weight:600; border:0; cursor:pointer; font-size:13px; }
.btn.secondary { background:transparent; color:var(--accent);
       border:1px solid var(--accent); }
.btn.danger { background:var(--bad); color:#1a0c0e; }
.row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
.muted { color:var(--muted); }
pre { background:#0b101b; border:1px solid var(--line); border-radius:8px;
      padding:12px; overflow:auto; font-size:12px; }
pre .add { color:var(--ok); } pre .del { color:var(--bad); }
pre .hunk { color:var(--warn); }
input,select,textarea { background:#0b101b; color:var(--ink);
      border:1px solid var(--line); border-radius:8px; padding:8px 10px;
      font-size:13px; font-family:inherit; }
input[type=text],textarea { width:100%; }
form .field { margin-bottom:12px; }
label { display:block; color:var(--muted); margin-bottom:4px; font-size:12px; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.pill { font-size:11px; color:var(--muted); }
.flash { padding:10px 14px; border-radius:8px; margin-bottom:14px; }
.flash.ok { background:rgba(70,201,142,.12); color:var(--ok); }
.flash.err { background:rgba(240,110,122,.12); color:var(--bad); }
svg rect,svg line,svg path { stroke:var(--line); }
.small { font-size:12px; }
"""


def page(title: str, body: str, flash: str | None = None) -> str:
    flash_html = f'<div class="flash {"err" if flash and "失败" in flash else "ok"}">{html.escape(flash)}</div>' if flash else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · 公告取证归档</title>
<style>{_CSS}</style></head><body>
<header>
  <a class="brand" href="/">公告取证归档</a>
  <a href="/">公告列表</a>
  <a href="/sim">来源模拟器</a>
  <a href="/jobs">后台作业</a>
  <a href="/events">审计事件</a>
</header><main>
{flash_html}
{body}
</main></body></html>"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def badge(status: str) -> str:
    return f'<span class="badge {esc(status)}">{esc(status)}</span>'


def short(sha: str | None, n: int = 12) -> str:
    if not sha:
        return '<span class="muted">—</span>'
    return f'<span class="mono" title="{esc(sha)}">{esc(sha[:n])}…</span>'


def render_diff(diff_lines: list[str]) -> str:
    out = []
    for line in diff_lines:
        cls = ""
        if line.startswith("+") and not line.startswith("+++"):
            cls = "add"
        elif line.startswith("-") and not line.startswith("---"):
            cls = "del"
        elif line.startswith("@@"):
            cls = "hunk"
        out.append(f'<span class="{cls}">{esc(line)}</span>')
    return "<pre>" + "\n".join(out) + "</pre>" if out else \
        '<p class="muted">两版正文完全相同。</p>'


def pretty_json(value) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return esc(value)
    return "<pre>" + esc(json.dumps(value, ensure_ascii=False, indent=2,
                                     sort_keys=True)) + "</pre>"
