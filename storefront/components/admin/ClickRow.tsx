"use client";

import { useRouter } from "next/navigation";
import type { MouseEvent, ReactNode } from "react";

/* A table row you can click anywhere to open it.

   - href: where the row goes (a page in the admin).
   - onOpen: or do something else when the row is clicked (open a panel, load a record).
   - A click on a link, button, field or label inside the row does that thing instead and never opens the row.
   - Ctrl/Cmd-click (or middle-click) opens the page in a new tab when the row has an href.
   - Dragging to select text in the row does not open it.
   - With no href and no onOpen it is just an ordinary row. */

const OWN_CLICK = "a, button, input, select, textarea, label, summary, [role='button'], [data-no-rowclick]";

export default function ClickRow({
  href,
  onOpen,
  className = "",
  children,
}: {
  href?: string;
  onOpen?: () => void;
  className?: string;
  children: ReactNode;
}) {
  const router = useRouter();
  const clickable = Boolean(href || onOpen);

  // True when the click landed on something inside this row that does its own thing.
  function onOwnControl(e: MouseEvent<HTMLTableRowElement>): boolean {
    const hit = (e.target as HTMLElement).closest(OWN_CLICK);
    return hit !== null && e.currentTarget.contains(hit);
  }

  function open(e: MouseEvent<HTMLTableRowElement>) {
    if (!clickable || onOwnControl(e)) return;
    if (window.getSelection()?.toString()) return; // the person was selecting text
    if (href && (e.ctrlKey || e.metaKey)) {
      window.open(href, "_blank", "noopener");
      return;
    }
    if (onOpen) onOpen();
    else if (href) router.push(href);
  }

  function openInNewTab(e: MouseEvent<HTMLTableRowElement>) {
    if (e.button !== 1 || !href || onOwnControl(e)) return;
    e.preventDefault();
    window.open(href, "_blank", "noopener");
  }

  return (
    <tr
      className={`${className} ${clickable ? "cursor-pointer hover:bg-gray-50" : ""}`.trim()}
      onClick={open}
      onAuxClick={openInNewTab}
    >
      {children}
    </tr>
  );
}
