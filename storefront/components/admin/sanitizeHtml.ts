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

const TIDY_EMPTY = new Set(["div", "p", "li", "ul", "ol", "b", "strong", "i", "em", "u", "sub", "sup", "a"]);

// Visible text, ignoring spaces and the invisible characters eBay's editor leaves in "empty" lines.
const textOf = (el: Element) => (el.textContent ?? "").replace(/[\u200b-\u200d\u2060\ufeff\s]+/g, "");

// A list item outside a list, or text / a nested list sitting directly inside <ul>, shows a bullet that the
// editor cannot delete. Put every item inside a list and every list child inside an item.
function fixLists(root: Element): void {
  root.querySelectorAll("ul,ol").forEach((list) => {
    Array.from(list.childNodes).forEach((n) => {
      if (n.nodeType === Node.ELEMENT_NODE && (n as Element).tagName === "LI") return;
      if (n.nodeType === Node.TEXT_NODE && (n.textContent ?? "").trim() === "") {
        n.parentNode?.removeChild(n);
        return;
      }
      const prev = n.previousSibling;
      const isList = n.nodeType === Node.ELEMENT_NODE && /^(UL|OL)$/.test((n as Element).tagName);
      if (isList && prev && prev.nodeType === Node.ELEMENT_NODE && (prev as Element).tagName === "LI") {
        prev.appendChild(n); // a sub-list belongs inside the item before it
        return;
      }
      const li = document.createElement("li");
      list.insertBefore(li, n);
      li.appendChild(n);
    });
  });
  root.querySelectorAll("li").forEach((li) => {
    const parent = li.parentElement;
    if (parent && (parent.tagName === "UL" || parent.tagName === "OL")) return;
    const ul = document.createElement("ul");
    li.before(ul);
    ul.appendChild(li);
    while (ul.nextSibling && ul.nextSibling.nodeType === Node.ELEMENT_NODE && (ul.nextSibling as Element).tagName === "LI") {
      ul.appendChild(ul.nextSibling);
    }
  });
}

// Removes the empty wrappers, empty bullets and stray line breaks the eBay editor leaves behind.
function tidy(root: Element): void {
  root.querySelectorAll("br").forEach((br) => {
    // A <br> at the very end of a block is invisible; drop it unless it is the only content.
    const parent = br.parentElement;
    if (parent && parent !== root && !br.nextSibling && br.previousSibling && parent.tagName !== "TD") br.remove();
  });
  fixLists(root);
  for (let pass = 0; pass < 12; pass++) {
    let removed = false;
    // innermost first, so a list whose only item was empty goes too
    Array.from(root.querySelectorAll("*")).reverse().forEach((el) => {
      const name = el.tagName.toLowerCase();
      if (!TIDY_EMPTY.has(name) || !root.contains(el)) return;
      if (textOf(el) === "" && !el.querySelector("hr,table")) {
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
