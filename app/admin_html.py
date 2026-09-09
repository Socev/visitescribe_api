"""Server-rendered admin interface.

Everything is inline: the pod must serve this with no network access and no
CDN. Light and dark are both handled through CSS custom properties.
"""
from __future__ import annotations

import html
import json
from typing import Any

from .util import human_bytes

CSS = """
:root{--bg:#f6f7f9;--panel:#fff;--ink:#16191d;--muted:#606a76;--line:#e2e6ea;
--accent:#1f6feb;--ok:#1a7f4b;--warn:#a25c00;--bad:#c0322b;--chip:#eef1f5;--code:#f1f3f6}
@media(prefers-color-scheme:dark){:root{--bg:#0f1216;--panel:#161a20;--ink:#e6e9ee;
--muted:#98a2b0;--line:#262c35;--accent:#5c9dff;--ok:#4bbf7f;--warn:#e0a343;
--bad:#f0736a;--chip:#1e242c;--code:#12161b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header.top{background:var(--panel);border-bottom:1px solid var(--line);padding:0 20px;
position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:22px;flex-wrap:wrap}
header.top .brand{font-weight:650;padding:14px 0;letter-spacing:-.01em}
header.top .brand span{color:var(--muted);font-weight:400;font-size:12px;margin-left:8px}
nav{display:flex;gap:20px}
nav a{padding:14px 2px;display:inline-block;color:var(--muted);font-weight:500;
border-bottom:2px solid transparent}
nav a.active{color:var(--ink);border-bottom-color:var(--accent)}
nav a:hover{color:var(--ink);text-decoration:none}
header.top .spacer{flex:1}
header.top .who{color:var(--muted);font-size:12px}
main{max-width:1240px;margin:0 auto;padding:22px 20px 64px}
h1{font-size:20px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:15px;margin:26px 0 10px;letter-spacing:-.01em}
h3{font-size:13px;margin:18px 0 8px;text-transform:uppercase;letter-spacing:.06em;
color:var(--muted)}
.sub{color:var(--muted);margin:0 0 18px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:16px}
.grid{display:grid;gap:12px}
.g4{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.g2{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.stat .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.stat .v{font-size:24px;font-weight:600;margin-top:4px;letter-spacing:-.02em}
.stat .n{color:var(--muted);font-size:12px;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;
text-transform:uppercase;letter-spacing:.05em;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
.scroll{overflow-x:auto}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
pre{background:var(--code);border:1px solid var(--line);border-radius:8px;padding:12px;
overflow-x:auto;font-size:12px;margin:0}
.chip{display:inline-block;padding:2px 8px;border-radius:999px;background:var(--chip);
font-size:11px;font-weight:600;letter-spacing:.02em;white-space:nowrap}
.chip.ok{background:rgba(26,127,75,.14);color:var(--ok)}
.chip.bad{background:rgba(192,50,43,.14);color:var(--bad)}
.chip.warn{background:rgba(162,92,0,.16);color:var(--warn)}
.chip.info{background:rgba(31,111,235,.14);color:var(--accent)}
button,.btn{font:inherit;font-size:13px;font-weight:550;padding:7px 13px;border-radius:7px;
border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
button:hover,.btn:hover{border-color:var(--accent);text-decoration:none}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{color:var(--bad);border-color:rgba(192,50,43,.4)}
button:disabled{opacity:.5;cursor:not-allowed}
input,select,textarea{font:inherit;font-size:13px;padding:7px 10px;border-radius:7px;
border:1px solid var(--line);background:var(--panel);color:var(--ink);width:100%}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px;font-weight:600}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.row>div{flex:1;min-width:150px}
.row .narrow{flex:0 0 auto;min-width:0}
.banner{border-radius:9px;padding:11px 14px;margin-bottom:16px;font-size:13px;
border:1px solid transparent}
.banner.warn{background:rgba(162,92,0,.1);border-color:rgba(162,92,0,.35);color:var(--warn)}
.banner.bad{background:rgba(192,50,43,.09);border-color:rgba(192,50,43,.35);color:var(--bad)}
.banner.info{background:rgba(31,111,235,.09);border-color:rgba(31,111,235,.3);color:var(--accent)}
.muted{color:var(--muted)}
.right{text-align:right}
.nowrap{white-space:nowrap}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:13px}
.kv dt{color:var(--muted)}
.kv dd{margin:0}
.timeline{border-left:2px solid var(--line);margin-left:8px;padding-left:16px}
.timeline li{list-style:none;margin-bottom:10px;position:relative}
.timeline li::before{content:"";position:absolute;left:-21px;top:6px;width:8px;height:8px;
border-radius:50%;background:var(--accent)}
.timeline li.pause::before{background:var(--warn)}
.timeline li.boundary::before{background:var(--ok)}
ul.plain{list-style:none;padding:0;margin:0}
#toast{position:fixed;right:18px;bottom:18px;background:var(--panel);
border:1px solid var(--line);border-radius:9px;padding:11px 15px;box-shadow:0 8px 28px
rgba(0,0,0,.16);display:none;max-width:420px;z-index:50;font-size:13px}
#toast.bad{border-color:var(--bad);color:var(--bad)}
#toast.ok{border-color:var(--ok)}
.login{max-width:340px;margin:14vh auto;}
.pager{display:flex;gap:8px;align-items:center;margin-top:12px;font-size:13px}
.secret{background:var(--code);border:1px dashed var(--accent);border-radius:8px;
padding:11px;margin-top:10px;word-break:break-all}
"""

JS = """
async function api(url, opts){
  const r = await fetch(url, Object.assign({headers:{'Content-Type':'application/json'}}, opts||{}));
  let body=null; try{ body = await r.json(); }catch(e){}
  if(!r.ok){ const m=(body&&body.error&&body.error.message)||('HTTP '+r.status);
    throw new Error(m); }
  return body;
}
function toast(msg, kind){
  const t=document.getElementById('toast');
  t.textContent=msg; t.className=kind||''; t.style.display='block';
  clearTimeout(window.__tt); window.__tt=setTimeout(()=>{t.style.display='none'}, 6000);
}
function copy(text){
  navigator.clipboard.writeText(text).then(()=>toast('Copied to clipboard','ok'),
    ()=>toast('Could not copy','bad'));
}
async function act(url, body, method){
  try{
    const r = await api(url, {method: method||'POST', body: JSON.stringify(body||{})});
    return r;
  }catch(e){ toast(e.message,'bad'); throw e; }
}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _j(value: Any) -> str:
    return html.escape(json.dumps(value, indent=2, default=str, ensure_ascii=False))


def layout(title: str, body: str, active: str = "", who: str = "") -> str:
    nav_items = [
        ("dashboard", "/admin/", "Overview"),
        ("sessions", "/admin/sessions", "Sessions"),
        ("devices", "/admin/devices", "Devices"),
        ("keys", "/admin/keys", "Keys"),
        ("audit", "/admin/audit", "Audit"),
    ]
    nav = "".join(
        f'<a href="{url}" class="{"active" if key == active else ""}">{label}</a>'
        for key, url, label in nav_items
    )
    who_html = f'<span class="who">{_e(who)}</span>' if who else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · VisiteScribe</title><style>{CSS}</style></head><body>
<header class="top"><div class="brand">VisiteScribe<span>ingest admin</span></div>
<nav>{nav}</nav><div class="spacer"></div>{who_html}
<button onclick="api('/admin/api/logout',{{method:'POST'}}).then(()=>location='/admin/login')"
 style="margin:8px 0">Sign out</button></header>
<main>{body}</main><div id="toast"></div><script>{JS}</script></body></html>"""


def render_login(error: str | None) -> str:
    err = f'<div class="banner bad">{_e(error)}</div>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · VisiteScribe</title><style>{CSS}</style></head><body>
<main><div class="login"><div class="panel">
<h1>VisiteScribe admin</h1><p class="sub">Sign in to continue.</p>{err}
<form onsubmit="return doLogin(event)">
<label for="pw">Admin password</label>
<input id="pw" type="password" autocomplete="current-password" autofocus>
<button class="primary" style="width:100%;margin-top:12px">Sign in</button>
</form></div></div></main><div id="toast"></div><script>{JS}
async function doLogin(e){{e.preventDefault();
 try{{ await api('/admin/api/login',{{method:'POST',
   body:JSON.stringify({{password:document.getElementById('pw').value}})}});
   location='/admin/'; }}catch(err){{ toast(err.message,'bad'); }}
 return false;}}
</script></body></html>"""


def _state_chip(state: str, confirmed: bool = False) -> str:
    kind = "info"
    if state in ("INGESTED", "READY_FOR_PROCESSING", "APPROVED"):
        kind = "ok"
    elif state in ("ERROR", "TRANSCRIPTION_FAILED", "PROCESSING_FAILED", "POLICY_BLOCKED"):
        kind = "bad"
    elif state in ("RECEIVING", "VALIDATING", "CREATED"):
        kind = "warn"
    elif state == "PURGED":
        kind = ""
    chip = f'<span class="chip {kind}">{_e(state)}</span>'
    if confirmed:
        chip += ' <span class="chip ok">ingest confirmed</span>'
    return chip


def _sessions_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="muted">No sessions yet.</p>'
    body = "".join(
        f"""<tr><td class="mono nowrap"><a href="/admin/sessions/{_e(r['session_id'])}">
{_e(r['session_id'][:8])}…</a></td>
<td class="nowrap">{_e(r['device_id'])}</td><td>{_e(r['mode'])}</td>
<td>{_state_chip(r['state'], r['ingest_confirmed'])}</td>
<td class="right nowrap">{r['received_chunks']} / {r['expected_chunks']}</td>
<td class="nowrap muted">{_e(r['client_status'] or '—')}</td>
<td class="nowrap muted">{_e((r['created_at'] or '')[:19].replace('T',' '))}</td></tr>"""
        for r in rows
    )
    return f"""<div class="scroll"><table><thead><tr><th>Session</th><th>Device</th>
<th>Mode</th><th>State</th><th class="right">Chunks</th><th>Client</th><th>Created</th>
</tr></thead><tbody>{body}</tbody></table></div>"""


def render_dashboard(d: dict[str, Any], who: str) -> str:
    banners = []
    if not d["password_protected"]:
        banners.append(
            '<div class="banner info">No <code>VS_ADMIN_PASSWORD</code> is set, so this '
            "page relies entirely on the Olares entrance being private. That is fine for a "
            "private entrance; set the variable if you ever expose it.</div>"
        )
    if d["flac_decoder"] != "libsndfile":
        banners.append(
            '<div class="banner warn">libsndfile is unavailable, so chunks are validated '
            "structurally but not fully decoded.</div>"
        )
    open_devices = [x for x in d["devices"]
                    if x["allow_header_only"] and not x["has_token"]
                    and not x["cert_fingerprint"]]
    if open_devices:
        names = ", ".join(_e(x["device_id"]) for x in open_devices)
        banners.append(
            f'<div class="banner warn">These devices authenticate with their device ID '
            f"alone: <strong>{names}</strong>. That is what the stock v0.2 recorder sends. "
            f'Issue a token on the <a href="/admin/devices">Devices</a> page to harden them.</div>'
        )
    failures = d["integrity_failures"] + d["security_failures"]
    if failures:
        banners.append(
            f'<div class="banner bad">{len(failures)} integrity or security failure(s) '
            f'recorded. See the <a href="/admin/audit?outcome=failure">audit log</a>.</div>'
        )

    by_state = "".join(
        f'<span class="chip">{_e(k)} · {v}</span> '
        for k, v in sorted(d["sessions_by_state"].items())
    ) or '<span class="muted">none</span>'

    disk = d["disk"]
    key = d["active_key"] or {}
    device_rows = "".join(
        f"""<tr><td class="nowrap"><a href="/admin/devices/{_e(x['device_id'])}">
{_e(x['device_id'])}</a></td>
<td>{'<span class="chip ok">enabled</span>' if x['enabled'] else '<span class="chip bad">disabled</span>'}</td>
<td>{_auth_chip(x)}</td>
<td class="muted nowrap">{_e((x['last_seen_at'] or '—')[:19].replace('T',' '))}</td>
<td class="right">{_e(x['battery_percent'] if x['battery_percent'] is not None else '—')}</td>
<td class="right">{_e(x['queue_count'] if x['queue_count'] is not None else '—')}</td>
<td class="muted">{_e(x['software_version'] or '—')}</td></tr>"""
        for x in d["devices"]
    ) or '<tr><td colspan="7" class="muted">No devices registered yet.</td></tr>'

    body = f"""<h1>Overview</h1>
<p class="sub">Encrypted session mailbox · v{_e(d['version'])} ·
running since {_e((d['installed_at'] or '')[:10])}</p>
{''.join(banners)}
<div class="grid g4">
  <div class="stat"><div class="k">Sessions</div><div class="v">{d['sessions_total']}</div>
    <div class="n">{d['sessions_confirmed']} with confirmed ingest</div></div>
  <div class="stat"><div class="k">Chunks stored</div><div class="v">{d['chunks_total']}</div>
    <div class="n">{_e(human_bytes(d['ciphertext_bytes']))} encrypted</div></div>
  <div class="stat"><div class="k">Blob storage</div>
    <div class="v">{_e(human_bytes(d['blob_bytes']))}</div>
    <div class="n">{_e(human_bytes(disk['free']))} free of
      {_e(human_bytes(disk['total']))}</div></div>
  <div class="stat"><div class="k">Devices</div><div class="v">{len(d['devices'])}</div>
    <div class="n">FLAC decoder: {_e(d['flac_decoder'])}</div></div>
</div>
<div class="panel"><h3 style="margin-top:0">Sessions by state</h3>{by_state}</div>
<h2>Devices</h2><div class="panel"><div class="scroll"><table><thead><tr>
<th>Device</th><th>Status</th><th>Auth</th><th>Last seen</th><th class="right">Battery</th>
<th class="right">Queue</th><th>Version</th></tr></thead><tbody>{device_rows}</tbody>
</table></div></div>
<h2>Recent sessions</h2><div class="panel">{_sessions_table(d['recent_sessions'])}</div>
<h2>Server key</h2><div class="panel">
<dl class="kv"><dt>Key ID</dt><dd class="mono">{_e(key.get('key_id','—'))}</dd>
<dt>Algorithm</dt><dd>{_e(key.get('algorithm','—'))}</dd>
<dt>Created</dt><dd>{_e((key.get('created_at') or '')[:19].replace('T',' '))}</dd>
<dt>Plaintext audio at rest</dt>
<dd>{'yes (VS_STORE_PLAINTEXT is on)' if d['store_plaintext'] else 'no — ciphertext only'}</dd>
</dl><p style="margin:12px 0 0"><a class="btn" href="/admin/keys">Manage keys</a></p></div>"""
    return layout("Overview", body, "dashboard", who)


def _auth_chip(device: dict[str, Any]) -> str:
    parts = []
    if device.get("cert_fingerprint"):
        parts.append('<span class="chip ok">mTLS pinned</span>')
    if device.get("has_token"):
        parts.append('<span class="chip ok">token</span>')
    if not parts:
        parts.append('<span class="chip warn">device-id only</span>')
    elif device.get("allow_header_only"):
        parts.append('<span class="chip warn">header-only allowed</span>')
    return " ".join(parts)


def render_sessions(d: dict[str, Any], who: str) -> str:
    f = d["filters"]
    states = "".join(
        f'<option value="{_e(s)}"{" selected" if s == f["state"] else ""}>{_e(s)}</option>'
        for s in d["states"]
    )
    devices = "".join(
        f'<option value="{_e(x)}"{" selected" if x == f["device"] else ""}>{_e(x)}</option>'
        for x in d["devices"]
    )
    pages = max(1, -(-d["total"] // d["per_page"]))
    prev = f'<a class="btn" href="?{_qs(f, d["page"]-1)}">Previous</a>' if d["page"] > 1 else ""
    nxt = f'<a class="btn" href="?{_qs(f, d["page"]+1)}">Next</a>' if d["page"] < pages else ""
    body = f"""<h1>Sessions</h1><p class="sub">{d['total']} session(s)</p>
<div class="panel"><form class="row" method="get">
<div><label>State</label><select name="state"><option value="">Any</option>{states}</select></div>
<div><label>Device</label><select name="device"><option value="">Any</option>{devices}</select></div>
<div><label>Session ID contains</label><input name="q" value="{_e(f['q'])}"></div>
<div class="narrow"><button class="primary">Filter</button></div>
<div class="narrow"><a class="btn" href="/admin/sessions">Reset</a></div>
</form></div>
<div class="panel">{_sessions_table(d['sessions'])}
<div class="pager">{prev}<span class="muted">Page {d['page']} of {pages}</span>{nxt}</div>
</div>"""
    return layout("Sessions", body, "sessions", who)


def _qs(filters: dict[str, Any], page: int) -> str:
    from urllib.parse import urlencode

    params = {k: v for k, v in filters.items() if v}
    params["page"] = page
    return urlencode(params)


def render_session_detail(d: dict[str, Any], who: str) -> str:
    s = d["session"]
    sid = s["session_id"]
    raw = d["raw"]
    banners = []
    if d["missing_chunks"]:
        banners.append(
            f'<div class="banner warn">Missing chunk(s): '
            f'<span class="mono">{_e(", ".join(str(x) for x in d["missing_chunks"]))}</span>'
            "</div>")
    if d["unverified_chunks"]:
        banners.append(
            f'<div class="banner bad">Chunk(s) failed verification: '
            f'<span class="mono">{_e(", ".join(str(x) for x in d["unverified_chunks"]))}</span>'
            "</div>")
    if s["client_status"] == "interrupted":
        banners.append(
            '<div class="banner warn">The recorder reported this session as '
            "<strong>interrupted</strong>. The ingest can still be complete and valid, but "
            "up to the last chunk interval of audio may never have been captured.</div>")
    if raw.get("purged_at"):
        banners.append(
            f'<div class="banner bad">Purged at {_e(raw["purged_at"])}. Audit records are '
            "retained.</div>")

    chunk_rows = "".join(
        f"""<tr><td class="right">{c['sequence']}</td>
<td>{_verify_chips(c)}</td>
<td class="right nowrap">{_e(human_bytes(c['ciphertext_size']))}</td>
<td class="right nowrap">{_e(human_bytes(c['plaintext_size']))}</td>
<td class="right nowrap">{_e(_ms(c['flac'].get('duration_ms')))}</td>
<td class="mono muted nowrap">{_e((c['ciphertext_sha256'] or '')[:12])}…</td>
<td class="muted nowrap">{_e((c['received_at'] or '')[11:19])}</td>
<td class="nowrap"><a href="/admin/api/sessions/{_e(sid)}/chunks/{c['sequence']}/download?form=encrypted">enc</a>
 · <a href="/admin/api/sessions/{_e(sid)}/chunks/{c['sequence']}/download?form=decrypted">flac</a></td>
</tr>"""
        for c in d["chunks"]
    ) or '<tr><td colspan="8" class="muted">No chunks received.</td></tr>'

    events = "".join(
        f"""<li class="{'pause' if 'privacy' in ev['event'] else
        ('boundary' if ev['event']=='patient_boundary' else '')}">
<strong>{_e(ev['event'])}</strong>
<span class="muted mono"> {_e(_ms(ev['offset_ms']))}</span>
{f'<span class="chip">patient {ev["patient_index"]}</span>' if ev.get('patient_index') is not None else ''}
<div class="muted mono" style="font-size:11px">{_e(ev['at'] or '')}</div></li>"""
        for ev in d["events"]
    ) or '<li class="muted">No events recorded.</li>'

    segments = "".join(
        f"""<tr><td class="right">{seg['index']}</td>
<td class="mono">{_e(_ms(seg['start_ms']))}</td>
<td class="mono">{_e(_ms(seg['end_ms']) if seg['end_ms'] is not None else 'open')}</td>
<td class="mono">{_e(_ms(seg['duration_ms']) if seg['duration_ms'] is not None else '—')}</td>
</tr>"""
        for seg in d["segments"]
    ) or '<tr><td colspan="4" class="muted">Single continuous segment.</td></tr>'

    gaps = "".join(
        f"""<tr><td class="mono">{_e(_ms(g['start_ms']))}</td>
<td class="mono">{_e(_ms(g['end_ms']) if g['end_ms'] is not None else 'still open')}</td>
<td>{'closed' if g['closed'] else '<span class="chip warn">unclosed</span>'}</td></tr>"""
        for g in d["privacy_gaps"]
    )
    gaps_block = (
        f"""<h2>Privacy pauses</h2><div class="panel"><p class="sub">
The recorder captured nothing during these periods. They are timeline gaps, not
missing data.</p><div class="scroll"><table><thead><tr><th>From</th><th>To</th>
<th>State</th></tr></thead><tbody>{gaps}</tbody></table></div></div>"""
        if gaps else ""
    )

    state_options = "".join(
        f'<option value="{_e(x)}"{" selected" if x == s["state"] else ""}>{_e(x)}</option>'
        for x in d["states"] if x != "PURGED"
    )
    route = (d["processing"] or {}).get("route") or ""
    route_options = "".join(
        f'<option value="{_e(x)}"{" selected" if x == route else ""}>{_e(x or "not routed")}</option>'
        for x in ("", "local", "ourmind", "plaud")
    )
    audit_rows = _audit_rows(d["audit"])

    body = f"""<p><a href="/admin/sessions">← Sessions</a></p>
<h1 class="mono">{_e(sid)}</h1>
<p class="sub">{_state_chip(s['state'], s['ingest_confirmed'])}
<span class="chip">{_e(s['mode'])}</span>
<span class="chip">device {_e(s['device_id'])}</span></p>
{''.join(banners)}
<div class="grid g2">
<div class="panel"><h3 style="margin-top:0">Session</h3><dl class="kv">
<dt>Started</dt><dd class="mono">{_e(raw.get('started_at') or '—')}</dd>
<dt>Completed</dt><dd class="mono">{_e(raw.get('completed_at') or '—')}</dd>
<dt>Client status</dt><dd>{_e(s['client_status'] or '—')}</dd>
<dt>Complete requested</dt><dd>{'yes' if raw.get('complete_requested') else 'no'}</dd>
<dt>Declared chunk_count</dt><dd>{_e(raw.get('complete_chunk_count'))}</dd>
<dt>Chunks</dt><dd>{s['received_chunks']} received / {s['expected_chunks']} manifested</dd>
<dt>Ingested at</dt><dd class="mono">{_e(raw.get('ingested_at') or '—')}</dd>
<dt>Error</dt><dd>{_e(raw.get('error_message') or '—')}</dd>
</dl></div>
<div class="panel"><h3 style="margin-top:0">Audio &amp; encryption</h3><dl class="kv">
<dt>Codec</dt><dd>{_e(d['audio'].get('codec'))}</dd>
<dt>Sample rate</dt><dd>{_e(d['audio'].get('sample_rate'))} Hz</dd>
<dt>Channels</dt><dd>{_e(d['audio'].get('channels'))}</dd>
<dt>Format</dt><dd>{_e(d['audio'].get('sample_format'))}</dd>
<dt>Chunk length</dt><dd>{_e(d['audio'].get('chunk_seconds'))} s</dd>
<dt>Cipher</dt><dd>{_e(d['encryption'].get('algorithm'))}</dd>
<dt>Key wrap</dt><dd class="mono">{_e(raw.get('wrap_algorithm'))} ·
{_e(raw.get('wrap_key_id'))}</dd>
</dl></div></div>
<div class="panel"><h3 style="margin-top:0">Actions</h3>
<div class="row">
<div><label>State</label><select id="st">{state_options}</select></div>
<div class="narrow"><button onclick="setState()">Apply state</button></div>
<div><label>Processing route</label><select id="rt">{route_options}</select></div>
<div class="narrow"><button onclick="setRoute()">Set route</button></div>
<div class="narrow"><a class="btn" href="/admin/api/sessions/{_e(sid)}/export.zip">
Export .zip</a></div>
<div class="narrow"><a class="btn" href="/admin/api/sessions/{_e(sid)}/audio.wav">
Download WAV</a></div>
<div class="narrow"><button class="danger" onclick="purge('all')">Purge session</button></div>
</div>
<p class="muted" style="margin:10px 0 0">Purging removes the stored audio. The audit
record that a purge happened is kept.</p></div>
<h2>Chunks</h2><div class="panel"><div class="scroll"><table><thead><tr>
<th class="right">#</th><th>Verification</th><th class="right">Encrypted</th>
<th class="right">FLAC</th><th class="right">Duration</th><th>Ciphertext SHA-256</th>
<th>Received</th><th>Download</th></tr></thead><tbody>{chunk_rows}</tbody></table></div></div>
<div class="grid g2">
<div class="panel"><h3 style="margin-top:0">Events</h3>
<ul class="plain timeline">{events}</ul></div>
<div class="panel"><h3 style="margin-top:0">Patient segments</h3>
<p class="muted" style="margin-top:0">Derived from <code>patient_boundary</code> events.
Segments are never combined into one note.</p>
<div class="scroll"><table><thead><tr><th class="right">#</th><th>From</th><th>To</th>
<th>Duration</th></tr></thead><tbody>{segments}</tbody></table></div></div></div>
{gaps_block}
<h2>Audit trail</h2><div class="panel"><div class="scroll"><table><thead><tr>
<th>Time</th><th>Category</th><th>Action</th><th>Outcome</th><th>Detail</th>
</tr></thead><tbody>{audit_rows}</tbody></table></div></div>
<script>
const SID={json.dumps(sid)};
async function setState(){{ await act('/admin/api/sessions/'+SID+'/state',
  {{state:document.getElementById('st').value}}); toast('State updated','ok');
  setTimeout(()=>location.reload(),600); }}
async function setRoute(){{ await act('/admin/api/sessions/'+SID+'/processing',
  {{route:document.getElementById('rt').value}}); toast('Route updated','ok'); }}
async function purge(scope){{
  if(!confirm('Purge '+scope+' for this session? Stored audio is deleted permanently.')) return;
  await act('/admin/api/sessions/'+SID+'/purge',{{scope:scope}});
  toast('Purged','ok'); setTimeout(()=>location.reload(),700); }}
</script>"""
    return layout(f"Session {sid[:8]}", body, "sessions", who)


def _verify_chips(c: dict[str, Any]) -> str:
    ok = (c["ciphertext_verified"] and c["decrypt_verified"]
          and c["plaintext_verified"] and c["flac_valid"])
    chips = ['<span class="chip ok">verified</span>' if ok
             else '<span class="chip bad">unverified</span>']
    if c.get("flac_deep_verified"):
        chips.append('<span class="chip">decoded</span>')
    if c.get("flac", {}).get("md5_verified"):
        chips.append('<span class="chip ok">md5</span>')
    return " ".join(chips)


def _ms(value: Any) -> str:
    if value is None:
        return "—"
    try:
        total = int(value)
    except (TypeError, ValueError):
        return "—"
    seconds, ms = divmod(total, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{ms:03d}"
    return f"{minutes}:{seconds:02d}.{ms:03d}"


def _audit_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="muted">Nothing recorded.</td></tr>'
    out = []
    for r in rows:
        chip = ('<span class="chip ok">ok</span>' if r["outcome"] == "success"
                else '<span class="chip bad">' + _e(r["outcome"]) + "</span>")
        detail = r.get("detail_json") or "{}"
        if len(detail) > 400:
            detail = detail[:400] + "…"
        seq = r.get("sequence")
        action = _e(r["action"]) + (
            f' <span class="chip">#{seq}</span>' if seq is not None else "")
        out.append(
            f"""<tr><td class="mono muted nowrap">{_e((r['ts'] or '')[:19].replace('T',' '))}</td>
<td>{_e(r['category'])}</td><td class="mono">{action}</td><td>{chip}</td>
<td class="mono muted" style="max-width:520px;word-break:break-word">{_e(detail)}</td></tr>"""
        )
    return "".join(out)


def render_devices(d: dict[str, Any], who: str) -> str:
    rows = "".join(
        f"""<tr><td class="nowrap"><a href="/admin/devices/{_e(x['device_id'])}">
{_e(x['device_id'])}</a><div class="muted">{_e(x['display_name'])}</div></td>
<td>{'<span class="chip ok">enabled</span>' if x['enabled'] else '<span class="chip bad">disabled</span>'}</td>
<td>{_auth_chip(x)}</td>
<td class="right">{x['sessions']}</td>
<td class="right">{x['sessions_confirmed']}</td>
<td class="muted nowrap">{_e((x['last_seen_at'] or '—')[:19].replace('T',' '))}</td>
<td class="muted">{_e(x['last_auth_method'] or '—')}</td></tr>"""
        for x in d["devices"]
    ) or '<tr><td colspan="7" class="muted">No devices yet.</td></tr>'

    windows = "".join(
        f'<li><span class="mono">{_e(k)}</span> until '
        f'<span class="mono">{_e(v)}</span></li>'
        for k, v in d["enrolment_windows"].items()
    )
    windows_block = (
        f'<div class="banner info">Enrolment window open for: <ul class="plain">{windows}</ul>'
        "The next request carrying that device ID registers the device automatically.</div>"
        if windows else ""
    )
    body = f"""<h1>Devices</h1>
<p class="sub">A device must exist here before it can upload anything.</p>
{windows_block}
<div class="panel"><h3 style="margin-top:0">Register a recorder</h3>
<div class="row">
<div><label>Device ID</label><input id="did" placeholder="visitescribe-001"></div>
<div><label>Label</label><input id="dname" placeholder="Practice bag recorder"></div>
<div class="narrow"><label>&nbsp;</label>
<label style="font-weight:400"><input type="checkbox" id="dtok" checked
 style="width:auto;margin-right:6px">Issue a token</label></div>
<div class="narrow"><button class="primary" onclick="createDevice()">Create</button></div>
</div>
<div id="secret"></div>
<p class="muted" style="margin-bottom:0">Without a token the device authenticates with its
<code>X-Device-ID</code> header alone — which is exactly what the stock v0.2 recorder
sends. With a token, the recorder must also send
<code>Authorization: Bearer &lt;token&gt;</code>.</p></div>
<div class="panel"><h3 style="margin-top:0">Open an enrolment window</h3>
<p class="muted" style="margin-top:0">Lets one specific, not-yet-known device ID register
itself on its next request. Unknown device IDs are never accepted otherwise.</p>
<div class="row"><div><label>Device ID</label><input id="eid"
 placeholder="visitescribe-002"></div>
<div class="narrow"><label>Minutes</label><input id="emin" type="number" value="30"
 style="width:100px"></div>
<div class="narrow"><button onclick="openWindow()">Open window</button></div></div></div>
<div class="panel"><div class="scroll"><table><thead><tr><th>Device</th><th>Status</th>
<th>Auth</th><th class="right">Sessions</th><th class="right">Confirmed</th>
<th>Last seen</th><th>Last method</th></tr></thead><tbody>{rows}</tbody></table></div></div>
<script>
async function createDevice(){{
  const id=document.getElementById('did').value.trim();
  if(!id){{toast('Device ID is required','bad');return;}}
  const r=await act('/admin/api/devices',{{device_id:id,
    display_name:document.getElementById('dname').value,
    issue_token:document.getElementById('dtok').checked}});
  if(r.token){{ document.getElementById('secret').innerHTML=
    '<div class="secret"><strong>Token for '+id+
    '</strong> — shown once, store it on the recorder now:<br><span class="mono">'+
    r.token+'</span><br><button style="margin-top:8px" onclick="copy('+
    JSON.stringify(r.token)+')">Copy</button></div>'; }}
  else {{ toast('Device created','ok'); setTimeout(()=>location.reload(),800); }}
}}
async function openWindow(){{
  const id=document.getElementById('eid').value.trim();
  if(!id){{toast('Device ID is required','bad');return;}}
  await act('/admin/api/devices/'+encodeURIComponent(id)+'/enrolment',
    {{minutes:parseInt(document.getElementById('emin').value||'30',10)}});
  toast('Enrolment window opened','ok'); setTimeout(()=>location.reload(),700);
}}
</script>"""
    return layout("Devices", body, "devices", who)


def render_device_detail(d: dict[str, Any], who: str) -> str:
    x = d["device"]
    did = x["device_id"]
    body = f"""<p><a href="/admin/devices">← Devices</a></p>
<h1 class="mono">{_e(did)}</h1><p class="sub">{_e(x['display_name'])} · {_auth_chip(x)}</p>
<div class="grid g2">
<div class="panel"><h3 style="margin-top:0">Status</h3><dl class="kv">
<dt>Enabled</dt><dd>{'yes' if x['enabled'] else 'no'}</dd>
<dt>Header-only auth</dt><dd>{'allowed' if x['allow_header_only'] else 'not allowed'}</dd>
<dt>Token</dt><dd>{_e(x['token_hint'] or 'none issued')}</dd>
<dt>Pinned certificate</dt><dd class="mono">{_e(x['cert_fingerprint'] or 'none')}</dd>
<dt>Certificate subject</dt><dd class="mono">{_e(x['cert_subject'] or '—')}</dd>
<dt>Last seen</dt><dd class="mono">{_e(x['last_seen_at'] or '—')}</dd>
<dt>Last method</dt><dd>{_e(x['last_auth_method'] or '—')}</dd>
<dt>Last IP</dt><dd class="mono">{_e(x['last_source_ip'] or '—')}</dd>
<dt>Enrolment window</dt><dd class="mono">{_e(d['enrolment_expires_at'] or 'closed')}</dd>
</dl></div>
<div class="panel"><h3 style="margin-top:0">Reported by the device</h3><dl class="kv">
<dt>Software</dt><dd>{_e(x['software_version'] or '—')}</dd>
<dt>Battery</dt><dd>{_e(x['battery_percent'] if x['battery_percent'] is not None else '—')}%</dd>
<dt>Queue</dt><dd>{_e(x['queue_count'] if x['queue_count'] is not None else '—')}</dd>
<dt>Free storage</dt><dd>{_e(human_bytes(x['storage_free_bytes']))}</dd>
<dt>Last recording</dt><dd class="mono">{_e(x['last_recording_at'] or '—')}</dd>
<dt>Network</dt><dd>{_e(x['network_state'] or '—')}</dd>
<dt>Config version</dt><dd>{_e(x['config_version'])}</dd>
</dl></div></div>
<div class="panel"><h3 style="margin-top:0">Manage</h3><div class="row">
<div class="narrow"><button onclick="upd({{enabled:{str(not x['enabled']).lower()}}})">
{'Disable' if x['enabled'] else 'Enable'} device</button></div>
<div class="narrow"><button onclick="upd({{upload_enabled:{str(not x['upload_enabled']).lower()}}})">
{'Pause' if x['upload_enabled'] else 'Resume'} uploads</button></div>
<div class="narrow"><button onclick="upd({{allow_header_only:{str(not x['allow_header_only']).lower()}}})">
{'Require credentials' if x['allow_header_only'] else 'Allow header-only'}</button></div>
<div class="narrow"><button class="primary" onclick="issueToken()">Issue new token</button></div>
<div class="narrow"><button class="danger" onclick="revokeToken()">Revoke token</button></div>
</div><div id="secret"></div></div>
<div class="panel"><h3 style="margin-top:0">Pin a client certificate</h3>
<p class="muted" style="margin-top:0">SHA-256 fingerprint of the recorder's client
certificate (DER). Once pinned, every request from this device must present it.</p>
<div class="row"><div><input id="fp" class="mono"
 value="{_e(x['cert_fingerprint'] or '')}" placeholder="64 hex characters"></div>
<div class="narrow"><button onclick="pin()">Save fingerprint</button></div>
<div class="narrow"><button onclick="unpin()">Remove pin</button></div></div></div>
<div class="panel"><h3 style="margin-top:0">Device configuration</h3>
<p class="muted" style="margin-top:0">Returned by <code>GET /v1/device/config</code>.
Security-critical values cannot be weakened from here.</p>
<textarea id="cfg" rows="8" class="mono">{_e(json.dumps(x['config'], indent=2))}</textarea>
<p><button onclick="saveCfg()">Save configuration</button></p></div>
<h2>Sessions</h2><div class="panel">{_sessions_table(d['sessions'])}</div>
<h2>Audit trail</h2><div class="panel"><div class="scroll"><table><thead><tr>
<th>Time</th><th>Category</th><th>Action</th><th>Outcome</th><th>Detail</th></tr></thead>
<tbody>{_audit_rows(d['audit'])}</tbody></table></div></div>
<script>
const DID={json.dumps(did)};
const base='/admin/api/devices/'+encodeURIComponent(DID);
async function upd(patch){{ await act(base, patch); toast('Saved','ok');
  setTimeout(()=>location.reload(),600); }}
async function issueToken(){{ const r=await act(base+'/token',{{}});
  document.getElementById('secret').innerHTML='<div class="secret"><strong>New token</strong>'+
  ' — shown once:<br><span class="mono">'+r.token+'</span><br><button style="margin-top:8px"'+
  ' onclick="copy('+JSON.stringify(r.token)+')">Copy</button></div>'; }}
async function revokeToken(){{ if(!confirm('Revoke this device token?'))return;
  await act(base+'/token',{{revoke:true}}); toast('Token revoked','ok');
  setTimeout(()=>location.reload(),600); }}
async function pin(){{ await upd({{cert_fingerprint:
  document.getElementById('fp').value.trim()}}); }}
async function unpin(){{ await upd({{cert_fingerprint:''}}); }}
async function saveCfg(){{ await upd({{config:document.getElementById('cfg').value}}); }}
</script>"""
    return layout(f"Device {did}", body, "devices", who)


def render_keys(keys: list[dict[str, Any]], who: str) -> str:
    rows = "".join(
        f"""<div class="panel"><div class="row" style="align-items:center">
<div><strong class="mono">{_e(k['key_id'])}</strong>
{'<span class="chip ok">active</span>' if k['active'] else '<span class="chip">retired</span>'}
<div class="muted">{_e(k['algorithm'])} · created
{_e((k['created_at'] or '')[:19].replace('T',' '))}
{('· retired ' + _e((k['retired_at'] or '')[:19].replace('T',' '))) if k['retired_at'] else ''}
</div></div>
<div class="narrow"><button onclick="copy({json.dumps(k['public_pem'])})">Copy public key</button>
</div></div>
<pre style="margin-top:10px">{_e(k['public_pem'])}</pre></div>"""
        for k in keys
    ) or '<div class="panel muted">No keys.</div>'
    body = f"""<h1>Server keys</h1>
<p class="sub">The recorder wraps each session's AES-256 key with the active public key
using RSA-OAEP (SHA-256, MGF1-SHA-256, no label). Retired keys stay loaded so older
sessions keep decrypting.</p>
<div class="banner info">Provisioning: put the <strong>active</strong> public key on the
recorder. A registered device can also fetch it from
<code>GET /v1/server/public-key</code>.</div>
<p><button class="primary" onclick="rotate()">Generate a new active key</button>
<span class="muted"> Existing sessions are unaffected.</span></p>
{rows}
<script>
async function rotate(){{
  if(!confirm('Generate a new active server key? New sessions must use the new public key.'))
    return;
  await act('/admin/api/keys/rotate',{{}}); toast('New key generated','ok');
  setTimeout(()=>location.reload(),800);
}}
</script>"""
    return layout("Keys", body, "keys", who)


def render_audit(rows: list[dict[str, Any]], total: int, page: int, per: int,
                 filters: dict[str, str], who: str) -> str:
    cats = ["", "auth", "device", "session", "chunk", "event", "integrity", "security",
            "export", "purge", "key", "admin", "processing"]
    cat_opts = "".join(
        f'<option value="{_e(c)}"{" selected" if c == filters["category"] else ""}>'
        f'{_e(c or "any")}</option>' for c in cats)
    out_opts = "".join(
        f'<option value="{_e(o)}"{" selected" if o == filters["outcome"] else ""}>'
        f'{_e(o or "any")}</option>' for o in ("", "success", "failure"))
    pages = max(1, -(-total // per))
    prev = f'<a class="btn" href="?{_qs(filters, page-1)}">Previous</a>' if page > 1 else ""
    nxt = f'<a class="btn" href="?{_qs(filters, page+1)}">Next</a>' if page < pages else ""
    body = f"""<h1>Audit</h1><p class="sub">{total} entries. No plaintext audio, session
keys or private key material is ever written here.</p>
<div class="panel"><form class="row" method="get">
<div><label>Category</label><select name="category">{cat_opts}</select></div>
<div><label>Outcome</label><select name="outcome">{out_opts}</select></div>
<div><label>Device</label><input name="device_id" value="{_e(filters['device_id'])}"></div>
<div><label>Session</label><input name="session_id" value="{_e(filters['session_id'])}"></div>
<div><label>Search</label><input name="q" value="{_e(filters['q'])}"></div>
<div class="narrow"><button class="primary">Filter</button></div>
<div class="narrow"><a class="btn" href="/admin/audit">Reset</a></div>
</form></div>
<div class="panel"><div class="scroll"><table><thead><tr><th>Time</th><th>Category</th>
<th>Action</th><th>Outcome</th><th>Device</th><th>Session</th><th>Detail</th>
</tr></thead><tbody>{_audit_full_rows(rows)}</tbody></table></div>
<div class="pager">{prev}<span class="muted">Page {page} of {pages}</span>{nxt}</div></div>"""
    return layout("Audit", body, "audit", who)


def _audit_full_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="7" class="muted">Nothing recorded.</td></tr>'
    out = []
    for r in rows:
        chip = ('<span class="chip ok">ok</span>' if r["outcome"] == "success"
                else '<span class="chip bad">' + _e(r["outcome"]) + "</span>")
        detail = r.get("detail_json") or "{}"
        if len(detail) > 300:
            detail = detail[:300] + "…"
        sid = r.get("session_id")
        sid_cell = (f'<a class="mono" href="/admin/sessions/{_e(sid)}">{_e(sid[:8])}…</a>'
                    if sid else '<span class="muted">—</span>')
        did = r.get("device_id")
        did_cell = (f'<a href="/admin/devices/{_e(did)}">{_e(did)}</a>'
                    if did else '<span class="muted">—</span>')
        out.append(
            f"""<tr><td class="mono muted nowrap">{_e((r['ts'] or '')[:19].replace('T',' '))}</td>
<td>{_e(r['category'])}</td><td class="mono">{_e(r['action'])}</td><td>{chip}</td>
<td class="nowrap">{did_cell}</td><td class="nowrap">{sid_cell}</td>
<td class="mono muted" style="max-width:420px;word-break:break-word">{_e(detail)}</td></tr>"""
        )
    return "".join(out)
