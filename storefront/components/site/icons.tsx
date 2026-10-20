import type { SVGProps } from "react";

const base = {
  width: 24,
  height: 24,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

export const SearchIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base} {...p}><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
);
export const UserIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base} {...p}><circle cx="12" cy="8" r="4" /><path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7" /></svg>
);
export const GarageIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base} {...p}><path d="M3 10 12 4l9 6v10H3z" /><path d="M7 20v-6h10v6" /><path d="M7 17h10" /></svg>
);
export const CartIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base} {...p}><circle cx="9" cy="20" r="1.5" /><circle cx="18" cy="20" r="1.5" /><path d="M2 3h3l2.4 11.2a1 1 0 0 0 1 .8h9.2a1 1 0 0 0 1-.8L20 7H6" /></svg>
);
// ATV side view, used as the cart icon. Drawn with currentColor, so it is white in the black header.
export const AtvIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg
    viewBox="8 50 448 292"
    fill="none"
    stroke="currentColor"
    strokeWidth={17}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    {...p}
  >
    <circle cx="88" cy="262" r="67" />
    <circle cx="88" cy="262" r="27" />
    <circle cx="378" cy="262" r="67" />
    <circle cx="378" cy="262" r="27" />
    <path d="M270 66H222L196 92" />
    <path d="M82 192C84 150 108 108 160 80L186 98" />
    <path d="M200 98C226 100 246 118 252 148" />
    <path d="M120 132H156" />
    <path d="M252 148L338 120L372 90H420Q432 90 432 102V110L338 170L346 190" />
    <path d="M252 148L196 180Q176 190 175 212V265" />
    <path d="M155 268H178L200 246H248L270 268H310" />
  </svg>
);
export const ChevronIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base} strokeWidth={2.5} {...p}><path d="m6 9 6 6 6-6" /></svg>
);
