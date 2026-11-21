"use client";

import { forwardRef, useState } from "react";
import { btnCls } from "../ui";

/**
 * The box the Zebra scanner types into. A scanner in its normal (keyboard) mode types the
 * code and presses Enter, which submits this form. You can also type a code by hand and
 * press the Enter button next to the box.
 */
const ScanBox = forwardRef<
  HTMLInputElement,
  { onScan: (code: string) => void; placeholder?: string; disabled?: boolean; buttonLabel?: string; hint?: string | false }
>(function ScanBox(
  { onScan, placeholder = "Scan a barcode here…", disabled, buttonLabel = "Enter", hint },
  ref
) {
  const [value, setValue] = useState("");
  return (
    <div>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          const code = value.trim();
          if (!code) return;
          setValue("");
          onScan(code);
        }}
      >
        <input
          ref={ref}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={disabled}
          placeholder={placeholder}
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          className="min-w-0 flex-1 rounded-lg border-2 border-gray-400 bg-white px-4 py-3 text-lg text-gray-900 focus:border-black focus:outline-none disabled:bg-gray-100"
        />
        <button type="submit" className={`${btnCls} px-5 text-base`} disabled={disabled || !value.trim()}>
          {buttonLabel}
        </button>
      </form>
      {hint !== false && (
        <p className="mt-1 text-xs text-gray-500">{hint ?? "Scan it, or type it and press the Enter button."}</p>
      )}
    </div>
  );
});

export default ScanBox;
