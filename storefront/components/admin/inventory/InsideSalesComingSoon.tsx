"use client";

import { useState } from "react";

/* Inventory > Pick & ship > Inside sales: placeholder until in-store (walk-in / phone) sales are built.

   To use a real photo or AI image instead of the drawn showroom, save it as  public/inside-sales.jpg  in the
   storefront project. The COMING SOON banner stays on top of it. With no such file, the drawing below shows. */
export default function InsideSalesComingSoon() {
  const [noPhoto, setNoPhoto] = useState(false);
  return (
    <div className="mx-auto max-w-4xl space-y-3">
      <div className="relative aspect-video overflow-hidden rounded-xl border border-gray-300 bg-gray-900 shadow-lg">
        {!noPhoto ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src="/inside-sales.jpg"
            alt="E&I Motorsports showroom"
            className="h-full w-full object-cover"
            onError={() => setNoPhoto(true)}
          />
        ) : (
          <Showroom />
        )}
        <div className="absolute inset-0 flex items-center justify-center bg-black/20">
          <div className="-rotate-3 rounded-lg border-4 border-white bg-red-600 px-6 py-3 text-3xl font-extrabold tracking-wider text-white shadow-2xl sm:px-10 sm:py-5 sm:text-5xl md:text-6xl">
            COMING SOON!!!!
          </div>
        </div>
      </div>
      <p className="text-center text-sm text-gray-500">Walk-in and phone sales will be recorded here.</p>
    </div>
  );
}

const PALETTE = ["#f97316", "#ef4444", "#3b82f6", "#e5e7eb"];

function Shelves({ x }: { x: number }) {
  return (
    <g>
      {[0, 1, 2].map((r) => {
        const y = 175 + r * 68;
        let cx = x + 8;
        return (
          <g key={r}>
            {Array.from({ length: 7 }).map((_, i) => {
              const w = 20 + ((i * 7 + r * 5) % 14);
              const h = 26 + ((i * 5 + r * 3) % 18);
              const bx = cx;
              cx += w + 6;
              return <rect key={i} x={bx} y={y - h} width={w} height={h} rx="2" fill={PALETTE[(i + r) % 4]} opacity="0.9" />;
            })}
            <rect x={x} y={y} width="230" height="7" rx="2" fill="#9ca3af" />
          </g>
        );
      })}
    </g>
  );
}

function Showroom() {
  return (
    <svg
      viewBox="0 0 960 540"
      className="h-full w-full"
      preserveAspectRatio="xMidYMid slice"
      role="img"
      aria-label="Illustration of the E&I Motorsports showroom"
    >
      <defs>
        <linearGradient id="isWall" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#4b5563" />
          <stop offset="1" stopColor="#1f2937" />
        </linearGradient>
        <linearGradient id="isFloor" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#6b7280" />
          <stop offset="1" stopColor="#111827" />
        </linearGradient>
        <linearGradient id="isLight" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#fde68a" stopOpacity="0.55" />
          <stop offset="1" stopColor="#fde68a" stopOpacity="0" />
        </linearGradient>
        <filter id="isGlow" x="-20%" y="-50%" width="140%" height="200%">
          <feGaussianBlur stdDeviation="7" />
        </filter>

        <symbol id="isBike" viewBox="0 0 200 110">
          <g fill="none" stroke="#030712" strokeWidth="9" strokeLinecap="round">
            <circle cx="45" cy="78" r="26" />
            <circle cx="155" cy="78" r="26" />
          </g>
          <g fill="none" stroke="#9ca3af" strokeWidth="2">
            <circle cx="45" cy="78" r="15" />
            <circle cx="155" cy="78" r="15" />
          </g>
          <g stroke="#030712" strokeWidth="6" strokeLinecap="round" fill="none">
            <line x1="155" y1="78" x2="138" y2="34" />
            <line x1="128" y1="30" x2="150" y2="24" />
            <line x1="45" y1="78" x2="84" y2="74" />
          </g>
          <rect x="78" y="56" width="44" height="30" rx="6" fill="#374151" />
          <path d="M82 52 Q104 28 134 38 L128 54 L84 58 Z" fill="currentColor" />
          <path d="M48 50 L90 50 L88 58 L44 60 Z" fill="#111827" />
          <path d="M30 56 Q44 40 62 46 L58 52 Z" fill="currentColor" />
          <path d="M84 80 Q60 84 36 70" stroke="#d1d5db" strokeWidth="5" fill="none" strokeLinecap="round" />
        </symbol>

        <symbol id="isQuad" viewBox="0 0 200 110">
          <g fill="none" stroke="#030712" strokeWidth="14" strokeLinecap="round">
            <circle cx="48" cy="76" r="24" />
            <circle cx="152" cy="76" r="24" />
          </g>
          <g fill="none" stroke="#9ca3af" strokeWidth="3">
            <circle cx="48" cy="76" r="12" />
            <circle cx="152" cy="76" r="12" />
          </g>
          <path d="M30 60 Q36 40 70 40 L120 40 Q150 40 168 62 L160 72 L40 72 Z" fill="currentColor" />
          <path d="M70 38 L118 38 L112 28 L76 28 Z" fill="#111827" />
          <g stroke="#030712" strokeWidth="6" strokeLinecap="round">
            <line x1="125" y1="40" x2="132" y2="16" />
            <line x1="120" y1="14" x2="146" y2="14" />
          </g>
          <rect x="150" y="48" width="22" height="10" rx="3" fill="#fde68a" />
        </symbol>
      </defs>

      {/* walls and floor */}
      <rect width="960" height="540" fill="url(#isWall)" />
      <polygon points="0,380 960,380 960,540 0,540" fill="url(#isFloor)" />
      <line x1="0" y1="380" x2="960" y2="380" stroke="#9ca3af" strokeWidth="2" opacity="0.5" />

      {/* parts shelves on both side walls */}
      <Shelves x={24} />
      <Shelves x={706} />

      {/* spotlights */}
      <polygon points="300,0 360,0 470,385 200,385" fill="url(#isLight)" />
      <polygon points="600,0 660,0 780,385 520,385" fill="url(#isLight)" />

      {/* neon sign */}
      <rect x="240" y="30" width="480" height="76" rx="12" fill="none" stroke="#f97316" strokeWidth="6" filter="url(#isGlow)" />
      <rect x="240" y="30" width="480" height="76" rx="12" fill="#0b0b0b" stroke="#f97316" strokeWidth="4" />
      <text
        x="480"
        y="82"
        textAnchor="middle"
        fontSize="40"
        fontWeight="800"
        letterSpacing="5"
        fill="#fb923c"
        fontFamily="system-ui, Arial, sans-serif"
      >
        {"E&I MOTORSPORTS"}
      </text>

      {/* the machines */}
      <ellipse cx="370" cy="432" rx="150" ry="14" fill="#000" opacity="0.45" />
      <ellipse cx="670" cy="432" rx="150" ry="14" fill="#000" opacity="0.45" />
      <use href="#isBike" x="215" y="275" width="310" height="170" style={{ color: "#ef4444" }} />
      <use href="#isQuad" x="520" y="278" width="310" height="170" style={{ color: "#f97316" }} />
    </svg>
  );
}
