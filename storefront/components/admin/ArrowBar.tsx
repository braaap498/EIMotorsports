"use client";

// Up / down arrows for stepping to the row above or below in a list.
export default function ArrowBar({
  upLabel,
  downLabel,
  position,
  canUp,
  canDown,
  busy,
  onUp,
  onDown,
}: {
  upLabel: string;
  downLabel: string;
  position?: string; // e.g. "12 of 340"
  canUp: boolean;
  canDown: boolean;
  busy?: boolean;
  onUp: () => void;
  onDown: () => void;
}) {
  const btn =
    "rounded border border-gray-300 bg-white px-2.5 py-1 text-sm font-medium text-gray-800 hover:bg-gray-50 disabled:opacity-40";
  return (
    <div className="flex items-center gap-2">
      <button type="button" className={btn} disabled={!canUp || busy} onClick={onUp} title={upLabel} aria-label={upLabel}>
        ▲
      </button>
      <button type="button" className={btn} disabled={!canDown || busy} onClick={onDown} title={downLabel} aria-label={downLabel}>
        ▼
      </button>
      {position && <span className="text-xs text-gray-500">{position}</span>}
    </div>
  );
}
