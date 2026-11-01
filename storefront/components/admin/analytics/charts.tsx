"use client";

// Small dependency-free SVG charts for the Analytics page.
import { useState, type ReactNode } from "react";

export const PALETTE = ["#4f46e5", "#f59e0b", "#10b981", "#ef4444", "#0ea5e9", "#8b5cf6", "#ec4899", "#64748b"];

export const money0 = (n: number) => `$${Math.round(n).toLocaleString("en-US")}`;
export const money2 = (n: number) => `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
export const num0 = (n: number) => Math.round(n).toLocaleString("en-US");
export const pct = (n: number, d = 1) => `${(n * 100).toFixed(d)}%`;
export function compact(n: number): string {
  const a = Math.abs(n);
  if (a >= 1e6) return `${(n / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
  if (a >= 1e3) return `${(n / 1e3).toFixed(a >= 1e4 ? 0 : 1)}k`;
  return String(Math.round(n));
}
export const compactMoney = (n: number) => `$${compact(n)}`;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const monthLabel = (ym: string) => {
  const [y, m] = ym.split("-");
  return `${MONTHS[Number(m) - 1]} ’${y.slice(2)}`;
};

// A "nice" top for an axis: 1, 2, 2.5, 5 or 10 times a power of ten.
function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

export function Card({ title, sub, children, className = "" }: { title: string; sub?: string; children: ReactNode; className?: string }) {
  return (
    <section className={`min-w-0 rounded-lg border border-gray-200 bg-white p-4 shadow-sm ${className}`}>
      <h2 className="text-sm font-semibold text-gray-900">{title}</h2>
      {sub && <p className="mb-2 text-xs text-gray-500">{sub}</p>}
      {!sub && <div className="mb-2" />}
      {children}
    </section>
  );
}

export function Empty({ text = "No data for this selection." }: { text?: string }) {
  return <p className="py-10 text-center text-sm text-gray-400">{text}</p>;
}

export function Legend({ items }: { items: { label: string; color: string }[] }) {
  return (
    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-600">
      {items.map((i) => (
        <span key={i.label} className="inline-flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: i.color }} />
          {i.label}
        </span>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ KPI card */
export function Kpi({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-gray-200 bg-white p-4 shadow-sm" style={accent ? { borderTop: `3px solid ${accent}` } : undefined}>
      <div className="text-xs font-medium uppercase tracking-wide text-gray-500">{label}</div>
      <div className="mt-1 truncate text-2xl font-semibold text-gray-900">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-gray-500">{sub}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ stacked / single bars over time */
export type Series = { key: string; label: string; color: string; values: number[] };

export function StackedBars({
  labels, series, fmt = compactMoney, tipFmt = money0, height = 260,
}: {
  labels: string[]; series: Series[]; fmt?: (n: number) => string; tipFmt?: (n: number) => string; height?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  if (!labels.length) return <Empty />;
  const W = 820, H = height, L = 52, R = 8, T = 10, B = 34;
  const totals = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
  const top = niceMax(Math.max(...totals));
  const iw = W - L - R, ih = H - T - B;
  const bw = iw / labels.length;
  const y = (v: number) => T + ih - (v / top) * ih;
  const every = Math.ceil(labels.length / 14);
  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img">
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}>
            <line x1={L} x2={W - R} y1={y(top * f)} y2={y(top * f)} stroke="#e5e7eb" />
            <text x={L - 6} y={y(top * f) + 4} textAnchor="end" fontSize="11" fill="#6b7280">{fmt(top * f)}</text>
          </g>
        ))}
        {labels.map((lab, i) => {
          let acc = 0;
          return (
            <g key={lab} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
              <rect x={L + i * bw} y={T} width={bw} height={ih} fill={hover === i ? "#f3f4f6" : "transparent"} />
              {series.map((s) => {
                const v = s.values[i] || 0;
                const h = (v / top) * ih;
                const yy = y(acc + v);
                acc += v;
                return h > 0 ? <rect key={s.key} x={L + i * bw + bw * 0.14} y={yy} width={bw * 0.72} height={h} fill={s.color} rx="2" /> : null;
              })}
              {i % every === 0 && (
                <text x={L + i * bw + bw / 2} y={H - 12} textAnchor="middle" fontSize="11" fill="#6b7280">{monthLabel(lab)}</text>
              )}
            </g>
          );
        })}
      </svg>
      {hover !== null && (
        <div className="pointer-events-none absolute right-2 top-2 rounded border border-gray-200 bg-white/95 px-3 py-2 text-xs shadow">
          <div className="font-semibold">{monthLabel(labels[hover])}</div>
          {series.map((s) => (
            <div key={s.key} className="flex items-center gap-1.5">
              <span className="inline-block h-2 w-2 rounded-sm" style={{ background: s.color }} />
              {s.label}: {tipFmt(s.values[hover] || 0)}
            </div>
          ))}
          {series.length > 1 && <div className="mt-0.5 border-t border-gray-100 pt-0.5 font-medium">Total: {tipFmt(totals[hover])}</div>}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ lines */
export function LineChart({
  labels, series, fmt = compactMoney, tipFmt = money0, height = 240, area = false,
}: {
  labels: string[]; series: Series[]; fmt?: (n: number) => string; tipFmt?: (n: number) => string; height?: number; area?: boolean;
}) {
  const [hover, setHover] = useState<number | null>(null);
  if (!labels.length) return <Empty />;
  const W = 820, H = height, L = 52, R = 14, T = 10, B = 30;
  const top = niceMax(Math.max(1, ...series.flatMap((s) => s.values.map((v) => v ?? 0))));
  const iw = W - L - R, ih = H - T - B;
  const x = (i: number) => L + (labels.length === 1 ? iw / 2 : (i / (labels.length - 1)) * iw);
  const y = (v: number) => T + ih - (v / top) * ih;
  const every = Math.ceil(labels.length / 12);
  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" onMouseLeave={() => setHover(null)}>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}>
            <line x1={L} x2={W - R} y1={y(top * f)} y2={y(top * f)} stroke="#e5e7eb" />
            <text x={L - 6} y={y(top * f) + 4} textAnchor="end" fontSize="11" fill="#6b7280">{fmt(top * f)}</text>
          </g>
        ))}
        {labels.map((lab, i) => (
          <g key={lab}>
            <rect x={x(i) - iw / labels.length / 2} y={T} width={iw / labels.length} height={ih} fill="transparent" onMouseEnter={() => setHover(i)} />
            {i % every === 0 && <text x={x(i)} y={H - 10} textAnchor="middle" fontSize="11" fill="#6b7280">{lab}</text>}
          </g>
        ))}
        {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={T} y2={T + ih} stroke="#d1d5db" strokeDasharray="3 3" />}
        {series.map((s) => {
          const pts = s.values.map((v, i) => (v === null || v === undefined ? null : [x(i), y(v)] as const));
          const segs: (readonly [number, number])[][] = [];
          let cur: (readonly [number, number])[] = [];
          pts.forEach((p) => {
            if (p) cur.push(p);
            else if (cur.length) { segs.push(cur); cur = []; }
          });
          if (cur.length) segs.push(cur);
          return (
            <g key={s.key}>
              {segs.map((sg, k) => (
                <g key={k}>
                  {area && sg.length > 1 && (
                    <path d={`M${sg[0][0]},${y(0)} ${sg.map((p) => `L${p[0]},${p[1]}`).join(" ")} L${sg[sg.length - 1][0]},${y(0)} Z`} fill={s.color} opacity="0.12" />
                  )}
                  <path d={sg.map((p, j) => `${j ? "L" : "M"}${p[0]},${p[1]}`).join(" ")} fill="none" stroke={s.color} strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
                </g>
              ))}
              {hover !== null && pts[hover] && <circle cx={pts[hover]![0]} cy={pts[hover]![1]} r="4" fill={s.color} stroke="#fff" strokeWidth="2" />}
            </g>
          );
        })}
      </svg>
      {hover !== null && (
        <div className="pointer-events-none absolute right-2 top-2 rounded border border-gray-200 bg-white/95 px-3 py-2 text-xs shadow">
          <div className="font-semibold">{labels[hover]}</div>
          {series.map((s) => (
            <div key={s.key} className="flex items-center gap-1.5">
              <span className="inline-block h-2 w-2 rounded-full" style={{ background: s.color }} />
              {s.label}: {s.values[hover] === null || s.values[hover] === undefined ? "—" : tipFmt(s.values[hover])}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ horizontal bars */
export type BarRow = { label: string; value: number; sub?: string; color?: string };

export function HBars({ rows, fmt = money0, color = PALETTE[0], labelWidth = "w-40" }: { rows: BarRow[]; fmt?: (n: number) => string; color?: string; labelWidth?: string }) {
  if (!rows.length) return <Empty />;
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <ul className="space-y-1.5">
      {rows.map((r) => (
        <li key={r.label} className="flex items-center gap-2 text-xs">
          <span className={`${labelWidth} shrink-0 truncate text-gray-700`} title={r.label}>{r.label}</span>
          <span className="relative h-5 min-w-0 flex-1 overflow-hidden rounded bg-gray-100">
            <span className="absolute inset-y-0 left-0 rounded" style={{ width: `${Math.max((r.value / max) * 100, 1.5)}%`, background: r.color ?? color }} />
          </span>
          <span className="w-24 shrink-0 text-right tabular-nums text-gray-800">
            {fmt(r.value)}
            {r.sub && <span className="block text-[10px] leading-3 text-gray-400">{r.sub}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------------ vertical columns (weekday, order size ...) */
export function Columns({ rows, fmt = num0, color = PALETTE[0], height = 180 }: { rows: BarRow[]; fmt?: (n: number) => string; color?: string; height?: number }) {
  if (!rows.length) return <Empty />;
  const W = 420, H = height, T = 18, B = 34;
  const max = Math.max(...rows.map((r) => r.value), 1);
  const bw = W / rows.length;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img">
      {rows.map((r, i) => {
        const h = (r.value / max) * (H - T - B);
        return (
          <g key={r.label}>
            <rect x={i * bw + bw * 0.16} y={H - B - h} width={bw * 0.68} height={h} rx="3" fill={r.color ?? color} />
            <text x={i * bw + bw / 2} y={H - B - h - 5} textAnchor="middle" fontSize="10" fill="#374151">{fmt(r.value)}</text>
            <text x={i * bw + bw / 2} y={H - 14} textAnchor="middle" fontSize="10.5" fill="#6b7280">{r.label}</text>
          </g>
        );
      })}
    </svg>
  );
}

/* ------------------------------------------------------------------ donut */
export function Donut({ rows, fmt = num0, center }: { rows: { label: string; value: number; color?: string }[]; fmt?: (n: number) => string; center?: { big: string; small: string } }) {
  const data = rows.filter((r) => r.value > 0);
  const total = data.reduce((a, r) => a + r.value, 0);
  if (!total) return <Empty />;
  const R = 62, r = 40, C = 80;
  let ang = -Math.PI / 2;
  const arcs = data.map((d, i) => {
    const a0 = ang;
    const a1 = ang + (d.value / total) * Math.PI * 2;
    ang = a1;
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const p = (rad: number, a: number) => `${C + rad * Math.cos(a)},${C + rad * Math.sin(a)}`;
    const full = data.length === 1;
    const path = full
      ? `M${C},${C - R} A${R},${R} 0 1 1 ${C - 0.01},${C - R} L${C - 0.01},${C - r} A${r},${r} 0 1 0 ${C},${C - r} Z`
      : `M${p(R, a0)} A${R},${R} 0 ${large} 1 ${p(R, a1)} L${p(r, a1)} A${r},${r} 0 ${large} 0 ${p(r, a0)} Z`;
    return { path, color: d.color ?? PALETTE[i % PALETTE.length], d };
  });
  return (
    <div className="flex flex-wrap items-center gap-4">
      <svg viewBox="0 0 160 160" className="h-36 w-36 shrink-0" role="img">
        {arcs.map((a) => <path key={a.d.label} d={a.path} fill={a.color} />)}
        {center && (
          <>
            <text x={C} y={C + 2} textAnchor="middle" fontSize="15" fontWeight="600" fill="#111827">{center.big}</text>
            <text x={C} y={C + 15} textAnchor="middle" fontSize="8.5" fill="#6b7280">{center.small}</text>
          </>
        )}
      </svg>
      <ul className="min-w-0 flex-1 space-y-1 text-xs">
        {arcs.map((a) => (
          <li key={a.d.label} className="flex items-center gap-2">
            <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: a.color }} />
            <span className="min-w-0 flex-1 truncate text-gray-700">{a.d.label}</span>
            <span className="tabular-nums text-gray-900">{fmt(a.d.value)}</span>
            <span className="w-11 text-right tabular-nums text-gray-400">{pct(a.d.value / total, 0)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ------------------------------------------------------------------ US tile-grid map */
const TILES: Record<string, [number, number]> = {
  AK: [0, 0], ME: [10, 0],
  WI: [5, 1], VT: [9, 1], NH: [10, 1],
  WA: [0, 2], ID: [1, 2], MT: [2, 2], ND: [3, 2], MN: [4, 2], IL: [5, 2], MI: [6, 2], NY: [8, 2], MA: [9, 2],
  OR: [0, 3], NV: [1, 3], WY: [2, 3], SD: [3, 3], IA: [4, 3], IN: [5, 3], OH: [6, 3], PA: [7, 3], NJ: [8, 3], CT: [9, 3], RI: [10, 3],
  CA: [0, 4], UT: [1, 4], CO: [2, 4], NE: [3, 4], MO: [4, 4], KY: [5, 4], WV: [6, 4], VA: [7, 4], MD: [8, 4], DE: [9, 4],
  AZ: [1, 5], NM: [2, 5], KS: [3, 5], AR: [4, 5], TN: [5, 5], NC: [6, 5], SC: [7, 5], DC: [8, 5],
  OK: [3, 6], LA: [4, 6], MS: [5, 6], AL: [6, 6], GA: [7, 6],
  HI: [0, 7], TX: [3, 7], FL: [7, 7],
};

export function TileMap({ data, fmt = money0 }: { data: { state: string; value: number; orders: number }[]; fmt?: (n: number) => string }) {
  const [hover, setHover] = useState<string | null>(null);
  const by = new Map(data.map((d) => [d.state, d]));
  const max = Math.max(...data.map((d) => d.value), 1);
  if (!data.length) return <Empty />;
  const S = 46, G = 4, W = 11 * (S + G), H = 8 * (S + G);
  const shade = (v: number) => {
    const t = Math.sqrt(v / max); // sqrt so mid-size states are still visible next to the biggest
    const a = [238, 242, 255], b = [67, 56, 202];
    return `rgb(${a.map((c, i) => Math.round(c + (b[i] - c) * t)).join(",")})`;
  };
  const h = hover ? by.get(hover) : null;
  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" onMouseLeave={() => setHover(null)}>
        {Object.entries(TILES).map(([st, [c, r]]) => {
          const d = by.get(st);
          const dark = d && Math.sqrt(d.value / max) > 0.55;
          return (
            <g key={st} onMouseEnter={() => setHover(st)}>
              <rect x={c * (S + G)} y={r * (S + G)} width={S} height={S} rx="6" fill={d ? shade(d.value) : "#f3f4f6"} stroke={hover === st ? "#111827" : "none"} strokeWidth="2" />
              <text x={c * (S + G) + S / 2} y={r * (S + G) + S / 2 + 4} textAnchor="middle" fontSize="12" fontWeight="600" fill={dark ? "#fff" : d ? "#312e81" : "#9ca3af"}>{st}</text>
            </g>
          );
        })}
      </svg>
      {h && hover && (
        <div className="pointer-events-none absolute right-2 top-2 rounded border border-gray-200 bg-white/95 px-3 py-2 text-xs shadow">
          <div className="font-semibold">{hover}</div>
          <div>{fmt(h.value)} net sales</div>
          <div>{num0(h.orders)} orders</div>
        </div>
      )}
      <div className="mt-2 flex items-center gap-2 text-[10px] text-gray-500">
        <span>Less</span>
        <span className="h-2 w-28 rounded" style={{ background: `linear-gradient(to right, ${shade(0)}, ${shade(max)})` }} />
        <span>More (net sales)</span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ heat grid (rows x 12 calendar months) */
// Values are seasonal indexes: 1.0 = a normal month, above 1 = busier (indigo), below 1 = quieter (amber).
export function Heatmap({
  rows, labels = MONTHS, note,
}: {
  rows: { label: string; values: (number | null)[]; right?: string }[]; labels?: string[]; note?: string;
}) {
  if (!rows.length) return <Empty />;
  const cell = (v: number | null): { bg: string; fg: string } => {
    if (v === null || v === undefined) return { bg: "#f3f4f6", fg: "#9ca3af" };
    if (v >= 1) {
      const t = Math.min((v - 1) / 1.5, 1);
      return { bg: `rgba(79,70,229,${0.08 + t * 0.85})`, fg: t > 0.5 ? "#fff" : "#1f2937" };
    }
    const t = Math.min((1 - v) / 0.8, 1);
    return { bg: `rgba(245,158,11,${0.08 + t * 0.55})`, fg: "#1f2937" };
  };
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[40rem] border-separate border-spacing-0.5 text-center text-[11px]">
        <thead>
          <tr>
            <th className="w-44 text-left font-medium text-gray-500" />
            {labels.map((m) => <th key={m} className="font-medium text-gray-500">{m}</th>)}
            <th className="pl-2 text-left font-medium text-gray-500" />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <td className="truncate pr-2 text-left text-xs text-gray-700" title={r.label}>{r.label}</td>
              {r.values.map((v, i) => {
                const c = cell(v);
                return <td key={i} className="h-7 rounded tabular-nums" style={{ background: c.bg, color: c.fg }} title={v === null ? "no data" : `${v.toFixed(2)}x a normal month`}>{v === null ? "" : v.toFixed(1)}</td>;
              })}
              <td className="whitespace-nowrap pl-2 text-left text-[11px] text-gray-500">{r.right}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {note && <p className="mt-1 text-[11px] text-gray-400">{note}</p>}
    </div>
  );
}
