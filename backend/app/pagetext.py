"""
Text for the About and Help pages.

These pages are edited in the admin area with the same visual toolbar as product
descriptions, so what is stored is cleaned HTML (see app/sanitize.py for the rules).

Older versions of these pages stored plain text, where a line starting with "## " was a
heading and a line starting with "- " was a bullet. Those rows (and the starter policies in
app/help_pages.py) are still plain text in that style, so page_html() turns them into HTML
when they are read. They become real HTML the next time the page is saved.

  page_html(raw)         what to show: safe HTML, "" when there is nothing
  clean_page_input(raw)  what to store when a page is saved
"""
import re
from html import escape
from typing import Optional

from app.sanitize import clean_description

# Tags that only HTML text (not the old plain-text style) would contain.
_HTML_HINT = re.compile(r"</?(?:p|div|br|hr|ul|ol|li|b|strong|i|em|u|h[1-6]|a|table)\b", re.I)
_BLOCK = re.compile(r"<(?:p|div|h[1-6]|ul|ol|table|hr)\b", re.I)


def _legacy_to_html(text: str) -> str:
    """Old plain text -> HTML. Everything typed is escaped, so it can only become text."""
    out: list[str] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(escape(p, quote=False) for p in para) + "</p>")
        para.clear()

    def flush_bullets() -> None:
        if bullets:
            out.append("<ul>" + "".join(f"<li>{escape(b, quote=False)}</li>" for b in bullets) + "</ul>")
        bullets.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            flush_bullets()
        elif line.startswith("## "):
            flush_para()
            flush_bullets()
            out.append(f"<h3>{escape(line[3:].strip(), quote=False)}</h3>")
        elif line.startswith("- "):
            flush_para()
            bullets.append(line[2:].strip())
        else:
            flush_bullets()
            para.append(line)
    flush_para()
    flush_bullets()
    return "".join(out)


def page_html(raw: Optional[str]) -> str:
    """Safe HTML for a stored page body (plain text in the old style is converted first)."""
    raw = raw or ""
    if not raw.strip():
        return ""
    html = raw if _HTML_HINT.search(raw) else _legacy_to_html(raw)
    return clean_description(html) or ""


def clean_page_input(raw: Optional[str]) -> str:
    """Clean a body that is about to be saved.

    The browser's editor leaves the first line bare (no tag around it) until Enter is
    pressed. Wrapping any leading bare text in a <div> means a saved body always contains
    at least one tag, which is how page_html() tells it apart from old plain text.
    """
    html = clean_description(raw) or ""
    if not html:
        return ""
    m = _BLOCK.search(html)
    if m is None:
        return f"<div>{html}</div>"
    if m.start() > 0:
        return f"<div>{html[:m.start()]}</div>{html[m.start():]}"
    return html
