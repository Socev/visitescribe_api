"""A report as the doctor copies it: one block per section (S, O, E, P, ...).

Since 1.13.0 OurMind's sections are stored as they arrive (notes.sections_json:
[{"title": "S:", "text": "..."}, ...]). Reports made before that, and reports
from other providers, only have the joined body text; for those the sections
are recovered from the headings in it. A body without recognisable headings
stays one block, never a guess.
"""
from __future__ import annotations

import json
import re
from typing import Any

# A line that is only a heading: "S:", "S", "Subjectief:", "**P:**", "## Plan".
_HEADING_LINE = re.compile(
    r"^\s*(?:#{1,6}\s*)?\**\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 ()/\-]{0,38}?)\s*:?\s*\**\s*:?\s*$")
# A heading with its text on the same line, only for the well-known SOEP-style
# headings, so "Advies: ..." inside a plan never splits a section.
_KNOWN = r"(?:S|O|E|P|A|Subjectief|Objectief|Evaluatie|Plan|Anamnese|Onderzoek|" \
         r"Conclusie|Beleid|Assessment)"
_INLINE = re.compile(
    rf"^\s*\**\s*({_KNOWN})(?:\s*\([^)]*\))?\s*\**\s*[:\-–—]\s*\**\s*(\S.*)$")


def _is_heading(line: str) -> str | None:
    raw = line.strip()
    if not raw or len(raw) > 44:
        return None
    m = _HEADING_LINE.match(raw)
    if not m:
        return None
    word = m.group(1).strip()
    # Needs a colon, a markdown heading, or to be a known SOEP heading; a
    # short sentence without a colon ("Geen bijzonderheden") is text.
    if raw.endswith(":") or raw.endswith(":**") or raw.lstrip().startswith("#") \
            or re.fullmatch(_KNOWN, word):
        return word
    return None


def _display(title: str) -> str:
    title = title.strip().rstrip(":").strip()
    return f"{title}:" if len(title) <= 2 else title


def sections_of(body: str | None, sections_json: str | None = None) -> list[dict[str, str]]:
    """[{"title": "S:", "text": "..."}]; title "" for a body without headings."""
    if sections_json:
        try:
            stored = json.loads(sections_json)
        except (TypeError, ValueError):
            stored = None
        if isinstance(stored, list):
            out = [{"title": _display(str(s.get("title") or "")) if s.get("title") else "",
                    "text": str(s.get("text") or "").strip()}
                   for s in stored if isinstance(s, dict)]
            out = [s for s in out if s["text"]]
            if out:
                return out
    text = (body or "").strip()
    if not text:
        return []
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        heading = _is_heading(line)
        inline = None if heading else _INLINE.match(line)
        if heading or inline:
            current = {"title": _display(heading or inline.group(1)), "lines": []}
            sections.append(current)
            if inline:
                current["lines"].append(inline.group(2))
            continue
        if current is None:
            current = {"title": "", "lines": []}
            sections.append(current)
        current["lines"].append(line)
    out = [{"title": s["title"], "text": "\n".join(s["lines"]).strip()} for s in sections]
    out = [s for s in out if s["text"] or s["title"]]
    # One heading-less block (or nothing recognisable): keep the body as is.
    if len(out) <= 1 and not (out and out[0]["title"]):
        return [{"title": "", "text": text}]
    return [s for s in out if s["text"]]
