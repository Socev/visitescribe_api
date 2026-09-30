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
.list{display:flex;flex-direction:column;gap:10px}
.list+.day{margin-top:28px}
.row{display:flex;align-items:center;gap:10px;background:var(--panel);
 border:1px solid var(--line);border-radius:12px;padding:12px 14px 12px 18px;
 color:inherit;text-decoration:none}
.row a.body{color:inherit;text-decoration:none}
.row form{margin:0}
.row .open{color:inherit;text-decoration:none}
.round-label{font-weight:600}
button.trash{background:transparent;border:1px solid transparent;border-radius:999px;
 padding:7px 9px;line-height:0;color:var(--muted);cursor:pointer}
button.trash.wide{display:inline-flex;align-items:center;gap:8px;line-height:1.2;
 padding:7px 14px 7px 10px;font-size:13.5px;border-color:var(--line)}
button.trash:hover{border-color:var(--line);color:var(--bad,#b42332)}
.ptbar{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 14px}
.ptbar button{border-radius:999px;padding:7px 14px;font-size:13.5px;max-width:340px;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap;background:var(--panel);
 border:1px solid var(--line);color:inherit;cursor:pointer}
.ptbar button.on{background:var(--chip);border-color:var(--muted);font-weight:600}
.ptbar button.gone{color:var(--muted);text-decoration:line-through}
.report h2.rtitle{margin:0;font-size:20px;letter-spacing:-.01em}
.report .rhead{display:flex;gap:10px;align-items:flex-start;justify-content:space-between}
.report .rtools{display:flex;gap:6px;align-items:center;flex:none}
.tabs{display:flex;gap:18px;border-bottom:1px solid var(--line);margin:12px 0 4px}
.tabs button{background:none;border:0;border-bottom:2px solid transparent;border-radius:0;
 padding:8px 2px;font-size:14px;color:var(--muted);cursor:pointer}
.tabs button.on{color:inherit;border-bottom-color:currentColor;font-weight:600}
.sec{padding:14px 0;border-top:1px solid var(--line)}
.sec:first-child{border-top:0}
.sec .sechead{display:flex;justify-content:space-between;align-items:center;gap:10px}
.sec .sechead b{font-size:15px}
.sec .sectext{white-space:pre-wrap;line-height:1.6;font-size:14.5px;margin-top:6px}
.verify{color:var(--muted);font-style:italic;font-size:12.5px;margin:10px 0 0}
.row:hover{border-color:var(--muted)}
.row .body{flex:1;min-width:0}
.row .title{font-size:15px;line-height:1.35;overflow:hidden;text-overflow:ellipsis;
 white-space:nowrap}
.row .title .seg{color:var(--muted);font-size:13px;margin-right:6px}
.row .title.none{color:var(--muted);font-style:italic}
.row .meta{color:var(--muted);font-size:12.5px;margin-top:3px;display:flex;gap:6px;
 align-items:center;flex-wrap:wrap}
.row .open{flex:none;background:var(--chip);border-radius:999px;padding:7px 16px;
 font-size:13.5px;font-weight:500}
@media(max-width:560px){.row .title{white-space:normal}.row .open{display:none}.row{padding:12px 14px}}
.day{display:flex;align-items:center;gap:12px;margin:18px 0 10px;font-size:13px;
 font-weight:600;letter-spacing:.02em;text-transform:uppercase;color:var(--muted)}
.day::after{content:"";flex:1;height:1px;background:var(--line)}
.rec .when{font-size:17px}
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
.headrow{display:flex;align-items:baseline;gap:10px;justify-content:space-between}
.headrow h3{margin:18px 0 8px}
button.copy{font-size:12px;padding:3px 10px;line-height:1.5}
.live{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}
.live .dot{width:7px;height:7px;border-radius:50%;background:var(--ok);
animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.35}50%{opacity:1}}
"""


# The page reloads itself when the server says something changed, rather than
# on a timer: a blind reload every ten seconds throws away your scroll position
# and your place in a transcript for nothing. `/api/stand` returns a cheap
# fingerprint; only a different one triggers the reload.
LIVE_JS = """
function copyBlock(button, id){
  const el = document.getElementById(id);
  if(!el) return;
  const text = el.innerText;
  const done = () => { const was = button.textContent;
    button.textContent = 'gekopieerd'; setTimeout(()=>{button.textContent = was}, 1500); };
  if(navigator.clipboard && window.isSecureContext){
    navigator.clipboard.writeText(text).then(done, () => fallback(text, done));
  } else { fallback(text, done); }
}
function fallback(text, done){
  // clipboard API needs a secure context; this works everywhere else.
  const ta = document.createElement('textarea');
  ta.value = text; ta.setAttribute('readonly','');
  ta.style.position = 'fixed'; ta.style.left = '-9999px';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); done(); } catch(e) { /* niets */ }
  document.body.removeChild(ta);
}
function showPt(id){
  document.querySelectorAll('.report').forEach(el => el.hidden = el.id !== id);
  document.querySelectorAll('.ptbar button').forEach(b => b.classList.toggle('on', b.dataset.pt === id));
  if(history.replaceState) history.replaceState(null, '', '#' + id);
}
function showTab(report, which){
  const r = document.getElementById(report);
  r.querySelectorAll('.pane').forEach(p => p.hidden = p.dataset.pane !== which);
  r.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.pane === which));
}
function pickPt(){
  const bar = document.querySelector('.ptbar');
  if(!bar) return;
  const want = location.hash.replace('#','');
  const ids = [...bar.querySelectorAll('button')].map(b => b.dataset.pt);
  const live = [...bar.querySelectorAll('button:not(.gone)')].map(b => b.dataset.pt);
  showPt(ids.includes(want) ? want : (live[0] || ids[0]));
}
function watch(url){
  let known = null, failures = 0;
  const tick = async () => {
    try {
      const r = await fetch(url, {headers: {'Accept': 'application/json'}});
      if(!r.ok) throw new Error(r.status);
      const stand = (await r.json()).stand;
      failures = 0;
      if(known === null){ known = stand; }
      else if(stand !== known){ location.reload(); return; }
    } catch(e) { failures += 1; }
    // back off rather than hammer a pod that is having a bad time
    setTimeout(tick, failures > 3 ? 60000 : 8000);
  };
  setTimeout(tick, 8000);
}
"""

def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def layout(title: str, body: str, active: str = "", who: str = "",
           signed_in: bool = True) -> str:
    nav = ""
    signout = ""
    if signed_in:
        items = [("opnames", "/", "Mijn opnames"),
                 ("apparaten", "/apparaten", "Mijn recorders"),
                 ("instellingen", "/instellingen", "Instellingen")]
        nav = "".join(
            f'<a href="{url}" class="{"active" if key == active else ""}">{label}</a>'
            for key, url, label in items)
        signout = ('<form method="post" action="/uitloggen" style="margin:8px 0">'
                   '<button>Uitloggen</button></form>')
    who_html = f'<span class="who">{_e(who)}</span>' if who else ""
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · VisiteScribe</title><style>{CSS}{EXTRA_CSS}</style>
<script>{JS}{LIVE_JS}</script></head><body>
<header class="top"><div class="brand">VisiteScribe<span>voor de praktijk</span></div>
<nav>{nav}</nav><div class="spacer"></div>{who_html}{signout}</header>
<main>{body}</main><div id="toast"></div></body></html>"""


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
        # One list per day, newest day first, with the date written once above
        # it instead of on every card. The day is the practice's day, so a
        # consultation at 00:30 sits under the night it belongs to, not under
        # the UTC date the row was stored with.
        cards = ""
        for label, day_rows in _by_day(rows):
            cards += (f'<h2 class="day">{_e(label)}</h2><div class="list">'
                      + "".join(_row(r, types) for r in day_rows) + "</div>")
    notice = (f'<div class="panel"><p class="sub" style="margin:0">{_e(d["notice"])}</p></div>'
              if d.get("notice") else "")
    return layout("Mijn opnames", f"""{notice}
<div class="hero"><div><h1>Mijn opnames</h1>
<p class="sub">Alles wat jouw recorder heeft ingestuurd.
<span class="live"><span class="dot"></span>ververst zichzelf</span></p></div></div>
{cards}
<script>watch('/api/stand');</script>""", active="opnames", who=who)


TRASH_SVG = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
             'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/>'
             '<path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>')


def _trash(action: str, question: str, label: str = "Wissen", text: bool = False) -> str:
    """A delete button that asks first. Deleting here is final.

    text=True shows the label next to the icon (the "whole round" button)."""
    inner = TRASH_SVG + (f'<span>{_e(label)}</span>' if text else "")
    cls = "trash wide" if text else "trash"
    return (f'<form method="post" action="{_e(action)}" '
            f'onsubmit="return confirm({_e(_js(question))})">'
            f'<button class="{cls}" title="{_e(label)}" aria-label="{_e(label)}">{inner}</button>'
            f'</form>')


def _is_round(r: dict[str, Any]) -> bool:
    titles = r.get("titles") or []
    return (r.get("segment_count") or 0) > 1 or any(
        t.get("segment_index") is not None for t in titles)


def _row(r: dict[str, Any], types: dict[str, Any]) -> str:
    """One recording as one line, the way OurMind lists its notes.

    A consult shows the report title. A recording with more patients is a
    "Visiteronde": one line per patient, numbered as the recorder numbered
    them ("Pt 1 - ..."), so the round reads as the visits it was. A
    recording with no report yet says so instead of showing an empty line.
    """
    kind = types.get(r["mode"], {}).get("title") or r["mode"]
    titles = r.get("titles") or []
    waiting = r["state"] in ("TRANSCRIBING", "PROCESSING") or r.get("route")
    if _is_round(r):
        count = max(r.get("segment_count") or 0, len(titles))
        lines = "".join(
            f'<div class="title"><span class="seg">Pt {_e(t["segment_index"])}</span>'
            + (f'<i>verwijderd</i>' if t.get("deleted") else _e(t["title"]))
            + '</div>' for t in titles if t.get("segment_index") is not None)
        if not lines:
            lines = (f'<div class="title none">'
                     f'{"wordt verwerkt" if waiting else "nog niet verwerkt"}</div>')
        head = (f'<div class="title"><span class="round-label">Visiteronde</span>'
                f' · {count} patiënten</div>{lines}')
        question = "Deze hele visiteronde wissen? Alle verslagen en transcripten verdwijnen definitief."
    elif titles:
        head = f'<div class="title">{_e(titles[0]["title"])}</div>'
        question = "Deze opname wissen? Verslag en transcript verdwijnen definitief."
    else:
        head = (f'<div class="title none">{_e(kind)} · '
                f'{"wordt verwerkt" if waiting else "nog niet verwerkt"}</div>')
        question = "Deze opname wissen? Hij verdwijnt definitief."
    route = f'<span class="pill">{_e(r["route"])}</span>' if r.get("route") else ""
    href = f"/opname/{_e(r['session_id'])}"
    return f"""<div class="row">
<a class="body" href="{href}">{head}
<div class="meta"><span>{_e(_when(r))}</span><span>· {_e(kind)}</span>
<span>· {_e(_duration(r.get('duration_seconds')))}</span>
{_state_pill(r['state'])}{route}</div></a>
<a class="open" href="{href}">Open</a>
{_trash(f"/opname/{r['session_id']}/wissen", question)}</div>"""


def _started(row: dict[str, Any]) -> str:
    return row.get("started_at") or row.get("created_at") or ""


def _when(row: dict[str, Any]) -> str:
    """On the practice's clock, not UTC.

    Timestamps are stored in UTC, which is right; showing them in UTC is not.
    A consultation at 21:45 in Leusden was appearing as 19:45. The date is on
    the day divider above the card, so the card itself carries only the time.
    """
    from .util import local_time

    return local_time(_started(row), with_date=False)[:5]


def _by_day(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group an already newest-first list into (label, rows) per practice day.

    A row whose timestamp cannot be parsed keeps its place in the list under
    the divider of the row before it rather than vanishing; a doctor must never
    see fewer recordings than the recorder sent.
    """
    from .util import dutch_date, local_datetime

    groups: list[tuple[str, list[dict[str, Any]]]] = []
    current = object()
    for row in rows:
        local = local_datetime(_started(row))
        key = local.date() if local else current
        if key != current or not groups:
            current = key
            label = dutch_date(local) if local else "Datum onbekend"
            groups.append((label, []))
        groups[-1][1].append(row)
    return groups


def _mmss(seconds: Any) -> str:
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    return f"{total // 60}:{total % 60:02d}"


_STATUS_TEXT = {
    "busy": "Wordt verwerkt…",
    "failed": "Verwerken is niet gelukt. Kies hieronder opnieuw verwerken, of vraag de beheerder.",
    "waiting": "Nog niet verwerkt.",
    "deleted": "Dit verslag is gewist.",
}


def _report(p: dict[str, Any], rec: dict[str, Any], round_: bool) -> str:
    """One patient's report, the way OurMind shows a note."""
    idx = p["segment_index"]
    rid = "pt" + (str(idx) if idx is not None else "0")
    if p["status"] != "ready":
        title = (f"Patiënt {idx}" if round_ else "Verslag")
        body = f'<p class="sub">{_e(_STATUS_TEXT.get(p["status"], ""))}</p>'
        tools = ""
        if round_ and p["status"] != "deleted" and idx is not None:
            tools = _trash(f"/opname/{rec['session_id']}/patient/{idx}/wissen",
                           f"Patiënt {idx} uit deze visiteronde wissen?",
                           "Deze patiënt wissen")
        return (f'<div class="panel report" id="{rid}"><div class="rhead">'
                f'<h2 class="rtitle">{_e(title)}</h2><div class="rtools">{tools}</div></div>'
                f'{body}</div>')

    title = p["title"] or (f"Patiënt {idx}" if round_ else "Verslag")
    secs = []
    full = []
    for n, sec in enumerate(p["sections"]):
        sid = f"{rid}-s{n}"
        head = _e(sec["title"]) if sec["title"] else ""
        secs.append(
            f'<div class="sec"><div class="sechead"><b>{head}</b>'
            f'<button class="copy" onclick="copyBlock(this, {_e(_js(sid))})">kopieer</button></div>'
            f'<div class="sectext" id="{sid}">{_e(sec["text"])}</div></div>')
        full.append((sec["title"] + "\n" if sec["title"] else "") + sec["text"])
    all_id = f"{rid}-all"
    trid = f"{rid}-tr"
    length = _mmss(p.get("seconds"))
    tools = (f'<button class="copy" onclick="copyBlock(this, {_e(_js(all_id))})">'
             f'alles kopiëren</button>')
    if round_ and idx is not None:
        tools += _trash(f"/opname/{rec['session_id']}/patient/{idx}/wissen",
                        f"Het verslag van patiënt {idx} wissen? Dit is definitief.",
                        "Deze patiënt wissen")
    transcript = p.get("transcript") or ""
    return f"""<div class="panel report" id="{rid}">
<div class="rhead"><h2 class="rtitle">{_e(title)}</h2><div class="rtools">{tools}</div></div>
<div class="tabs">
 <button class="on" data-pane="note" onclick="showTab({_e(_js(rid))},'note')">Verslag</button>
 <button data-pane="tr" onclick="showTab({_e(_js(rid))},'tr')">Transcript{f' ({length})' if length else ''}</button>
</div>
<div class="pane" data-pane="note">
 <p class="verify">Controleer het verslag altijd: AI kan fouten maken.</p>
 {''.join(secs)}
 <div id="{all_id}" hidden style="white-space:pre-wrap">{_e((chr(10) * 2).join(full))}</div>
</div>
<div class="pane" data-pane="tr" hidden>
 <div class="headrow"><h3>Transcript</h3>
 {'<button class="copy" onclick="copyBlock(this, %s)">kopieer</button>' % _e(_js(trid)) if transcript else ''}</div>
 <div class="note" id="{trid}">{_e(transcript) or '<i>nog geen transcript</i>'}</div>
</div></div>"""


def render_recording(d: dict[str, Any], who: str) -> str:
    rec = d["recording"]
    types = {t["mode"]: t for t in d.get("types") or []}
    kind = types.get(rec["mode"], {}).get("title") or rec["mode"]
    patients = d.get("patients") or []
    round_ = len(patients) > 1 or any(p["segment_index"] is not None for p in patients)
    if round_:
        kind = "Visiteronde"

    bar = ""
    if round_:
        chips = []
        for p in patients:
            idx = p["segment_index"]
            label = f"Pt {idx}"
            if p["status"] == "deleted":
                label += " – verwijderd"
            elif p["title"]:
                label += f" – {p['title']}"
            elif p["status"] == "busy":
                label += " – wordt verwerkt"
            elif p["status"] == "failed":
                label += " – niet gelukt"
            gone = " gone" if p["status"] == "deleted" else ""
            chips.append(f'<button class="{gone.strip()}" data-pt="pt{_e(idx)}" '
                         f'onclick="showPt(\'pt{_e(idx)}\')" title="{_e(label)}">{_e(label)}</button>')
        bar = f'<div class="ptbar">{"".join(chips)}</div>'
    reports = "".join(_report(p, rec, round_) for p in patients) or \
        """<div class="panel"><div class="empty">Er is nog niets verwerkt voor deze opname.</div></div>"""

    routes = d.get("allowed_routes") or []
    options = "".join(f'<option value="{_e(r)}">{_e(r)}</option>' for r in routes)
    action = ""
    if rec.get("audio_purged_at"):
        action = """<p class="sub">De audio van deze opname is na verwerking verwijderd;
opnieuw verwerken kan niet meer. Verslag en transcript blijven hier staan.</p>"""
    elif routes and not d.get("busy"):
        action = f"""<div class="panel"><h2>Verwerken</h2>
<p class="sub">Toegestaan voor {_e(kind)}: <b>{_e(', '.join(routes))}</b>.</p>
<form method="post" action="/opname/{_e(rec['session_id'])}/verwerken"
      style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
<select name="route">{options}</select><button>Verwerken</button></form></div>"""

    what = "visiteronde" if round_ else "opname"
    delete_all = ('<div style="margin-top:18px">'
                  + _trash(f"/opname/{rec['session_id']}/wissen",
                           f"Deze hele {what} wissen? Alle verslagen en transcripten "
                           "verdwijnen definitief.", f"Hele {what} wissen", text=True)
                  + '</div>')
    live = ('<span class="live"><span class="dot"></span>ververst zichzelf</span>'
            if d.get("busy") else "")
    return layout("Opname", f"""
<p><a href="/">← alle opnames</a></p>
<div class="hero"><div><h1>{_e(_when(rec))} · {_e(kind)}</h1>
<p class="sub">{_e(_duration(rec.get('duration_seconds')))}
{f' · {len(patients)} patiënten' if round_ else ''}
 · {_state_pill(rec['state'])} {live}</p></div></div>
{bar}{reports}{action}{delete_all}
<script>pickPt(); watch('/api/stand?opname={_e(rec['session_id'])}');</script>""",
                  active="opnames", who=who)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def render_settings(d: dict[str, Any], who: str) -> str:
    templates = d.get("templates") or []
    rules = d.get("rules") or {}
    banner = ""
    if not d.get("auto_allowed", True):
        # A server-wide switch that silently disables a per-user checkbox is a
        # trap; it has to announce itself where the checkbox is.
        banner += ('<div class="banner warn">Automatisch versturen staat op deze '
                   'server uitgeschakeld. Je kunt het hieronder wel instellen, '
                   'maar er vertrekt niets vanzelf totdat de beheerder het '
                   'aanzet.</div>')
    if d.get("template_error"):
        banner += (f'<div class="banner warn">Templates konden niet opgehaald '
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


# ---------------------------------------------------------------------------
# my recorders: Wi-Fi and update status
# ---------------------------------------------------------------------------

_UPDATE_NL = {
    "pending": "klaargezet, wordt geïnstalleerd bij de volgende synchronisatie",
    "downloading": "wordt gedownload",
    "installing": "wordt geïnstalleerd",
    "deferred": "wacht tot Brian aan de lader staat (of de accu vol genoeg is)",
    "failed": "mislukt; de beheerder is op de hoogte",
}


def render_devices(d: dict[str, Any], who: str) -> str:
    from .util import local_time

    notice = ""
    if d.get("notice"):
        notice = f'<div class="banner info">{_e(d["notice"])}</div>'
    if d.get("error"):
        notice = f'<div class="banner bad">{_e(d["error"])}</div>'
    cards = []
    for dev in d.get("devices") or []:
        did = dev["device_id"]
        w = dev["wifi"]
        reported = w.get("reported")
        if reported is None:
            nets = ('<p class="sub">Brian heeft zijn netwerken nog niet doorgegeven. Dat '
                    'gebeurt bij de volgende synchronisatie.</p>')
        elif not reported:
            nets = '<p class="sub">Geen netwerken bekend.</p>'
        else:
            items = []
            for n in reported:
                last = len(reported) == 1
                warn = ("Dit is het laatste netwerk. Zonder netwerk moet Brian opnieuw "
                        "ingesteld worden via zijn eigen hotspot. Doorgaan?" if last else
                        f"{n} verwijderen van Brian?")
                items.append(
                    f'<li class="netrow"><span>{_e(n)}</span>'
                    f'<form method="post" action="/apparaten/{_e(did)}/wifi/verwijderen" '
                    f'onsubmit="return confirm({_e(_js(warn))})">'
                    f'<input type="hidden" name="ssid" value="{_e(n)}">'
                    f'<button class="danger">Verwijderen</button></form></li>')
            nets = f'<ul class="plain nets">{"".join(items)}</ul>'
            if len(reported) >= int(w.get("max_networks") or 8):
                nets += ('<p class="sub"><b>Brian is vol.</b> Verwijder eerst een netwerk; '
                         'een nieuw netwerk blijft anders wachten.</p>')
        pend = "".join(
            f'<li class="netrow"><span><span class="chip info">'
            f'{"wordt toegevoegd" if p["op"] == "add" else "wordt verwijderd"}</span> '
            f'{_e(p["ssid"])}</span>'
            f'<form method="post" action="/apparaten/{_e(did)}/wifi/{int(p["id"])}/annuleren">'
            f'<button>Annuleren</button></form></li>'
            for p in w.get("pending") or [])
        pend_html = (f'<h3>Onderweg naar Brian</h3><ul class="plain nets">{pend}</ul>'
                     f'<p class="sub">Brian haalt dit op bij de volgende synchronisatie, '
                     f'bijvoorbeeld als hij op de lader staat.</p>' if pend else "")
        upd = dev.get("update")
        upd_html = ""
        if upd and upd.get("state") in _UPDATE_NL:
            upd_html = (f'<p class="sub">Software-update {_e(upd["version"])}: '
                        f'{_e(_UPDATE_NL[upd["state"]])}.</p>')
        battery = dev.get("battery_percent")
        seen = local_time(dev.get("last_seen_at")) if dev.get("last_seen_at") else "nog nooit"
        cards.append(f"""<div class="panel">
<h2 style="margin-top:0">{_e(dev.get('display_name') or did)}</h2>
<p class="sub">Laatst gezien: {_e(seen)}
{(' · accu ' + _e(battery) + '%') if battery is not None else ''}
{(' · versie ' + _e(dev['software_version'])) if dev.get('software_version') else ''}</p>
{upd_html}
<h3>Wi-Fi-netwerken</h3>
{nets}{pend_html}
<form method="post" action="/apparaten/{_e(did)}/wifi" class="addnet" autocomplete="off">
<div><label>Netwerknaam</label><input name="ssid" required maxlength="32"></div>
<div><label>Wachtwoord</label><input name="password" type="password"
 autocomplete="new-password" maxlength="63"></div>
<div><button>Toevoegen</button></div></form>
<p class="sub" style="margin-top:8px">Het wachtwoord gaat versleuteld naar Brian en wordt
daarna van de server gewist. Brian onthoudt maximaal {int(w.get('max_networks') or 8)}
netwerken.</p></div>""")
    body = "".join(cards) or ('<div class="panel"><p class="sub">Er is nog geen recorder aan '
                              'je gekoppeld. Vraag de beheerder om Brian te koppelen.</p></div>')
    return layout("Mijn recorders", f"""
<style>.nets{{display:flex;flex-direction:column;gap:8px}}
.netrow{{display:flex;align-items:center;justify-content:space-between;gap:12px;
border:1px solid var(--line);border-radius:9px;padding:8px 12px}}
.netrow form{{margin:0}}
.addnet{{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-top:14px}}
.addnet>div{{flex:1;min-width:160px}}.addnet>div:last-child{{flex:0 0 auto;min-width:0}}</style>
<div class="hero"><div><h1>Mijn recorders</h1>
<p class="sub">Beheer de Wi-Fi-netwerken waarmee Brian zijn opnames verstuurt.</p></div></div>
{notice}{body}""", active="apparaten", who=who)


def _js(value: str) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
