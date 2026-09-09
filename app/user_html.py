"""The pages a doctor sees: their own recordings and what happens to them.

A separate module from admin_html on purpose. This is a different audience
with a different vocabulary -- Dutch throughout, no chunk hashes, no device
internals -- and it runs as a separate app so that sharing rendering helpers
never becomes a route from one to the other.

Everything is inline. The pod serves this with no CDN and no network.
"""
from __future__ import annotations

import html
from typing import Any

from .admin_html import CSS, JS

EXTRA_CSS = """
.hero{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap;margin-bottom:6px}
.hero .who{font-size:13px;color:var(--muted)}
.cards{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.rec{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.rec .when{font-weight:600;letter-spacing:-.01em}
.rec .meta{color:var(--muted);font-size:12.5px;margin-top:2px}
.rec .foot{margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.typerow{display:grid;gap:10px;align-items:center;
grid-template-columns:minmax(150px,1fr) minmax(130px,180px) minmax(150px,1fr) auto;
padding:12px 0;border-top:1px solid var(--line)}
.typerow:first-of-type{border-top:0}
.typerow .name{font-weight:600}
.typerow .hint{color:var(--muted);font-size:12.5px;font-weight:400;margin-top:1px}
@media(max-width:760px){.typerow{grid-template-columns:1fr}}
.empty{color:var(--muted);padding:26px 0;text-align:center}
.loginwrap{max-width:420px;margin:14vh auto 0}
.note{white-space:pre-wrap;background:var(--code);border:1px solid var(--line);
border-radius:8px;padding:12px;font-size:13.5px;line-height:1.55}
.two{display:grid;gap:14px;grid-template-columns:1fr 1fr}
@media(max-width:900px){.two{grid-template-columns:1fr}}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;background:var(--chip);
font-size:12px;color:var(--muted)}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def layout(title: str, body: str, active: str = "", who: str = "",
           signed_in: bool = True) -> str:
    nav = ""
    signout = ""
    if signed_in:
        items = [("opnames", "/", "Mijn opnames"),
                 ("instellingen", "/instellingen", "Instellingen")]
        nav = "".join(
            f'<a href="{url}" class="{"active" if key == active else ""}">{label}</a>'
            for key, url, label in items)
        signout = ('<form method="post" action="/uitloggen" style="margin:8px 0">'
                   '<button>Uitloggen</button></form>')
    who_html = f'<span class="who">{_e(who)}</span>' if who else ""
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · VisiteScribe</title><style>{CSS}{EXTRA_CSS}</style></head><body>
<header class="top"><div class="brand">VisiteScribe<span>voor de praktijk</span></div>
<nav>{nav}</nav><div class="spacer"></div>{who_html}{signout}</header>
<main>{body}</main><div id="toast"></div><script>{JS}</script></body></html>"""


# ---------------------------------------------------------------------------
# signing in
# ---------------------------------------------------------------------------

def render_login(*, email: str = "", stage: str = "email", error: str = "",
                 notice: str = "") -> str:
    err = f'<div class="banner bad">{_e(error)}</div>' if error else ""
    note = f'<div class="banner">{_e(notice)}</div>' if notice else ""
    if stage == "code":
        form = f"""
<form method="post" action="/inloggen/code">
  <input type="hidden" name="email" value="{_e(email)}">
  <p class="sub">We hebben een code gestuurd naar <b>{_e(email)}</b>.
     Hij komt van OurMind en is een paar minuten geldig.</p>
  <label>Code uit de e-mail</label>
  <input name="code" inputmode="numeric" autocomplete="one-time-code" autofocus
         placeholder="123456" required>
  <div style="margin-top:14px;display:flex;gap:10px;align-items:center">
    <button type="submit">Inloggen</button>
    <a href="/inloggen">ander adres</a>
  </div>
</form>"""
    else:
        form = """
<form method="post" action="/inloggen">
  <p class="sub">Je logt in met je OurMind-account. Vul je e-mailadres in, dan
     sturen we je een code.</p>
  <label>E-mailadres bij OurMind</label>
  <input name="email" type="email" autocomplete="email" autofocus required>
  <div style="margin-top:14px"><button type="submit">Stuur me een code</button></div>
</form>"""
    body = f"""<div class="loginwrap"><div class="panel">
<h1>Inloggen</h1>{err}{note}{form}
</div></div>"""
    return layout("Inloggen", body, signed_in=False)


# ---------------------------------------------------------------------------
# recordings
# ---------------------------------------------------------------------------

_STATE_LABEL = {
    "INGESTED": ("Binnen", ""),
    "READY_FOR_PROCESSING": ("Binnen", ""),
    "TRANSCRIBING": ("Bezig", "warn"),
    "PROCESSING": ("Bezig", "warn"),
    "REVIEW_REQUIRED": ("Klaar om na te lezen", "ok"),
    "APPROVED": ("Akkoord", "ok"),
    "TRANSCRIPTION_FAILED": ("Mislukt", "bad"),
    "PROCESSING_FAILED": ("Mislukt", "bad"),
    "PURGED": ("Gewist", ""),
}


def _state_pill(state: str) -> str:
    label, tone = _STATE_LABEL.get(state, (state.replace("_", " ").capitalize(), ""))
    cls = f"chip {tone}" if tone else "chip"
    return f'<span class="{cls}">{_e(label)}</span>'


def _duration(seconds: Any) -> str:
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "—"
    if total <= 0:
        return "—"
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}" if minutes else f"{secs}s"


def render_recordings(d: dict[str, Any], who: str) -> str:
    rows = d.get("recordings") or []
    types = {t["mode"]: t for t in d.get("types") or []}
    if not rows:
        cards = """<div class="panel"><div class="empty">
Nog geen opnames. Zodra je recorder iets instuurt verschijnt het hier.</div></div>"""
    else:
        cards = '<div class="cards">' + "".join(
            f"""<div class="rec">
<div class="when">{_e(_when(r))}</div>
<div class="meta">{_e(types.get(r['mode'], {}).get('title') or r['mode'])}
 · {_e(_duration(r.get('duration_seconds')))}
 {'· ' + _e(str(r['segment_count'])) + ' patiënten' if (r.get('segment_count') or 0) > 1 else ''}</div>
<div class="foot">{_state_pill(r['state'])}
{'<span class="pill">' + _e(r['route']) + '</span>' if r.get('route') else ''}
<span class="spacer" style="flex:1"></span>
<a href="/opname/{_e(r['session_id'])}">bekijken</a></div>
</div>""" for r in rows)
        cards += "</div>"
    return layout("Mijn opnames", f"""
<div class="hero"><div><h1>Mijn opnames</h1>
<p class="sub">Alles wat jouw recorder heeft ingestuurd.</p></div></div>
{cards}""", active="opnames", who=who)


def _when(row: dict[str, Any]) -> str:
    value = row.get("started_at") or row.get("created_at") or ""
    return str(value).replace("T", " ")[:16].replace("Z", "")


def render_recording(d: dict[str, Any], who: str) -> str:
    rec = d["recording"]
    types = {t["mode"]: t for t in d.get("types") or []}
    kind = types.get(rec["mode"], {}).get("title") or rec["mode"]

    pairs = []
    for item in d.get("results") or []:
        title = ("Hele opname" if item.get("segment_index") is None
                 else f"Patiënt {int(item['segment_index']) + 1}")
        transcript = item.get("transcript") or ""
        note = item.get("note") or ""
        pairs.append(f"""<div class="panel"><h2>{_e(title)}</h2>
<div class="two">
  <div><h3>Verslag</h3>
    <div class="note">{_e(note) or '<i>nog geen verslag</i>'}</div></div>
  <div><h3>Transcript</h3>
    <div class="note">{_e(transcript) or '<i>nog geen transcript</i>'}</div></div>
</div></div>""")
    results = "".join(pairs) or """<div class="panel"><div class="empty">
Er is nog niets verwerkt voor deze opname.</div></div>"""

    routes = d.get("allowed_routes") or []
    options = "".join(f'<option value="{_e(r)}">{_e(r)}</option>' for r in routes)
    action = ""
    if routes and not d.get("busy"):
        action = f"""<div class="panel"><h2>Verwerken</h2>
<p class="sub">Toegestaan voor {_e(kind)}: <b>{_e(', '.join(routes))}</b>.</p>
<form method="post" action="/opname/{_e(rec['session_id'])}/verwerken"
      style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
<select name="route">{options}</select><button>Verwerken</button></form></div>"""

    return layout("Opname", f"""
<div class="hero"><div><h1>{_e(_when(rec))}</h1>
<p class="sub">{_e(kind)} · {_e(_duration(rec.get('duration_seconds')))}
 · {_state_pill(rec['state'])}</p></div></div>
{action}{results}
<p><a href="/">← alle opnames</a></p>""", active="opnames", who=who)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def render_settings(d: dict[str, Any], who: str) -> str:
    templates = d.get("templates") or []
    rules = d.get("rules") or {}
    banner = ""
    if d.get("template_error"):
        banner = (f'<div class="banner warn">Templates konden niet opgehaald '
                  f'worden: {_e(d["template_error"])}</div>')

    rows = []
    for kind in d.get("types") or []:
        mode = kind["mode"]
        rule = rules.get(mode) or {}
        route_opts = ['<option value="">niet automatisch</option>']
        for route in kind.get("allowed") or []:
            sel = " selected" if rule.get("route") == route else ""
            route_opts.append(f'<option value="{_e(route)}"{sel}>{_e(route)}</option>')

        tmpl_opts = ['<option value="">standaard van OurMind</option>']
        for t in templates:
            value = f"{t['id']}:{t['type']}"
            sel = " selected" if rule.get("template_id") == t["id"] else ""
            tmpl_opts.append(
                f'<option value="{_e(value)}"{sel}>{_e(t["title"])}</option>')

        auto = " checked" if rule.get("auto") else ""
        hint = kind.get("description") or ""
        if not kind.get("patient_audio"):
            hint = (hint + " Geen patiëntaudio.").strip()
        rows.append(f"""<div class="typerow">
<div><div class="name">{_e(kind['title'])}</div>
     <div class="hint">{_e(hint)}</div></div>
<div><select name="route__{_e(mode)}">{''.join(route_opts)}</select></div>
<div><select name="template__{_e(mode)}">{''.join(tmpl_opts)}</select></div>
<div><label style="display:flex;gap:7px;align-items:center;white-space:nowrap">
<input type="checkbox" name="auto__{_e(mode)}"{auto}> meteen versturen</label></div>
</div>""")

    quota = d.get("quota") or {}
    quota_html = ""
    if quota.get("monthly_reports") is not None:
        quota_html = (f'<p class="sub">OurMind: <b>{_e(quota.get("reports_left"))}</b> '
                      f'van {_e(quota.get("monthly_reports"))} verslagen over deze maand.</p>')

    return layout("Instellingen", f"""
<div class="hero"><div><h1>Instellingen</h1>
<p class="sub">Bepaal per soort opname waar hij heen gaat en met welk
OurMind-template het verslag gemaakt wordt.</p></div></div>
{banner}
<form method="post" action="/instellingen">
<div class="panel">
<div class="typerow" style="color:var(--muted);font-size:12px;
text-transform:uppercase;letter-spacing:.06em">
<div>Soort opname</div><div>Gaat naar</div><div>Template</div><div>Automatisch</div></div>
{''.join(rows)}
<div style="margin-top:16px"><button>Opslaan</button></div>
</div></form>
<div class="panel"><h2>OurMind</h2>
<p class="sub">Ingelogd als <b>{_e(d.get('email'))}</b>
{' · ' + _e(d['org_name']) if d.get('org_name') else ''}.</p>
{quota_html}
<form method="post" action="/ourmind/loskoppelen"
      onsubmit="return confirm('Hierna kan er niets meer naar OurMind tot je opnieuw inlogt.')">
<button class="danger">OurMind loskoppelen</button></form></div>
""", active="instellingen", who=who)
