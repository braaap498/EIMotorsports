"use client";

import type { ReactNode } from "react";

export const inputCls =
  "w-full min-w-0 rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 disabled:bg-gray-100 disabled:text-gray-400";
export const btnCls =
  "rounded bg-black px-4 py-2 text-sm font-semibold text-white hover:bg-gray-800 disabled:opacity-40";
export const btn2Cls =
  "rounded border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-800 hover:bg-gray-50 disabled:opacity-40";
export const dangerCls =
  "rounded border border-red-300 bg-white px-3 py-2 text-sm font-medium text-red-700 hover:bg-red-50 disabled:opacity-40";

export function Field({
  label,
  children,
  className = "",
}: {
  label: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={`block min-w-0 text-sm font-medium text-gray-700 ${className}`}>
      <span className="mb-1 block">{label}</span>
      {children}
    </label>
  );
}

export function Notice({
  kind = "info",
  children,
}: {
  kind?: "info" | "error" | "success";
  children: ReactNode;
}) {
  const color =
    kind === "error"
      ? "border-red-200 bg-red-50 text-red-800"
      : kind === "success"
      ? "border-green-200 bg-green-50 text-green-800"
      : "border-blue-200 bg-blue-50 text-blue-800";
  return <p className={`rounded border px-3 py-2 text-sm ${color}`}>{children}</p>;
}

export function Badge({ children, tone = "gray" }: { children: ReactNode; tone?: "gray" | "green" | "amber" | "blue" }) {
  const color = {
    gray: "bg-gray-100 text-gray-700",
    green: "bg-green-100 text-green-800",
    amber: "bg-amber-100 text-amber-800",
    blue: "bg-blue-100 text-blue-800",
  }[tone];
  return <span className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${color}`}>{children}</span>;
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4">
      <div className="my-8 w-full max-w-2xl rounded-lg bg-white p-5 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button type="button" onClick={onClose} className="text-2xl leading-none text-gray-500 hover:text-black" aria-label="Close">
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Pager({
  total,
  limit,
  offset,
  onChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onChange: (offset: number) => void;
}) {
  if (total <= limit) return <p className="text-sm text-gray-500">{total} total</p>;
  const from = offset + 1;
  const to = Math.min(offset + limit, total);
  return (
    <div className="flex items-center gap-3 text-sm text-gray-600">
      <span>
        {from}–{to} of {total}
      </span>
      <button type="button" className={btn2Cls} disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>
        Previous
      </button>
      <button type="button" className={btn2Cls} disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>
        Next
      </button>
    </div>
  );
}
