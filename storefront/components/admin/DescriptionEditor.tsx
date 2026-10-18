"use client";

import { useEffect, useRef } from "react";
import { isSafeLink, sanitizeHtml } from "./sanitizeHtml";

// A visual editor for product descriptions: staff see the text as it will look
// on the store and use buttons instead of typing HTML. The same stylesheet class
// as the store page (.product-description, in globals.css) is used on purpose.
//
// It is built on the browser's own contentEditable, so no package is needed.
// Whatever it produces is cleaned again by the server when the product is saved.

type Props = {
  label: string;
  value: string;
  onChange: (html: string) => void;
  disabled?: boolean;
};

const btn =
  "rounded border border-gray-300 bg-white px-2.5 py-1 text-sm text-gray-800 hover:bg-gray-50 disabled:opacity-40";

export default function DescriptionEditor({ label, value, onChange, disabled = false }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  // The last HTML this editor handed to the form. A different value means the
  // product was (re)loaded, and the editor must show it.
  const lastEmitted = useRef<string | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || value === lastEmitted.current) return;
    el.innerHTML = sanitizeHtml(value); // never put stored HTML in the page uncleaned
    lastEmitted.current = value;
  }, [value]);

  function emit() {
    const el = ref.current;
    if (!el) return;
    const empty = (el.textContent ?? "").trim() === "" && !el.querySelector("li,hr,table");
    const html = empty ? "" : el.innerHTML;
    lastEmitted.current = html;
    onChange(html);
  }

  // Runs a formatting command on the current selection.
  function run(command: string, arg?: string) {
    if (disabled) return;
    ref.current?.focus();
    document.execCommand(command, false, arg);
    emit();
  }

  function makeLink() {
    if (disabled) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) {
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

  // The toolbar buttons must not steal focus, or the selection would be lost.
  const keepSelection = (e: React.MouseEvent) => e.preventDefault();

  return (
    <div className="block min-w-0 text-sm font-medium text-gray-700">
      <span className="mb-1 block">{label}</span>
      <div className="mb-1 flex flex-wrap gap-1" role="toolbar" aria-label="Formatting">
        <button type="button" className={`${btn} font-bold`} disabled={disabled} onMouseDown={keepSelection} onClick={() => run("bold")} title="Bold">
          B
        </button>
        <button type="button" className={`${btn} italic`} disabled={disabled} onMouseDown={keepSelection} onClick={() => run("italic")} title="Italic">
          I
        </button>
        <button type="button" className={btn} disabled={disabled} onMouseDown={keepSelection} onClick={() => run("formatBlock", "h3")} title="Make the line a heading">
          Heading
        </button>
        <button type="button" className={btn} disabled={disabled} onMouseDown={keepSelection} onClick={() => run("formatBlock", "div")} title="Back to normal text">
          Normal
        </button>
        <button type="button" className={btn} disabled={disabled} onMouseDown={keepSelection} onClick={() => run("insertUnorderedList")} title="Bulleted list">
          • Bullets
        </button>
        <button type="button" className={btn} disabled={disabled} onMouseDown={keepSelection} onClick={() => run("insertOrderedList")} title="Numbered list">
          1. Numbers
        </button>
        <button type="button" className={btn} disabled={disabled} onMouseDown={keepSelection} onClick={makeLink} title="Link the selected words">
          Link
        </button>
        <button type="button" className={btn} disabled={disabled} onMouseDown={keepSelection} onClick={() => run("unlink")} title="Remove the link">
          Unlink
        </button>
        <button type="button" className={btn} disabled={disabled} onMouseDown={keepSelection} onClick={() => run("removeFormat")} title="Clear bold, italic and links from the selection">
          Clear
        </button>
        <button type="button" className={btn} disabled={disabled} onMouseDown={keepSelection} onClick={() => run("undo")} title="Undo">
          Undo
        </button>
        <button type="button" className={btn} disabled={disabled} onMouseDown={keepSelection} onClick={() => run("redo")} title="Redo">
          Redo
        </button>
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
        className={`product-description min-h-40 w-full min-w-0 rounded border border-gray-300 px-3 py-2 text-sm font-normal text-gray-900 ${
          disabled ? "bg-gray-100 text-gray-500" : "bg-white"
        }`}
      />
    </div>
  );
}
