"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { isSafeLink, normalizeBlocks, sanitizeHtml } from "./sanitizeHtml";

// A visual editor for product descriptions: staff see the text as it will look
// on the store and use a Word-style toolbar instead of typing HTML. The same
// stylesheet class as the store page (.product-description, in globals.css) is
// used on purpose, so the editor matches what shoppers see.
//
// It is built on the browser's own contentEditable, so no package is needed.
// Whatever it produces is cleaned again by the server when the product is saved.

type Props = {
  label: string;
  value: string;
  onChange: (html: string) => void;
  disabled?: boolean;
  // Look of the editing area. The default matches product descriptions; the About and
  // Help pages pass the .page-content look so the editor matches those pages.
  contentClass?: string;
};

type Active = { bold: boolean; italic: boolean; underline: boolean; ul: boolean; ol: boolean; heading: boolean };
const NONE: Active = { bold: false, italic: false, underline: false, ul: false, ol: false, heading: false };

// --- icons (inline SVG, so nothing needs installing) -------------------------
function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const Glyph = ({ children, cls }: { children: string; cls: string }) => (
  <span className={`text-[15px] leading-none ${cls}`}>{children}</span>
);

const icons = {
  bold: <Glyph cls="font-extrabold">B</Glyph>,
  italic: <Glyph cls="font-serif italic font-semibold">I</Glyph>,
  underline: <Glyph cls="font-semibold underline underline-offset-2">U</Glyph>,
  bullets: (
    <Icon>
      <circle cx="4" cy="6" r="1.3" fill="currentColor" />
      <circle cx="4" cy="12" r="1.3" fill="currentColor" />
      <circle cx="4" cy="18" r="1.3" fill="currentColor" />
      <path d="M9 6h12M9 12h12M9 18h12" />
    </Icon>
  ),
  numbers: (
    <Icon>
      <path d="M10 6h11M10 12h11M10 18h11" />
      <text x="1" y="8" fontSize="7" fill="currentColor" stroke="none" fontWeight="700">1</text>
      <text x="1" y="14" fontSize="7" fill="currentColor" stroke="none" fontWeight="700">2</text>
      <text x="1" y="20" fontSize="7" fill="currentColor" stroke="none" fontWeight="700">3</text>
    </Icon>
  ),
  link: (
    <Icon>
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </Icon>
  ),
  unlink: (
    <Icon>
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
      <path d="M3 3l18 18" strokeWidth="2.5" />
    </Icon>
  ),
  clear: (
    <Icon>
      <path d="M4 7V4h12v3M10 4v13M7 17h6" />
      <path d="M15 14l5 5M20 14l-5 5" />
    </Icon>
  ),
  single: (
    <Icon>
      <path d="M4 5h16M4 9h16M4 13h16M4 17h16" />
    </Icon>
  ),
  paragraphs: (
    <Icon>
      <path d="M4 4h16M4 8h16M4 16h16M4 20h16" />
    </Icon>
  ),
  undo: (
    <Icon>
      <path d="M3 7v6h6" />
      <path d="M21 17a9 9 0 0 0-15-6.7L3 13" />
    </Icon>
  ),
  redo: (
    <Icon>
      <path d="M21 7v6h-6" />
      <path d="M3 17a9 9 0 0 1 15-6.7L21 13" />
    </Icon>
  ),
};

const Divider = () => <span className="mx-1 h-5 w-px bg-gray-300" aria-hidden="true" />;

function Tool(props: { title: string; on?: boolean; disabled: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      title={props.title}
      aria-label={props.title.replace(/ \(.*\)$/, "")}
      aria-pressed={props.on ?? undefined}
      disabled={props.disabled}
      // Toolbar buttons must not steal focus, or the text selection would be lost.
      onMouseDown={(e) => e.preventDefault()}
      onClick={props.onClick}
      className={`flex h-8 w-8 items-center justify-center rounded text-gray-800 hover:bg-gray-200 disabled:opacity-40 ${
        props.on ? "bg-gray-300" : ""
      }`}
    >
      {props.children}
    </button>
  );
}

export default function DescriptionEditor({
  label,
  value,
  onChange,
  disabled = false,
  contentClass = "product-description min-h-40 text-sm text-gray-900",
}: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  // The last HTML this editor handed to the form. A different value means the
  // product was (re)loaded, and the editor must show it.
  const lastEmitted = useRef<string | null>(null);
  // Where the cursor/selection was last inside the editor. Clicking a toolbar
  // control (especially the style dropdown) can move focus away from the text.
  const savedRange = useRef<Range | null>(null);
  const [active, setActive] = useState<Active>(NONE);

  useEffect(() => {
    const el = ref.current;
    if (!el || value === lastEmitted.current) return;
    el.innerHTML = sanitizeHtml(value); // never put stored HTML in the page uncleaned
    lastEmitted.current = value;
  }, [value]);

  // Keep the toolbar's highlighted state in step with the cursor.
  const refreshActive = useCallback(() => {
    const el = ref.current;
    const sel = window.getSelection();
    if (!el || !sel || sel.rangeCount === 0 || !el.contains(sel.anchorNode)) return;
    savedRange.current = sel.getRangeAt(0).cloneRange();
    const block = String(document.queryCommandValue("formatBlock")).toLowerCase().replace(/[<>]/g, "");
    setActive({
      bold: document.queryCommandState("bold"),
      italic: document.queryCommandState("italic"),
      underline: document.queryCommandState("underline"),
      ul: document.queryCommandState("insertUnorderedList"),
      ol: document.queryCommandState("insertOrderedList"),
      heading: /^h[1-6]$/.test(block),
    });
  }, []);

  useEffect(() => {
    document.addEventListener("selectionchange", refreshActive);
    return () => document.removeEventListener("selectionchange", refreshActive);
  }, [refreshActive]);

  function emit() {
    const el = ref.current;
    if (!el) return;
    el.querySelectorAll("[style]").forEach((x) => x.removeAttribute("style")); // the browser may add inline fonts; they are not kept when saved
    const empty = (el.textContent ?? "").trim() === "" && !el.querySelector("li,hr,table");
    const html = empty ? "" : el.innerHTML;
    lastEmitted.current = html;
    onChange(html);
  }

  function restoreSelection() {
    ref.current?.focus();
    const sel = window.getSelection();
    if (sel && savedRange.current) {
      sel.removeAllRanges();
      sel.addRange(savedRange.current);
    }
  }

  // Runs a formatting command on the current selection.
  function run(command: string, arg?: string) {
    if (disabled) return;
    restoreSelection();
    document.execCommand("styleWithCSS", false, "false"); // use <b>/<i> tags, not inline styles that change the font
    document.execCommand(command, false, arg);
    emit();
    refreshActive();
  }

  // Line spacing. "Single" puts the chosen lines into one block with plain line breaks between them (no gap); "Paragraphs"
  // makes every line its own paragraph (the standard gap). It applies to the selected lines, or to the whole description when
  // nothing is selected. Text, bold and links are kept.
  function setSpacing(mode: "single" | "paragraphs") {
    const el = ref.current;
    if (!el || disabled) return;
    restoreSelection();
    const sel = window.getSelection();
    const range = sel && sel.rangeCount && el.contains(sel.anchorNode) && !sel.getRangeAt(0).collapsed ? sel.getRangeAt(0) : null;
    const isLine = (n: Element) => /^(DIV|P)$/.test(n.tagName) && !n.querySelector("div,p,ul,ol,li,table,h2,h3,h4");
    const kids = Array.from(el.children);
    const chosen = new Set(kids.filter((k) => isLine(k) && (!range || range.intersectsNode(k))));
    if (chosen.size === 0) return;
    let last: Element | null = null;
    if (mode === "single") {
      let i = 0;
      while (i < kids.length) {
        if (!chosen.has(kids[i])) {
          i++;
          continue;
        }
        const group: Element[] = [];
        while (i < kids.length && chosen.has(kids[i])) group.push(kids[i++]);
        const one = document.createElement("div");
        group.forEach((blk, n) => {
          if (n > 0) one.appendChild(document.createElement("br"));
          Array.from(blk.childNodes).forEach((c) => {
            if (!(c instanceof HTMLBRElement && !c.nextSibling)) one.appendChild(c); // a trailing break would add an empty line
          });
        });
        group[0].before(one);
        group.forEach((blk) => blk.remove());
        last = one;
      }
    } else {
      chosen.forEach((blk) => {
        const lines: Node[][] = [[]];
        Array.from(blk.childNodes).forEach((c) => (c instanceof HTMLBRElement ? lines.push([]) : lines[lines.length - 1].push(c)));
        lines.forEach((ln) => {
          if (ln.every((n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? "").trim() === "")) return;
          const piece = blk.cloneNode(false);
          ln.forEach((n) => piece.appendChild(n));
          blk.before(piece);
          last = piece as Element;
        });
        blk.remove();
      });
    }
    if (last && sel) {
      const r = document.createRange();
      r.selectNodeContents(last);
      r.collapse(false);
      sel.removeAllRanges();
      sel.addRange(r);
    }
    emit();
    refreshActive();
  }

  function makeLink() {
    if (disabled) return;
    const sel = window.getSelection();
    const range = savedRange.current;
    if (!range || range.collapsed || !sel) {
      window.alert("Select the words you want to turn into a link first.");
      return;
    }
    const url = window.prompt("Link address (start with https://)", "https://");
    if (!url) return;
    if (!isSafeLink(url)) {
      window.alert("Links must start with https://, http://, mailto: or tel:");
      return;
    }
    run("createLink", url.trim());
  }

  function onPaste(e: React.ClipboardEvent<HTMLDivElement>) {
    if (disabled) return;
    e.preventDefault();
    const html = e.clipboardData.getData("text/html");
    const text = e.clipboardData.getData("text/plain");
    const clean = html ? sanitizeHtml(html) : "";
    if (clean) document.execCommand("insertHTML", false, clean);
    else document.execCommand("insertText", false, text);
    tidyKeepingCaret(); // the browser may nest the pasted blocks inside the block the cursor was in
    emit();
  }

  // Gives the text the same one-level shape as a loaded description, and keeps the cursor where it was.
  function tidyKeepingCaret() {
    const el = ref.current;
    if (!el) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || !el.contains(sel.anchorNode)) {
      normalizeBlocks(el);
      return;
    }
    const r = sel.getRangeAt(0).cloneRange();
    r.collapse(false);
    const mark = document.createElement("span");
    r.insertNode(mark);
    normalizeBlocks(el);
    const back = document.createRange();
    back.setStartAfter(mark);
    back.collapse(true);
    sel.removeAllRanges();
    sel.addRange(back);
    mark.remove();
  }

  return (
    <div className="block min-w-0 text-sm font-medium text-gray-700">
      <span className="mb-1 block">{label}</span>
      <div
        className="flex flex-wrap items-center gap-0.5 rounded-t border border-b-0 border-gray-300 bg-gray-50 p-1"
        role="toolbar"
        aria-label="Formatting"
      >
        <select
          aria-label="Text style"
          title="Text style"
          disabled={disabled}
          value={active.heading ? "h3" : "div"}
          onChange={(e) => run("formatBlock", e.target.value)}
          className="mr-1 h-8 rounded border border-gray-300 bg-white px-2 text-sm font-normal text-gray-800 disabled:opacity-40"
        >
          <option value="div">Normal text</option>
          <option value="h3">Heading</option>
        </select>
        <Divider />
        <Tool disabled={disabled} title="Bold (Ctrl+B)" on={active.bold} onClick={() => run("bold")}>{icons.bold}</Tool>
        <Tool disabled={disabled} title="Italic (Ctrl+I)" on={active.italic} onClick={() => run("italic")}>{icons.italic}</Tool>
        <Tool disabled={disabled} title="Underline (Ctrl+U)" on={active.underline} onClick={() => run("underline")}>{icons.underline}</Tool>
        <Divider />
        <Tool disabled={disabled} title="Bulleted list" on={active.ul} onClick={() => run("insertUnorderedList")}>{icons.bullets}</Tool>
        <Tool disabled={disabled} title="Numbered list" on={active.ol} onClick={() => run("insertOrderedList")}>{icons.numbers}</Tool>
        <Tool disabled={disabled} title="Single line spacing (selected lines, or all lines if nothing is selected)" onClick={() => setSpacing("single")}>{icons.single}</Tool>
        <Tool disabled={disabled} title="Paragraph spacing: a gap between lines (selected lines, or all lines if nothing is selected)" onClick={() => setSpacing("paragraphs")}>{icons.paragraphs}</Tool>
        <Divider />
        <Tool disabled={disabled} title="Add link to selected text" onClick={makeLink}>{icons.link}</Tool>
        <Tool disabled={disabled} title="Remove link" onClick={() => run("unlink")}>{icons.unlink}</Tool>
        <Tool disabled={disabled} title="Clear formatting" onClick={() => run("removeFormat")}>{icons.clear}</Tool>
        <Divider />
        <Tool disabled={disabled} title="Undo (Ctrl+Z)" onClick={() => run("undo")}>{icons.undo}</Tool>
        <Tool disabled={disabled} title="Redo (Ctrl+Y)" onClick={() => run("redo")}>{icons.redo}</Tool>
      </div>
      <div
        ref={ref}
        contentEditable={!disabled}
        suppressContentEditableWarning
        role="textbox"
        aria-multiline="true"
        aria-label={label}
        onInput={emit}
        onPaste={onPaste}
        onDrop={(e) => e.preventDefault()}
        className={`${contentClass} w-full min-w-0 rounded-b border border-gray-300 px-3 py-2 font-normal ${
          disabled ? "bg-gray-100 text-gray-500" : "bg-white"
        }`}
      />
    </div>
  );
}
