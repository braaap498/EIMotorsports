"use client";

import type { ReactNode } from "react";

// A sortable column heading, the same look as the Automatic Distributors catalog: small, upper case, grey, with ▲▼ markers
// (the active column is black with one arrow). Used by the Rocky Mountain catalog and the saved-parts list so all three match.
export default function CatalogTh({
  children,
  active,
  dir,
  onClick,
  right,
  title,
}: {
  children: ReactNode;
  active: boolean;
  dir: "asc" | "desc";
  onClick: () => void;
  right?: boolean;
  title?: string;
}) {
  return (
    <th className={`whitespace-nowrap py-1 pr-3 ${right ? "text-right" : ""}`} title={title}>
      <button type="button" onClick={onClick} className={`uppercase hover:text-black ${active ? "text-black" : ""}`}>
        {children} <span className={active ? "" : "text-gray-300"}>{active ? (dir === "asc" ? "▲" : "▼") : "▲▼"}</span>
      </button>
    </th>
  );
}
