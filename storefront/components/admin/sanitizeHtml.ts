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

const TIDY_EMPTY = new Set(["div", "p", "li", "ul", "ol", "b", "strong", "i", "em", "u", "sub", "sup", "a", "h2", "h3", "h4"]);
const INLINE_WRAP = new Set(["b", "strong", "i", "em", "u", "sub", "sup", "a"]);
const BLOCK = /^(DIV|P|H2|H3|H4|TABLE|HR)$/;
const LIST = /^(UL|OL)$/;

// Visible text, ignoring spaces and the invisible characters eBay's editor leaves in "empty" lines.
const INVISIBLE = /[\u200b-\u200d\u2060\ufeff\s]+/g;
const textOf = (el: Element) => (el.textContent ?? "").replace(INVISIBLE, "");
const hasObject = (el: Element) => !!el.querySelector("hr,table");
const isEl = (n: Node | null | undefined, re?: RegExp): n is Element =>
  !!n && n.nodeType === Node.ELEMENT_NODE && (!re || re.test((n as Element).tagName));
const blankNode = (n: Node) =>
  n.nodeType === Node.COMMENT_NODE || (n.nodeType === Node.TEXT_NODE && (n.textContent ?? "").replace(INVISIBLE, "") === "");
const isBlankBlock = (e: Element) => /^(DIV|P|H2|H3|H4)$/.test(e.tagName) && textOf(e) === "" && !hasObject(e);

// Bold, italic and underline sometimes wrap whole blocks (a bold <b> around a list, say). Then a line that is moved or given
// a bullet in the editor leaves the <b> behind and changes font. Put the formatting inside each block instead, so it
// travels with the text.
const FORMAT_WRAP = /^(B|STRONG|I|EM|U|SUB|SUP)$/;
const BLOCKISH = /^(DIV|P|H2|H3|H4|UL|OL|LI)$/;
function pushFormattingDown(root: Element): void {
  for (let pass = 0; pass < 40; pass++) {
    const w = Array.from(root.querySelectorAll("*")).find(
      (e) => FORMAT_WRAP.test(e.tagName) && Array.from(e.children).some((c) => BLOCKISH.test(c.tagName)),
    );
    if (!w) return;
    const wrapAround = (nodes: Node[]) => {
      const c = w.cloneNode(false);
      nodes.forEach((x) => c.appendChild(x));
      return c;
    };
    let run: Node[] = [];
    const flush = () => {
      if (run.length && run.some((x) => !blankNode(x))) w.before(wrapAround(run));
      else run.forEach((x) => w.before(x));
      run = [];
    };
    Array.from(w.childNodes).forEach((k) => {
      if (isEl(k, BLOCKISH)) {
        flush();
        const inner = Array.from(k.childNodes);
        if (inner.some((x) => !blankNode(x))) k.appendChild(wrapAround(inner));
        w.before(k);
      } else {
        run.push(k);
      }
    });
    flush();
    w.remove();
  }
}

// Bold / italic / link wrappers with no words in them only hide a <br> or nothing at all: keep what is inside.
function unwrapEmptyInline(root: Element): void {
  Array.from(root.querySelectorAll("*")).reverse().forEach((el) => {
    if (INLINE_WRAP.has(el.tagName.toLowerCase()) && textOf(el) === "" && !hasObject(el)) {
      el.replaceWith(...Array.from(el.childNodes));
    }
  });
}

// eBay's editor often starts a list item with an empty line (a <br> or an empty div) and puts the real text on the next
// line. The bullet then sits alone on the first line and cannot be selected or deleted. Such an item was never a real
// list item, so it is taken out of the list and its content is shown as ordinary text (the list is split around it).
function unwrapSpacerItems(root: Element): void {
  root.querySelectorAll("li").forEach((li) => {
    const list = li.parentElement;
    if (!list || !LIST.test(list.tagName) || textOf(li) === "") return; // fully empty ones are removed separately
    const first = Array.from(li.childNodes).find((n) => !blankNode(n));
    if (!(isEl(first) && (first.tagName === "BR" || isBlankBlock(first)))) return;
    first.remove(); // the empty first line itself
    const after = document.createElement(list.tagName.toLowerCase());
    while (li.nextSibling) after.appendChild(li.nextSibling);
    list.after(after);
    const kids = Array.from(li.childNodes).filter((c) => !blankNode(c));
    if (kids.every((c) => isEl(c, BLOCK) || isEl(c, LIST))) {
      kids.forEach((c) => after.before(c));
    } else {
      // text and inline parts: keep them together as their own paragraph so they do not run into the next line
      const div = document.createElement("div");
      kids.forEach((c) => div.appendChild(c));
      after.before(div);
    }
    li.remove();
  });
}

// A list may only hold list items. Anything else inside it (a heading or paragraph, loose text, a list that is not inside
// an item) shows up with a bullet the editor cannot delete, or makes the bullet button act on the line above. Items that
// hold nothing but a sub-list are replaced by that sub-list; a list placed directly in a list is merged into it;
// everything else is taken out of the list, which is split around it.
function normalizeList(list: Element): void {
  let n: Node | null = list.firstChild;
  while (n) {
    const next: Node | null = n.nextSibling;
    if (blankNode(n)) {
      n.parentNode?.removeChild(n);
      n = next;
      continue;
    }
    if (isEl(n, /^LI$/)) {
      const kids = Array.from(n.childNodes).filter((c) => !blankNode(c));
      if (kids.length > 0 && kids.every((c) => isEl(c, LIST))) {
        const first = kids[0];
        Array.from(n.childNodes).forEach((c) => list.insertBefore(c, n));
        n.remove();
        n = first;
        continue;
      }
      n = next;
      continue;
    }
    if (isEl(n, LIST)) {
      // a list placed directly inside a list is eBay's way of indenting; its bullets join the outer list so every
      // bullet behaves the same (a real sub-list inside an item is left alone)
      const kids = Array.from(n.childNodes);
      kids.forEach((c) => list.insertBefore(c, n));
      n.remove();
      n = kids[0] ?? next;
      continue;
    }
    // not a list item: take it out of the list (a run of loose text / inline parts becomes one paragraph)
    let out = n as ChildNode;
    if (!isEl(n, BLOCK) && !isEl(n, LIST)) {
      const div = document.createElement("div");
      list.insertBefore(div, n);
      let m: Node | null = n;
      while (m && !isEl(m, /^LI$/) && !isEl(m, BLOCK) && !isEl(m, LIST)) {
        const nx: Node | null = m.nextSibling;
        div.appendChild(m);
        m = nx;
      }
      out = div;
    }
    const after = document.createElement(list.tagName.toLowerCase());
    while (out.nextSibling) after.appendChild(out.nextSibling);
    list.after(out);
    out.after(after);
    normalizeList(after);
    return;
  }
}

// A list item outside any list shows a bullet too; give such items a list.
function wrapOrphanItems(root: Element): void {
  root.querySelectorAll("li").forEach((li) => {
    const parent = li.parentElement;
    if (parent && LIST.test(parent.tagName)) return;
    const ul = document.createElement("ul");
    li.before(ul);
    ul.appendChild(li);
    while (isEl(ul.nextSibling, /^LI$/)) ul.appendChild(ul.nextSibling);
  });
}

// Removes the empty wrappers, empty bullets and stray line breaks the eBay editor leaves behind.
function tidy(root: Element): void {
  pushFormattingDown(root);
  unwrapEmptyInline(root);
  unwrapSpacerItems(root);
  // bullets with nothing in them
  Array.from(root.querySelectorAll("li")).reverse().forEach((li) => {
    if (textOf(li) === "" && !hasObject(li)) li.remove();
  });
  root.querySelectorAll("br").forEach((br) => {
    const parent = br.parentElement;
    if (!parent || parent.tagName === "TD" || parent.tagName === "TH") return;
    // a <br> with nothing before it in its block only adds an empty first line
    let leading = true;
    for (let n = br.previousSibling; n; n = n.previousSibling) {
      if (!blankNode(n) && !isEl(n, /^BR$/)) leading = false;
    }
    // a <br> right before a block is an extra blank line; a <br> at the very end of a block is invisible
    const next = br.nextSibling;
    const beforeBlock = isEl(next, /^(DIV|P|UL|OL|TABLE|H2|H3|H4|HR)$/);
    const atEnd = parent !== root && !next && !!br.previousSibling;
    if ((leading && (next || parent !== root)) || beforeBlock || atEnd) br.remove();
  });
  Array.from(root.querySelectorAll("ul,ol")).reverse().forEach((list) => {
    if (root.contains(list)) normalizeList(list);
  });
  wrapOrphanItems(root);
  for (let pass = 0; pass < 12; pass++) {
    let removed = false;
    // innermost first, so a list whose only item was empty goes too
    Array.from(root.querySelectorAll("*")).reverse().forEach((el) => {
      if (!TIDY_EMPTY.has(el.tagName.toLowerCase()) || !root.contains(el)) return;
      if (textOf(el) === "" && !hasObject(el)) {
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
