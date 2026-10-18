"""
Cleans product descriptions (HTML pasted in by staff or imported from eBay).

Allowlist approach: only the tags below survive, almost every attribute is
dropped, and anything that can run code (script, style, iframe, event
handlers, javascript: links) is removed. Unknown tags are unwrapped, so their
text is kept. Output always has balanced tags, so a broken description cannot
break the page layout.

Used in two places (see admin_catalog.py and fitment.py):
  * when a product is saved, so the database holds clean text
  * when the store serves a product, so old or imported rows are safe too

Standard library only.
"""
import re
from html import escape
from html.parser import HTMLParser
from typing import Optional

# Tags kept as-is.
ALLOWED = {
    "p", "div", "br", "hr",
    "ul", "ol", "li",
    "b", "strong", "i", "em", "u", "sub", "sup",
    "h2", "h3", "h4",
    "table", "thead", "tbody", "tr", "th", "td",
    "a",
}
# Headings the old page might use are mapped down so they do not compete
# with the product title (an <h1>).
RENAME = {"h1": "h2", "h5": "h4", "h6": "h4"}
VOID = {"br", "hr"}
# Removed together with everything inside them.
DROP_WITH_CONTENT = {
    "script", "style", "iframe", "frame", "frameset", "object", "noscript",
    "template", "svg", "math", "textarea", "title", "head", "select", "applet",
}
# Attributes kept per tag.
ATTRS = {"a": {"href"}, "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}}
SAFE_LINK = re.compile(r"^(https?:|mailto:|tel:)", re.I)
CONTROL = re.compile(r"[\x00-\x20\x7f-\x9f]+")


def _clean_href(value: str) -> Optional[str]:
    # Browsers ignore whitespace/control characters inside a scheme, so
    # "java\nscript:" must not slip past the check.
    compact = CONTROL.sub("", value or "")
    return value.strip() if compact and SAFE_LINK.match(compact) else None


class _Cleaner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.skip_tag: Optional[str] = None
        self.skip_depth = 0

    # -- start tags ---------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.skip_tag, self.skip_depth = tag, 1
            return
        tag = RENAME.get(tag, tag)
        if tag not in ALLOWED:
            return  # unwrap: drop the tag, keep its text
        self._close_implied(tag)
        parts = [tag]
        keep = ATTRS.get(tag, set())
        for name, value in attrs:
            name = (name or "").lower()
            if name not in keep or value is None:
                continue
            if name == "href":
                href = _clean_href(value)
                if href is None:
                    continue
                parts.append(f'href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer nofollow"')
            elif value.isdigit() and len(value) <= 3:
                parts.append(f'{name}="{value}"')
        if tag in VOID:
            self.out.append(f"<{' '.join(parts)}>")
            return
        self.out.append(f"<{' '.join(parts)}>")
        self.open.append(tag)

    def _close_implied(self, tag: str) -> None:
        """A new <li>/<p>/<td>/<th>/<tr> ends the previous one at the same level."""
        scope = {"li": {"ul", "ol"}, "td": {"tr"}, "th": {"tr"}, "tr": {"table", "thead", "tbody"}, "p": set()}
        if tag not in scope:
            return
        same = {"td": {"td", "th"}, "th": {"td", "th"}}.get(tag, {tag})
        for open_tag in reversed(self.open):
            if open_tag in scope[tag] or (tag == "p" and open_tag != "p"):
                return
            if open_tag in same:
                while self.open:
                    top = self.open.pop()
                    self.out.append(f"</{top}>")
                    if top == open_tag:
                        return

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and not self.skip_tag:
            self.handle_endtag(tag)

    # -- end tags -----------------------------------------------------------
    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        tag = RENAME.get(tag, tag)
        if tag in VOID or tag not in self.open:
            return
        while self.open:  # close anything left open inside it, then the tag itself
            top = self.open.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    # -- text ---------------------------------------------------------------
    def handle_data(self, data):
        if self.skip_tag:
            return
        self.out.append(escape(data.replace("\xa0", " "), quote=False))

    # Comments, doctype, processing instructions and CDATA are ignored (the
    # base-class handlers do nothing).

    def result(self) -> str:
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        return "".join(self.out)


_EMPTY = re.compile(r"<(div|p|li|td|th|tr|a)(?: [^>]*)?>\s*</\1>", re.I)
_EMPTY_INLINE = re.compile(r"<(b|strong|i|em|u|sub|sup)>(?:\s|<br>)*</\1>", re.I)
_BR_RUN = re.compile(r"(?:<br>\s*){2,}", re.I)
_BR_AT_EDGE = re.compile(r"(?:<br>\s*)+(?=</(?:li|div|p|td|th)>)|(?<=<li>)(?:\s*<br>)+|(?<=<div>)(?:\s*<br>)+", re.I)
_SPACES = re.compile(r"[ \t\r\n]+")


def clean_description(raw: Optional[str]) -> Optional[str]:
    """Return a safe, tidied version of `raw`; None/blank stays None."""
    if raw is None or not raw.strip():
        return None
    parser = _Cleaner()
    parser.feed(raw)
    parser.close()
    html = parser.result()
    html = _SPACES.sub(" ", html)
    # Tidy what the eBay editor leaves behind. Looping handles nesting such as
    # <div><div></div></div>.
    for _ in range(6):
        before = html
        html = _BR_AT_EDGE.sub("", html)
        html = _BR_RUN.sub("<br>", html)
        html = _EMPTY_INLINE.sub("", html)
        html = _EMPTY.sub("", html)
        if html == before:
            break
    # A lone <br> between block elements only adds a blank gap.
    html = re.sub(r"(</(?:div|p|ul|ol|table)>)\s*(?:<br>\s*)+", r"\1", html, flags=re.I)
    html = re.sub(r"(?:<br>\s*)+(<(?:div|p|ul|ol|table)\b)", r"\1", html, flags=re.I)
    html = html.strip()
    return html or None
