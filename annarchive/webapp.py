"""Stdlib web application: browse versions, inspect provenance, trigger
re-fetches, compare versions, export evidence and drive the simulator."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db, templates
from .config import Config
from .diffing import diff_versions
from .exporter import build_export_zip
from .jobs import JobRunner
from .pipeline import download_version, fetch_body, publish_version, run_full_pipeline


class WebState:
    def __init__(self, cfg: Config, sim_base_url: str | None = None):
        self.cfg = cfg
        self.sim_base_url = sim_base_url
        self.runner = JobRunner(cfg)
        self.runner.start()


def _sim_admin(sim_base_url: str, route: str, payload: dict) -> dict:
    req = urllib.request.Request(
        sim_base_url.rstrip("/") + route,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


class _WebHandler(BaseHTTPRequestHandler):
    server_version = "annarchive-web/1.0"

    @property
    def state(self) -> WebState:
        return self.server.web_state  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- plumbing
    def _send(self, status: int, content_type: str, body: bytes,
              extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, markup: str, status: int = 200,
              flash: str | None = None, title: str = "公告取证归档") -> None:
        self._send(status, "text/html; charset=utf-8",
                   templates.page(title, markup, flash).encode("utf-8"))

    def _json(self, payload: dict | list, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8",
                   json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _redirect(self, location: str, flash: str | None = None) -> None:
        url = location + (("?flash=" + urllib.parse.quote(flash))
                          if flash else "")
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _query(self) -> dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(parsed.query).items()}

    # ----------------------------------------------------------------- GET
    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/":
                return self._home()
            if path == "/announcements":
                slug = self._query().get("slug")
                if slug:
                    return self._announcement(slug)
                return self._redirect("/")
            if path.startswith("/versions/") and path.endswith("/export"):
                vid = int(path.split("/")[2])
                return self._export(vid)
            if path.startswith("/versions/"):
                return self._version(int(path.rsplit("/", 1)[-1]))
            if path == "/diff":
                return self._diff_page()
            if path == "/graph":
                return self._graph_page()
            if path == "/jobs":
                return self._jobs_page()
            if path == "/events":
                return self._events_page()
            if path == "/sim":
                return self._sim_page()
            if path == "/api/jobs":
                return self._json([self._job_dict(j)
                                   for j in self.state.runner.list_jobs()])
            if path.startswith("/blobs/"):
                digest = path[len("/blobs/"):]
                return self._blob_download(digest)
            self._html("<h1>404</h1>", status=404)
        except Exception as exc:  # noqa: BLE001
            self._html(f"<h1>服务器错误</h1><pre>{templates.esc(exc)}</pre>",
                       status=500)

    # ---------------------------------------------------------------- POST
    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/actions/refetch":
                return self._action_refetch()
            if path == "/actions/download":
                return self._action_download()
            if path == "/actions/publish":
                return self._action_publish()
            if path == "/sim/correct-body":
                return self._sim_correct_body()
            if path == "/sim/replace-file":
                return self._sim_replace_file()
            self._json({"error": "not found"}, 404)
        except urllib.error.URLError as exc:
            self._html(f"<h1>模拟器请求失败</h1><pre>{templates.esc(exc)}</pre>",
                       status=502)
        except Exception as exc:  # noqa: BLE001
            self._html(f"<h1>服务器错误</h1><pre>{templates.esc(exc)}</pre>",
                       status=500)

    # ------------------------------------------------------------- pages
    def _home(self) -> None:
        conn = db.connect(self.state.cfg.db_path)
        try:
            anns = db.list_announcements(conn)
            rows = []
            for a in anns:
                rows.append(
                    "<tr>"
                    f"<td><a href='/announcements?slug={urllib.parse.quote(a['slug'])}'>"
                    f"{templates.esc(a['title'])}</a><br>"
                    f"<span class='pill mono'>{templates.esc(a['slug'])}</span></td>"
                    f"<td class='mono small'>{templates.esc(a['source_url'])}</td>"
                    f"<td>{a['version_count']}</td>"
                    f"<td>{('v' + str(a['published_version'])) if a['published_version'] else '<span class=muted>未发布</span>'}</td>"
                    "</tr>")
            body = f"""
<h1>公告列表</h1>
<div class="panel"><table>
<tr><th>公告</th><th>来源地址</th><th>版本数</th><th>已发布版本</th></tr>
{''.join(rows) or '<tr><td colspan=4 class=muted>尚无公告，先在“来源模拟器”页播种并抓取。</td></tr>'}
</table></div>
<div class="panel"><h2>按来源抓取 / 重抓（阶段一：锁定正文）</h2>
<form method="post" action="/actions/refetch" class="grid2">
  <div class="field"><label>来源 URL</label>
    <input type="text" name="source_url" required
      value="{templates.esc((self.state.sim_base_url or '') + '/announcements/yunhe-bulletin-2026-11')}"></div>
  <div class="field"><label>公告 slug</label>
    <input type="text" name="slug" required value="yunhe-bulletin-2026-11"></div>
  <button class="btn" type="submit">抓取并锁定新版本</button>
  <p class="muted small">正文字节相同则复用既有版本；发现更正会生成不可变新版本，
     在途下载永远绑定旧版本。</p>
</form></div>"""
            self._html(body, flash=self._query().get("flash"))
        finally:
            conn.close()

    def _announcement(self, slug: str) -> None:
        conn = db.connect(self.state.cfg.db_path)
        try:
            ann = db.find_announcement(conn, slug)
            if not ann:
                return self._html("<h1>公告不存在</h1>", status=404)
            versions = db.list_versions(conn, int(ann["id"]))
            vrows = []
            for v in versions:
                provs = db.get_provenance(conn, int(v["id"]))
                ok = sum(1 for p in provs if p["outcome"] == "verified")
                vrows.append(
                    "<tr>"
                    f"<td><a href='/versions/{v['id']}'>v{v['version_no']}</a></td>"
                    f"<td>{templates.badge(v['status'])}</td>"
                    f"<td>{templates.short(v['body_blob_sha256'])}</td>"
                    f"<td>{ok}/{len(provs)}</td>"
                    f"<td class='small muted'>{templates.esc(v['fetched_at'])}</td>"
                    f"<td class='small'>{templates.esc(v['fail_reason'] or '')}</td>"
                    "</tr>")
            ids = [str(v["id"]) for v in versions]
            opts_a = "".join(f"<option value='{i}'>v{n}</option>"
                             for i, n in [(v["id"], v["version_no"])
                                          for v in versions])
            body = f"""
<h1>{templates.esc(ann['title'])}</h1>
<p class="muted mono small">{templates.esc(ann['source_url'])}</p>
<div class="panel"><table>
<tr><th>版本</th><th>状态</th><th>正文哈希</th><th>附件核对</th><th>锁定时间</th><th>失败原因</th></tr>
{''.join(vrows)}
</table></div>
<div class="panel row">
<form method="get" action="/diff">
  <input type="hidden" name="a" value="{ids[0] if ids else ''}">
  <input type="hidden" name="b" value="{ids[-1] if ids else ''}">
  <button class="btn secondary" {'disabled' if len(ids) < 2 else ''}>逐版对照</button></form>
<a class="btn secondary" href="/graph?slug={urllib.parse.quote(slug)}">查看引用关系图</a>
<form method="post" action="/actions/refetch">
  <input type="hidden" name="source_url" value="{templates.esc(ann['source_url'])}">
  <input type="hidden" name="slug" value="{templates.esc(slug)}">
  <button class="btn">重新抓取正文</button></form>
</div>"""
            self._html(body)
        finally:
            conn.close()

    def _version(self, version_id: int) -> None:
        conn = db.connect(self.state.cfg.db_path)
        try:
            v = db.get_version(conn, version_id)
            if not v:
                return self._html("<h1>版本不存在</h1>", status=404)
            ann = conn.execute("SELECT * FROM announcements WHERE id=?",
                               (v["announcement_id"],)).fetchone()
            refs = db.get_refs(conn, version_id)
            provs = {p["ref_id"]: p for p in
                     db.get_provenance(conn, version_id)}
            body_blob = db.get_blob(conn, v["body_blob_sha256"])
            body_text = Path(body_blob["stored_path"]).read_text(
                encoding="utf-8", errors="replace") if body_blob else ""

            rrows = []
            for ref in refs:
                p = provs.get(ref["id"])
                outcome = templates.badge(p["outcome"]) if p else \
                    '<span class="muted">未下载</span>'
                blob_cell = (templates.short(p["blob_sha256"]) +
                             (f' <span class="pill">复用</span>'
                              if p and p["outcome"] == "verified"
                              and self._is_reused(conn, p) else "")) \
                    if p and p["blob_sha256"] else '<span class="muted">—</span>'
                rrows.append(
                    "<tr>"
                    f"<td>{ref['link_index']}</td>"
                    f"<td>{templates.esc(ref['label'])}</td>"
                    f"<td class='mono small'>{templates.esc(ref['source_url'])}</td>"
                    f"<td>{templates.short(ref['declared_sha256'])}</td>"
                    f"<td>{outcome}</td>"
                    f"<td>{templates.short(p['observed_sha256']) if p else '—'}</td>"
                    f"<td>{blob_cell}</td>"
                    f"<td class='small'>{templates.esc(p['error']) if p else ''}</td>"
                    "</tr>")
            body = f"""
<h1>{templates.esc(ann['title'])} · v{v['version_no']}
   {templates.badge(v['status'])}</h1>
<p class="muted small">锁定于 {templates.esc(v['locked_at'])} ·
  正文 URL <span class="mono">{templates.esc(v['body_url'])}</span> ·
  正文哈希 <span class="mono">{templates.esc(v['body_blob_sha256'])}</span></p>
<div class="panel row">
<form method="post" action="/actions/download">
  <input type="hidden" name="version_id" value="{version_id}">
  <button class="btn">下载并核对附件（阶段二）</button></form>
<form method="post" action="/actions/publish">
  <input type="hidden" name="version_id" value="{version_id}">
  <button class="btn secondary">归档发布（阶段三）</button></form>
<a class="btn secondary" href="/versions/{version_id}/export">导出原始证据 ZIP</a>
<a class="btn secondary" href="/announcements?slug={urllib.parse.quote(ann['slug'])}">返回公告</a>
</div>
<div class="panel"><h2>锁定正文（不可变快照）</h2>
<pre>{templates.esc(body_text)}</pre></div>
<div class="panel"><h2>附件引用与逐版本来源记录</h2>
<table><tr><th>#</th><th>名称</th><th>来源 URL（本版独立）</th>
<th>声明 SHA-256</th><th>核对结果</th><th>实测 SHA-256</th>
<th>归档 blob</th><th>错误</th></tr>
{''.join(rrows)}</table>
<p class="muted small">重复附件共用 blob 存储，但每行来源记录（请求/响应头、作业号、
状态码、尝试次数）均归属于本版本，不与其他版本共用。</p></div>
<div class="panel"><h2>正文响应头证据</h2>
{templates.pretty_json(v['body_headers_json'])}</div>"""
            self._html(body)
        finally:
            conn.close()

    @staticmethod
    def _is_reused(conn, prov) -> bool:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM attachment_provenance "
            "WHERE blob_sha256=? AND id<>?",
            (prov["blob_sha256"], prov["id"])).fetchone()
        return row["n"] > 0

    def _diff_page(self) -> None:
        q = self._query()
        conn = db.connect(self.state.cfg.db_path)
        try:
            versions = list(conn.execute(
                "SELECT v.*, a.slug FROM versions v JOIN announcements a "
                "ON a.id=v.announcement_id ORDER BY v.id"))
            options = "".join(
                f"<option value='{v['id']}' {'selected' if str(v['id'])==q.get('a','') else ''}>"
                f"{templates.esc(v['slug'])} v{v['version_no']}</option>"
                for v in versions)
            options_b = "".join(
                f"<option value='{v['id']}' {'selected' if str(v['id'])==q.get('b','') else ''}>"
                f"{templates.esc(v['slug'])} v{v['version_no']}</option>"
                for v in versions)
            body = f"""
<h1>逐版对照</h1>
<div class="panel"><form method="get" action="/diff" class="row">
  <div><label>左版本</label><select name="a">{options}</select></div>
  <div><label>右版本</label><select name="b">{options_b}</select></div>
  <button class="btn" type="submit">对照</button></form></div>"""
            if q.get("a") and q.get("b"):
                d = diff_versions(conn, int(q["a"]), int(q["b"]))
                change_rows = "".join(
                    "<tr>"
                    f"<td>{c.link_index}</td><td>{templates.esc(c.label)}</td>"
                    f"<td>{templates.badge(c.kind)}</td>"
                    f"<td class='mono small'>{templates.esc(c.left_url or '—')}<br>"
                    f"{templates.short(c.left_hash)}</td>"
                    f"<td class='mono small'>{templates.esc(c.right_url or '—')}<br>"
                    f"{templates.short(c.right_hash)}</td></tr>"
                    for c in d.ref_changes)
                body += f"""
<div class="panel"><h2>正文 unified diff（v{d.left_version_no} → v{d.right_version_no}）</h2>
{templates.render_diff(d.body_diff)}</div>
<div class="panel"><h2>引用变化</h2><table>
<tr><th>#</th><th>名称</th><th>变化类型</th><th>左：URL / 声明哈希</th>
<th>右：URL / 声明哈希</th></tr>{change_rows}</table></div>"""
            self._html(body)
        finally:
            conn.close()

    def _graph_page(self) -> None:
        conn = db.connect(self.state.cfg.db_path)
        try:
            slug = self._query().get("slug")
            where = "WHERE a.slug=?" if slug else ""
            params = (slug,) if slug else ()
            versions = list(conn.execute(
                f"SELECT v.*, a.slug, a.title FROM versions v "
                f"JOIN announcements a ON a.id=v.announcement_id {where} "
                "ORDER BY v.id", params))
            nodes: list[dict] = []
            edges: list[dict] = []
            blob_seen: set[str] = set()
            x, y = 40, 60
            for v in versions:
                vid = f"version-{v['id']}"
                nodes.append({"id": vid, "x": x, "y": y, "kind": "version",
                              "label": f"{v['slug']} v{v['version_no']} ({v['status']})"})
                for ref in db.get_refs(conn, v["id"]):
                    rid = f"ref-{ref['id']}"
                    nodes.append({"id": rid, "x": x + 260, "y": y,
                                  "kind": "ref",
                                  "label": f"#{ref['link_index']} {ref['label']}"})
                    edges.append((vid, rid))
                    prov = conn.execute(
                        "SELECT * FROM attachment_provenance WHERE ref_id=?",
                        (ref["id"],)).fetchone()
                    if prov and prov["blob_sha256"]:
                        bid = f"blob-{prov['blob_sha256'][:12]}"
                        if bid not in blob_seen:
                            blob_seen.add(bid)
                            nodes.append({"id": bid, "x": x + 560,
                                          "y": 40 + 90 * (len(blob_seen) - 1),
                                          "kind": "blob",
                                          "label": prov["blob_sha256"][:16] + "…"})
                        edges.append((rid, bid))
                    y += 90
                y += 30
            svg = self._render_svg(nodes, edges)
            body = f"""
<h1>引用关系图{(' · ' + templates.esc(slug)) if slug else ''}</h1>
<div class="panel"><p class="muted small">
版本 → 本版引用（独立来源记录）→ 内容寻址 blob。多个版本指向同一 blob
即“共用存储、来源不共用”。</p>{svg}</div>"""
            self._html(body)
        finally:
            conn.close()

    @staticmethod
    def _render_svg(nodes: list[dict], edges: list[tuple]) -> str:
        width, height = 900, max(200, 90 * len(nodes) + 80)
        pos = {n["id"]: (n["x"], n["y"]) for n in nodes}
        edge_svg = "".join(
            f'<line x1="{pos[a][0] + 150}" y1="{pos[a][1] + 22}" '
            f'x2="{pos[b][0]}" y2="{pos[b][1] + 22}" '
            f'stroke="#3a476b" marker-end="url(#arrow)"/>'
            for a, b in edges)
        colors = {"version": "#1d3a5f", "ref": "#24334f", "blob": "#14402f"}
        node_svg = "".join(
            f'<g><rect x="{n["x"]}" y="{n["y"]}" width="220" height="44" rx="8" '
            f'fill="{colors[n["kind"]]}" stroke="#3a476b"/>'
            f'<text x="{n["x"] + 12}" y="{n["y"] + 27}" fill="#e6ebf5" '
            f'font-size="12">{templates.esc(n["label"])}</text></g>'
            for n in nodes)
        return (f'<svg width="{width}" height="{height}" '
                f'viewBox="0 0 {width} {height}">'
                '<defs><marker id="arrow" markerWidth="8" markerHeight="8" '
                'refX="7" refY="4" orient="auto">'
                '<path d="M0,0 L8,4 L0,8 z" fill="#5db3ff"/></marker></defs>'
                f'{edge_svg}{node_svg}</svg>')

    def _jobs_page(self) -> None:
        rows = "".join(
            "<tr>"
            f"<td class='mono'>{templates.esc(j.id)}</td>"
            f"<td>{templates.esc(j.kind)}</td>"
            f"<td>{templates.esc(j.target)}</td>"
            f"<td>{templates.badge(j.state)}</td>"
            f"<td class='small'>{templates.esc(j.error or '')}</td>"
            f"<td class='small muted'>{templates.esc(j.created_at)}</td></tr>"
            for j in self.state.runner.list_jobs())
        body = f"""
<h1>后台作业</h1>
<div class="panel"><table><tr><th>ID</th><th>类型</th><th>目标</th>
<th>状态</th><th>错误</th><th>创建时间</th></tr>{rows}</table></div>
<p class="muted small"><code>GET /api/jobs</code> 返回 JSON，供轮询。</p>"""
        self._html(body)

    def _events_page(self) -> None:
        conn = db.connect(self.state.cfg.db_path)
        try:
            events = db.list_events(conn, 300)
            rows = "".join(
                "<tr>"
                f"<td class='small muted'>{templates.esc(e['ts'])}</td>"
                f"<td><span class='mono'>{templates.esc(e['kind'])}</span></td>"
                f"<td>{templates.esc(e['entity'])}/{templates.esc(e['entity_id'] or '')}</td>"
                f"<td class='small mono'>{templates.esc(e['detail_json'])}</td></tr>"
                for e in events)
            self._html(
                f"<h1>审计事件</h1><div class='panel'><table>"
                "<tr><th>时间</th><th>事件</th><th>对象</th><th>详情</th></tr>"
                f"{rows}</table></div>")
        finally:
            conn.close()

    def _sim_page(self) -> None:
        state = None
        sim_error = None
        if self.state.sim_base_url:
            try:
                with urllib.request.urlopen(
                        self.state.sim_base_url + "/__admin__/state",
                        timeout=10) as resp:
                    state = json.loads(resp.read().decode("utf-8"))
            except urllib.error.URLError as exc:
                sim_error = str(exc)
        files_html = ""
        if state:
            rows = []
            for path, entry in state["files"].items():
                versions = entry["versions"]
                rows.append(
                    "<tr>"
                    f"<td class='mono small'>{templates.esc(path)}</td>"
                    f"<td>{len(versions)}</td>"
                    f"<td class='mono small'>{templates.short(versions[-1]['sha256'])}</td>"
                    f"<td class='small muted'>{templates.esc(versions[-1].get('note',''))}</td>"
                    "</tr>")
            files_html = (
                "<div class='panel'><h2>当前同址文件（URL 不变、内容可替换）</h2>"
                "<table><tr><th>URL 路径</th><th>历史版本数</th>"
                f"<th>当前哈希</th><th>备注</th></tr>{''.join(rows)}</table></div>")
        body = f"""
<h1>来源模拟器控制台</h1>
<p class="muted small">模拟器地址：
<span class="mono">{templates.esc(self.state.sim_base_url or '未配置')}</span>
{('<br>连接失败：' + templates.esc(sim_error)) if sim_error else ''}</p>
{files_html}
<div class="panel"><h2>下载途中更正正文</h2>
<form method="post" action="/sim/correct-body" class="grid2">
  <div class="field"><label>slug</label>
    <input type="text" name="slug" value="yunhe-bulletin-2026-11"></div>
  <div class="field"><label>更正原因</label>
    <input type="text" name="reason" value="mid-flight correction"></div>
  <div class="field" style="grid-column:1/3"><label>新正文（Markdown）</label>
    <textarea name="body" rows="10" required></textarea></div>
  <button class="btn danger" type="submit">立即替换正文</button>
  <p class="muted small">在另一终端对旧版本执行下载时提交本表单，即可复现
     “边下载边发现更正”：在途版本保持锁定，更正只进入下一次抓取的新版本。</p>
</form></div>
<div class="panel"><h2>同址替换附件字节</h2>
<form method="post" action="/sim/replace-file" class="grid2">
  <div class="field"><label>URL 路径</label>
    <input type="text" name="path" value="/files/bulletin.pdf"></div>
  <div class="field"><label>备注</label>
    <input type="text" name="note" value="same-url replacement"></div>
  <div class="field" style="grid-column:1/3">
    <label>新内容（base64；CLI 的 <code>seed-demo</code> 已备好 rev B 字节）</label>
    <input type="text" name="content_base64" required></div>
  <button class="btn danger" type="submit">同址替换文件</button>
</form></div>
<div class="panel"><h2>确定性撕裂下载（测试用 URL 模式）</h2>
<pre class="small">{templates.esc(
  (self.state.sim_base_url or 'http://127.0.0.1:8080')
  + '/files/bulletin.pdf?torn_after=200&chunk_delay=0.2')}</pre>
<p class="muted small">前 200 字节为当前内容、其余取自替换内容，响应分块传输；
归档端实测哈希必然不等于任何声明值，附件进入隔离区且版本不得发布。</p></div>"""
        self._html(body)

    # ------------------------------------------------------------ actions
    def _submit_pipeline_job(self, kind: str, target: str, func) -> str:
        job = self.state.runner.submit(kind, target, func)
        return f"作业 {job.id} 已提交（{kind}），可在“后台作业”页查看结果。"

    def _action_refetch(self) -> None:
        form = self._form()
        cfg = self.state.cfg

        def work():
            conn = db.connect(cfg.db_path)
            try:
                return run_full_pipeline(conn, cfg, form["source_url"],
                                         form["slug"])
            finally:
                conn.close()

        flash = self._submit_pipeline_job(
            "refetch+full-pipeline", form["slug"], work)
        self._redirect("/", flash)

    def _action_download(self) -> None:
        version_id = int(self._form()["version_id"])
        cfg = self.state.cfg

        def work():
            conn = db.connect(cfg.db_path)
            try:
                return download_version(conn, cfg, version_id)
            finally:
                conn.close()

        flash = self._submit_pipeline_job(
            "download-attachments", f"version {version_id}", work)
        self._redirect(f"/versions/{version_id}", flash)

    def _action_publish(self) -> None:
        version_id = int(self._form()["version_id"])
        cfg = self.state.cfg
        conn = db.connect(cfg.db_path)
        try:
            result = publish_version(conn, version_id)
            flash = f"版本 {version_id} 已发布：{result}"
        finally:
            conn.close()
        self._redirect(f"/versions/{version_id}", flash)

    def _sim_correct_body(self) -> None:
        form = self._form()
        result = _sim_admin(self.state.sim_base_url,
                            "/__admin__/correct-body", form)
        self._redirect("/sim",
                       f"正文已替换，新内容 id：{result['content_id'][:12]}…")

    def _sim_replace_file(self) -> None:
        form = self._form()
        result = _sim_admin(self.state.sim_base_url,
                            "/__admin__/replace-file", form)
        self._redirect("/sim",
                       f"文件已同址替换，新哈希：{result['sha256'][:12]}…")

    # ------------------------------------------------------------- files
    def _blob_download(self, digest: str) -> None:
        conn = db.connect(self.state.cfg.db_path)
        try:
            blob = db.get_blob(conn, digest)
            if not blob or not Path(blob["stored_path"]).exists():
                return self._json({"error": "blob not found"}, 404)
            data = Path(blob["stored_path"]).read_bytes()
            self._send(200, blob["media_type"] or "application/octet-stream",
                       data,
                       {"Content-Disposition":
                        f'attachment; filename="{digest[:16]}.bin"'})
        finally:
            conn.close()

    def _export(self, version_id: int) -> None:
        conn = db.connect(self.state.cfg.db_path)
        try:
            path = build_export_zip(conn, self.state.cfg, version_id)
            data = path.read_bytes()
            self._send(200, "application/zip", data,
                       {"Content-Disposition":
                        f'attachment; filename="{path.name}"'})
        finally:
            conn.close()

    @staticmethod
    def _job_dict(job) -> dict:
        return {"id": job.id, "kind": job.kind, "target": job.target,
                "state": job.state, "result": job.result, "error": job.error,
                "created_at": job.created_at,
                "finished_at": job.finished_at}


def make_server(host: str, port: int, cfg: Config,
                sim_base_url: str | None, verbose: bool = False
                ) -> ThreadingHTTPServer:
    db.init_db(cfg.db_path)
    server = ThreadingHTTPServer((host, port), _WebHandler)
    server.web_state = WebState(cfg, sim_base_url)  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server
