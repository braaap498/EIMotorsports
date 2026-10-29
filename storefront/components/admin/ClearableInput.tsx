"use client";

import { useRef, type ChangeEvent, type InputHTMLAttributes } from "react";

// A text box with an × that empties it in one click.
// Use it exactly like <input>. By default the × calls the box's own onChange with an empty value, so
// a search that updates as you type clears itself. For a search that only runs when Search is pressed,
// pass onClear to also reset the applied search.
type Props = Omit<InputHTMLAttributes<HTMLInputElement>, "value"> & {
  value: string;
  onClear?: () => void;
};

// Width / spacing classes belong on the wrapper so the box keeps its place in the layout.
const LAYOUT = /^(w-|max-w-|min-w-|mb-|mt-|flex-|shrink|grow)/;

export default function ClearableInput({ className = "", onClear, onChange, value, disabled, readOnly, ...rest }: Props) {
  const ref = useRef<HTMLInputElement>(null);
  const tokens = className.split(/\s+/).filter(Boolean);
  const wrap = tokens.filter((t) => LAYOUT.test(t));
  const inner = tokens.filter((t) => !LAYOUT.test(t));
  if (!wrap.some((t) => t.startsWith("w-"))) wrap.push("w-full");

  function clear() {
    if (onClear) onClear();
    else onChange?.({ target: { value: "" }, currentTarget: { value: "" } } as unknown as ChangeEvent<HTMLInputElement>);
    ref.current?.focus();
  }

  return (
    <div className={`relative ${wrap.join(" ")}`}>
      <input ref={ref} className={`${inner.join(" ")} w-full pr-8`} value={value} onChange={onChange} disabled={disabled} readOnly={readOnly} {...rest} />
      {value && !disabled && !readOnly && (
        <button
          type="button"
          aria-label="Clear"
          title="Clear"
          className="absolute right-2 top-1/2 -translate-y-1/2 text-lg leading-none text-gray-400 hover:text-black"
          onClick={clear}
        >
          ×
        </button>
      )}
    </div>
  );
}
