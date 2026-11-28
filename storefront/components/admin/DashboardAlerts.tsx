"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";

/* Important messages across the top of the Dashboard, colored by importance:
   red = critical (act now), orange = warning (fix soon), yellow = notice, blue = information.
   Each one can be opened to see the items behind it. The list comes from GET /admin/dashboard/alerts (app/dashboard_alerts.py);
   it refreshes by itself every few minutes while the Dashboard is open. A failed refresh keeps what is already shown. */

type Level = "critical" | "warning" | "notice" | "info";
type Item = { label: string; text: string; href?: string | null };
type Alert = { id: string; level: Level; title: string; detail: string; href?: string | null; items: Item[]; more: number };

const STYLE: Record<Level, { box: string; tag: string; label: string; icon: string }> = {
  critical: { box: "border-red-300 bg-red-50 text-red-900", tag: "bg-red-600 text-white", label: "Critical", icon: "⛔" },
  warning: { box: "border-orange-300 bg-orange-50 text-orange-900", tag: "bg-orange-500 text-white", label: "Warning", icon: "⚠️" },
  notice: { box: "border-yellow-300 bg-yellow-50 text-yellow-900", tag: "bg-yellow-400 text-yellow-950", label: "Notice", icon: "🔔" },
  info: { box: "border-blue-200 bg-blue-50 text-blue-900", tag: "bg-blue-600 text-white", label: "Info", icon: "ℹ️" },
};

const REFRESH_MS = 5 * 60 * 1000;

export default function DashboardAlerts() {
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [failed, setFailed] = useState<string[]>([]); // checks the server could not run
  const [loadError, setLoadError] = useState(""); // the request itself failed

  const load = useCallback(async () => {
    try {
      const r = await api<{ alerts: Alert[]; failed?: string[] }>("/dashboard/alerts");
      setAlerts(r.alerts);
      setFailed(r.failed ?? []);
      setLoadError("");
    } catch (e) {
      // keep what is shown, but say so: an empty Dashboard must not hide a broken one
      setLoadError(errMsg(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);

  const problems = (
    <>
      {loadError && (
        <p className="text-sm text-red-700" role="alert">
          The Dashboard messages could not be loaded: {loadError}
        </p>
      )}
      {failed.length > 0 && (
        <p className="text-sm text-orange-700">
          {failed.length} check{failed.length === 1 ? "" : "s"} could not run, so there may be messages missing: {failed.join("; ")}
        </p>
      )}
    </>
  );

  if (!alerts) return loadError ? <div className="space-y-1">{problems}</div> : null;
  if (alerts.length === 0) {
    return (
      <div className="space-y-1">
        {problems}
        {!loadError && failed.length === 0 && <p className="text-xs text-gray-500">No messages. Every check ran and nothing needs attention.</p>}
      </div>
    );
  }

  const toggle = (id: string) =>
    setOpen((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  return (
    <div className="space-y-2" role="region" aria-label="Important messages">
      {alerts.map((a) => {
        const st = STYLE[a.level] ?? STYLE.info;
        const isOpen = open.has(a.id);
        return (
          <div key={a.id} className={`w-full rounded border ${st.box}`}>
            <div className="flex flex-wrap items-center gap-3 px-4 py-3">
              <span className={`rounded px-2 py-0.5 text-xs font-bold uppercase ${st.tag}`}>
                <span aria-hidden="true">{st.icon} </span>
                {st.label}
              </span>
              <div className="min-w-0 flex-1">
                <div className="font-semibold">{a.title}</div>
                {a.detail && <div className="text-sm opacity-90">{a.detail}</div>}
              </div>
              <div className="flex items-center gap-3 text-sm">
                {a.items.length > 0 && (
                  <button type="button" className="underline" onClick={() => toggle(a.id)} aria-expanded={isOpen}>
                    {isOpen ? "Hide" : `Show ${a.items.length + a.more}`}
                  </button>
                )}
                {a.href && (
                  <Link href={a.href} className="rounded border border-current px-3 py-1 font-medium">
                    Open
                  </Link>
                )}
              </div>
            </div>
            {isOpen && (
              <ul className="space-y-1 border-t border-current/20 px-4 py-2 text-sm">
                {a.items.map((it, i) => (
                  <li key={i}>
                    {it.href ? (
                      <Link href={it.href} className="font-medium underline">
                        {it.label || "Open"}
                      </Link>
                    ) : (
                      <span className="font-medium">{it.label}</span>
                    )}
                    {it.text ? <span>{it.label ? ": " : ""}{it.text}</span> : null}
                  </li>
                ))}
                {a.more > 0 && <li className="opacity-80">…and {a.more} more.</li>}
              </ul>
            )}
          </div>
        );
      })}
      {problems}
    </div>
  );
}
