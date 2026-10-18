// Cleans description HTML in the browser before it is shown in the editor
// (stored eBay HTML, or something pasted in). It mirrors the server's rules in
// backend/app/sanitize.py: only the tags below survive, nearly every attribute
// is dropped, and anything that can run code is removed.
//
// The text is parsed with DOMParser, which builds an inert document: scripts do
// not run and images do not load while it is being read. A fresh, clean tree is
// then built from the allowed parts only.

const ALLOWED = new Set([
  "p", "div", "br", "hr", "ul", "ol", "li", "b", "strong", "i", "em", "u", "sub", "sup",
  "h2", "h3", "h4", "table", "thead", "tbody", "tr", "th", "td", "a",
]);
const RENAME: Record<string, string> = { h1: "h2", h5: "h4", h6: "h4" };
// Removed together with everything inside them.
const DROP = new Set([
  "script", "style", "iframe", "frame", "frameset", "object", "embed", "noscript", "template",
  "svg", "math", "textarea", "title", "head", "select", "applet",
]);
const SAFE_LINK = /^(https?:|mailto:|tel:)/i;

export function isSafeLink(href: string): boolean {
  // Browsers ignore whitespace and control characters inside a scheme.
  // eslint-disable-next-line no-control-regex
  return SAFE_LINK.test(href.replace(/[\u0000-\u0020\u007f-\u009f]+/g, ""));
}

function copyInto(source: Node, target: Node): void {
  source.childNodes.forEach((child) => {
    if (child.nodeType === Node.TEXT_NODE) {
      target.appendChild(document.createTextNode((child.textContent ?? "").replace(/\u00a0/g, " ")));
      return;
    }
    if (child.nodeType !== Node.ELEMENT_NODE) return; // comments etc.
    const el = child as Element;
    const tag = el.tagName.toLowerCase();
    if (DROP.has(tag)) return;
    const name = RENAME[tag] ?? tag;
    if (!ALLOWED.has(name)) {
      copyInto(el, target); // unknown tag: keep its text, lose the tag
      return;
    }
    const clean = document.createElement(name);
    if (name === "a") {
      const href = el.getAttribute("href") ?? "";
      if (isSafeLink(href)) {
        clean.setAttribute("href", href.trim());
        clean.setAttribute("target", "_blank");
        clean.setAttribute("rel", "noopener noreferrer nofollow");
      }
    } else if (name === "td" || name === "th") {
      for (const attr of ["colspan", "rowspan"]) {
        const v = el.getAttribute(attr);
        if (v && /^\d{1,3}$/.test(v)) clean.setAttribute(attr, v);
      }
    }
    copyInto(el, clean);
    target.appendChild(clean);
  });
}

const TIDY_EMPTY = new Set(["div", "p", "li", "b", "strong", "i", "em", "u", "sub", "sup", "a"]);

// Removes the empty wrappers and stray line breaks the eBay editor leaves behind.
function tidy(root: Element): void {
  root.querySelectorAll("br").forEach((br) => {
    // A <br> at the very end of a block is invisible; drop it unless it is the only content.
    const parent = br.parentElement;
    if (parent && parent !== root && !br.nextSibling && br.previousSibling && parent.tagName !== "TD") br.remove();
  });
  for (let pass = 0; pass < 5; pass++) {
    let removed = false;
    root.querySelectorAll("*").forEach((el) => {
      const name = el.tagName.toLowerCase();
      if (!TIDY_EMPTY.has(name) || !root.contains(el)) return;
      const blank = (el.textContent ?? "").trim() === "" && !el.querySelector("hr,table,ul,ol,li");
      if (blank) {
        el.remove();
        removed = true;
      }
    });
    if (!removed) break;
  }
}

/** Returns safe, tidy HTML for `raw` ("" when there is nothing visible). */
export function sanitizeHtml(raw: string): string {
  if (!raw || !raw.trim()) return "";
  const parsed = new DOMParser().parseFromString(raw, "text/html");
  const holder = document.createElement("div");
  copyInto(parsed.body, holder);
  tidy(holder);
  return holder.innerHTML.trim();
}
