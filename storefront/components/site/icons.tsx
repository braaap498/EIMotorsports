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
// ATV side view, used on the ATV button on the home page. Drawn with currentColor, so it takes the text color.
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
// All the side views face left, like AtvIcon (the ATV and PWC are drawn facing left; the others are mirrored with a flip transform).
// Side views for the UTV, Snowmobile, Dirt Bike, Motorcycle and PWC buttons on the home page. Same drawing style as AtvIcon: currentColor, so they take the text color.
export const UtvIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg
    viewBox="0 0 52 32"
    fill="none"
    stroke="currentColor"
    strokeWidth={2.2}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    {...p}
  >
    <g transform="translate(51 0) scale(-1 1)">
      <circle cx="7.3" cy="24.2" r="5.4" />
      <circle cx="42.2" cy="24.2" r="5.4" />
      <path d="M0.7 13.6V11.3L10.8 10.2L36.2 10.8L47.6 12.6L48.7 15.3L48.1 18.3" />
      <path d="M0.7 13.6H8.2" />
      <path d="M10.4 10.2L12.7 3.9L32.7 4.3L36.2 9.8" />
      <path d="M18 11L22 18H29.8L32.5 11" />
      <path d="M18 21.8H33.4" />
      <path d="M18 21.8L14.2 22.8" />
      <path d="M33.4 21.8L37.2 22.8" />
      <path d="M8.2 14.2L9.7 19" />
    </g>
  </svg>
);
// Wider drawing (56 x 32) than the other icons, so give it w-[42px] instead of w-9 to keep the same line weight.
export const SnowmobileIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg
    viewBox="0 0 56 32"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    {...p}
  >
    <g transform="translate(56 0) scale(-1 1)">
      <path d="M6 9.8Q5.6 7.7 10 7.9Q22 9 31.3 10.4Q33 8 35.5 7.7L52.6 15L46.8 17.4L43.6 19.7L41.3 21.9H31.5L1.3 15.8" />
      <path d="M1.3 15.8L3 15.1Q8 15.4 12 15.4L39 19.8" />
      <path d="M31.3 10.4L29 14.8L7.4 10.2" />
      <path d="M6.2 11Q6 13.4 8.3 14.2" />
      <path d="M35.8 7.5L36.6 3.4Q42 4 46.2 8.9L43.5 9.3" />
      <path d="M31.5 4.9H34L36.3 6.8" />
      <path d="M37.6 16.4H42.5" />
      <path d="M42 16.4L50 27.2" />
      <path d="M38 28.8H50Q54.3 28.6 54.5 25.3" />
      <path d="M5 21.8A3.3 3.3 0 0 0 5 28.4H24Q29 28 32 23.4" />
      <path d="M5 21.8L19 19.3" />
      <circle cx="5" cy="25" r="1.5" />
    </g>
  </svg>
);
export const DirtBikeIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg
    viewBox="0 0 48 32"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    {...p}
  >
    <g transform="translate(48 0) scale(-1 1)">
      <circle cx="9" cy="23" r="6" />
      <circle cx="9" cy="23" r="1.5" />
      <circle cx="39" cy="23" r="6" />
      <circle cx="39" cy="23" r="1.5" />
      <path d="M3 12Q8 10.5 13 12.5L19 14.5Q24 14.5 28 11.5L32.5 9.5" />
      <path d="M31 6L39 23" />
      <path d="M28 6H32" />
      <path d="M9 23L21 19.5H30" />
      <path d="M14 13.5L18 19.7" />
      <path d="M34 11.5Q40 8.5 45 12.5" />
    </g>
  </svg>
);
export const MotorcycleIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg
    viewBox="0 0 48 32"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    {...p}
  >
    <g transform="translate(48 0) scale(-1 1)">
      <circle cx="11" cy="23" r="5.5" />
      <circle cx="38" cy="23" r="5.5" />
      <path d="M4 21Q5 14.5 12 14.5" />
      <path d="M10 14.5H19Q22 11.5 28 11.5L33 12.5L32 17.5H22.5L18.5 14.5" />
      <path d="M32 8L38 23" />
      <path d="M29 8H33" />
      <path d="M34.5 11L39 10V14L35.5 14" />
      <path d="M11 23H19L22.5 20.5H31" />
    </g>
  </svg>
);
export const PwcIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg
    viewBox="0 0 48 32"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    {...p}
  >
    <path d="M3 19Q9 14 17 12.5Q21 12 24 14.5L37 13Q40.5 12.5 40.5 16L43 18" />
    <path d="M3 19H44Q46 19 46 21Q46 25 42 25H13Q6 25 3 19Z" />
    <path d="M25 14L28.5 9H32" />
    <path d="M30 22H37" />
  </svg>
);
export const ChevronIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...base} strokeWidth={2.5} {...p}><path d="m6 9 6 6 6-6" /></svg>
);
