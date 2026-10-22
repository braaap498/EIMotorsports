"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { isSafeLink, sanitizeHtml } from "./sanitizeHtml";

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
    document.execCommand(command, false, arg);
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
    emit();
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
